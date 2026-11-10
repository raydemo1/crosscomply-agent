"""Pair the original review Agent with the deployed material and Python tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from law_agent.config import load_env_file, require_agent_llm_config
from law_agent.llm.openai_compatible import OpenAICompatibleClient
from law_agent.review.agent import AgentModel, run_agent
from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric
from law_agent.review.python_sandbox import sandbox_from_env
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.service_backends import ServiceAdapters
from scripts.eval_numeric_tools import (
    ARM,
    FrozenSearch,
    empty_web,
    make_model,
    skip_semantic_verification,
    trial,
)
from scripts.numeric_tool_cases import CONTEXT, LAW, SOURCE, ledger

SECTIONS = (
    (
        "值班与交接",
        "值班员每日核对告警队列，完成交接时登记工单及处理人。交接事项按照台账顺序填写，负责人每周抽查。",
    ),
    (
        "故障演练",
        "演练期间建立回滚检查点，按顺序验证恢复步骤。演练报告保存版本及受影响模块，安排复盘。",
    ),
    (
        "设备登记",
        "设备配置以资产台账为准，变更须登记设备编号、操作时间和核对人。盘点时核对状态并记录差异。",
    ),
    (
        "访问控制",
        "权限申请须说明岗位、授权期限和审批人。临时授权到期后自动关闭，管理员定期核对授权记录。",
    ),
    (
        "告警处理",
        "运维中心按告警级别安排处理。高优先级故障在值班群通报，复核记录包含影响范围和恢复时间。",
    ),
    ("接口变更", "接口变更须经过测试、验收和发布确认。版本升级前核对依赖模块，旧版保留回滚说明。"),
    (
        "完整性检查",
        "核对记录与备份副本的校验值。发现差异时暂停覆盖并记录异常，按既定流程检查数据来源。",
    ),
    ("交付管理", "负责人汇总交付清单、验收记录及待办事项。每项资料记录交付人、接收人和当前状态。"),
)

CASES = (
    (
        "retention",
        310,
        "本次境外接收方仅可将客服工单保存至结案后第45日。",
        "核对协议约定的境外客服工单保存期限，说明期限及起算点。",
        "45日，从结案后起算",
    ),
    (
        "onward_transfer",
        460,
        "境外接收方拟向第三方转移个人信息前，须取得委托方书面同意并提交分包方名单。",
        "核对协议是否允许境外接收方直接向第三方转移个人信息。",
        "须书面同意并提交分包方名单",
    ),
    (
        "data_categories",
        250,
        "| 导出字段 | 工单号、客户手机号后四位、客服语言 |",
        "列出最终出境清单的导出字段，区分完整手机号和后四位。",
        "工单号、客户手机号后四位、客服语言",
    ),
    (
        "notice_deadline",
        380,
        "涉及个人信息泄露的安全事件，应自首次发现起12小时内书面通知委托方。",
        "核对协议对涉及个人信息泄露事件的通知时限、起算点和方式。",
        "首次发现起12小时内书面通知",
    ),
    (
        "annual_boundary",
        520,
        "附件核定，自2024年1月1日起累计实际出境的普通个人信息主体为100,000人，已按自然人去重。",
        "核对累计人数和适用的出境机制。",
        "100,000人，标准合同或认证",
    ),
    (
        "missing_overlap",
        280,
        "三个月的实际出境人数分别为39000人、37500人、32000人，只有月内去重，缺少跨月主体明细。",
        "能否确定当年累计去重出境人数与出境机制？",
        "人数不确定，免予或标准合同/认证",
    ),
    (
        "cancel_table",
        450,
        ledger(328, 999940, 1000000, crosscheck=True),
        "按主台账、补录日志及撤销表核对累计去重出境人数与出境机制。",
        "1,000,000人，申报安全评估",
    ),
    (
        "short_control",
        0,
        "附件核定，自2024年1月1日起累计实际出境的普通个人信息主体为99,999人，已按自然人去重。",
        "核对累计人数和适用的出境机制。",
        "99,999人，免予三项出境机制",
    ),
)


def cases():
    for case_id, length, fact, question, expected in CASES:
        filler = "\n\n".join(
            f"### 附录{i + 1:03d} {title}\n{body}"
            for i in range(length)
            for title, body in [SECTIONS[i % len(SECTIONS)]]
        )
        material = "\n\n".join(
            part for part in (CONTEXT, filler, "### 本案业务附件\n" + fact) if part
        )
        target = "【撤销表】" if case_id == "cancel_table" else fact
        assert (material.index(target) >= 12000) == (length > 0)
        yield (
            AgentCase(
                case_id=case_id,
                material_text=material,
                question=question
                + " 请说明材料出处并摘录支持判断的原文。涉及法律机制时依据《"
                + LAW
                + "》。",
                intake={"as_of_date": "2024-03-23"},
                rubric=AgentRubric(should_abstain=None),
                reference_basis=[SOURCE],
            ),
            {"answer": expected, "target": target, "characters": len(material)},
        )


def model_for_arm(*, model_id, python_enabled):
    arm = ARM.get()[0]
    if arm == "baseline":
        return make_model(model_id=model_id)
    return AgentModel(model_id=model_id, python_enabled=python_enabled)


def run_agent_for_arm(state, **kwargs):
    if ARM.get()[0] == "baseline":
        kwargs["material_versions"] = ()
    return run_agent(state, **kwargs)


_stream_tools = OpenAICompatibleClient._stream_tools
_request_lock = Lock()
_request_interval = 0.0
_last_request = 0.0


def retry_transient_http(client, payload, on_text):
    global _last_request
    for attempt in range(3):
        with _request_lock:
            time.sleep(max(0, _request_interval - (time.monotonic() - _last_request)))
            _last_request = time.monotonic()
        try:
            return _stream_tools(client, payload, on_text)
        except RuntimeError as exc:
            cause = exc.__cause__
            if not isinstance(cause, HTTPError):
                raise
            print(f"Provider HTTP {cause.code} (attempt {attempt + 1})", flush=True)
            if cause.code not in {402, 408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(8 * (attempt + 1))


def main():
    global _request_interval
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arm", action="append", choices=("baseline", "tools"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--request-interval", type=float, default=0)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--skip-completed-from", type=Path, action="append", default=[])
    args = parser.parse_args()
    _request_interval = args.request_interval
    if args.env_file:
        load_env_file(args.env_file)
    cfg = require_agent_llm_config()
    if cfg.model not in {"deepseek-flash", "DeepSeek-V4.1-Flash-1017"}:
        raise ValueError("This pilot uses deepseek-flash only")
    sandbox = sandbox_from_env()
    if sandbox is None or not sandbox.run("print(42)", "")["ok"]:
        raise RuntimeError("The production WASI sandbox is required")
    arms = args.arm or ("baseline", "tools")
    jobs = [(case, expected, arm) for case, expected in cases() for arm in arms]
    completed = set()
    for path in args.skip_completed_from:
        with path.open(encoding="utf-8") as prior:
            header = json.loads(next(prior))
            if (
                header["model"] != cfg.model
                or header["semantic_verification"]
                or header.get("provider", urlsplit(cfg.base_url).hostname)
                != urlsplit(cfg.base_url).hostname
            ):
                parser.error(f"incompatible prior run: {path}")
            for line in prior:
                row = json.loads(line)
                if row["run"]["status"] == "succeeded" and row["report"]:
                    completed.add((row["run"]["case_id"], row["arm"]))
    jobs = [job for job in jobs if (job[0].case_id, job[2]) not in completed]
    random.Random(20261109).shuffle(jobs)
    selected = [
        chunk for chunk in load_corpus(DEFAULT_CHUNKS_PATH) if chunk.title.strip("《》") == LAW
    ]
    search = FrozenSearch(selected)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as directory:
        corpus = Path(directory) / "rules.jsonl"
        data = "".join(chunk.model_dump_json() + "\n" for chunk in selected)
        corpus.write_text(data, encoding="utf-8")
        runner = partial(trial, corpus=corpus, model=cfg.model, image=None, csv_limit=False)
        with (
            args.out.open("x", encoding="utf-8") as output,
            patch("law_agent.review.agent_runtime.AgentModel", model_for_arm),
            patch("law_agent.review.agent_runtime.run_agent", run_agent_for_arm),
            patch.object(OpenAICompatibleClient, "_stream_tools", retry_transient_http),
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
                        "model": cfg.model,
                        "provider": urlsplit(cfg.base_url).hostname,
                        "reasoning_effort": cfg.reasoning_effort,
                        "semantic_verification": False,
                        "corpus_sha256": hashlib.sha256(data.encode()).hexdigest(),
                        "jobs": [[case.case_id, arm] for case, _, arm in jobs],
                    }
                )
                + "\n"
            )
            output.flush()
            for future in as_completed([pool.submit(runner, *job) for job in jobs]):
                row = future.result()
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                print(row["run"]["case_id"], row["arm"], row["run"]["status"], flush=True)


if __name__ == "__main__":
    main()
