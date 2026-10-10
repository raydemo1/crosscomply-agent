import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from law_agent.config import LLMConfig
from tests.test_review_llm import FakeClient
from tests.test_review_result_builder import _hit
from tests.test_semantic_grounding import _draft


@pytest.mark.parametrize("citation,bad_read", [("c1", False), ("invented", False), ("c1", True)])
def test_investigation_has_no_candidate_report_and_cannot_invent_references(monkeypatch, tmp_path, citation, bad_read):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    probe = importlib.import_module("run_issue_probe")
    context = {"review_goal": "判断适用条件", "agent_extracted_facts": {},
               "frozen_material": "冻结材料", "draft": _draft().model_dump(mode="json")}
    probe.write_json(tmp_path / "case.json", context)
    client = FakeClient(outputs=[
        {"issues": [{"statement_ref": "conclusion", "question": "适用条件是什么？"}]},
        {"action": "search_evidence", "summary": "调查", "queries": [{"query_id": "q1", "text": "适用条件", "query_type": "legal_issue"}]},
        {"action": "finish", "summary": "完成", "findings": [{"answer": "条件性判断", "evidence_chunk_ids": [citation]}]},
    ])
    if bad_read:
        client.outputs.insert(2, {"action": "read_evidence", "summary": "继续阅读", "source_id": "s1", "chunk_id": "c1", "offset": 200})
    config = LLMConfig("https://invalid.example", "https://invalid.example", None, "deepseek-flash", 10, "json_object", "low")
    monkeypatch.setattr(probe, "require_llm_config", lambda: config)
    monkeypatch.setattr(probe, "ProbeClient", lambda *args: client)
    closed = []

    def read(*args):
        raise ValueError("按 chunk_id 读取不使用 offset")

    tools = SimpleNamespace(
        _chunks_by_id={"c1": SimpleNamespace(applicable_region="CN", applicable_subjects=["数据处理者"])},
        search=lambda *args: [_hit()], read_evidence=read, close=lambda: closed.append(True),
    )
    monkeypatch.setattr(probe, "ComplianceAgentTools", lambda **kwargs: tools)
    if citation == "invented":
        with pytest.raises(ValueError, match="not returned"):
            probe.investigate(tmp_path, "case")
    else:
        assert probe.investigate(tmp_path, "case")["status"] == "completed"
    assert closed == [True]
    assert json.loads(client.calls[0][1].content)["report_statements"]["conclusion"] == context["draft"]["conclusion"]
    research = [json.loads(call[1].content) for call in client.calls[1:]]
    assert all("draft" not in payload for payload in research)
    assert research[0]["evidence"] == []
    assert research[-1]["evidence"][0]["applicable_region"] == "CN"
    assert research[-1]["evidence"][0]["applicable_subjects"] == ["数据处理者"]
    if bad_read:
        assert research[-1]["steps"][-1]["observation"] == {"error": "按 chunk_id 读取不使用 offset"}
