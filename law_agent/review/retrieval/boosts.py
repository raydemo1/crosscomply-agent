"""Retrieval weights and conservative checks against explicit source scopes.

Citation authority remains metadata for legal judgment and formal citation
gates; it does not replace query relevance in retrieval ranking.
"""

from __future__ import annotations

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

CONDITIONAL_LOCAL_BASIS_BOOST = 1.5
CONDITIONAL_LOCAL_MISMATCH_WEIGHT = 0.45
CONDITIONAL_INDUSTRY_BASIS_BOOST = 1.4
MISSING_INFORMATION_QUERY_WEIGHT = 0.7


def compute_boost_for_hit(
    hit: RetrievalHit,
    chunk: Chunk,
    facts: ReviewFacts,
    query_type: RetrievalQueryType | None = None,
) -> float:
    """Compute a multiplicative boost factor for a single hit.

    Returns 1.0 when no applicability or query-purpose weight applies.
    """

    boost = 1.0
    role = hit.citation_role

    # Conditional local basis: boost when any explicit region matches
    if role == "conditional_local_basis":
        fact_regions = _specific_regions(facts.regions)
        chunk_region = _normalize_region(chunk.applicable_region)
        if fact_regions and _is_specific_region(chunk_region):
            if any(_region_matches(chunk_region, region) for region in fact_regions):
                boost *= CONDITIONAL_LOCAL_BASIS_BOOST
            else:
                boost *= CONDITIONAL_LOCAL_MISMATCH_WEIGHT

    # Conditional industry basis: boost when industry matches
    if (
        role == "conditional_industry_basis"
        and facts.industry
        and _industry_matches(chunk, facts.industry)
    ):
        boost *= CONDITIONAL_INDUSTRY_BASIS_BOOST

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

    summary: dict[str, float] = {}

    fact_regions = _specific_regions(facts.regions)
    if fact_regions:
        for region in fact_regions:
            summary[f"conditional_local_basis:{region}"] = CONDITIONAL_LOCAL_BASIS_BOOST
        summary["conditional_local_basis:mismatch"] = CONDITIONAL_LOCAL_MISMATCH_WEIGHT

    if facts.industry:
        summary[f"conditional_industry_basis:{facts.industry}"] = CONDITIONAL_INDUSTRY_BASIS_BOOST

    if "missing_information" in query_types:
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


def _specific_regions(regions: list[str]) -> list[str]:
    """Normalized codes of the fact regions that name a local jurisdiction."""

    codes: list[str] = []
    for value in regions:
        code = _normalize_region(value)
        if _is_specific_region(code) and code not in codes:
            codes.append(code)
    return codes


def _industry_matches(chunk: Chunk, industry: str) -> bool:
    keywords = _INDUSTRY_KEYWORD_MAP.get(industry, [industry])
    combined = " ".join([*chunk.applicable_subjects, *chunk.topic_tags])
    return any(keyword and keyword in combined for keyword in keywords)


def outside_explicit_scope(chunk: Chunk, facts: ReviewFacts, query_text: str,
                           excluded_scope_terms=(), *, requested_text: str | None = None) -> bool:
    requested_text = query_text if requested_text is None else requested_text
    if chunk.title and chunk.title in requested_text:
        return False
    scoped_source = chunk.citation_role in {"conditional_local_basis", "conditional_industry_basis"} or (
        chunk.citation_role in {"interpretation_auxiliary", "implementation_reference"}
        and (_is_specific_region(chunk.applicable_region) or chunk.applicable_subjects)
    )
    if scoped_source and any(term in " ".join([chunk.title, *chunk.applicable_subjects]) for term in excluded_scope_terms):
        return True
    if chunk.citation_role == "conditional_local_basis":
        def known_region(region):
            return region.startswith("CN-") and any(
                _region_matches(region, code) for code in _REGION_CODE_MAP.values()
            )

        regions = [region for region in _specific_regions(facts.regions) if known_region(region)]
        source_region = _normalize_region(chunk.applicable_region)
        mentioned = [code for name, code in _REGION_CODE_MAP.items() if name in query_text or code in query_text]
        if any(_region_matches(source_region, region) for region in mentioned):
            return False
        return bool(regions and known_region(source_region)
                    and not any(_region_matches(source_region, region) for region in regions))
    if chunk.citation_role == "conditional_industry_basis" and facts.industry in _INDUSTRY_KEYWORD_MAP:
        if _industry_matches(chunk, facts.industry):
            return False
        subjects = " ".join(chunk.applicable_subjects)
        source_industries = [industry for industry, keywords in _INDUSTRY_KEYWORD_MAP.items()
                             if any(keyword in subjects for keyword in keywords)]
        if any(keyword in query_text for industry in source_industries for keyword in _INDUSTRY_KEYWORD_MAP[industry]):
            return False
        return bool(source_industries)
    return False
