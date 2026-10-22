"""Capture native QA answer requests and compare one context change at a time."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from law_agent.config import require_agent_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.usage import usage_scope
from law_agent.review import qa_agent
from law_agent.review.evalset.delivery_cases import get_qa_cases
from law_agent.review.evalset.qa_runner import run_qa_case
from law_agent.review.qa_agent import execute_qa, is_tool_call_text

CASE_IDS = ("agent_hr_exception_missing_001", "eval_standard_contract_003")
REPO = Path(__file__).resolve().parents[1]
SCOPE_REPLACEMENTS = (
    (
        "分别判断程序机制、处理合法性基础和具体义务；某一机制豁免不能直接决定其他义务是否适用，其他义务自身的条件及例外也须考虑。",
        "按用户当前问题决定需要判断哪些事项：只展开会改变当前结论或避免实质误导的程序机制、处理合法性基础和具体义务；其他事项作简短边界说明。展开的事项仍须结合各自条件及例外，不能由机制豁免推定其他义务全部免除。",
    ),
    (
        "程序机制、处理合法性基础和具体义务可能需要不同查询，按实际缺口分批补查，不固定顺序。",
        "只为会改变当前结论或避免实质误导的关联规则分批补查，不固定顺序。有原文支持的条件分支也是完整答复，不以取得业务证明材料或完成全面检查为结束条件。",
    ),
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_hashes():
    return {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (REPO / "law_agent").rglob("*.py")}


class Captured(BaseException):
    pass


class CaptureClient(OpenAICompatibleClient):
    def __init__(self, config, output, case):
        super().__init__(config)
        self.output = output
        self.case = case
        self.calls = []
        self.captured = False

    def chat_tools(self, messages, *, tools, model=None):
        response = super().chat_tools(messages, tools=tools, model=model)
        self.calls.append({"messages": [m.to_dict() for m in messages],
                           "tools_sha256": digest(tools), "response": response})
        save(self.output / f"{self.case.case_id}.investigation.json", self.calls)
        return response

    def stream_text(self, messages, *, model=None, tools=None):
        packet = {"case_id": self.case.case_id, "question": self.case.question,
                  "messages": [m.to_dict() for m in messages], "tools": tools}
        assert packet["messages"][-1]["role"] in {"tool", "user"}
        assert "ready_to_answer" in packet["messages"][-1]["content"]
        save(self.output / f"{self.case.case_id}.snapshot.json", packet)
        self.captured = True
        raise Captured()


def capture(output, config):
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "capture.manifest.json"
    if manifest_path.exists():
        raise RuntimeError("Refuse overwrite or recapture")
    cases = {case.case_id: case for case in get_qa_cases()}
    manifest = {"started_at": datetime.now(UTC).isoformat(), "case_ids": CASE_IDS,
                "model": config.model, "reasoning_effort": config.reasoning_effort,
                "source_hashes": source_hashes(),
                "case_hashes": {cid: digest(cases[cid].model_dump(mode="json")) for cid in CASE_IDS},
                "limits": "One real investigation per case. Capture before final stream, without sending an answer request. Native history, original sources and provider reasoning retained; no handoff summary, oracle, judge or resampling. Capture-only runs are not completed QA answers."}
    save(manifest_path, manifest)
    save(output / "cases.json", [cases[cid].model_dump(mode="json") for cid in CASE_IDS])
    for cid in CASE_IDS:
        client = CaptureClient(config, output, cases[cid])

        def execute(conversation, run, *, checkpoint, chunks_path, client=client):
            try:
                execute_qa(conversation, run, checkpoint=checkpoint, chunks_path=chunks_path, client=client)
            except Captured:
                pass

        started = time.monotonic()
        result = run_qa_case(cases[cid], execute=execute)
        save(output / f"{cid}.capture_result.json", result)
        if not client.captured:
            raise RuntimeError(f"Investigation did not reach final answer: {cid}")
        print(json.dumps({"event": "snapshot_captured", "case_id": cid,
                          "latency_ms": round((time.monotonic() - started) * 1000),
                          "searches": result["searches"], "reads": result["reads"]}), flush=True)
    save(output / "capture.completion.json", {"finished_at": datetime.now(UTC).isoformat(),
        "source_unchanged": source_hashes() == manifest["source_hashes"]})


def variant(snapshot, arm):
    packet = deepcopy(snapshot)
    if arm == "question_echo":
        message = packet["messages"][-1]
        payload = json.loads(message["content"])
        control = payload["observation"] if message["role"] == "tool" else payload
        assert control["ready_to_answer"] is True
        assert "user_question" not in control
        control["user_question"] = snapshot["question"]
        message["content"] = json.dumps(payload, ensure_ascii=False)
        restored = deepcopy(packet)
        payload2 = json.loads(restored["messages"][-1]["content"])
        control2 = payload2["observation"] if message["role"] == "tool" else payload2
        del control2["user_question"]
        restored["messages"][-1]["content"] = json.dumps(payload2, ensure_ascii=False)
        assert restored == snapshot
    elif arm == "scope_rule":
        system = packet["messages"][0]
        assert system["role"] == "system"
        for old, new in SCOPE_REPLACEMENTS:
            assert system["content"].count(old) == 1
            system["content"] = system["content"].replace(old, new)
        assert packet["messages"][1:] == snapshot["messages"][1:]
    elif arm != "baseline":
        raise ValueError(arm)
    assert packet["tools"] == snapshot["tools"]
    if arm != "scope_rule":
        assert packet["messages"][:-1] == snapshot["messages"][:-1]
    return packet


class ObservedResponse:
    def __init__(self, response, events):
        self.response = response
        self.events = events

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def __iter__(self):
        for line in self.response:
            value = line.decode("utf-8").strip()
            if value.startswith("data:") and value[5:].strip() != "[DONE]":
                self.events.append(json.loads(value[5:].strip()))
            yield line


def answer_once(packet, *, config, stem):
    client = OpenAICompatibleClient(config)
    usage, events = [], []
    urlopen = urllib.request.urlopen

    def observe(request, **kwargs):
        payload = json.loads(request.data)
        assert payload["messages"] == packet["messages"]
        assert payload["tools"] == packet["tools"]
        assert payload["tool_choice"] == "none" and payload["stream"] is True
        save(stem.with_suffix(".request.json"), payload)
        return ObservedResponse(urlopen(request, **kwargs), events)

    started = time.monotonic()
    answer = ""
    try:
        with (
            patch("law_agent.llm.openai_compatible.urllib.request.urlopen", side_effect=observe),
            usage_scope("qa_scope_slice_answer", usage.append),
            closing(client.stream_text(
                [ChatMessage(**m) for m in packet["messages"]], tools=packet["tools"],
            )) as stream,
        ):
            for delta in stream:
                answer += delta
                if len(answer) > 60000 or is_tool_call_text(answer):
                    raise RuntimeError("Invalid or oversized QA answer")
        row = {"status": "completed"}
    except (OSError, RuntimeError, ValueError) as exc:
        row = {"status": "failed", "error": type(exc).__name__}
    reasoning = "".join(choice.get("delta", {}).get("reasoning_content") or ""
                        for event in events for choice in event.get("choices", []))
    finish = [choice["finish_reason"] for event in events for choice in event.get("choices", [])
              if choice.get("finish_reason")]
    row.update(latency_ms=round((time.monotonic() - started) * 1000), usage=usage,
               finish_reasons=finish, reasoning_characters=len(reasoning), answer_characters=len(answer))
    stem.with_suffix(".answer.md").write_text(answer, encoding="utf-8")
    save(stem.with_suffix(".response.json"), {"answer": answer, "reasoning_content": reasoning, "events": events})
    save(stem.with_suffix(".result.json"), row)
    return row


def compare(output, config):
    manifest_path = output / "phase_a.manifest.json"
    if manifest_path.exists():
        raise RuntimeError("Refuse overwrite or resampling")
    captured = json.loads((output / "capture.manifest.json").read_text(encoding="utf-8"))
    assert config.model == captured["model"] and config.reasoning_effort == captured["reasoning_effort"]
    assert source_hashes() == captured["source_hashes"]
    snapshots = {cid: json.loads((output / f"{cid}.snapshot.json").read_text(encoding="utf-8")) for cid in CASE_IDS}
    order = [(cid, arm, repeat) for repeat, arms in [(1, ("baseline", "question_echo")),
                                                   (2, ("question_echo", "baseline"))]
             for cid in CASE_IDS for arm in arms]
    manifest = {"started_at": datetime.now(UTC).isoformat(), "model": config.model,
                "reasoning_effort": config.reasoning_effort, "order": order, "max_answer_requests": 8,
                "snapshot_hashes": {cid: digest(s) for cid, s in snapshots.items()},
                "change": "Add only verbatim user_question to final control. All prior messages, original evidence, provider reasoning, tool schemas, system prompt and streaming tool_choice=none unchanged.",
                "primary_checks": ["reopens_user_given_necessity", "invented_contract_content_requirements",
                                   "unknown_treated_as_false", "blanket_consent_without_exception",
                                   "unasked_business_proof_or_audit_checklist", "core_answer_and_refs_preserved"],
                "assessment": "Read answers against original fixtures and actual source text; provider reasoning used for diagnosis, not answer scoring. No judge, private-summary regeneration or favorable-result resampling. Two repetitions per case/arm are exploratory, not a stable accuracy estimate."}
    save(manifest_path, manifest)
    rows = []
    for cid, arm, repeat in order:
        packet = variant(snapshots[cid], arm)
        row = answer_once(packet, config=config, stem=output / f"{cid}.{arm}.r{repeat}.stream")
        row.update(case_id=cid, arm=arm, repeat=repeat, input_sha256=digest(packet))
        rows.append(row)
        save(output / "phase_a.results.json", rows)
        print(json.dumps({key: row[key] for key in ("case_id", "arm", "repeat", "status", "latency_ms")}), flush=True)
    save(output / "phase_a.completion.json", {"finished_at": datetime.now(UTC).isoformat(),
        "answer_requests": len(rows), "source_unchanged": source_hashes() == captured["source_hashes"]})


def scope_compare(output, config):
    manifest_path = output / "phase_b.manifest.json"
    if manifest_path.exists():
        raise RuntimeError("Refuse overwrite or resampling")
    captured = json.loads((output / "capture.manifest.json").read_text(encoding="utf-8"))
    assert config.model == captured["model"] and config.reasoning_effort == captured["reasoning_effort"]
    assert source_hashes() == captured["source_hashes"]
    assert (output / "phase_a.completion.json").exists()
    snapshots = {cid: json.loads((output / f"{cid}.snapshot.json").read_text(encoding="utf-8")) for cid in CASE_IDS}
    order = [(CASE_IDS[0], 1), (CASE_IDS[1], 1), (CASE_IDS[1], 2), (CASE_IDS[0], 2)]
    manifest = {"started_at": datetime.now(UTC).isoformat(), "model": config.model,
        "reasoning_effort": config.reasoning_effort, "order": order, "max_answer_requests": 4,
        "snapshot_hashes": {cid: digest(s) for cid, s in snapshots.items()},
        "replacements": SCOPE_REPLACEMENTS,
        "change": "Replace only the two instructions defining related-issue scope and completion; do not add question_echo. All native user/assistant/tool messages and tools remain identical to the frozen baseline.",
        "limits": "Two candidates per case, compared with the two existing phase-A baseline outputs. Exploratory sequential slice, not simultaneous randomized evaluation or full investigative rerun. No oracle, judge, added sources, retries or favorable-result resampling. Same predeclared answer checks as phase A."}
    save(manifest_path, manifest)
    rows = []
    for cid, repeat in order:
        packet = variant(snapshots[cid], "scope_rule")
        row = answer_once(packet, config=config, stem=output / f"{cid}.scope_rule.r{repeat}.stream")
        row.update(case_id=cid, arm="scope_rule", repeat=repeat, input_sha256=digest(packet))
        rows.append(row)
        save(output / "phase_b.results.json", rows)
        print(json.dumps({key: row[key] for key in ("case_id", "arm", "repeat", "status", "latency_ms")}), flush=True)
    save(output / "phase_b.completion.json", {"finished_at": datetime.now(UTC).isoformat(),
        "answer_requests": len(rows), "source_unchanged": source_hashes() == captured["source_hashes"]})


class LiveClient(OpenAICompatibleClient):
    def __init__(self, config, output, case_id):
        super().__init__(config)
        self.output = output
        self.case_id = case_id
        self.calls = []

    def chat_tools(self, messages, *, tools, model=None):
        response = super().chat_tools(messages, tools=tools, model=model)
        self.calls.append({"messages": [m.to_dict() for m in messages],
                           "tools_sha256": digest(tools), "response": response})
        save(self.output / f"{self.case_id}.scope_live.calls.json", self.calls)
        return response

    def stream_text(self, messages, *, model=None, tools=None):
        events = []
        urlopen = urllib.request.urlopen

        def observe(request, **kwargs):
            save(self.output / f"{self.case_id}.scope_live.answer.request.json", json.loads(request.data))
            return ObservedResponse(urlopen(request, **kwargs), events)

        answer = ""
        try:
            with (
                patch("law_agent.llm.openai_compatible.urllib.request.urlopen", side_effect=observe),
                closing(super().stream_text(messages, model=model, tools=tools)) as stream,
            ):
                for delta in stream:
                    answer += delta
                    yield delta
        finally:
            reasoning = "".join(choice.get("delta", {}).get("reasoning_content") or ""
                                for event in events for choice in event.get("choices", []))
            save(self.output / f"{self.case_id}.scope_live.answer.response.json",
                 {"answer": answer, "reasoning_content": reasoning, "events": events})


def live_scope(output, config):
    manifest_path = output / "phase_b_live.manifest.json"
    if manifest_path.exists():
        raise RuntimeError("Refuse overwrite or rerun")
    captured = json.loads((output / "capture.manifest.json").read_text(encoding="utf-8"))
    assert source_hashes() == captured["source_hashes"]
    assert (output / "phase_b.completion.json").exists()
    cases = {case.case_id: case for case in get_qa_cases()}
    assert all(digest(cases[cid].model_dump(mode="json")) == captured["case_hashes"][cid] for cid in CASE_IDS)
    candidate = qa_agent.PROMPT
    for old, new in SCOPE_REPLACEMENTS:
        assert candidate.count(old) == 1
        candidate = candidate.replace(old, new)
    save(manifest_path, {"started_at": datetime.now(UTC).isoformat(), "model": config.model,
        "reasoning_effort": config.reasoning_effort, "case_ids": CASE_IDS, "runs_per_case": 1,
        "replacements": SCOPE_REPLACEMENTS, "candidate_prompt_sha256": digest(candidate),
        "case_hashes": captured["case_hashes"],
        "limits": "Apply the same scope-only candidate from the first native request, in this diagnostic process only. One actual QA run per case. Same original cases, tools, corpus, model and budgets, but retrieval and sampling can differ from baseline captures; this is behavior observation, not fixed-evidence causal proof. No question_echo, judge, manual handoff, oracle, retries or resampling. Stop after two runs."})
    rows = []
    for cid in CASE_IDS:
        client = LiveClient(config, output, cid)

        def execute(conversation, run, *, checkpoint, chunks_path, client=client):
            execute_qa(conversation, run, checkpoint=checkpoint, chunks_path=chunks_path, client=client)

        with patch.object(qa_agent, "PROMPT", candidate):
            result = run_qa_case(cases[cid], execute=execute)
        save(output / f"{cid}.scope_live.result.json", result)
        (output / f"{cid}.scope_live.answer.md").write_text(result["run"]["answer"], encoding="utf-8")
        row = {key: result[key] for key in ("case_id", "status", "latency_ms", "searches", "reads", "checks")}
        rows.append(row)
        save(output / "phase_b_live.results.json", rows)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    save(output / "phase_b_live.completion.json", {"finished_at": datetime.now(UTC).isoformat(),
        "runs": len(rows), "source_unchanged": source_hashes() == captured["source_hashes"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "compare", "scope", "live-scope"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = require_agent_llm_config()
    if config.model != "deepseek-flash" or config.reasoning_effort != "low":
        raise RuntimeError("This fixed comparison expects deepseek-flash / low")
    {"capture": capture, "compare": compare, "scope": scope_compare,
     "live-scope": live_scope}[args.mode](args.output, config)


if __name__ == "__main__":
    main()
