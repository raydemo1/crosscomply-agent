"""Behavioral tests for the dynamic single-Agent loop."""

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import (
    AgentDecision,
    AgentModel,
    AgentState,
    EvidenceRead,
    EvidenceReadRequest,
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


def test_rejected_report_is_available_to_next_decision_and_checkpoint():
    from law_agent.review.semantic_grounding import (
        SemanticGroundingRejected,
        SemanticVerdict,
    )

    draft = _draft().model_copy(update={"recommended_actions": ["需要修正的原始建议"]})
    checkpoints = []
    calls = 0

    def decide(state, _intake):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AgentDecision(action="finish", summary="提交报告", draft=draft)
        assert state.model_dump(mode="json").get("report_draft") == draft.model_dump(mode="json")
        assert state.result is None
        return AgentDecision(action="request_input", summary="确认事实", question="补充事实？")

    def reject(_draft_value, _state):
        raise SemanticGroundingRejected(SemanticVerdict(
            status="uncertain", claim_checks=[], conclusion_reason="建议缺少前提",
        ))

    state = run_agent(
        AgentState(goal="审查案件"), material="材料", intake={}, decide=decide,
        search=lambda *_: [], web_search=lambda *_: [], finalize=reject,
        checkpoint=lambda value: checkpoints.append(value.model_dump(mode="json")),
    )
    restored = AgentState.model_validate(checkpoints[-1])
    assert restored.report_draft == draft
    assert state.fact_ledger == []
    assert state.status == "waiting_input"


def test_report_draft_tracks_latest_submission_and_clears_after_acceptance():
    from law_agent.review.semantic_grounding import (
        SemanticGroundingRejected,
        SemanticVerdict,
    )

    drafts = [_draft().model_copy(update={"recommended_actions": [text]}) for text in ("原建议", "已修正建议")]
    decisions = iter(drafts)
    submissions = []

    def finalize(draft, state):
        assert state.report_draft == draft
        submissions.append(draft)
        if len(submissions) == 1:
            raise SemanticGroundingRejected(SemanticVerdict(
                status="uncertain", claim_checks=[], conclusion_reason="建议缺少前提",
            ))
        return {"accepted": draft.model_dump(mode="json")}

    state = run_agent(
        AgentState(goal="审查案件"), material="材料", intake={},
        decide=lambda *_: AgentDecision(action="finish", summary="提交报告", draft=next(decisions)),
        search=lambda *_: [], web_search=lambda *_: [], finalize=finalize,
        checkpoint=lambda _: None,
    )
    assert state.result["accepted"] == drafts[1].model_dump(mode="json")
    assert state.report_draft is None


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
            read_requests=[EvidenceReadRequest(source_id="standard_contract_measures", article_no="第三条")],
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
    assert state.steps[1].observation["read_results"][0]["chunk_ids"] == ["c2"]
    assert state.steps[1].observation["read_results"][0]["following_chunk_id"] is None
    assert state.status == "completed"


def test_agent_only_reads_inside_a_source_the_search_already_found() -> None:
    decisions = iter([
        AgentDecision(
            action="read_evidence",
            summary="直接读取未检索到的地方清单",
            read_requests=[EvidenceReadRequest(source_id="not_found_source", article_no="第三条")],
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
    assert "只能读取本次已检索到的来源" in state.steps[0].observation["read_results"][0]["error"]


def test_agent_read_budget_is_enforced() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence",
            summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="标准合同")],
        ),
        AgentDecision(
            action="read_evidence", summary="读第一条",
            read_requests=[EvidenceReadRequest(source_id="s1", article_no="第一条")],
        ),
        AgentDecision(
            action="read_evidence", summary="再读第二条",
            read_requests=[EvidenceReadRequest(source_id="s1", article_no="第二条")],
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
    assert "预算已用尽" in state.steps[2].observation["read_results"][0]["error"]


def test_agent_does_not_spend_read_budget_on_the_same_chunk_twice() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence", summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="银行清单")],
        ),
        AgentDecision(
            action="read_evidence", summary="读清单",
            read_requests=[EvidenceReadRequest(source_id="s1", chunk_id="c1")],
        ),
        AgentDecision(
            action="read_evidence", summary="重复读清单",
            read_requests=[EvidenceReadRequest(source_id="s1", chunk_id="c1")],
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
    assert "已经读过" in state.steps[2].observation["read_results"][0]["error"]
    assert state.status == "completed"


def test_agent_rejects_a_chunk_already_returned_by_a_previous_window() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence", summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="清单")],
        ),
        AgentDecision(
            action="read_evidence", summary="读第一窗口",
            read_requests=[EvidenceReadRequest(source_id="s1", chunk_id="c1")],
        ),
        AgentDecision(
            action="read_evidence", summary="重复读窗口内 chunk",
            read_requests=[EvidenceReadRequest(source_id="s1", chunk_id="c2")],
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
    assert "此前读取窗口" in state.steps[2].observation["read_results"][0]["error"]
    assert state.steps[2].observation["read_results"][0]["next_unread_chunk_ids"] == ["c3"]


def test_agent_rejects_chunk_offset_before_spending_read_budget() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence", summary="找到来源",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="清单")],
        ),
        AgentDecision(
            action="read_evidence", summary="误用偏移量",
            read_requests=[EvidenceReadRequest(source_id="s1", chunk_id="c1", offset=1)],
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
    assert "不使用 offset" in state.steps[1].observation["read_results"][0]["error"]


@pytest.mark.parametrize("max_reads", [1, 2])
def test_batch_reads_preserve_successes_and_account_for_each_request(max_reads: int) -> None:
    requests = [
        EvidenceReadRequest(source_id="s1", article_no="第一条"),
        EvidenceReadRequest(source_id="s1", article_no="第一条"),
        EvidenceReadRequest(source_id="unseen", article_no="第一条"),
        EvidenceReadRequest(source_id="s1", article_no="第二条"),
    ]
    decisions = iter([
        AgentDecision(action="read_evidence", summary="核对两个条款", read_requests=requests),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    calls = []

    def read(source_id, article_no, chunk_id, facts, offset):
        calls.append(article_no)
        return EvidenceRead(hits=[_hit(f"read-{len(calls)}", source_id)], next_offset=5)

    state = run_agent(
        AgentState(goal="审查", evidence=[_hit("found", "s1")], max_reads=max_reads),
        material="", intake={}, decide=lambda *_args: next(decisions),
        search=lambda *_args: [], read_evidence=read, web_search=lambda *_args: [],
        finalize=lambda *_args: {"ok": True}, checkpoint=lambda _state: None,
    )
    results = state.steps[0].observation["read_results"]
    assert results[0]["chunk_ids"] == ["read-1"]
    assert results[0]["next_offset"] == 5
    assert "已经读过" in results[1]["error"]
    assert "error" in results[2]
    if max_reads == 2:
        assert "只能读取本次已检索到的来源" in results[2]["error"]
        assert results[3]["chunk_ids"] == ["read-2"]
    else:
        assert "预算已用尽" in results[3]["error"]
    assert calls == ["第一条", "第二条"][:max_reads]
    assert state.reads == len(state.read_history) == max_reads
    assert {hit.chunk_id for hit in state.evidence} == {"found", *[f"read-{i + 1}" for i in range(max_reads)]}
    assert state.status == "completed"


def test_source_outline_is_paginated_temporal_and_not_citable() -> None:
    from datetime import date

    chunks = [_chunk(f"c{i}", f"第{i}条", f"第{i}条 " + "正文" * 100, index=i)
              for i in range(31)]
    chunks.append(_chunk("same-article", "第0条", "同条第二片段", index=32))
    chunks.append(_chunk("future", "第三十二条", "尚未生效内容", index=33).model_copy(update={
        "publish_date": "2027-01-01", "effective_date": "2027-01-01",
    }))
    tools = _tools_with(chunks)
    facts = ReviewFacts(as_of_date=date(2026, 10, 8))
    first = tools.read_evidence("s1", None, None, facts)
    rest = tools.read_evidence("s1", None, None, facts, first.next_offset)
    assert first.hits == rest.hits == []
    assert len(first.outline) == 30 and first.next_offset == 30
    assert [entry.chunk_id for entry in rest.outline] == ["c30"]
    assert rest.next_offset is None
    assert all(len(entry.preview) == 180 for entry in first.outline)
    assert "future" not in {entry.chunk_id for entry in first.outline + rest.outline}
    assert tools.read_evidence("s1", rest.outline[0].article_no, None, facts).hits[0].chunk_id == "c30"


def test_browsing_a_source_does_not_add_preview_to_claim_evidence() -> None:
    chunks = [_chunk("new", "第二条", "第二条 原文", index=0)]
    tools = _tools_with(chunks)
    decisions = iter([
        AgentDecision(action="read_evidence", summary="查看来源目录", read_requests=[
            EvidenceReadRequest(source_id="s1"),
        ]),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查", evidence=[_hit("found", "s1")]),
        material="", intake={}, decide=lambda *_args: next(decisions),
        search=lambda *_args: [], read_evidence=tools.read_evidence, web_search=lambda *_args: [],
        finalize=lambda *_args: {"ok": True}, checkpoint=lambda _state: None,
    )
    observation = state.steps[0].observation["read_results"][0]
    assert observation["outline"] == [{"article_no": "第二条", "chunk_id": "new", "preview": "第二条 原文"}]
    assert observation["chunk_ids"] == []
    assert [hit.chunk_id for hit in state.evidence] == ["found"]
    assert state.read_history[0].returned_chunk_ids == []
    assert state.reads == 1


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


def test_search_excludes_future_references_in_candidates_and_neighbors() -> None:
    from types import SimpleNamespace

    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.schemas import RetrievalQuery

    current = _chunk("c1", "第一条", "第一条 当前规则。", index=0, next_id="c2").model_copy(
        update={"publish_date": "2024-03-22"},
    )
    future = _chunk("c2", "", "未来参考说明。", index=1, prev="c1").model_copy(
        update={"publish_date": "2025-06-27", "can_cite_clause": False},
    )
    tools = _tools_with([current, future])
    search = SimpleNamespace(search_many=lambda *_args, **_kwargs: [[
        hit_from_chunk(current, 0), hit_from_chunk(future, 1),
    ]])
    tools._adapters = SimpleNamespace(keyword=search, vector=search)
    tools._rerank_config = SimpleNamespace(window=0)
    tools._rerank_mode = "off"
    tools._top_k = 10
    tools._question = "历史判断"
    tools._material_text = ""
    tools._neighbor_hits = {}

    hits = tools.search(
        [RetrievalQuery(query_id="q", text="示例规则", query_type="legal_issue")],
        ReviewFacts(as_of_date="2024-03-23"),
    )
    assert {hit.chunk_id for hit in hits} == {"c1"}
    assert "c2" not in tools._candidate_hits
    assert "c2" not in tools._neighbor_hits


def test_search_preserves_small_query_sources_without_extra_adapter_calls() -> None:
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    broad = [
        _chunk(f"b{index}", "第一条", "通用依据", index=0).model_copy(
            update={"source_id": f"broad{index}"},
        ) for index in range(50)
    ]
    specific = _chunk("specific", "", "具体问题的官方解释", index=0).model_copy(update={
        "source_id": "specific_source", "citation_role": "interpretation_auxiliary",
        "can_cite_clause": False,
    })
    calls = []

    def search_many(queries, *, top_k):
        calls.append((queries, top_k))
        return [
            [hit_from_chunk(c, i).model_copy(update={"score": 1000 - i}) for i, c in enumerate(broad)],
            [hit_from_chunk(specific, 0).model_copy(update={"score": 0.001})],
        ]

    tools = _search_tools([*broad, specific], search_many, top_k=2)
    queries = [RetrievalQuery(query_id=str(i), text=f"查询{i}", query_type="legal_issue") for i in range(2)]
    hits = tools.search(queries, ReviewFacts())
    assert {h.source_id for h in hits} == {"broad0", "specific_source"}
    assert tools._candidate_hits["specific"].can_cite_clause is False
    assert len(calls) == 2
    assert all(pairs == [(q.text, q.query_type) for q in queries] for pairs, _ in calls)


def test_search_does_not_reward_source_length_over_a_specific_match() -> None:
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    specific = _chunk("specific", "", "具体问题的官方解释", index=0).model_copy(update={
        "source_id": "specific_source", "citation_role": "interpretation_auxiliary",
        "can_cite_clause": False,
    })
    long_source = [
        _chunk(f"long{i}", f"第{i + 1}条", text, index=i) for i, text in enumerate([
            "处理安全的通用要求", "运营组织的一般职责", "管理流程与责任制度",
        ])
    ]
    chunks = [specific, *long_source]
    hits = [hit_from_chunk(c, i).model_copy(update={"score": 1 - i * 0.01}) for i, c in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *_args, **_kwargs: [hits], top_k=1)
    result = tools.search(
        [RetrievalQuery(query_id="q", text="具体问题", query_type="legal_issue")],
        ReviewFacts(cross_border_transfer=True),
    )
    assert [h.chunk_id for h in result] == ["specific"]
    assert result[0].citation_role == "interpretation_auxiliary"
    assert result[0].can_cite_clause is False


def test_search_reranks_queries_separately_with_one_shared_source_budget(monkeypatch) -> None:
    from types import SimpleNamespace

    from law_agent.review.retrieval.neighbors import hit_from_chunk

    chunks = [
        _chunk(name, "第一条", "示例依据", index=0).model_copy(update={"source_id": name})
        for name in ["a", "b", "c"]
    ]
    hits = [hit_from_chunk(chunk, i) for i, chunk in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *_args, **_kwargs: [hits[:2], hits[1:]], top_k=2)
    tools._rerank_mode = "embedding"
    calls = []

    def rerank(results, *, queries, top_k, **kwargs):
        calls.append(queries)
        return SimpleNamespace(hits=list(reversed(results))[:top_k])

    monkeypatch.setattr("law_agent.review.agent_tools.rerank_hits", rerank)
    queries = [RetrievalQuery(query_id=str(i), text=f"查询{i}", query_type="legal_issue") for i in range(2)]
    result = tools.search(queries, ReviewFacts())
    assert calls == [[queries[0]], [queries[1]]]
    assert {hit.source_id for hit in result} == {"b", "c"}


def _search_tools(chunks, search_many, *, top_k):
    from types import SimpleNamespace

    tools = _tools_with(chunks)
    tools._adapters = SimpleNamespace(
        keyword=SimpleNamespace(search_many=search_many),
        vector=SimpleNamespace(search_many=search_many),
    )
    tools._rerank_config = SimpleNamespace(window=50)
    tools._rerank_mode = "off"
    tools._top_k = top_k
    tools._question = "审查具体适用关系"
    tools._material_text = ""
    tools._neighbor_hits = {}
    return tools


def test_omitted_keyword_source_is_discoverable_but_requires_reading_before_citation():
    from law_agent.review.result_builder import validate_grounded_claims
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.schemas import GroundedClaim

    primary = _chunk("primary", "第一条", "主要命中", index=0)
    target = _chunk("target", "第二条", "关键词命中的条件", index=0).model_copy(update={"source_id": "other"})
    duplicate = target.model_copy(update={"chunk_id": "duplicate", "chunk_index": 1})
    future = target.model_copy(update={"chunk_id": "future", "source_id": "future-source",
                                       "publish_date": "2027-01-01", "effective_date": "2027-01-01"})
    keyword = [hit_from_chunk(c, i) for i, c in enumerate([primary, future, target, duplicate])]
    tools = _search_tools([primary, target, duplicate, future], lambda *_args, **_kwargs: [keyword], top_k=1)
    tools._adapters.vector.search_many = lambda *_args, **_kwargs: [[keyword[0]]]
    claim = GroundedClaim(text="应核对该条件", supporting_chunk_ids=["target"])
    decisions = iter([
        AgentDecision(action="search_evidence", summary="寻找依据", queries=[
            RetrievalQuery(query_id="q", text="条件", query_type="legal_issue"),
        ]),
        AgentDecision(action="read_evidence", summary="精读关键词候选", read_requests=[
            EvidenceReadRequest(source_id="other", article_no="第二条"),
        ]),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    snapshots = []
    state = run_agent(
        AgentState(goal="核对条件", facts=ReviewFacts(as_of_date="2026-10-08")),
        material="", intake={}, decide=lambda *_: next(decisions), search=tools.search,
        search_navigation=tools.search_navigation, read_evidence=tools.read_evidence,
        web_search=lambda *_args: [], finalize=lambda *_args: {"ok": True},
        checkpoint=lambda current: snapshots.append(current.model_copy(deep=True)),
    )
    candidates = state.steps[0].observation["keyword_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["source_id"] == "other" and candidates[0]["chunk_id"] == "target"
    assert candidates[0]["query_id"] == state.queries[0].query_id
    searched = next(s for s in snapshots if len(s.steps) == 1)
    assert {h.source_id for h in searched.evidence} == {"s1"}
    with pytest.raises(ValueError, match="未检索到"):
        validate_grounded_claims([claim], searched.evidence)
    validate_grounded_claims([claim], state.evidence)
    assert state.reads == 1
    assert "future" not in {h.chunk_id for h in state.evidence}


def test_keyword_navigation_keeps_omitted_articles_in_an_already_returned_source():
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    primary, target, generic1, generic2 = [
        _chunk(name, article, text, index=index)
        for index, (name, article, text) in enumerate([
            ("primary", "第一条", "一般义务"),
            ("target", "第二条", "义务例外"),
            ("generic1", "第三条", "一般范围"),
            ("generic2", "第四条", "通用范围"),
        ])
    ]
    keyword = [hit_from_chunk(c, 1) for c in (primary, target, generic1, generic2)]
    vector = [hit_from_chunk(c, 1) for c in (generic1, generic2, primary)]
    tools = _search_tools([primary, target, generic1, generic2], lambda *_a, **_kw: [keyword], top_k=1)
    tools._adapters.vector.search_many = lambda *_a, **_kw: [vector]
    hits = tools.search([
        RetrievalQuery(query_id="q", text="一般义务及例外", query_type="legal_issue"),
    ], ReviewFacts())

    assert {h.source_id for h in hits} == {"s1"}
    assert "target" not in {h.chunk_id for h in hits}
    assert [item["chunk_id"] for item in tools.search_navigation()] == ["target"]
    read = tools.read_evidence("s1", "第二条", None, ReviewFacts())
    assert [h.chunk_id for h in read.hits] == ["target"]


def test_search_preserves_query_anchors_within_the_same_source():
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    targets = [_chunk(f"target{i}", f"第{i + 1}条", f"目标条款{i}", index=i) for i in range(4)]
    generic = [_chunk(f"generic{i}", f"第{i + 5}条", "通用条件", index=i + 4) for i in range(2)]
    batches = [
        [hit_from_chunk(chunk, rank).model_copy(update={"score": 1 / (rank + 1)})
         for rank, chunk in enumerate([target, *generic])]
        for target in targets
    ]
    tools = _search_tools([*targets, *generic], lambda *_args, **_kwargs: batches, top_k=1)
    queries = [RetrievalQuery(query_id=f"q{i}", text=f"查找条款{i}", query_type="legal_issue") for i in range(4)]

    hits = tools.search(queries, ReviewFacts())

    assert {target.chunk_id for target in targets} <= {hit.chunk_id for hit in hits}
    assert {hit.source_id for hit in hits} == {"s1"}
    by_id = {hit.chunk_id: hit for hit in hits}
    for index, target in enumerate(targets):
        assert by_id[target.chunk_id].matched_query_ids == [f"q{index}"]


def test_search_observation_keeps_query_matches_distinct_between_batches():
    decisions = iter([
        AgentDecision(action="search_evidence", summary="检索", queries=[
            RetrievalQuery(query_id="q1", query_type="legal_issue", text=text),
        ]) for text in ("第一个问题", "第二个问题")
    ] + [AgentDecision(action="finish", summary="交付", draft=_draft())])

    def search(queries, _facts):
        return [_hit("c1", "s1").model_copy(update={"matched_query_ids": [queries[0].query_id]})]

    state = run_agent(
        AgentState(goal="审查"), material="材料", intake={},
        decide=lambda *_: next(decisions), search=search, web_search=lambda *_: [],
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )

    groups = [step.observation["query_results"][0] for step in state.steps[:2]]
    assert groups[0]["query_id"] != groups[1]["query_id"]
    assert [group["text"] for group in groups] == ["第一个问题", "第二个问题"]
    assert all(group["chunk_ids"] == ["c1"] for group in groups)


def test_final_report_rechecks_date_and_never_adds_unseen_cached_evidence() -> None:
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.schemas import GroundedClaim

    chunks = [
        _chunk("c1", "第一条", "第一条 当前规则。", index=0),
        _chunk("future", "", "未来指南。", index=1).model_copy(
            update={"publish_date": "2025-06-27", "can_cite_clause": False},
        ),
        _chunk("unseen", "第二条", "第二条 未返回的候选。", index=2),
    ]
    tools = _tools_with(chunks)
    tools._candidate_hits = {chunk.chunk_id: hit_from_chunk(chunk, 0) for chunk in chunks}
    tools._neighbor_hits = dict(tools._candidate_hits)
    tools._top_k = 10
    tools._material_text = ""
    tools._material_versions_by_id = {}
    tools._semantic_verifier = object()
    state = AgentState(
        goal="历史审查", facts=ReviewFacts(as_of_date="2024-03-23"),
        evidence=[tools._candidate_hits["c1"], tools._candidate_hits["future"]],
    )
    payload = tools.finalize(
        _draft(), state, case_id="case", intake_snapshot={"facts": {}}, system_abstention=True,
    )
    assert {hit["chunk_id"] for hit in payload["evidence_chunks"]} == {"c1"}
    assert {citation["chunk_id"] for citation in payload["review_result"]["citations"]} == {"c1"}
    draft = _draft().model_copy(update={
        "claims": [GroundedClaim(text="未来内容", supporting_chunk_ids=["future"])],
    })
    with pytest.raises(ValueError, match="审查时点 2024-03-23 不可用"):
        tools.finalize(draft, state, case_id="case", intake_snapshot={"facts": {}}, system_abstention=True)


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
    tools._keyword_navigation = []
    return tools


def test_agent_accepts_a_focused_report_without_auxiliary_sections():
    from law_agent.review.agent import AgentModel
    from tests.test_review_llm import FakeClient

    draft = _draft().model_dump(mode="json")
    auxiliary = ("trigger_reasons", "missing_information", "recommended_actions", "risk_boundaries")
    for field in auxiliary:
        del draft[field]
    client = FakeClient(outputs=[{"action": "finish", "summary": "交付本题判断", "draft": draft}])
    model = AgentModel(model_id="fake", client=client)
    model.node.max_retries = 0
    decision = model(AgentState(goal="判断本题"), {})

    assert decision.draft.conclusion == draft["conclusion"]
    assert decision.draft.risk_level == draft["risk_level"]
    assert all(getattr(decision.draft, field) == [] for field in auxiliary)
    assert len(client.calls) == 1
