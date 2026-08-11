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
        region=semantic_null,
    )

    assert facts.business_activity is None
    assert facts.overseas_recipient is None
    assert facts.processing_purpose is None
    assert facts.legal_basis_or_consent is None
    assert facts.industry is None
    assert facts.region is None
