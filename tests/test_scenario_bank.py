import pytest
from pydantic import ValidationError

from law_agent.review.evalset.bank.checks import run_checks
from law_agent.review.evalset.bank.loader import load_personas, load_scenarios, materialize
from law_agent.review.evalset.bank.schema import MaterialFile, RetrievalLabels
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.materials import material_from_file
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH


def test_bank_loads_with_known_personas_and_valid_intake() -> None:
    personas = load_personas()
    scenarios = load_scenarios()
    assert scenarios
    for scenario in scenarios:
        assert scenario.persona_id in personas
        assert set(scenario.intake) <= IntakePayload.model_fields.keys()
        IntakePayload.model_validate(scenario.intake)


def test_materialized_bundle_parses_like_an_upload(tmp_path) -> None:
    scenario = next(s for s in load_scenarios() if any(m.sheets for m in s.materials))
    for path in materialize(scenario, tmp_path):
        record = material_from_file(path)
        assert record.material_text.strip()
        if path.suffix == ".xlsx":
            assert "## 工作表" in record.material_text


def test_schema_rejects_unlocated_citations_and_mixed_bodies() -> None:
    with pytest.raises(ValidationError):
        RetrievalLabels(queries=["q"], must_cite=[{"source_id": "s", "note": "n"}])
    with pytest.raises(ValidationError):
        MaterialFile(filename="a.xlsx", kind="data_inventory", text="x")


@pytest.mark.skipif(not DEFAULT_CHUNKS_PATH.exists(), reason="corpus is not in Git")
def test_labels_are_consistent_with_corpus() -> None:
    assert run_checks() == []
