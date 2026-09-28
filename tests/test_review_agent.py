"""Behavioral tests for the dynamic single-Agent loop."""

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import (
    AgentDecision,
    AgentModel,
    AgentState,
    EvidenceRead,
    answer_agent,
    run_agent,
)
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import RetrievalHit, RetrievalQuery, ReviewFacts
from tests.test_review_llm import FakeClient


def _draft() -> LLMReviewResultDraft:
    return LLMReviewResultDraft(
        risk_level="insufficient_evidence",
        decision_summary="当前材料和法源证据仍不足以形成正式风险结论，需要明确标示调查边界、尚未核实事项以及后续补充要求。",
        conclusion="现有证据不足，暂不形成正式合规路径结论。",
        trigger_reasons=["证据不足"],
        missing_information=["境外接收方所在地区"],
        recommended_actions=["补充接收方信息"],
        risk_boundaries=["未覆盖地方和行业特别规则"],
    )


def test_agent_can_finish_without_fixed_intermediate_steps() -> None:
    decisions = iter([
        AgentDecision(action="finish", summary="交付当前调查结果", draft=_draft())
    ])
    checkpoints: list[AgentState] = []

    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"review_result": {"risk_level": "insufficient_evidence"}},
        checkpoint=lambda current: checkpoints.append(current.model_copy(deep=True)),
    )

    assert state.status == "completed"
    assert [step.action for step in state.steps] == ["finish"]
    assert checkpoints[-1].status == "completed"


def test_agent_exposes_frozen_material_version_ids_to_decider() -> None:
    seen: list[dict[str, object]] = []

    def decide(_state: AgentState, intake: dict[str, object]) -> AgentDecision:
        seen.append(intake)
        return AgentDecision(action="finish", summary="交付", draft=_draft())

    run_agent(
        AgentState(goal="审查"),
        material="【材料 业务说明 v1 | mv_case_001】\n字段清单：姓名。",
        intake={"id": "snap_case_001", "facts": {}},
        decide=decide,
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert seen[0]["_material_version_ids"] == ["mv_case_001"]


def test_agent_can_choose_multiple_retrieval_batches() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence",
            summary="先查数据出境一般规则",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="数据出境规则")],
        ),
        AgentDecision(
            action="search_evidence",
            summary="再查境外 SaaS 场景",
            queries=[RetrievalQuery(query_id="q2", query_type="material_fact", text="境外 SaaS 接收方")],
        ),
        AgentDecision(action="finish", summary="交付调查结果", draft=_draft()),
    ])
    searched: list[str] = []
    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda queries, _facts: searched.extend(item.text for item in queries) or [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert searched == ["数据出境规则", "境外 SaaS 接收方"]
    assert [step.action for step in state.steps] == [
        "search_evidence", "search_evidence", "finish"
    ]


def test_agent_pauses_and_resumes_from_human_input() -> None:
    waiting = run_agent(
        AgentState(goal="确认接收方地区"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: AgentDecision(
            action="request_input",
            summary="需要确认接收方地区",
            question="境外接收方位于哪个国家或地区？",
        ),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {},
        checkpoint=lambda _state: None,
    )
    assert waiting.status == "waiting_input"
    resumed = answer_agent(
        waiting,
        gate_id=waiting.gate_id or "",
        answer="接收方位于新加坡。",
    )

    assert resumed.status == "running"
    assert resumed.steps[-1].action == "human_input"
    assert resumed.steps[-1].observation["answer"] == "接收方位于新加坡。"


def test_agent_plan_is_visible_without_blocking_the_run() -> None:
    decisions = iter([
        AgentDecision(
            action="propose_plan",
            summary="提交调查计划",
            plan=["核对材料", "检索法源", "形成有引用的结论"],
        ),
        AgentDecision(action="finish", summary="交付当前调查结果", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.status == "completed"
    assert state.plan == ["核对材料", "检索法源", "形成有引用的结论"]
    assert [step.action for step in state.steps] == ["propose_plan", "finish"]


def test_agent_model_retries_invalid_decision_json_instead_of_failing_task() -> None:
    client = FakeClient(
        outputs=[
            {"type": "json_object", "plan": ["读取冻结材料"]},
            {
                "action": "propose_plan",
                "summary": "给出执行计划",
                "plan": ["读取冻结材料", "检索数据出境界定规则"],
            },
        ]
    )
    model = AgentModel(model_id="test-model", client=client)  # type: ignore[arg-type]

    assert model.node.max_retries >= 1

    decision = model(AgentState(goal="审查境外 SaaS 接入"), {})

    assert decision.action == "propose_plan"
    assert len(client.calls) == 2


def test_agent_model_sends_decision_schema_in_system_prompt() -> None:
    client = FakeClient(
        outputs=[
            {
                "action": "propose_plan",
                "summary": "给出执行计划",
                "plan": ["读取冻结材料", "检索数据出境界定规则"],
            }
        ]
    )
    model = AgentModel(model_id="test-model", client=client)  # type: ignore[arg-type]

    model(AgentState(goal="审查境外 SaaS 接入"), {})

    system = client.calls[0][0]
    assert system.role == "system"
    assert '"action"' in system.content
    assert '"summary"' in system.content


def _hit(chunk_id: str, source_id: str, *, text: str = "条款原文", rank: int = 0) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id=source_id,
        source_id=source_id,
        title="个人信息出境标准合同办法",
        text=text,
        score=0.02,
        rank=rank,
        retriever="hybrid",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        source_url="https://www.cac.gov.cn/rule",
        article_no="第三条",
    )


def test_agent_reads_more_of_a_source_it_has_already_found() -> None:
    """The first search returns a few chunks per source; the Agent must be able
    to continue inside a found source to check every condition of a clause."""

    found = _hit("c1", "standard_contract_measures")
    read = _hit("c2", "standard_contract_measures", text="第三条第二款 免予情形", rank=0)
    decisions = iter([
        AgentDecision(
            action="search_evidence",
            summary="先查标准合同办法",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="标准合同")],
        ),
        AgentDecision(
            action="read_evidence",
            summary="读回第三条全文核对免予情形",
            source_id="standard_contract_measures",
            article_no="第三条",
        ),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    reads: list[tuple[str, str | None, str | None]] = []
    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [found],
        read_evidence=lambda source_id, article_no, chunk_id, _facts, _offset: (
            reads.append((source_id, article_no, chunk_id)) or EvidenceRead(hits=[read])
        ),
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert reads == [("standard_contract_measures", "第三条", None)]
    assert state.reads == 1
    assert [hit.chunk_id for hit in state.evidence] == ["c1", "c2"]
    assert state.steps[1].observation["chunk_ids"] == ["c2"]
    assert state.steps[1].observation["following_chunk_id"] is None
    assert state.status == "completed"


def test_agent_only_reads_inside_a_source_the_search_already_found() -> None:
    decisions = iter([
        AgentDecision(
            action="read_evidence",
            summary="直接读取未检索到的地方清单",
            source_id="not_found_source",
            article_no="第三条",
        ),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        read_evidence=lambda *_args: EvidenceRead(hits=[_hit("c9", "not_found_source")]),
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.evidence == []
    assert state.reads == 0
    assert "只能读取本次已检索到的来源" in state.steps[0].observation["error"]


def test_agent_read_budget_is_enforced() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence",
            summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="标准合同")],
        ),
        AgentDecision(
            action="read_evidence", summary="读第一条",
            source_id="s1", article_no="第一条",
        ),
        AgentDecision(
            action="read_evidence", summary="再读第二条",
            source_id="s1", article_no="第二条",
        ),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查", max_reads=1),
        material="",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [_hit("c1", "s1")],
        read_evidence=lambda *_args: EvidenceRead(hits=[_hit("c2", "s1")]),
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.reads == 1
    assert "预算已用尽" in state.steps[2].observation["error"]


def test_agent_does_not_spend_read_budget_on_the_same_chunk_twice() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence", summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="银行清单")],
        ),
        AgentDecision(
            action="read_evidence", summary="读清单", source_id="s1", chunk_id="c1",
        ),
        AgentDecision(
            action="read_evidence", summary="重复读清单", source_id="s1", chunk_id="c1",
        ),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    calls: list[str] = []
    state = run_agent(
        AgentState(goal="审查", max_reads=1),
        material="",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [_hit("c1", "s1")],
        read_evidence=lambda *_args: (
            calls.append("read") or EvidenceRead(hits=[_hit("c1", "s1")])
        ),
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert calls == ["read"]
    assert state.reads == 1
    assert "已经读过" in state.steps[2].observation["error"]
    assert state.status == "completed"


def test_agent_rejects_a_chunk_already_returned_by_a_previous_window() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence", summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="清单")],
        ),
        AgentDecision(
            action="read_evidence", summary="读第一窗口", source_id="s1", chunk_id="c1",
        ),
        AgentDecision(
            action="read_evidence", summary="重复读窗口内 chunk", source_id="s1", chunk_id="c2",
        ),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    calls: list[str] = []
    state = run_agent(
        AgentState(goal="审查", max_reads=2),
        material="",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [_hit("c1", "s1")],
        read_evidence=lambda *_args: (
            calls.append("read")
            or EvidenceRead(
                hits=[_hit("c1", "s1"), _hit("c2", "s1")],
                following_chunk_id="c3",
            )
        ),
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert calls == ["read"]
    assert state.reads == 1
    assert state.read_history[0].returned_chunk_ids == ["c1", "c2"]
    assert "此前读取窗口" in state.steps[2].observation["error"]
    assert state.steps[2].observation["next_unread_chunk_ids"] == ["c3"]


def test_agent_rejects_chunk_offset_before_spending_read_budget() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence", summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="清单")],
        ),
        AgentDecision(
            action="read_evidence", summary="误用偏移量",
            source_id="s1", chunk_id="c1", offset=1,
        ),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查", max_reads=1),
        material="",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [_hit("c1", "s1")],
        read_evidence=lambda *_args: (_ for _ in ()).throw(AssertionError("不应调用读取工具")),
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.reads == 0
    assert "不使用 offset" in state.steps[1].observation["error"]


def test_read_evidence_returns_the_whole_clause_or_the_paragraphs_around_a_chunk() -> None:
    chunks = [
        _chunk("c1", "第一条", "第一条 为了规范个人信息出境活动。", index=0, prev=None, next_id="c2"),
        _chunk("c2", "第三条", "第三条 个人信息处理者向境外提供个人信息，应当具备下列条件之一。", index=1, prev="c1", next_id="c3"),
        _chunk("c3", "第三条", "（二）未达到安全评估门槛。", index=2, prev="c2", next_id=None),
    ]
    tools = _tools_with(chunks)

    by_article = tools.read_evidence("s1", "第3条", None, ReviewFacts())
    assert all(hit.source_has_articles is True for hit in by_article.hits)
    by_chunk = tools.read_evidence("s1", None, "c1", ReviewFacts())

    assert [hit.chunk_id for hit in by_article.hits] == ["c2", "c3"]
    assert [hit.chunk_id for hit in by_chunk.hits] == ["c1", "c2", "c3"]
    assert by_chunk.previous_chunk_id is None
    assert by_chunk.following_chunk_id is None
    assert all(hit.can_cite_clause for hit in by_article.hits)
    assert by_article.next_offset is None


def test_chunk_read_points_to_the_next_unread_neighbor() -> None:
    chunks = [
        _chunk(
            f"c{index}", "第一条", f"清单第{index}项", index=index - 1,
            prev=f"c{index - 1}" if index > 1 else None,
            next_id=f"c{index + 1}" if index < 10 else None,
        )
        for index in range(1, 11)
    ]
    tools = _tools_with(chunks)

    first = tools.read_evidence("s1", None, "c2", ReviewFacts())
    assert [hit.chunk_id for hit in first.hits] == [f"c{index}" for index in range(1, 6)]
    assert first.following_chunk_id == "c6"
    second = tools.read_evidence("s1", None, first.following_chunk_id, ReviewFacts())
    assert [hit.chunk_id for hit in second.hits] == ["c5", "c6", "c7", "c8", "c9"]


def test_read_evidence_reports_a_clause_longer_than_one_read_as_unfinished() -> None:
    """A nine-chunk clause does not come back as if it were the whole clause.

    Reading it whole was silently cut at five chunks: the model saw a list of
    parallel conditions with the remaining chunks missing, was told this was 该条全文, and
    had no way to ask for the rest.
    """

    chunks = [
        _chunk(
            f"c{index}",
            "第五条",
            f"第五条 免予情形第{index}项。",
            index=index,
            prev=f"c{index - 1}" if index else None,
            next_id=f"c{index + 1}" if index < 8 else None,
        )
        for index in range(9)
    ]
    tools = _tools_with(chunks)

    first = tools.read_evidence("s1", "第五条", None, ReviewFacts())

    assert [hit.chunk_id for hit in first.hits] == [f"c{index}" for index in range(5)]
    assert first.next_offset == 5

    rest = tools.read_evidence("s1", "第五条", None, ReviewFacts(), first.next_offset)

    assert [hit.chunk_id for hit in rest.hits] == ["c5", "c6", "c7", "c8"]
    assert rest.next_offset is None


def test_read_evidence_reports_the_clauses_a_source_actually_carries() -> None:
    tools = _tools_with([_chunk("c1", "第一条", "第一条 正文。", index=0)])

    with pytest.raises(ValueError, match="没有条款 第八条"):
        tools.read_evidence("s1", "第八条", None, ReviewFacts())


def test_read_evidence_refuses_sources_outside_the_controlled_corpus() -> None:
    tools = _tools_with([_chunk("c1", "第一条", "第一条 正文。", index=0)])

    with pytest.raises(ValueError, match="没有来源 s9"):
        tools.read_evidence("s9", "第一条", None, ReviewFacts())
    with pytest.raises(ValueError, match="不属于来源 s1"):
        tools.read_evidence("s1", None, "c9", ReviewFacts())


def test_evidence_returned_to_the_agent_stays_citable_in_the_report() -> None:
    """Report assembly keeps one representative per source plus two supporting
    chunks. Anything the run already handed to the model — a clause read back
    explicitly, or an earlier search result — has to survive that collapse, or a
    claim citing it is rejected as 未检索到 and the run retries until its budget
    is gone."""

    chunks = [
        _chunk("c1", "第一条", "第一条 正文。", index=0, next_id="c2"),
        _chunk("c2", "第三条", "第三条 应当具备下列条件之一。", index=1, prev="c1", next_id="c3"),
        _chunk("c3", "第三条", "（二）未达到门槛。", index=2, prev="c2"),
    ]
    tools = _tools_with(chunks)
    tools._neighbor_hits = {}
    tools._top_k = 10
    tools._material_text = ""
    tools._material_versions_by_id = {}
    tools._semantic_verifier = object()  # the abstention branch never calls it
    # run_agent merges every search and read result into state.evidence.
    read = tools.read_evidence("s1", "第三条", None, ReviewFacts())

    result = tools.finalize(
        _draft(),
        AgentState(goal="审查", evidence=[_hit("c1", "s1"), *read.hits]),
        case_id="case",
        intake_snapshot={"facts": {}},
        system_abstention=True,
    )

    assert {"c1", "c2", "c3"} <= {hit["chunk_id"] for hit in result["evidence_chunks"]}


def _chunk(
    chunk_id: str,
    article_no: str,
    text: str,
    *,
    index: int,
    prev: str | None = None,
    next_id: str | None = None,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc",
        source_id="s1",
        title="个人信息出境标准合同办法",
        text=text,
        chunk_index=index,
        doc_type="law",
        heading_path=["个人信息出境标准合同办法", article_no],
        article_no=article_no,
        authority="administrative_regulation",
        law_status="effective",
        source_url="https://www.cac.gov.cn/rule",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        prev_chunk_id=prev,
        next_chunk_id=next_id,
        char_count=len(text),
    )


def _tools_with(chunks: list[Chunk]) -> ComplianceAgentTools:
    """A tools instance over a fixed corpus, without live search services."""

    tools = ComplianceAgentTools.__new__(ComplianceAgentTools)
    tools._chunks = chunks
    tools._chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    tools._sources_with_articles = {chunk.source_id for chunk in chunks if chunk.article_no}
    tools._candidate_hits = {}
    return tools
