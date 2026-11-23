"""Region and industry confirmed at intake must reach the facts that drive retrieval boosts."""

from law_agent.review.http.cases import _intake_from_extraction
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.retrieval.boosts import normalize_industry
from law_agent.review.schemas import ReviewFacts


def test_extraction_prefills_region_and_maps_industry_onto_fixed_choices() -> None:
    intake = _intake_from_extraction(ReviewFacts(regions=["上海"], industry="智能网联汽车制造"))
    assert intake.regions == ["上海"] and intake.industry == "汽车"
    assert normalize_industry("在线旅游") == "其他" and normalize_industry(" ") is None


def test_confirmed_intake_feeds_review_facts_the_way_agent_runtime_builds_them() -> None:
    confirmed = IntakePayload(regions=["深圳"], in_free_trade_zone=True, industry="金融").model_dump(mode="json")
    facts = ReviewFacts.model_validate({k: v for k, v in confirmed.items() if k in ReviewFacts.model_fields})
    assert facts.regions == ["深圳"] and facts.industry == "金融"


def test_ftz_only_list_is_a_mismatch_for_a_processor_outside_the_zone() -> None:
    from law_agent.review.retrieval.boosts import (
        CONDITIONAL_LOCAL_BASIS_BOOST, CONDITIONAL_LOCAL_MISMATCH_WEIGHT, compute_boost_for_hit,
    )
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    chunks = load_corpus(DEFAULT_CHUNKS_PATH)
    zhejiang = next(c for c in chunks if c.source_id.startswith("zhejiang_free_trade_zone"))
    shanghai = next(c for c in chunks if c.source_id == "shanghai_free_trade_zone_data_export_negative_list_2025")

    def boost(chunk, **facts):
        return compute_boost_for_hit(hit_from_chunk(chunk, 1), chunk, ReviewFacts(**facts))

    assert boost(zhejiang, regions=["浙江"], in_free_trade_zone=True) == CONDITIONAL_LOCAL_BASIS_BOOST
    assert boost(zhejiang, regions=["浙江"], in_free_trade_zone=False) == CONDITIONAL_LOCAL_MISMATCH_WEIGHT
    # The Shanghai list covers the whole city, so zone status does not matter.
    assert boost(shanghai, regions=["上海"], in_free_trade_zone=False) == CONDITIONAL_LOCAL_BASIS_BOOST
