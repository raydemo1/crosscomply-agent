import importlib
import json
from pathlib import Path

import pytest

from law_agent.review.schemas import ReviewFacts
from tests.test_review_llm import FakeClient
from tests.test_review_result_builder import _hit
from tests.test_semantic_grounding import _draft


@pytest.fixture
def experiment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("run_agent_ab")


def test_count_contrast_changes_only_the_number_and_keeps_judgment_constraints(experiment):
    cases = experiment.comparison_cases()
    original, contrast = cases[0], cases[-1]
    assert len(cases) == 6
    assert contrast.material_text.replace("出境120万人", "出境12万人") == original.material_text
    assert {key for key in contrast.intake if contrast.intake[key] != original.intake[key]} == {"annual_non_sensitive_count"}
    assert contrast.rubric == original.rubric
    assert contrast.question == original.question


@pytest.mark.parametrize("variant", ["A", "B"])
def test_experiment_preserves_cited_authorities_and_only_b_adds_returned_contrast(experiment, tmp_path, variant):
    client = FakeClient(outputs=[{
        "status": "supported", "conclusion_reason": "支持",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}],
    }])
    verifier = experiment.ComparisonVerifier(variant=variant, directory=tmp_path, model_id="test", client=client)
    evidence = [_hit(), _hit().model_copy(update={"chunk_id": "c2", "article_no": "第十三条"}),
                _hit().model_copy(update={"chunk_id": "guide", "can_cite_clause": False})]
    verifier(draft=_draft(), review_goal="审查", confirmed_intake={}, extracted_facts=ReviewFacts(), material="材料", evidence=evidence)
    payload = json.loads(client.calls[0][1].content)
    assert [item["chunk_id"] for item in payload["cited_authorities"]] == ["c1"]
    if variant == "B":
        assert [item["chunk_id"] for item in payload["contrast_authorities"]] == ["c2"]
    else:
        assert "contrast_authorities" not in payload
    recorded = json.loads((tmp_path / "verifier_inputs.jsonl").read_text(encoding="utf-8"))
    assert json.loads(recorded["messages"][1]["content"]) == payload


def test_contrast_context_has_limits_and_does_not_truncate_articles(experiment):
    evidence = [_hit().model_copy(update={"chunk_id": f"c{i}", "article_no": str(i), "full_article_text": "正文" * 900}) for i in range(12)]
    selected = experiment.contrast_authorities(evidence, _draft(), {})
    assert len(selected) <= 8
    assert sum(len(item["article_text"]) for item in selected) <= 12000
    assert all(item["article_text"] == "正文" * 900 for item in selected)


def test_prompt_comparison_freezes_identical_inputs_and_rejects_tampering(experiment, monkeypatch, tmp_path):
    probe = importlib.import_module("run_prompt_probe")
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "data/review_runs/grounding_probe_20261010"
    source.mkdir(parents=True)
    context = {"draft": _draft().model_dump(mode="json"), "confirmed_intake": {"count": 0}, "report_items": {"unused": "text"}}
    for _, filename, _ in probe.SOURCES:
        (source / filename).write_text(json.dumps({"messages": [
            {"role": "system", "content": "old"},
            {"role": "user", "content": json.dumps(context)},
        ]}), encoding="utf-8")
    output = tmp_path / "comparison"
    output.mkdir()
    requests = probe.freeze_requests(output)
    assert len(requests) == 6
    for case in {request["case"] for request in requests}:
        pair = [request for request in requests if request["case"] == case]
        assert pair[0]["messages"][1] == pair[1]["messages"][1]
    assert probe.freeze_requests(output) == requests
    (output / "requests.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        probe.freeze_requests(output)
