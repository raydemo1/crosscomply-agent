from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentModel
from law_agent.review.agent_tools import (
    ComplianceAgentTools,
    _spread_sources,
)
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.retrieval.scopes import (
    GBA_HK_LABEL,
    GBA_MO_LABEL,
    SCOPE_ALL,
    SCOPE_NATIONAL,
    available_scopes,
    chunk_in_scope,
    validate_scope,
)
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from law_agent.review.tool_calling import InvalidToolCall, ToolSession


def _make_chunk(
    chunk_id: str,
    *,
    source_id: str = "s1",
    article_no: str | None = "第一条",
    text: str = "测试条文原文",
    citation_role: str = "primary_legal_basis",
    can_cite_clause: bool = True,
    doc_type: str = "law",
    applicable_region: str = "CN",
    applicable_subjects: list[str] | None = None,
    topic_tags: list[str] | None = None,
    index: int = 0,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=f"{source_id}:v1",
        source_id=source_id,
        title=f"来源{source_id}",
        text=text,
        chunk_index=index,
        doc_type=doc_type,
        article_no=article_no,
        citation_role=citation_role,
        can_cite_clause=can_cite_clause,
        source_url="https://example.gov.cn/doc",
        applicable_region=applicable_region,
        applicable_subjects=applicable_subjects or [],
        topic_tags=topic_tags or [],
        char_count=len(text),
    )


def _mock_tools(chunks: list[Chunk], *, national_non_clause_slots: int | None = 4) -> ComplianceAgentTools:
    tools = object.__new__(ComplianceAgentTools)
    tools._chunks = list(chunks)
    tools._chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    tools._default_as_of_date = date(2026, 10, 15)
    tools._sources_with_articles = {chunk.source_id for chunk in chunks if chunk.article_no}
    tools._later_provisions = {}
    tools._top_k = 10
    tools._national_non_clause_slots = national_non_clause_slots
    tools._question = ""
    tools._material_text = ""
    tools._material_versions_by_id = {}
    tools._rerank_config = SimpleNamespace(window=50)
    tools._rerank_mode = "off"
    tools._candidate_hits = {}
    tools._search_results = []
    tools._last_query_candidates = []
    tools._web_research = None
    tools._semantic_verifier = None
    return tools


def test_available_scopes_splits_gba_hk_mo_and_only_includes_non_empty_entries():
    chunks = [
        _make_chunk("nat", source_id="nat_law"),
        _make_chunk("sh", source_id="sh_list", citation_role="conditional_local_basis",
                    can_cite_clause=False, article_no=None, applicable_region="CN-SH"),
        _make_chunk("gba_hk", source_id="gba_hk", citation_role="conditional_local_basis",
                    can_cite_clause=False, article_no=None, applicable_region="CN-GBA-HK"),
        _make_chunk("gba_mo", source_id="gba_mo", citation_role="conditional_local_basis",
                    can_cite_clause=False, article_no=None, applicable_region="CN-GBA-MO"),
        _make_chunk("auto", source_id="auto_guide", citation_role="conditional_industry_basis",
                    can_cite_clause=False, article_no=None, applicable_subjects=["汽车数据处理者"]),
    ]
    assert available_scopes(chunks) == [
        SCOPE_NATIONAL,
        "地区:上海",
        f"地区:{GBA_HK_LABEL}",
        f"地区:{GBA_MO_LABEL}",
        "行业:汽车",
        SCOPE_ALL,
    ]


def test_chunk_in_scope_overlays_local_and_industry_on_national_while_isolating_others():
    nat_law = _make_chunk("nat", source_id="nat_law")
    nat_faq = _make_chunk("faq", source_id="nat_faq", citation_role="interpretation_auxiliary",
                          can_cite_clause=False, article_no=None)
    sh_list = _make_chunk("sh", source_id="sh_list", citation_role="conditional_local_basis",
                          can_cite_clause=False, article_no=None, applicable_region="CN-SH")
    sh_qna = _make_chunk("sh_qna", source_id="sh_qna", citation_role="interpretation_auxiliary",
                         can_cite_clause=False, article_no=None, applicable_region="CN-SH")
    cq_auto = _make_chunk("cq_auto", source_id="cq_list", citation_role="conditional_local_basis",
                          can_cite_clause=False, article_no=None, applicable_region="CN-CQ",
                          applicable_subjects=["重庆自贸试验区企业", "汽车行业企业"])
    gba_hk = _make_chunk("gba_hk", source_id="gba_hk", citation_role="conditional_local_basis",
                         can_cite_clause=False, article_no=None, applicable_region="CN-GBA-HK")
    gba_mo = _make_chunk("gba_mo", source_id="gba_mo", citation_role="conditional_local_basis",
                         can_cite_clause=False, article_no=None, applicable_region="CN-GBA-MO")
    sz_reg = _make_chunk("sz", source_id="sz_reg", citation_role="conditional_local_basis",
                         applicable_region="CN-GD-SZ")
    auto_guide = _make_chunk("auto", source_id="auto_guide", citation_role="conditional_industry_basis",
                             can_cite_clause=False, article_no=None, applicable_subjects=["智能网联汽车企业"])
    fin_guide = _make_chunk("fin", source_id="fin_guide", citation_role="conditional_industry_basis",
                            can_cite_clause=False, article_no=None, applicable_subjects=["金融数据处理者"])

    # Default national scope excludes all local and industry documents.
    assert chunk_in_scope(nat_law, SCOPE_NATIONAL)
    assert chunk_in_scope(nat_faq, SCOPE_NATIONAL)
    assert not chunk_in_scope(sh_list, SCOPE_NATIONAL)
    assert not chunk_in_scope(sh_qna, SCOPE_NATIONAL)
    assert not chunk_in_scope(gba_hk, SCOPE_NATIONAL)
    assert not chunk_in_scope(auto_guide, SCOPE_NATIONAL)

    # Local scope keeps national superior law + target region, excluding other regions & industries.
    assert chunk_in_scope(nat_law, "地区:上海")
    assert chunk_in_scope(nat_faq, "地区:上海")
    assert chunk_in_scope(sh_list, "地区:上海")
    assert chunk_in_scope(sh_qna, "地区:上海")
    assert not chunk_in_scope(cq_auto, "地区:上海")
    assert not chunk_in_scope(auto_guide, "地区:上海")

    # GBA HK and Macao are separated; both keep national law/Q&As.
    assert chunk_in_scope(nat_faq, f"地区:{GBA_HK_LABEL}")
    assert chunk_in_scope(gba_hk, f"地区:{GBA_HK_LABEL}")
    assert not chunk_in_scope(gba_mo, f"地区:{GBA_HK_LABEL}")
    assert chunk_in_scope(gba_mo, f"地区:{GBA_MO_LABEL}")
    assert not chunk_in_scope(gba_hk, f"地区:{GBA_MO_LABEL}")

    assert chunk_in_scope(sz_reg, "地区:深圳")
    assert chunk_in_scope(sz_reg, "地区:广东")
    # Guangdong and Shenzhen overlays include GBA mainland-HK/Macao rules so provincial/city queries don't miss them.
    assert chunk_in_scope(gba_hk, "地区:广东")
    assert chunk_in_scope(gba_mo, "地区:广东")
    assert chunk_in_scope(gba_hk, "地区:深圳")
    assert chunk_in_scope(gba_mo, "地区:深圳")
    assert not chunk_in_scope(gba_hk, "地区:上海")

    # Industry scope keeps national superior law + target industry, excluding local FTZ lists & other industries.
    assert chunk_in_scope(nat_law, "行业:汽车")
    assert chunk_in_scope(auto_guide, "行业:汽车")
    assert not chunk_in_scope(cq_auto, "行业:汽车")
    assert not chunk_in_scope(fin_guide, "行业:汽车")

    for chunk in (nat_law, nat_faq, sh_list, cq_auto, gba_hk, gba_mo, sz_reg, auto_guide, fin_guide):
        assert chunk_in_scope(chunk, SCOPE_ALL)

    with pytest.raises(ValueError, match="无效的检索域"):
        validate_scope("未知域")


def test_spread_sources_caps_contract_templates_and_national_non_citable_with_backfill():
    # Contract template chunks carry article_no ("第八条", "第三条") but are NOT citable legal clauses.
    contracts = [
        _make_chunk(f"tpl{i}", source_id="gba_contract_tpl", article_no="第八条" if i < 3 else "第三条",
                    doc_type="contract", citation_role="implementation_reference",
                    can_cite_clause=False, index=i)
        for i in range(5)
    ]
    guide = _make_chunk("guide0", source_id="gba_guide", article_no="第二条",
                        doc_type="policy", citation_role="conditional_local_basis",
                        can_cite_clause=False, index=0)
    ranked_gba = [hit_from_chunk(c, i) for i, c in enumerate([*contracts, guide])]
    spread_gba = [h.chunk_id for h in _spread_sources(ranked_gba)]
    # Only 2 chunks of the contract template stay ahead of the guide.
    assert spread_gba[:3] == ["tpl0", "tpl1", "guide0"]

    faqs = [
        _make_chunk(f"faq{i}", source_id=f"faq_src_{i}", article_no=None,
                    citation_role="interpretation_auxiliary", can_cite_clause=False, index=i)
        for i in range(6)
    ]
    laws = [
        _make_chunk(f"law{i}", source_id="law_src", article_no=f"第{i + 1}条", index=i)
        for i in range(5)
    ]
    ranked = [hit_from_chunk(c, i) for i, c in enumerate([*faqs, *laws])]

    capped_hits = _spread_sources(ranked, max_non_clause=3)
    assert [h.chunk_id for h in capped_hits][:8] == ["faq0", "faq1", "faq2", "law0", "law1", "law2", "law3", "law4"]
    assert [h.chunk_id for h in capped_hits][8:] == ["faq3", "faq4", "faq5"]
    assert [h.rank for h in capped_hits] == list(range(len(capped_hits)))

    scarce = [hit_from_chunk(c, i) for i, c in enumerate([*faqs, *laws[:2]])]
    backfilled = [h.chunk_id for h in _spread_sources(scarce, max_non_clause=3)]
    assert backfilled == ["faq0", "faq1", "faq2", "law0", "law1", "faq3", "faq4", "faq5"]


def test_search_passes_per_query_overlay_allowed_ids_and_applies_national_cap():
    nat_faqs = [
        _make_chunk(f"faq{i}", source_id=f"faq_{i}", article_no=None,
                    citation_role="interpretation_auxiliary", can_cite_clause=False)
        for i in range(5)
    ]
    nat_laws = [
        _make_chunk(f"law{i}", source_id="flow_rules", article_no=f"第{i + 5}条", index=i)
        for i in range(4)
    ]
    nat_policy = _make_chunk("pol0", source_id="cert_notice", article_no=None,
                             doc_type="policy", citation_role="implementation_reference",
                             can_cite_clause=False)
    sh_list = _make_chunk("sh0", source_id="sh_list", article_no=None,
                          citation_role="conditional_local_basis", can_cite_clause=False,
                          applicable_region="CN-SH")
    bj_list = _make_chunk("bj0", source_id="bj_list", article_no=None,
                          citation_role="conditional_local_basis", can_cite_clause=False,
                          applicable_region="CN-BJ")
    chunks = [*nat_faqs, *nat_laws, nat_policy, sh_list, bj_list]
    tools = _mock_tools(chunks, national_non_clause_slots=3)

    seen_allowed: list[list[str]] = []

    def fake_search_many(query_pairs, *, top_k=10, allowed_chunk_ids=None):
        seen_allowed.append([list(ids) for ids in allowed_chunk_ids])
        out = []
        for ids in allowed_chunk_ids:
            allowed_set = set(ids)
            out.append([
                hit_from_chunk(c, rank).model_copy(update={"score": 100 - rank})
                for rank, c in enumerate(chunks)
                if c.chunk_id in allowed_set
            ])
        return out

    tools._adapters = SimpleNamespace(
        keyword=SimpleNamespace(search_many=fake_search_many),
        vector=SimpleNamespace(search_many=fake_search_many),
    )

    tools.search(
        [
            RetrievalQuery(query_id="q1", query_type="legal_issue", text="门槛", scope=SCOPE_NATIONAL),
            RetrievalQuery(query_id="q2", query_type="region_condition", text="上海负面清单", scope="地区:上海"),
        ],
        ReviewFacts(),
    )

    # National query excludes both local lists; Shanghai overlay keeps national + sh0, excluding bj0.
    assert "sh0" not in seen_allowed[0][0] and "bj0" not in seen_allowed[0][0]
    assert "sh0" in seen_allowed[0][1] and "law0" in seen_allowed[0][1] and "bj0" not in seen_allowed[0][1]

    nat_ranked = [h.chunk_id for h in tools.last_query_candidates()[0]]
    assert nat_ranked[:7] == ["faq0", "faq1", "faq2", "law0", "law1", "law2", "law3"]

    # First search announces scope_catalog once per newly-seen scope (citable national + national policy + specialized local).
    catalog = tools.scope_catalog()
    assert {e["source_id"] for e in catalog[SCOPE_NATIONAL]} == {"flow_rules", "cert_notice"}
    assert [e["source_id"] for e in catalog["地区:上海"]] == ["sh_list"]
    # Second search in the same scopes does not repeat the catalog.
    tools.search(
        [RetrievalQuery(query_id="q3", query_type="legal_issue", text="门槛", scope=SCOPE_NATIONAL)],
        ReviewFacts(),
    )
    assert tools.scope_catalog() == {}


def test_tool_calling_agent_dynamically_injects_scope_enum_defaults_and_validates():
    scopes = [SCOPE_NATIONAL, "地区:上海", "行业:汽车", SCOPE_ALL]

    class FakeClient:
        def __init__(self, responses):
            self._responses = iter(responses)
            self.seen_tools = []

        def chat_tools(self, messages, *, tools, model=None, on_text=None):
            self.seen_tools.append(tools)
            return next(self._responses)

    def tool_call(call_id: str, args: dict) -> dict:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "search_evidence", "arguments": json.dumps(args, ensure_ascii=False)},
            }],
        }

    client = FakeClient([
        # 1. Omitting scope defaults to SCOPE_NATIONAL.
        tool_call("c1", {
            "summary": "查全国规定",
            "queries": [{"query_id": "q1", "query_type": "legal_issue", "text": "门槛"}],
        }),
        # 2. Invalid scope raises InvalidToolCall.
        tool_call("c2", {
            "summary": "查未收录省份",
            "queries": [{"query_id": "q2", "query_type": "region_condition", "text": "湖北", "scope": "地区:湖北"}],
        }),
    ])
    model = AgentModel(model_id="test", client=client, query_scopes=scopes)
    search_tool = next(t for t in model.protocol.tools if t["function"]["name"] == "search_evidence")
    scope_prop = search_tool["function"]["parameters"]["$defs"]["RetrievalQuery"]["properties"]["scope"]
    assert scope_prop["enum"] == scopes
    assert scope_prop["default"] == SCOPE_NATIONAL

    # search_web does NOT receive corpus scope enum.
    web_tool = next(t for t in model.protocol.tools if t["function"]["name"] == "search_web")
    web_scope_prop = web_tool["function"]["parameters"]["$defs"]["RetrievalQuery"]["properties"]["scope"]
    assert "enum" not in web_scope_prop

    session = ToolSession()
    ctx = {"state": {"evidence": [], "steps": []}}
    decision = model.protocol.decide(session, prompt="p", context=ctx)
    assert decision.queries[0].scope == SCOPE_NATIONAL

    session.complete({"ok": True}, ctx)
    with pytest.raises(InvalidToolCall, match="不在当前知识库可选检索域内"):
        model.protocol.decide(session, prompt="p", context=ctx)


def test_rerank_document_includes_chinese_doc_type_headings_and_scope_metadata():
    from law_agent.review.retrieval.rerank import _build_rerank_document

    chunk = _make_chunk(
        "sh_item",
        source_id="sh_list",
        article_no=None,
        doc_type="policy",
        citation_role="conditional_local_basis",
        can_cite_clause=False,
        applicable_region="CN-SH",
        applicable_subjects=["上海自贸试验区企业"],
        text="会员管理数据超过100万人需标准合同。",
    ).model_copy(update={"heading_path": ["来源sh_list", "商贸领域", "（二）标准合同清单"]})
    doc = _build_rerank_document(hit_from_chunk(chunk, 0), chunk)
    assert "类型：政策文件；证据角色：地方专项依据" in doc
    assert "章节：商贸领域 / （二）标准合同清单" in doc
    assert "适用范围：CN-SH；上海自贸试验区企业" in doc
    assert "正文：会员管理数据超过100万人需标准合同。" in doc


def test_explicit_query_scope_does_not_penalize_target_ftz_list_when_outside_zone():
    from law_agent.review.retrieval.boosts import (
        CONDITIONAL_LOCAL_MISMATCH_WEIGHT,
        compute_boost_for_hit,
    )

    zj = _make_chunk(
        "zj0",
        source_id="zj_list",
        article_no=None,
        citation_role="conditional_local_basis",
        can_cite_clause=False,
        applicable_region="CN-ZJ",
        applicable_subjects=["浙江自贸试验区企业"],
    )
    hit = hit_from_chunk(zj, 0)
    facts = ReviewFacts(regions=["浙江"], in_free_trade_zone=False)
    # Without explicit scope, legacy fact-level penalty applies.
    assert compute_boost_for_hit(hit, zj, facts) == CONDITIONAL_LOCAL_MISMATCH_WEIGHT
    # With explicit query scope targeting Zhejiang, no conflicting mismatch penalty applies.
    assert compute_boost_for_hit(hit, zj, facts, query_scope="地区:浙江") == 1.0

