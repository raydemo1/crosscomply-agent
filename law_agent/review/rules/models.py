"""Typed inputs and outputs for the national cross-border compliance rules."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from law_agent.data.schemas import StrictModel

CompliancePathCode = Literal[
    "not_applicable",
    "statutory_exemption",
    "security_assessment",
    "standard_contract_or_certification",
    "no_filing_below_threshold",
]
PathConfidence = Literal["determined", "possible"]
DecisionStatus = Literal["determined", "needs_info"]
ExemptionKind = Literal[
    "overseas_data_transit",
    "individual_contract",
    "hr_management",
    "emergency",
]
SpecialRegime = Literal["free_trade_zone", "greater_bay_area", "industry_specific"]


class ComplianceFacts(StrictModel):
    """Confirmed facts consumed by the deterministic rule engine.

    Counts cover data subjects cumulatively provided abroad since January 1
    of the current calendar year. ``cumulative_personal_information_subjects``
    excludes sensitive personal information, matching the regulatory wording.
    ``None`` always means unknown; the engine never fills it by inference.
    """

    cross_border_transfer: bool | None = None
    is_ciio: bool | None = None
    important_data: bool | None = None
    contains_personal_information: bool | None = None
    contains_sensitive_personal_information: bool | None = None
    cumulative_personal_information_subjects: int | None = Field(default=None, ge=0)
    cumulative_sensitive_personal_information_subjects: int | None = Field(default=None, ge=0)
    claimed_exemption: ExemptionKind | None = None
    exemption_facts_confirmed: bool | None = None
    special_regimes: list[SpecialRegime] = Field(default_factory=list)

    @field_validator("special_regimes")
    @classmethod
    def normalize_special_regimes(cls, value: list[SpecialRegime]) -> list[SpecialRegime]:
        return sorted(set(value))

    @model_validator(mode="after")
    def reject_contradictory_personal_information_facts(self) -> ComplianceFacts:
        if self.contains_personal_information is False:
            if self.contains_sensitive_personal_information is True:
                raise ValueError("sensitive personal information is personal information")
            if (self.cumulative_personal_information_subjects or 0) > 0:
                raise ValueError(
                    "personal information count contradicts contains_personal_information"
                )
            if (self.cumulative_sensitive_personal_information_subjects or 0) > 0:
                raise ValueError(
                    "sensitive personal information count contradicts contains_personal_information"
                )
        if (
            self.contains_sensitive_personal_information is False
            and (self.cumulative_sensitive_personal_information_subjects or 0) > 0
        ):
            raise ValueError(
                "sensitive personal information count contradicts "
                "contains_sensitive_personal_information"
            )
        return self


class OfficialBasis(StrictModel):
    """An official provision supporting a deterministic rule."""

    basis_id: str
    title: str
    article: str
    issuing_body: str
    source_url: str


class MissingFact(StrictModel):
    """A fact that must be confirmed before selecting one final path."""

    key: str
    reason: str


class RuleHit(StrictModel):
    """One rule evaluated as relevant to the supplied facts."""

    rule_id: str
    summary: str
    basis_ids: list[str] = Field(default_factory=list)


class CandidatePath(StrictModel):
    """A compliance path selected or still possible because facts are missing."""

    code: CompliancePathCode
    label: str
    confidence: PathConfidence
    reason: str


class ComplianceDecision(StrictModel):
    """Stable, auditable output of the national-path rules."""

    status: DecisionStatus
    rule_version: str
    candidate_paths: list[CandidatePath]
    needs_info: list[MissingFact] = Field(default_factory=list)
    rule_hits: list[RuleHit] = Field(default_factory=list)
    official_bases: list[OfficialBasis] = Field(default_factory=list)
    requires_rag_human_confirmation: bool = False
    manual_confirmation_reasons: list[str] = Field(default_factory=list)
