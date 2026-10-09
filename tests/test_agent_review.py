import json
import socket

import pytest

from law_agent.llm.openai_compatible import OpenAICompatibleClient
from law_agent.review.evalset import agent_review


def test_review_export_is_offline_and_keeps_pending_decisions(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline review must not call a model or network")

    monkeypatch.setattr(OpenAICompatibleClient, "_post_chat", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    directory = tmp_path / "review"
    manifest = agent_review.export_review_package(directory)
    cases = json.loads((directory / "cases.json").read_text(encoding="utf-8"))
    reviews = json.loads((directory / "review.json").read_text(encoding="utf-8"))

    assert manifest["evaluation_performed"] is False
    assert manifest["approved_cases"] == 0
    assert manifest["production_hashes"]
    assert all(case["review_status"] == "candidate" for group in cases.values() for case in group)
    assert len(reviews) == sum(len(group) for group in cases.values())
    assert all(review["decision"] == "pending" and not review["reviewer"] for review in reviews)
    for group in cases.values():
        for case in group:
            parsed = agent_review.AgentCase.model_validate(case)
            assert manifest["case_hashes"][parsed.case_id] == agent_review.case_hash(parsed)
    with pytest.raises(FileExistsError):
        agent_review.export_review_package(directory)


def test_review_rejects_overlap_between_regression_and_holdout(monkeypatch):
    regression = agent_review.get_agent_cases("core")
    monkeypatch.setattr(agent_review, "build_holdout_cases", lambda: [regression[0]])

    with pytest.raises(ValueError, match="duplicate review case"):
        agent_review.review_groups()


def test_review_fingerprint_changes_when_rubric_changes():
    case = agent_review.build_holdout_cases()[0]
    original = agent_review.case_hash(case)
    case.rubric.forbidden_judgments.append("另一个待审定判断约束")

    assert agent_review.case_hash(case) != original


def test_incremental_selection_cannot_use_changed_case_or_tampered_plan(monkeypatch):
    groups = agent_review.review_groups()
    _record, plan = agent_review.reviewed_selection(groups)
    assert len(agent_review.cases_from_selection(plan)) == plan["selected_count"]
    plan["selected"][0]["case_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="differs from the reviewed case list"):
        agent_review.cases_from_selection(plan)

    groups["regression"][0].rubric.forbidden_judgments.append("尚未审定的新边界")
    monkeypatch.setattr(agent_review, "review_groups", lambda: groups)
    with pytest.raises(ValueError, match="case changed after review"):
        agent_review.reviewed_selection(groups)


def test_baseline_dry_run_never_loads_paid_model_config(tmp_path, monkeypatch, capsys):
    from scripts import run_agent_baseline

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry run must not configure or execute a paid model")

    agent_review.export_review_package(tmp_path / "review")
    monkeypatch.setattr(run_agent_baseline, "require_llm_config", forbidden)
    monkeypatch.setattr(run_agent_baseline, "run_one", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("sys.argv", ["run_agent_baseline.py", "--selection-plan",
                                     str(tmp_path / "review" / "selection.json"), "--dry-run"])

    assert run_agent_baseline.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation_performed"] is False
    assert result["model_calls"] == 0
    assert all(case_id.startswith("holdout_") for case_id in result["case_ids"][-6:])


def test_selection_is_portable_across_review_record_line_endings(tmp_path, monkeypatch):
    _record, plan = agent_review.reviewed_selection(agent_review.review_groups())
    portable_record = tmp_path / "review.json"
    text = agent_review.REVIEW_RECORD_PATH.read_text(encoding="utf-8")
    original = agent_review.REVIEW_RECORD_PATH.read_bytes()
    newline = "\n" if b"\r\n" in original else "\r\n"
    portable_record.write_bytes(text.replace("\n", newline).encode())
    assert portable_record.read_bytes() != original
    monkeypatch.setattr(agent_review, "REVIEW_RECORD_PATH", portable_record)

    assert len(agent_review.cases_from_selection(plan)) == plan["selected_count"]
