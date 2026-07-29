"""Cleaning pipeline for normalized documents."""

from __future__ import annotations

from law_agent.data.cleaners.common import clean_text
from law_agent.data.schemas import CleanedDocument, Document


def clean_document(document: Document) -> CleanedDocument:
    result = clean_text(document.text, title=document.title)
    return CleanedDocument.model_validate(
        document.model_dump()
        | {"text": result.text, "cleaning_rule_hits": result.rule_hits}
    )
