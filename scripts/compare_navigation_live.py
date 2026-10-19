"""Bounded, source-seeded LLM comparison of flat and hierarchical reading tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from pydantic import Field

from law_agent.config import require_agent_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.agent import EvidenceReadRequest
from law_agent.review.ids import utc_now_iso
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.schemas import ReviewFacts
from scripts.analyze_navigation import file_hash, flat_read, offline_tools

FIXTURE = Path("docs/fixtures/navigation_live_tasks_20261019.json")
MAX_TURNS = 12
MAX_CALLS = 192


class FlatRequest(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)
    article_no: str | None = Field(default=None, max_length=40)
    chunk_id: str | None = Field(default=None, max_length=200)
    offset: int = Field(default=0, ge=0)


class FlatDecision(StrictModel):
    action: Literal["read", "finish"]
    summary: str
    read_requests: list[FlatRequest] = Field(default_factory=list, max_length=4)
    answer: str = ""
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    supporting_chunk_ids: list[str] = Field(default_factory=list)


class TreeDecision(StrictModel):
    action: Literal["read", "finish"]
    summary: str
    read_requests: list[EvidenceReadRequest] = Field(default_factory=list, max_length=4)
    answer: str = ""
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    supporting_chunk_ids: list[str] = Field(default_factory=list)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def production_hashes():
    return {p.as_posix(): file_hash(p) for p in sorted(Path("law_agent").rglob("*.py"))}


def prepare():
    if FIXTURE.exists():
        raise ValueError("Frozen task file already exists")
    old_path = Path("docs/fixtures/navigation_tasks_fixed_20261019.json")
    frozen = json.loads(old_path.read_text(encoding="utf-8"))
    if file_hash(DEFAULT_CHUNKS_PATH) != frozen["corpus_sha256"]:
        raise ValueError("Corpus differs from audited version")
    chunks = load_corpus(DEFAULT_CHUNKS_PATH)
    ranges = [(13, 13), (38, 43), (27, 35), (57, 67), (1, 8), (79, 90), (61, 64), (65, 68)]
    questions = [
        "阅读《个人信息保护法》第十三条的完整原文，概括处理个人信息的合法性基础及该条的同意规则。",
        "完整阅读《个人信息保护法》中个人信息跨境提供的规则这一章，概括各项规则、条件和例外。",
        "完整阅读《个人信息安全规范》中个人信息收集这一章，概括要求、限制与授权同意的例外。",
        "完整阅读《个人信息安全规范》中个人信息的委托处理、共享、转让、公开披露这一章，概括不同情形及其限制与例外。",
        "完整阅读北京2025版数据出境负面清单的汽车行业部分，包括两类程序对应的数据清单及说明，概括数据范围、场景、门槛和例外。",
        "完整阅读北京2025版数据出境负面清单的银行业部分，包括两类程序对应的数据清单及说明，概括不同场景、门槛和例外。",
        "完整阅读《汽车数据出境安全指引（2026版）》的数据出境流程这一章，概括办理方式、条件与衔接。",
        "完整阅读《汽车数据出境安全指引（2026版）》的汽车数据出境安全保护要求这一章，概括管理、技术、日志和应急处置要求。",
    ]
    tasks = []
    for index, (task, (start, end), question) in enumerate(zip(frozen["tasks"], ranges, questions, strict=True)):
        source = sorted((c for c in chunks if c.source_id == task["source_id"]), key=lambda c: c.chunk_index)
        targets = [c for c in source if start <= c.chunk_index <= end]
        assert len(targets) == end - start + 1
        before = next((c for c in source if c.chunk_index == start - 1), None)
        after = next((c for c in source if c.chunk_index == end + 1), None)
        tasks.append({
            **task, "question": question,
            "target_chunk_ids": [c.chunk_id for c in targets],
            "target_sha256": {c.chunk_id: hashlib.sha256(c.text.encode()).hexdigest() for c in targets},
            "target_index_range": [start, end],
            "boundary_audit": {
                "basis": "Manual check of ordered governed full text and next section boundary; not heading membership alone",
                "first_text": targets[0].text[:180], "last_text": targets[-1].text[-180:],
                "previous_text": before.text[-180:] if before else None,
                "next_text": after.text[:180] if after else None,
                "upstream_defect": "Industry text split across unrelated heading paths; full contiguous text remains required" if task["id"].startswith("beijing") else None,
                "changed_from_previous_target": [c.chunk_id for c in targets] != task["target_chunk_ids"],
            },
            "arm_order": ["flat", "tree"] if index % 2 == 0 else ["tree", "flat"],
        })
    write_json(FIXTURE, {
        "frozen_at": utc_now_iso(), "as_of_date": frozen["as_of_date"],
        "corpus_path": DEFAULT_CHUNKS_PATH.as_posix(), "corpus_sha256": file_hash(DEFAULT_CHUNKS_PATH),
        "previous_fixture_sha256": file_hash(old_path), "tasks": tasks,
        "max_reads": 10, "max_turns": MAX_TURNS, "max_http_calls": MAX_CALLS,
        "scope": "8 paired source-seeded reading tasks, one attempt per arm; no full legal review or model judge",
        "targets_hidden_from_model": True, "source_discovery_measured": False,
        "target_limits": "Coverage of audited governed text; not a PDF extraction completeness or legal accuracy verdict",
    })


class ReadingSession:
    def __init__(self, tools, source_id, facts, arm):
        self.tools, self.source_id, self.facts, self.arm = tools, source_id, facts, arm
        self.reads = 0
        self.evidence = {}
        self.history = []
        self.steps = []
        self.response_characters = 0

    def invoke(self, request):
        req = request.model_dump(mode="json", exclude_none=True)
        if req["source_id"] != self.source_id:
            return {"request": req, "error": "只能读取本任务已发现的来源", "executed": False}
        if req.get("chunk_id") and req.get("offset"):
            return {"request": req, "error": "chunk_id模式不接受offset", "executed": False}
        if sum(bool(req.get(k)) for k in ("article_no", "chunk_id", "node_id", "parent_node_id")) > 1:
            return {"request": req, "error": "定位字段冲突", "executed": False}
        if any(h["request"] == req for h in self.history):
            return {"request": req, "error": "相同位置已经读过", "executed": False}
        if req.get("chunk_id") in self.evidence:
            returned = set(self.evidence)
            return {"request": req, "error": "目标chunk已在此前窗口返回，请使用尚未返回的相邻chunk_id",
                    "next_unread_chunk_ids": list(dict.fromkeys(
                        h["response"][k] for h in self.history for k in ("following_chunk_id",)
                        if h["response"].get(k) and h["response"][k] not in returned)), "executed": False}
        if self.reads >= 10:
            return {"request": req, "error": "10次共用读取预算已用尽，请按已有证据交付并披露缺口", "executed": False}
        self.reads += 1
        try:
            selectors = {k: req[k] for k in ("node_id", "parent_node_id") if k in req}
            if self.arm == "tree":
                response = self.tools.read_evidence(
                    self.source_id, req.get("article_no"), req.get("chunk_id"), self.facts,
                    req["offset"], **selectors,
                ).model_dump(mode="json")
            else:
                response = flat_read(self.tools, self.source_id, req.get("article_no"),
                                     req.get("chunk_id"), self.facts, req["offset"])
                if not response["hits"] and not response["outline"]:
                    raise ValueError("所选位置没有可读目录或正文")
                response["mode"] = "text" if req.get("article_no") or req.get("chunk_id") else "outline"
            if response["next_offset"] is not None:
                response["next_read_request"] = {**req, "offset": response["next_offset"]}
                response["instruction"] = "该部分尚未读完，请保留全部选择字段按next_read_request续读"
            self.evidence.update({h["chunk_id"]: h for h in response["hits"]})
            self.history.append({"request": req, "response": response})
            self.response_characters += len(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
            return {"request": req, "response": response, "executed": True, "read_index": self.reads}
        except (ValueError, StopIteration) as exc:
            return {"request": req, "error": str(exc), "executed": True, "read_index": self.reads}

    def context(self):
        return {"source_id": self.source_id, "reads": self.reads, "max_reads": 10,
                "evidence": list(self.evidence.values()), "steps": self.steps}


def prompt(arm, model):
    common = (
        "你是受控文档阅读助手。已给定一个已发现的source_id；自主定位并完整阅读任务所指的原文，"
        "再概括要求、条件与例外并保留原文定位。不要猜未读内容，不依赖模型记忆补原文。"
        "每轮action=read时read_requests填1至4项；目录与正文共用10次实际读取预算。"
        "仅source_id浏览目录；source_id+article_no读条文；source_id+chunk_id读该块及邻近正文。"
        "可以直接读条文或chunk，不要求先浏览目录。定位字段互斥。"
        "目录每页最多30项，正文每次最多5块；目录预览不是完整原文，不是引用证据。"
        "有next_read_request须保留完整参数续读，chunk_id模式不用offset，使用相邻块ID继续。"
        "不要重复已读窗口。已有正文同时保存在evidence及历史工具响应中。"
        "完成或无法继续时action=finish，answer概括已读内容与缺口，coverage声明complete/partial/unknown，"
        "supporting_chunk_ids只列实际取得正文的ID。没有读全不得宣称完整；不需要读取任务之外的整份文档。"
        "最多12轮。只输出Schema所示的单个JSON，没有API tools。"
    )
    capability = (
        "本工具提供层级目录：parent_node_id展开节点的直接子目录；node_id读取该节点及所有子节点正文。"
        if arm == "tree" else
        "本工具提供扁平目录：每项含article_no、chunk_id和原文预览；通过条号或chunk_id精读。"
    )
    return common + capability + "\nSchema:\n" + json.dumps(model.model_json_schema(), ensure_ascii=False)


def preflight(frozen, tools):
    if file_hash(DEFAULT_CHUNKS_PATH) != frozen["corpus_sha256"]:
        raise ValueError("Corpus hash mismatch")
    facts = ReviewFacts(as_of_date=frozen["as_of_date"])
    checked = 0
    for task in frozen["tasks"]:
        for cid, expected in task["target_sha256"].items():
            if hashlib.sha256(tools._chunks_by_id[cid].text.encode()).hexdigest() != expected:
                raise ValueError("Target text mismatch")
    for source_id in dict.fromkeys(t["source_id"] for t in frozen["tasks"]):
        chunks = [c for c in tools._chunks if c.source_id == source_id]
        requests = [(None, c.chunk_id) for c in chunks]
        requests += [(article, None) for article in dict.fromkeys(c.article_no for c in chunks if c.article_no)]
        for article, cid in requests:
            old = flat_read(tools, source_id, article, cid, facts)
            new = tools.read_evidence(source_id, article, cid, facts).model_dump(mode="json")
            for key in ("hits", "next_offset", "previous_chunk_id", "following_chunk_id"):
                if old[key] != new[key]:
                    raise ValueError(f"Text arm parity failed: {source_id} {article} {cid} {key}")
            checked += 1
    return {"body_read_parity_checks": checked, "target_hashes_valid": True}


def run_task(root, task, arm, tools, config, as_of_date):
    directory = root / f"{task['id']}__{arm}"
    directory.mkdir()
    model = TreeDecision if arm == "tree" else FlatDecision
    session = ReadingSession(tools, task["source_id"], ReviewFacts(as_of_date=as_of_date), arm)
    node = StructuredLLMNode(node_name="navigation_paired_reading", output_model=model,
                             client=OpenAICompatibleClient(config), max_retries=0,
                             structured_output_mode="json_object")
    started = time.perf_counter()
    decision = None
    error = None
    try:
        for turn in range(MAX_TURNS):
            state = session.context()
            write_json(directory / "state.json", state)
            decision = node.run([
                ChatMessage(role="system", content=prompt(arm, model)),
                ChatMessage(role="user", content=json.dumps({"question": task["question"], "turn": turn + 1,
                                                            "state": state}, ensure_ascii=False)),
            ])
            append(directory / "events.jsonl", {"kind": "decision", "at": utc_now_iso(),
                                                "value": decision.model_dump(mode="json")})
            if decision.action == "finish":
                break
            if not decision.read_requests:
                raise ValueError("read action has no requests")
            observations = [session.invoke(request) for request in decision.read_requests]
            session.steps.append({"turn": turn + 1, "summary": decision.summary, "read_results": observations})
            append(directory / "events.jsonl", {"kind": "step", "value": session.steps[-1]})
            write_json(directory / "state.json", session.context())
    except (RuntimeError, ValueError, OSError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    returned = set(session.evidence)
    targets = set(task["target_chunk_ids"])
    finished = decision is not None and decision.action == "finish" and error is None
    result = {
        "task_id": task["id"], "arm": arm, "finished": finished, "error": error,
        "reads": session.reads, "target_complete": targets <= returned,
        "returned_target_chunk_ids": sorted(targets & returned), "missing_target_chunk_ids": sorted(targets - returned),
        "returned_chunk_ids": sorted(returned), "response_characters": session.response_characters,
        "false_complete_claim": bool(finished and decision.coverage == "complete" and not targets <= returned),
        "invalid_refs": sorted(set(decision.supporting_chunk_ids) - returned) if finished else [],
        "result": decision.model_dump(mode="json") if decision else None,
        "latency_seconds": round(time.perf_counter() - started, 3),
        "errors": [r for s in session.steps for r in s["read_results"] if "error" in r],
        "section_reads": sum(bool(h["request"].get("node_id")) for h in session.history),
        "child_directory_reads": sum(bool(h["request"].get("parent_node_id")) for h in session.history),
    }
    write_json(directory / "state.json", session.context())
    write_json(directory / "result.json", result)
    return result


def run(root, fixture_path=FIXTURE):
    root.mkdir(parents=True, exist_ok=False)
    frozen = json.loads(fixture_path.read_text(encoding="utf-8"))
    tools = offline_tools(load_corpus(DEFAULT_CHUNKS_PATH))
    checks = preflight(frozen, tools)
    config = require_agent_llm_config()
    manifest = {"started_at": utc_now_iso(), "fixture": frozen, "fixture_sha256": file_hash(fixture_path),
                "model": config.model, "reasoning_effort": config.reasoning_effort,
                "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "production_sha256": production_hashes(), "script_sha256": file_hash(Path(__file__)),
                "preflight": checks, "max_http_calls": MAX_CALLS,
                "arm_prompts": {"flat": prompt("flat", FlatDecision), "tree": prompt("tree", TreeDecision)},
                "limits": "One run per task/arm; supplied source, no retrieval/DB/Judge. Prompts differ only in available tool contract/schema. Alternating arm order; no cache or sampling control."}
    write_json(root / "manifest.json", manifest)
    original = OpenAICompatibleClient._post_chat
    calls = 0
    label = ""

    def recorded(client, payload, base_url):
        nonlocal calls
        if calls >= MAX_CALLS:
            raise RuntimeError("Paid model-call ceiling reached")
        calls += 1
        call_id = calls
        append(root / "model_calls.jsonl", {"event": "started", "at": utc_now_iso(),
                                            "call_id": call_id, "task": label, "request": payload})
        started = time.perf_counter()
        try:
            data = original(client, payload, base_url)
            append(root / "model_calls.jsonl", {"event": "completed", "at": utc_now_iso(), "call_id": call_id,
                "task": label, "model": data.get("model"), "usage": data.get("usage", {}),
                "choices": [{"finish_reason": c.get("finish_reason"), "content": c.get("message", {}).get("content")}
                            for c in data.get("choices", [])],
                "latency_seconds": round(time.perf_counter() - started, 3)})
            return data
        except Exception as exc:
            append(root / "model_calls.jsonl", {"event": "failed", "call_id": call_id, "task": label,
                                                "error": str(exc)})
            raise

    results = []
    with patch.object(OpenAICompatibleClient, "_post_chat", recorded):
        for task in frozen["tasks"]:
            for arm in task["arm_order"]:
                label = f"{task['id']}__{arm}"
                result = run_task(root, task, arm, tools, config, frozen["as_of_date"])
                results.append(result)
                write_json(root / "status.json", {"results": results, "model_calls": calls})
                print(json.dumps({k: result[k] for k in ("task_id", "arm", "target_complete", "reads", "error")}, ensure_ascii=False), flush=True)
    write_json(root / "summary.json", {"finished_at": utc_now_iso(), "results": results, "model_calls": calls,
        "production_changed": production_hashes() != manifest["production_sha256"],
        "corpus_changed": file_hash(DEFAULT_CHUNKS_PATH) != frozen["corpus_sha256"],
        "script_changed": file_hash(Path(__file__)) != manifest["script_sha256"],
        "fixture_changed": file_hash(fixture_path) != manifest["fixture_sha256"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--fixture", type=Path, default=FIXTURE, help="Frozen tasks for --run or --preflight, matching the exact corpus version")
    args = parser.parse_args()
    if sum((args.prepare, args.run is not None, args.preflight)) != 1:
        parser.error("Choose exactly one operation")
    if args.prepare:
        if args.fixture != FIXTURE:
            parser.error("--prepare creates the original task file; --fixture is only for run/preflight")
        prepare()
    elif args.preflight:
        frozen = json.loads(args.fixture.read_text(encoding="utf-8"))
        print(json.dumps(preflight(frozen, offline_tools(load_corpus(DEFAULT_CHUNKS_PATH)))))
    else:
        run(args.run, args.fixture)


if __name__ == "__main__":
    main()
