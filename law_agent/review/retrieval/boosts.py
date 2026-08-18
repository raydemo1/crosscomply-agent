"""Metadata boost rules for hybrid retrieval.

Issue 6: Apply soft metadata boosts based on ``ReviewFacts`` and query type.
Boosts are multipliers on the RRF score — they elevate matching evidence
without hard-filtering other roles, per the implementation plan: "检索阶段
默认软加权，引用阶段严格治理".

Boost rules:
- ``primary_legal_basis``: always slightly boosted (national law priority)
- ``conditional_local_basis``: boosted when ``ReviewFacts.region`` matches
  the chunk's ``applicable_region``; lightly demoted when another explicit
  local region is known not to match
- ``conditional_industry_basis``: boosted when ``ReviewFacts.industry``
  matches the chunk's ``applicable_subjects`` or ``topic_tags``; lightly
  demoted when another explicit industry is known not to match
- ``implementation_reference``: boosted for missing_information queries
- ``interpretation_auxiliary``: always slightly demoted (keep retrievable
  but lower authority)
"""

from __future__ import annotations

from law_agent.data.citation_policy import IMPLEMENTATION_REFERENCE_SOURCE_IDS
from law_agent.data.schemas import Chunk
from law_agent.review.schemas import RetrievalHit, RetrievalQueryType, ReviewFacts

# ---------------------------------------------------------------------------
# Region mapping: Chinese name -> ISO 3166-2 subdivision code
# ---------------------------------------------------------------------------

_REGION_CODE_MAP: dict[str, str] = {
    "上海": "CN-SH",
    "广东": "CN-GD",
    "深圳": "CN-GD-SZ",
    "天津": "CN-TJ",
    "福建": "CN-FJ",
    "广西": "CN-GX",
    "重庆": "CN-CQ",
    "浙江": "CN-ZJ",
    "海南": "CN-HI",
    "北京": "CN-BJ",
    "江苏": "CN-JS",
}

_NON_LOCAL_REGION_VALUES: frozenset[str] = frozenset(
    {"CN", "中国", "全国", "境内", "全国范围", "null", "none", "unknown"}
)

# ---------------------------------------------------------------------------
# Industry matching: fact industry -> keywords to match in applicable_subjects
# and topic_tags
# ---------------------------------------------------------------------------

_INDUSTRY_KEYWORD_MAP: dict[str, list[str]] = {
    "智能网联汽车": ["汽车", "智能网联"],
    "汽车": ["汽车"],
    "车联网": ["车联网", "智能网联", "汽车"],
    "跨境电商": ["跨境电商", "电子商务", "电商"],
    "金融": ["金融"],
    "金融信息服务": ["金融"],
    "医疗": ["医疗", "健康"],
    "教育": ["教育"],
}


# ---------------------------------------------------------------------------
# Boost factor constants
# ---------------------------------------------------------------------------

PRIMARY_LEGAL_BASIS_BOOST = 1.2
CROSS_BORDER_PRIMARY_LEGAL_BASIS_BOOST = 1.25
CONDITIONAL_LOCAL_BASIS_BOOST = 1.5
CONDITIONAL_LOCAL_MISMATCH_WEIGHT = 0.45
CONDITIONAL_INDUSTRY_BASIS_BOOST = 1.4
CONDITIONAL_INDUSTRY_MISMATCH_WEIGHT = 0.75
IMPLEMENTATION_REFERENCE_BOOST = 1.15
INTERPRETATION_AUXILIARY_BOOST = 0.85
MISSING_INFORMATION_QUERY_WEIGHT = 0.7

_CROSS_BORDER_TERMS: tuple[str, ...] = ("数据出境", "跨境", "境外", "跨境流动")


def compute_boost_for_hit(
    hit: RetrievalHit,
    chunk: Chunk,
    facts: ReviewFacts,
    query_type: RetrievalQueryType | None = None,
) -> float:
    """Compute a multiplicative boost factor for a single hit.

    Returns 1.0 when no boost applies. Multiple matching conditions
    stack multiplicatively, but the total is capped to avoid runaway
    inflation.
    """

    boost = 1.0
    role = hit.citation_role

    # Primary legal basis: always slightly elevated.
    # Filing guide + standard contract template (in IMPLEMENTATION_REFERENCE_SOURCE_IDS)
    # are implementation documents demoted from CLAUSE_CITABLE_SOURCE_IDS for citation
    # governance only (can_cite_clause=False). Their *retrievability* must stay
    # identical to primary law — so they are treated as primary-like for boosts.
    is_primary_like = (
        role == "primary_legal_basis" or hit.source_id in IMPLEMENTATION_REFERENCE_SOURCE_IDS
    )
    if is_primary_like:
        boost *= PRIMARY_LEGAL_BASIS_BOOST
        if facts.cross_border_transfer and _chunk_mentions_any(chunk, _CROSS_BORDER_TERMS):
            boost *= CROSS_BORDER_PRIMARY_LEGAL_BASIS_BOOST

    # Conditional local basis: boost when region matches
    if role == "conditional_local_basis" and facts.region:
        region_code = _normalize_region(facts.region)
        if _region_matches(chunk.applicable_region, region_code):
            boost *= CONDITIONAL_LOCAL_BASIS_BOOST
        elif _is_specific_region(region_code):
            boost *= CONDITIONAL_LOCAL_MISMATCH_WEIGHT

    # Conditional industry basis: boost when industry matches
    if role == "conditional_industry_basis" and facts.industry:
        if _industry_matches(chunk, facts.industry):
            boost *= CONDITIONAL_INDUSTRY_BASIS_BOOST
        elif _is_specific_industry(facts.industry):
            boost *= CONDITIONAL_INDUSTRY_MISMATCH_WEIGHT

    # Implementation reference: boost for missing_information queries
    if role == "implementation_reference" and query_type == "missing_information":
        boost *= IMPLEMENTATION_REFERENCE_BOOST

    # Interpretation auxiliary: always slightly demoted
    if role == "interpretation_auxiliary":
        boost *= INTERPRETATION_AUXILIARY_BOOST

    # Missing-information queries are intentionally broad and often retrieve
    # generic privacy-law clauses. Keep them as recall support, but stop them
    # from dominating the final RRF/source-fusion ranking.
    effective_query_type = query_type or hit.matched_query_type
    if effective_query_type == "missing_information":
        boost *= MISSING_INFORMATION_QUERY_WEIGHT

    return boost


def compute_boosts_summary(
    facts: ReviewFacts,
    query_types: list[RetrievalQueryType | None],
) -> dict[str, float]:
    """Build a human-readable summary of active boost rules for the trace.

    This is stored in ``RetrievalTrace.metadata_boosts`` so the trace
    records which boost rules were active, even if individual hits don't
    all trigger them.
    """

    summary: dict[str, float] = {
        "primary_legal_basis": PRIMARY_LEGAL_BASIS_BOOST,
        "interpretation_auxiliary": INTERPRETATION_AUXILIARY_BOOST,
    }
    if facts.cross_border_transfer:
        summary["primary_legal_basis:cross_border"] = CROSS_BORDER_PRIMARY_LEGAL_BASIS_BOOST

    if facts.region:
        region_code = _normalize_region(facts.region)
        summary[f"conditional_local_basis:{region_code}"] = CONDITIONAL_LOCAL_BASIS_BOOST
        if _is_specific_region(region_code):
            summary["conditional_local_basis:mismatch"] = CONDITIONAL_LOCAL_MISMATCH_WEIGHT

    if facts.industry:
        summary[f"conditional_industry_basis:{facts.industry}"] = CONDITIONAL_INDUSTRY_BASIS_BOOST
        if _is_specific_industry(facts.industry):
            summary["conditional_industry_basis:mismatch"] = CONDITIONAL_INDUSTRY_MISMATCH_WEIGHT

    if "missing_information" in query_types:
        summary["implementation_reference:missing_information"] = IMPLEMENTATION_REFERENCE_BOOST
        summary["query_type:missing_information"] = MISSING_INFORMATION_QUERY_WEIGHT

    return summary


def apply_boosts_to_hits(
    hits: list[RetrievalHit],
    chunks_by_id: dict[str, Chunk],
    facts: ReviewFacts,
    query_type: RetrievalQueryType | None = None,
) -> list[RetrievalHit]:
    """Apply metadata boosts to a list of hits, returning updated copies.

    The ``score`` field is multiplied by the boost factor. Original scores
    are not preserved separately — the trace's ``metadata_boosts`` dict
    records the active rules.
    """

    boosted: list[RetrievalHit] = []
    for hit in hits:
        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk is None:
            boosted.append(hit)
            continue
        factor = compute_boost_for_hit(hit, chunk, facts, query_type)
        boosted.append(hit.model_copy(update={"score": round(hit.score * factor, 6)}))
    return boosted


def _normalize_region(region: str) -> str:
    value = region.strip()
    if not value:
        return value
    mapped = _REGION_CODE_MAP.get(value)
    if mapped is not None:
        return mapped
    for name, code in _REGION_CODE_MAP.items():
        if name in value:
            return code
    return value


def _is_specific_region(region: str) -> bool:
    value = region.strip()
    if not value:
        return False
    if value.lower() in _NON_LOCAL_REGION_VALUES:
        return False
    return value != "CN"


def _region_matches(chunk_region: str, fact_region: str) -> bool:
    chunk_value = _normalize_region(chunk_region)
    fact_value = _normalize_region(fact_region)
    if not _is_specific_region(fact_value):
        return False
    if chunk_value == fact_value:
        return True
    # LLMs often emit free-trade-zone scoped codes such as CN-CQ-FTZ,
    # while corpus metadata stores the province/municipality code.
    return fact_value.startswith(f"{chunk_value}-") or chunk_value.startswith(f"{fact_value}-")


def _industry_matches(chunk: Chunk, industry: str) -> bool:
    keywords = _INDUSTRY_KEYWORD_MAP.get(industry, [industry])
    combined = " ".join([*chunk.applicable_subjects, *chunk.topic_tags])
    return any(keyword and keyword in combined for keyword in keywords)


def _is_specific_industry(industry: str) -> bool:
    return industry.strip().lower() not in {"", "null", "none", "unknown", "无"}


def _chunk_mentions_any(chunk: Chunk, terms: tuple[str, ...]) -> bool:
    combined = " ".join(
        [
            chunk.title,
            chunk.text[:500],
            *chunk.legal_domain,
            *chunk.applicable_subjects,
            *chunk.topic_tags,
        ]
    )
    return any(term in combined for term in terms)
