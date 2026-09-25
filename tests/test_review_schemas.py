from datetime import date

import pytest
from pydantic import ValidationError

from law_agent.review.schemas import ReviewFacts


def test_review_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReviewFacts(extra_field="not allowed")


@pytest.mark.parametrize(
    "semantic_null",
    ["null", " NULL ", "None", "unknown", "N/A", "未知", "未提供", "未说明"],
)
def test_review_facts_normalize_semantic_null_strings(semantic_null: str) -> None:
    facts = ReviewFacts(
        business_activity=semantic_null,
        overseas_recipient=semantic_null,
        processing_purpose=semantic_null,
        legal_basis_or_consent=semantic_null,
        industry=semantic_null,
    )

    assert facts.business_activity is None
    assert facts.overseas_recipient is None
    assert facts.processing_purpose is None
    assert facts.legal_basis_or_consent is None
    assert facts.industry is None


def test_review_facts_reject_legacy_single_region_field() -> None:
    # ``regions`` replaced the single-value ``region`` outright; old payloads
    # must fail loudly instead of being silently ignored.
    with pytest.raises(ValidationError):
        ReviewFacts(region="上海")


def test_review_facts_normalize_regions_list() -> None:
    facts = ReviewFacts(regions=[" 上海 ", "", "浙江", "上海", "  "])

    assert facts.regions == ["上海", "浙江"]


def test_review_facts_keep_non_local_region_values_visible() -> None:
    # "CN" must survive so extraction quality stays measurable; downstream
    # region matching decides what counts as a specific jurisdiction.
    facts = ReviewFacts(regions=["CN"])

    assert facts.regions == ["CN"]


def test_agent_json_date_is_accepted_under_strict_validation() -> None:
    facts = ReviewFacts.model_validate({"as_of_date": "2026-09-25"}, strict=True)
    assert facts.as_of_date == date(2026, 9, 25)
