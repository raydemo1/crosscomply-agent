"""Run the controlled three-arm numerical tool experiment on the review Agent."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import operator
import random
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from fractions import Fraction
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch
from uuid import uuid4

from pydantic import Field

from law_agent.config import require_agent_llm_config
from law_agent.llm.usage import usage_scope
from law_agent.review.agent import AgentDecision, AgentModel, run_agent
from law_agent.review.evalset.agent_runner import make_production_executor, run_agent_case
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.retrieval.service_backends import ServiceAdapters
from law_agent.review.tool_calling import ToolCallingAgent, ToolSpec
from law_agent.review.web_research import WebSearchBatch
from scripts.numeric_tool_cases import LAW, cases

ARMS = ("baseline", "calculator", "python")
ARM = ContextVar("numeric_eval_arm")
IMAGE = "python:3.12-slim"
OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
SPECS = {
    "calculator": ToolSpec(
        ("expression",),
        "精确计算十进制数字与整数的加减乘除（支持括号）。先核实材料中的数量、单位与统计口径；输出只代表算式的值。",
        ("expression",),
    ),
    "python": ToolSpec(
        ("code",),
        "在无网络的独立 Python 容器中执行标准库代码。预置字符串 material 为本案完整冻结材料；"
        "可用 csv 等库做清洗、筛选、去重、交叉核对或计算。print 输出所需结果。每次运行独立。"
        "结果是对输入的计算，报告仍应注明材料来源和统计口径。",
        ("code",),
    ),
}

CONTAINER_ENTRY = r"""
import json, resource, subprocess, sys, tempfile
payload = json.load(sys.stdin)
program = 'import json as _json, sys as _sys\nmaterial = _json.load(_sys.stdin)\n' + payload['code']
resource.setrlimit(resource.RLIMIT_FSIZE, (8192, 8192))
with tempfile.TemporaryFile() as output:
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program],
        input=json.dumps(payload['material']).encode(), stdout=output, stderr=output)
    output.seek(0)
    print(json.dumps({'exit_code': result.returncode, 'output': output.read(8192).decode(errors='replace')}))
"""


class ToolDecision(AgentDecision):
    action: Literal[
        "propose_plan",
        "read_material",
        "search_evidence",
        "read_evidence",
        "search_web",
        "queue_enrichment",
        "request_input",
        "finish",
        "calculator",
        "python",
    ]
    expression: str = Field(default="", max_length=400)
    code: str = Field(default="", max_length=6000)


def exact_calculation(expression):
    if not expression.strip():
        raise ValueError("需要提供算式")
    expression = expression.strip()
    tree = ast.parse(expression, mode="eval")

    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            literal = ast.get_source_segment(expression, node)
            if all(ch in "0123456789." for ch in literal):
                return Fraction(literal)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](evaluate(node.left), evaluate(node.right))
        raise ValueError("仅支持十进制数字、括号和加减乘除")

    return {"expression": expression, "exact_result": str(evaluate(tree.body))}


def run_python(code, material, image):
    if not code.strip():
        raise ValueError("需要提供 Python 代码")
    name = "crosscomply-eval-" + uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "--name",
        name,
        "--network=none",
        "--log-driver=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=24",
        "--memory=96m",
        "--memory-swap=96m",
        "--cpus=0.5",
        "--user=65534:65534",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=1m",
        "--workdir=/tmp",
        image,
        "python",
        "-I",
        "-B",
        "-c",
        CONTAINER_ENTRY,
    ]
    try:
        process = subprocess.run(
            command,
            input=json.dumps({"code": code, "material": material}),
            capture_output=True,
            encoding="utf-8",
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=8, check=False)
        return {"error": "Python 超出 8 秒执行时限，请缩小计算范围后重试"}
    if process.returncode:
        return {"error": "容器运行失败", "details": process.stderr[:1200]}
    result = json.loads(process.stdout)
    if result["exit_code"]:
        result["error"] = "Python 执行失败，请根据错误输出修复代码"
    return result


def make_model(*, model_id, python_enabled=False):
    model = AgentModel(model_id=model_id)
    arm, _, _, _ = ARM.get()
    protocol = model.protocol
    specs = dict(protocol.specs)
    del specs["read_materials"]
    del specs["grep_materials"]
    specs["read_material"] = ToolSpec(
        ("offset",),
        "读取本次冻结材料包的原文。首次材料预览已提供，需要查看更多内容时按返回的续读位置读取。"
        "返回材料原文及next_read_request；truncated=false且next_read_request=null表示材料包已读完。",
    )
    if arm != "baseline":
        specs[arm] = SPECS[arm]
    model.protocol = ToolCallingAgent(
        client=protocol.client,
        model_id=model_id,
        decision_model=AgentDecision if arm == "baseline" else ToolDecision,
        specs=specs,
    )
    return model


def experimental_agent(state, **kwargs):
    arm, trace, image, csv_limit = ARM.get()
    material = kwargs["material"]
    kwargs["material_versions"] = ()

    def execute(decision):
        started = time.perf_counter()
        argument = decision.expression if arm == "calculator" else decision.code
        try:
            observation = (
                exact_calculation(argument)
                if arm == "calculator"
                else run_python(
                    "import csv as _csv\n_csv.field_size_limit(4)\n" + argument
                    if csv_limit
                    else argument,
                    material,
                    image,
                )
            )
        except (
            ValueError,
            ArithmeticError,
            SyntaxError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            observation = {"error": f"{type(exc).__name__}: {exc}"[:1200]}
        trace.append(
            {
                "argument": argument,
                "turn": state.turns,
                "observation": observation,
                "latency_ms": round(1000 * (time.perf_counter() - started)),
            }
        )
        return observation

    return run_agent(state, extra_tools={arm: execute} if arm != "baseline" else None, **kwargs)


class FrozenSearch:
    def __init__(self, chunks):
        self.chunks = [
            chunk for chunk in chunks if chunk.article_no in {"第五条", "第七条", "第八条"}
        ]
        if {chunk.article_no for chunk in self.chunks} != {"第五条", "第七条", "第八条"}:
            raise ValueError("缺少冻结法源条文")

    def search_many(self, query_pairs, top_k, allowed_chunk_ids):
        return [
            [
                hit_from_chunk(chunk, rank + 1)
                for rank, chunk in enumerate(
                    chunk for chunk in self.chunks if chunk.chunk_id in allowed
                )
            ][:top_k]
            for allowed in allowed_chunk_ids
        ]


def empty_web(_tools, *_args, **_kwargs):
    return WebSearchBatch()


def skip_semantic_verification(*, model_id):
    return lambda **_inputs: None


def trial(case, expected, arm, *, corpus, model, image, csv_limit):
    usage, tool_trace, states = [], [], []
    token = ARM.set((arm, tool_trace, image, csv_limit))
    executor = make_production_executor(case, chunks_path=corpus)

    def capture(task, store):
        try:
            return executor(task, store)
        finally:
            saved = store.get_task(task.id)
            if saved and saved.agent_state:
                states.append(saved.agent_state)

    try:
        with usage_scope("numeric_experiment", usage.append):
            result = run_agent_case(case, execute=capture, model_id=model)
    finally:
        ARM.reset(token)
    state = states[-1] if states else {}
    return {
        "arm": arm,
        "expected": expected,
        "run": result.model_dump(mode="json"),
        "tool_trace": tool_trace,
        "steps": state.get("steps", []),
        "report": state.get("result"),
        "usage": usage,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--arm", action="append", choices=ARMS, default=[])
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--skip-completed-from", type=Path, action="append", default=[])
    parser.add_argument(
        "--csv-limit-probe",
        action="store_true",
        help="separate recovery probe with a 4-character csv.reader field limit",
    )
    args = parser.parse_args()
    config = require_agent_llm_config()
    arms = args.arm or ARMS
    if args.csv_limit_probe and (arms != ["python"] or args.case != ["quoted_csv"]):
        parser.error("--csv-limit-probe requires --arm python --case quoted_csv")
    jobs = [
        (case, expected, arm)
        for case, expected in cases()
        if not args.case or case.case_id in args.case
        for arm in arms
    ]
    if not jobs or set(args.case) - {case.case_id for case, _, _ in jobs}:
        parser.error("unknown case")
    completed = set()
    for path in args.skip_completed_from:
        with path.open(encoding="utf-8") as prior:
            header = json.loads(next(prior))
            if header["model"] != config.model or header.get("csv_limit_probe"):
                parser.error(f"incompatible prior run: {path}")
            for line in prior:
                row = json.loads(line)
                if row["run"]["status"] == "succeeded" and row["report"]:
                    completed.add((row["run"]["case_id"], row["arm"]))
    jobs = [job for job in jobs if (job[0].case_id, job[2]) not in completed]
    if not jobs:
        print("All selected cases already have reports.")
        return
    random.Random(20261109).shuffle(jobs)
    image = (
        subprocess.check_output(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], text=True, timeout=10
        ).strip()
        if "python" in arms
        else None
    )
    if image and run_python("print(len(material))", "probe", image).get("output") != "5\n":
        raise RuntimeError("Python sandbox probe failed")
    selected = [
        chunk for chunk in load_corpus(DEFAULT_CHUNKS_PATH) if chunk.title.strip("《》") == LAW
    ]
    search = FrozenSearch(selected)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as directory:
        corpus = Path(directory) / "rules.jsonl"
        data = "".join(chunk.model_dump_json() + "\n" for chunk in selected)
        corpus.write_text(data, encoding="utf-8")
        runner = partial(
            trial, corpus=corpus, model=config.model, image=image, csv_limit=args.csv_limit_probe
        )
        with (
            args.out.open("x", encoding="utf-8") as output,
            patch("law_agent.review.agent_runtime.AgentModel", make_model),
            patch("law_agent.review.agent_runtime.run_agent", experimental_agent),
            patch(
                "law_agent.review.agent_tools.SemanticGroundingVerifier", skip_semantic_verification
            ),
            patch("law_agent.review.agent_tools.require_service_config", return_value=None),
            patch(
                "law_agent.review.agent_tools.build_service_adapters",
                return_value=ServiceAdapters(search, search),
            ),
            patch("law_agent.review.agent_tools.ComplianceAgentTools.search_web", empty_web),
            ThreadPoolExecutor(max_workers=args.workers) as pool,
        ):
            output.write(
                json.dumps(
                    {
                        "model": config.model,
                        "reasoning_effort": config.reasoning_effort,
                        "semantic_verification": False,
                        "image": image,
                        "csv_limit_probe": args.csv_limit_probe,
                        "corpus_sha256": hashlib.sha256(data.encode()).hexdigest(),
                        "jobs": [[case.case_id, arm] for case, _, arm in jobs],
                    }
                )
                + "\n"
            )
            output.flush()
            futures = [pool.submit(runner, *job) for job in jobs]
            for future in as_completed(futures):
                row = future.result()
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                print(
                    row["run"]["case_id"],
                    row["arm"],
                    row["run"]["status"],
                    row["run"]["turns"],
                    len(row["usage"]),
                    flush=True,
                )


if __name__ == "__main__":
    main()
