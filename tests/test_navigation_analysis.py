"""Offline attribution must retain ambiguity and corpus version boundaries."""

import json
from pathlib import Path

import pytest

from scripts.analyze_navigation import history_report, read_results
from tests.test_review_agent import _chunk


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("batched", [False, True])
def test_history_links_single_and_batch_reads_to_verifier(tmp_path, batched):
    target = _chunk("target", "第十三条", "完整原文", index=0).model_copy(update={
        "title": "中华人民共和国个人信息保护法",
    })
    corpus = tmp_path / "chunks.jsonl"
    corpus.write_text(target.model_dump_json() + "\n", encoding="utf-8")
    root = tmp_path / "runs"
    case = root / "A" / "eval_standard_contract_003"
    result = {"read_request": {"source_id": "s1", "article_no": "第十三条", "offset": 0},
              "chunk_ids": ["target"]}
    write_json(case / "state.json", {"steps": [
        {"number": 1, "action": "read_evidence", "observation":
         {"read_results": [result]} if batched else result},
    ], "evidence": [{"chunk_id": "target", "text": "完整原文"}]})
    write_json(case / "verifier_inputs.jsonl", {"messages": [{"role": "user", "content": json.dumps({
        "cited_authorities": [{"chunk_id": "target"}],
    })}]})
    write_json(root / "offline_assessment.json", {"findings": [
        {"case_id": case.name, "variants": ["A"], "finding": "已有复核发现"},
    ]})
    row = history_report(corpus, [root])["records"][0]
    assert row["category"] == "text_returned_with_reviewed_error"
    assert row["returned_target_chunk_ids"] == row["verifier_target_chunk_ids"] == ["target"]


def test_unknown_historical_ids_are_not_silently_remapped(tmp_path):
    target = _chunk("new-id", "第十三条", "新原文", index=0).model_copy(update={
        "title": "中华人民共和国个人信息保护法",
    })
    corpus = tmp_path / "chunks.jsonl"
    corpus.write_text(target.model_dump_json() + "\n", encoding="utf-8")
    root = tmp_path / "runs"
    case = root / "B" / "eval_standard_contract_003"
    write_json(case / "state.json", {"steps": [{
        "number": 1, "action": "search_evidence", "observation": {"chunk_ids": ["old-id"]},
    }]})
    row = history_report(corpus, [root])["records"][0]
    assert row["category"] == "version_mismatch"
    assert row["unmatched_chunk_ids"] == ["old-id"]
    assert row["returned_target_chunk_ids"] == []


def test_missing_targets_are_not_navigation_failures():
    assert read_results({"chunk_ids": ["search-hit"]}) == []
    assert read_results({"read_results": []}) == []
