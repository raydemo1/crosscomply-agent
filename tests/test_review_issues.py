"""Tests for deterministic issue grounding in the Agent finalizer."""

import pytest

from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.agent_tools import finalize_issues
from law_agent.review.citations import group_citations
from law_agent.review.enterprise_store import MaterialVersion
from law_agent.review.result_builder import (
    LLMReviewResultDraft,
    MaterialEvidenceDraft,
    ReviewIssueDraft,
)
from law_agent.review.schemas import RetrievalHit, ReviewFacts

PLAN_TEXT = "本方案仅向服务商发送匿名后的客服工单，不包含个人信息。"
FIELD_LIST_TEXT = "字段清单：customer_name、email、mobile、ticket_content。"


def _version(version_id: str, text: str, *, logical_name: str, version_number: int) -> MaterialVersion:
    return MaterialVersion(
        id=version_id,
        material_id="material_001",
        case_id="case_001",
        logical_name=logical_name,
        version_number=version_number,
        filename=f"{logical_name}.txt",
        content_type="text/plain",
        object_key=f"cases/case_001/{logical_name}.txt",
        sha256="a" * 64,
        byte_size=len(text),
        uploaded_by="user_001",
        parse_status="ready",
        parsed_text=text,
    )


def _frozen_versions() -> dict[str, MaterialVersion]:
    plan = _version("mv_plan", PLAN_TEXT, logical_name="接入方案", version_number=1)
    fields = _version("mv_fields", FIELD_LIST_TEXT, logical_name="字段清单", version_number=2)
    return {version.id: version for version in (plan, fields)}


def _hit(
    chunk_id: str = "c1",
    *,
    can_cite: bool = True,
    citation_role: str = "primary_legal_basis",
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id="d1",
        source_id="s1",
        title="个人信息保护法",
        text="第十三条　个人信息处理者处理个人信息应当具备合法性基础。",
        score=1.0,
        rank=0,
        retriever="hybrid",
        citation_role=citation_role,
        can_cite_clause=can_cite,
        source_url="https://example.com/pip_law",
        citation_label="个人信息保护法 第十三条",
        article_no="第十三条",
    )


def _citation_groups(hits: list[RetrievalHit]):
    groups, _violations = group_citations(hits, ReviewFacts(), {})
    return groups


def _conflict_draft(*excerpts: tuple[str, str]) -> ReviewIssueDraft:
    return ReviewIssueDraft(
        kind="material_conflict",
        title="数据发送范围不一致",
        finding="接入方案称仅发送匿名工单，但字段清单包含姓名、邮箱、手机号。",
        material_evidence=[
            MaterialEvidenceDraft(material_version_id=version_id, quote=quote)
            for version_id, quote in excerpts
        ],
        supporting_chunk_ids=[],
        unknowns=[],
        recommended_action="核对生产环境实际发送字段。",
    )


def test_material_conflict_grounds_both_excerpts() -> None:
    issues = finalize_issues(
        [_conflict_draft(("mv_plan", "仅向服务商发送匿名后的客服工单"), ("mv_fields", FIELD_LIST_TEXT))],
        evidence=[],
        citation_groups=[],
        material_versions_by_id=_frozen_versions(),
    )

    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "material_conflict"
    assert issue.id.startswith("issue_")
    assert [(item.material_version_id, item.quote) for item in issue.material_evidence] == [
        ("mv_plan", "仅向服务商发送匿名后的客服工单"),
        ("mv_fields", FIELD_LIST_TEXT),
    ]
    plan_ref, fields_ref = issue.material_evidence
    assert (plan_ref.logical_name, plan_ref.version_number, plan_ref.filename) == (
        "接入方案",
        1,
        "接入方案.txt",
    )
    assert plan_ref.start_offset == PLAN_TEXT.find(plan_ref.quote)
    assert plan_ref.end_offset - plan_ref.start_offset == len(plan_ref.quote)
    assert fields_ref.start_offset == FIELD_LIST_TEXT.find(FIELD_LIST_TEXT)


def test_hallucinated_quote_is_rejected() -> None:
    with pytest.raises(ValueError):
        finalize_issues(
            [_conflict_draft(("mv_plan", "仅发送脱敏后的匿名工单"), ("mv_fields", FIELD_LIST_TEXT))],
            evidence=[],
            citation_groups=[],
            material_versions_by_id=_frozen_versions(),
        )


def test_excerpt_outside_frozen_snapshot_is_rejected() -> None:
    with pytest.raises(ValueError):
        finalize_issues(
            [_conflict_draft(("mv_other", PLAN_TEXT), ("mv_fields", FIELD_LIST_TEXT))],
            evidence=[],
            citation_groups=[],
            material_versions_by_id=_frozen_versions(),
        )


def test_conflict_with_only_one_excerpt_is_rejected() -> None:
    with pytest.raises(ValueError):
        finalize_issues(
            [_conflict_draft(("mv_plan", PLAN_TEXT))],
            evidence=[],
            citation_groups=[],
            material_versions_by_id=_frozen_versions(),
        )


def test_repeated_quote_requires_a_longer_excerpt() -> None:
    repeated = _version(
        "mv_repeat",
        "仅发送匿名工单。\n\n补充说明。\n\n仅发送匿名工单。",
        logical_name="重复材料",
        version_number=1,
    )
    with pytest.raises(ValueError):
        finalize_issues(
            [
                _conflict_draft(
                    ("mv_repeat", "仅发送匿名工单。"),
                    ("mv_fields", FIELD_LIST_TEXT),
                )
            ],
            evidence=[],
            citation_groups=[],
            material_versions_by_id={**_frozen_versions(), repeated.id: repeated},
        )


def _legal_gap_draft(chunk_ids: list[str]) -> ReviewIssueDraft:
    return ReviewIssueDraft(
        kind="legal_gap",
        title="缺少合法性基础",
        finding="字段清单包含姓名、邮箱、手机号，材料未说明处理这些个人信息的合法性基础。",
        material_evidence=[
            MaterialEvidenceDraft(material_version_id="mv_fields", quote=FIELD_LIST_TEXT)
        ],
        supporting_chunk_ids=chunk_ids,
        unknowns=[],
        recommended_action="补充告知同意或合同必需依据。",
    )


def test_legal_gap_needs_material_fact_and_citable_chunk() -> None:
    issues = finalize_issues(
        [_legal_gap_draft(["c1"])],
        evidence=[_hit("c1")],
        citation_groups=_citation_groups([_hit("c1")]),
        material_versions_by_id=_frozen_versions(),
    )

    issue = issues[0]
    assert issue.kind == "legal_gap"
    assert [item.material_version_id for item in issue.material_evidence] == ["mv_fields"]
    assert issue.supporting_chunk_ids == ["c1"]
    assert issue.supporting_citation_refs == ["法源-01"]


def test_legal_gap_without_material_fact_is_rejected() -> None:
    draft = _legal_gap_draft(["c1"]).model_copy(update={"material_evidence": []})
    with pytest.raises(ValueError):
        finalize_issues(
            [draft],
            evidence=[_hit("c1")],
            citation_groups=_citation_groups([_hit("c1")]),
            material_versions_by_id=_frozen_versions(),
        )


def test_legal_gap_with_non_citable_chunk_is_rejected() -> None:
    guide = _hit("c9", can_cite=False, citation_role="implementation_reference")
    with pytest.raises(ValueError):
        finalize_issues(
            [_legal_gap_draft(["c9"])],
            evidence=[guide],
            citation_groups=_citation_groups([guide]),
            material_versions_by_id=_frozen_versions(),
        )


def test_missing_information_keeps_unknowns_without_material_quote() -> None:
    draft = ReviewIssueDraft(
        kind="missing_information",
        title="无法确认是否开启模型训练",
        finding="材料未说明供应商是否使用客户数据训练模型。",
        material_evidence=[],
        supporting_chunk_ids=[],
        unknowns=["供应商是否使用客户数据训练模型"],
        recommended_action="要求供应商书面确认训练数据范围。",
    )
    issues = finalize_issues(
        [draft],
        evidence=[],
        citation_groups=[],
        material_versions_by_id=_frozen_versions(),
    )

    assert issues[0].material_evidence == []
    assert issues[0].unknowns == ["供应商是否使用客户数据训练模型"]


def test_missing_information_without_unknowns_is_rejected() -> None:
    draft = ReviewIssueDraft(
        kind="missing_information",
        title="信息缺口",
        finding="材料未说明接收方地区。",
        material_evidence=[],
        supporting_chunk_ids=[],
        unknowns=[],
        recommended_action="补充接收方地区。",
    )
    with pytest.raises(ValueError):
        finalize_issues(
            [draft],
            evidence=[],
            citation_groups=[],
            material_versions_by_id=_frozen_versions(),
        )


def _agent_draft(issue: ReviewIssueDraft | None) -> LLMReviewResultDraft:
    return LLMReviewResultDraft(
        risk_level="insufficient_evidence",
        decision_summary="当前材料和法源证据仍不足以形成正式风险结论，需要明确调查边界、尚未核实事项以及后续补充要求。",
        conclusion="现有证据不足，暂不形成正式合规路径结论。",
        trigger_reasons=["证据不足"],
        missing_information=["境外接收方所在地区"],
        recommended_actions=["补充接收方信息"],
        risk_boundaries=["未覆盖地方和行业特别规则"],
        issues=[] if issue is None else [issue],
    )


def test_agent_can_retry_after_finalizer_rejects_an_issue() -> None:
    decisions = iter([
        AgentDecision(
            action="finish",
            summary="交付调查结果",
            draft=_agent_draft(_conflict_draft(("mv_plan", PLAN_TEXT))),
        ),
        AgentDecision(
            action="finish",
            summary="补正冲突双方引用后交付",
            draft=_agent_draft(
                _conflict_draft(("mv_plan", PLAN_TEXT), ("mv_fields", FIELD_LIST_TEXT))
            ),
        ),
    ])

    def finalize(draft, _state):
        issues = finalize_issues(
            draft.issues,
            evidence=[],
            citation_groups=[],
            material_versions_by_id=_frozen_versions(),
        )
        return {"issues": [issue.model_dump(mode="json") for issue in issues]}

    state = run_agent(
        AgentState(goal="审查接入方案", plan_confirmed=True),
        material=PLAN_TEXT,
        rule={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        finalize=finalize,
        checkpoint=lambda _state: None,
    )

    assert state.status == "completed"
    assert [step.action for step in state.steps] == ["finish", "finish"]
    assert "至少需要两条不同的材料原文" in state.steps[0].observation["error"]
    assert len(state.result["issues"]) == 1