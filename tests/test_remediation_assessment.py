"""Behavior tests for low-friction remediation progress and Agent re-review."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from law_agent.llm.openai_compatible import ChatMessage
from law_agent.review.agent import AgentState
from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.remediation import (
    AssessmentBasisDraft,
    AssessmentPointDraft,
    RemediationAssessmentDraft,
    RemediationAssessmentError,
    RemediationDecision,
    RemediationTaskDraft,
    RemediationTaskDrafter,
    RemediationTaskDraftSet,
    RereviewAttachment,
    build_rereview_packet,
    execute_rereview,
    finalize_assessment,
    guided_actions_from_review,
    validate_task_drafts,
)
from law_agent.review.schemas import RetrievalQuery

TRAINING_QUOTE = "Training with customer data: Disabled"
CONTRACT_QUOTE = "供应商可将 Customer Content 用于服务改进。"
ACCEPTED_QUOTE = "供应商不得将 Customer Content 用于模型训练。"


@dataclass(frozen=True)
class _Version:
    id: str
    parsed_text: str


def _issue() -> dict[str, Any]:
    return {
        "id": "issue_1",
        "kind": "missing_information",
        "title": "训练用途未知",
        "finding": "材料未说明供应商是否将客户数据用于模型训练。",
        "recommended_action": "确认生产环境训练功能状态并修订合同用途条款。",
        "unknowns": ["是否用于模型训练"],
        "material_evidence": [
            {
                "logical_name": "vendor_dpa",
                "version_number": 1,
                "material_version_id": "mv_1",
                "quote": CONTRACT_QUOTE,
            }
        ],
    }


def _review_result() -> dict[str, Any]:
    return {
        "issues": [_issue()],
        "applicable_evidence": [
            {
                "citations": [
                    {
                        "can_cite_clause": True,
                        "chunk_id": "chunk_1",
                        "citation_ref": "C1",
                        "title": "个人信息保护法",
                        "article_no": "第二十一条",
                        "full_article_text": "个人信息处理者委托处理个人信息的，应当与受托人约定委托处理的目的、期限、处理方式等。",
                    }
                ]
            }
        ],
    }


def _task() -> dict[str, Any]:
    return {
        "id": "remediation_task_1",
        "title": "关闭训练用途",
        "description": "确认供应商不使用客户数据训练模型。",
        "acceptance_criteria": "能确认生产环境训练功能关闭，且合同不存在授权训练用途的条款。",
    }


def _packet(
    *,
    attachments: tuple[RereviewAttachment, ...] = (),
    accepted: list[dict[str, Any]] | None = None,
    task_kind: str = "control_remediation",
) -> Any:
    task = _task()
    task["task_kind"] = task_kind
    return build_rereview_packet(
        task=task,
        review_result=_review_result(),
        issue=_issue(),
        material_versions=[_Version("mv_1", CONTRACT_QUOTE)],
        accepted_revisions=accepted or [],
        history=(),
        submission={"note": "已关闭生产环境的模型训练功能。"},
        attachments=attachments,
    )


def _point(text: str, *basis: AssessmentBasisDraft) -> AssessmentPointDraft:
    return AssessmentPointDraft(text=text, basis=list(basis))


def _draft(**overrides: Any) -> RemediationAssessmentDraft:
    payload: dict[str, Any] = {
        "status": "resolved",
        "summary": "生产环境训练功能已关闭。",
        "confirmed_points": [],
        "remaining_gaps": [],
        "next_request": "",
    }
    payload.update(overrides)
    return RemediationAssessmentDraft(**payload)


# --- deterministic trust gate -------------------------------------------------


def test_resolved_backed_only_by_user_statement_is_rejected() -> None:
    draft = _draft(
        confirmed_points=[
            _point("生产环境已关闭训练", AssessmentBasisDraft(source="user_statement", quote="已关闭"))
        ]
    )
    with pytest.raises(RemediationAssessmentError, match="可核验材料"):
        finalize_assessment(draft, _packet())


def test_fact_confirmation_accepts_an_exact_applicant_statement() -> None:
    statement = "已关闭生产环境的模型训练功能。"
    draft = _draft(
        confirmed_points=[
            _point("申报人已声明生产环境关闭训练功能", AssessmentBasisDraft(source="user_statement", quote=statement))
        ]
    )
    payload = finalize_assessment(draft, _packet(task_kind="fact_confirmation"))
    assert payload["status"] == "resolved"
    basis = payload["grounded_evidence"][0]
    assert basis["source"] == "user_statement"
    assert basis["quote"] == statement
    assert basis["start_offset"] == 0


def test_legal_basis_cannot_prove_a_control_was_implemented() -> None:
    draft = _draft(
        confirmed_points=[
            _point(
                "合同已完成约定",
                AssessmentBasisDraft(
                    source="legal_basis",
                    reference="chunk_1",
                    quote="个人信息处理者委托处理个人信息的，应当与受托人约定委托处理的目的、期限、处理方式等。",
                ),
            )
        ]
    )
    with pytest.raises(RemediationAssessmentError, match="法条本身不能证明整改已经完成"):
        finalize_assessment(draft, _packet())


def test_guided_actions_reconcile_and_keep_fact_and_remediation_boundaries() -> None:
    store = InMemoryCaseStore(seed_password="pw")
    initial_review = {
        "review_result_id": "result_1",
        "issues": [
            {
                "id": "issue_fact",
                "kind": "missing_information",
                "title": "处理目的未确认",
                "finding": "材料没有说明处理目的。",
                "unknowns": ["是否仅用于履行服务合同"],
            },
            {
                "id": "issue_control",
                "kind": "legal_gap",
                "title": "委托合同条款不完整",
                "finding": "合同未约定委托处理期限。",
                "recommended_action": "补充期限条款并提交已签署合同。",
            },
        ],
        "recommended_actions": ["获批后每年复核供应商合同"],
    }
    actions = guided_actions_from_review(initial_review, requester_id="requester_1")
    assert [item["task_kind"] for item in actions] == [
        "fact_confirmation",
        "control_remediation",
        "recommendation",
    ]
    assert actions[0]["blocking"] is True and actions[0]["phase"] == "pre_approval"
    assert actions[2]["blocking"] is False and actions[2]["phase"] == "post_approval"

    plan = store.sync_guided_remediation_plan("case_1", "reviewer_1", actions)
    fact, control, recommendation = plan["tasks"]
    store.start_remediation_task(fact["id"])
    fact_submission = store.create_remediation_submission(
        fact["id"], submitted_by="requester_1", note="仅用于履行服务合同。"
    )
    verified = store.apply_agent_remediation_assessment(
        fact_submission["id"], assessment_status="resolved"
    )
    assert verified["status"] == "agent_verified"
    assert store.get_remediation_task(fact["id"])["status"] == "completed"

    store.start_remediation_task(control["id"])
    control_submission = store.create_remediation_submission(
        control["id"], submitted_by="requester_1", note="已补充期限条款。"
    )
    pending_review = store.apply_agent_remediation_assessment(
        control_submission["id"], assessment_status="resolved"
    )
    assert pending_review["status"] == "pending_review"
    assert store.get_remediation_task(control["id"])["status"] == "pending_review"

    refreshed = store.sync_guided_remediation_plan("case_1", "reviewer_1", actions)
    refreshed_by_key = {item["source_key"]: item for item in refreshed["tasks"]}
    assert refreshed_by_key[control["source_key"]]["id"] == control["id"]
    assert refreshed_by_key[control["source_key"]]["status"] == "pending_review"
    assert refreshed_by_key[recommendation["source_key"]]["id"] == recommendation["id"]

    next_review = {**initial_review, "review_result_id": "result_2", "issues": [initial_review["issues"][1]]}
    next_actions = guided_actions_from_review(next_review, requester_id="requester_1")
    reconciled = store.sync_guided_remediation_plan("case_1", "reviewer_1", next_actions)
    fact_history = next(item for item in reconciled["tasks"] if item["id"] == fact["id"])
    assert fact_history["is_current"] is False
    assert all(item["is_current"] for item in reconciled["tasks"] if item["id"] != fact["id"])


def test_guided_fact_answer_type_is_explicit_and_survives_reconciliation() -> None:
    store = InMemoryCaseStore(seed_password="pw")
    review = {
        "review_result_id": "result_answer_type",
        "issues": [{
            "id": "issue_count",
            "kind": "missing_information",
            "title": "请确认覆盖规模",
            "finding": "材料没有给出覆盖规模。",
            "unknowns": ["实际覆盖多少名用户"],
            "answer_type": "count",
        }],
    }
    actions = guided_actions_from_review(review, requester_id="requester_1")
    assert actions[0]["answer_type"] == "count"
    plan = store.sync_guided_remediation_plan("case_1", "reviewer_1", actions)
    assert plan["tasks"][0]["answer_type"] == "count"
    updated = store.sync_guided_remediation_plan("case_1", "reviewer_1", actions)
    assert updated["tasks"][0]["answer_type"] == "count"

    unlocated = guided_actions_from_review({
        "review_result_id": "result_unlocated",
        "missing_information": ["请提供本年度覆盖用户总数"],
        "missing_answer_types": {"请提供本年度覆盖用户总数": "count"},
    }, requester_id="requester_1")
    assert unlocated[0]["answer_type"] == "count"


def test_declared_control_without_document_waits_for_human_review() -> None:
    store = InMemoryCaseStore(seed_password="pw")
    plan = store.sync_guided_remediation_plan(
        "case_1", "reviewer_1", [
            {
                "source_key": "control:training", "title": "限制服务商训练使用",
                "description": "", "acceptance_criteria": "限制训练使用",
                "assignee_id": "requester_1", "priority": "high", "due_date": None,
                "task_kind": "control_remediation", "phase": "pre_approval", "blocking": True,
            }
        ],
    )
    task_id = plan["tasks"][0]["id"]
    store.start_remediation_task(task_id)
    submission = store.create_remediation_submission(
        task_id, submitted_by="requester_1", note="已关闭训练功能。",
        response_choice="completed",
    )
    reviewed = store.apply_agent_remediation_assessment(
        submission["id"], assessment_status="insufficient_evidence"
    )
    assert reviewed["status"] == "pending_review"
    assert store.get_remediation_task(task_id)["status"] == "pending_review"
    accepted = store.review_remediation_submission(
        submission["id"], decision="accepted", reviewed_by="reviewer_1",
        review_note="已人工核实供应商后台设置。",
    )
    assert accepted["status"] == "accepted"
    assert store.get_remediation_task(task_id)["status"] == "completed"


def test_guided_actions_do_not_duplicate_gaps_or_defer_required_controls() -> None:
    actions = guided_actions_from_review(
        {
            "issues": [
                {
                    "kind": "missing_information",
                    "title": "人数口径未确认",
                    "unknowns": ["自当年1月1日起累计出境去重人数"],
                },
                {
                    "kind": "compliance_gap",
                    "title": "服务商训练用途未限制",
                    "unknowns": ["服务商是否已关闭模型训练用途"],
                    "recommended_action": "取得书面限制条款和后台设置证据",
                },
            ],
            "missing_information": [
                "自当年1月1日起累计向境外提供的个人信息去重人数及是否含敏感个人信息",
                "是否已完成个人信息保护影响评估及其报告",
                "服务商是否已关闭模型训练用途，是否有后台设置证据",
            ],
            "recommended_actions": [
                "开展个人信息保护影响评估并形成报告",
                "获批后定期复核服务商合同",
            ],
        },
        requester_id="requester_1",
    )
    assert len(actions) == 4
    assert [item["task_kind"] for item in actions] == [
        "fact_confirmation", "control_remediation", "control_remediation", "recommendation",
    ]
    assert actions[1]["phase"] == "pre_approval"
    assert actions[2]["phase"] == "pre_approval"
    assert actions[3]["phase"] == "post_approval"


def test_resolved_with_grounded_attachment_records_offsets_and_hash() -> None:
    packet = _packet(
        attachments=(
            RereviewAttachment(
                evidence_id="evidence_1",
                kind="file",
                label="settings.png",
                text=f"AI settings\n{TRAINING_QUOTE}\n",
                sha256="b" * 64,
            ),
        )
    )
    draft = _draft(
        confirmed_points=[
            _point(
                "生产环境训练功能已关闭",
                AssessmentBasisDraft(source="attachment", reference="evidence_1", quote=TRAINING_QUOTE),
            )
        ]
    )
    payload = finalize_assessment(draft, packet)
    assert payload["status"] == "resolved"
    grounded = payload["grounded_evidence"][0]
    assert grounded["start_offset"] == 12
    assert grounded["sha256"] == "b" * 64


def test_resolved_with_remaining_gaps_is_rejected() -> None:
    draft = _draft(
        confirmed_points=[
            _point(
                "合同用途条款已修订",
                AssessmentBasisDraft(source="case_material", reference="mv_1", quote=CONTRACT_QUOTE),
            )
        ],
        remaining_gaps=["尚未确认正式签署版本"],
    )
    with pytest.raises(RemediationAssessmentError, match="不能同时保留"):
        finalize_assessment(draft, _packet())


def test_partial_assessment_requires_gaps_and_next_request() -> None:
    point = _point(
        "合同用途条款已修订",
        AssessmentBasisDraft(source="case_material", reference="mv_1", quote=CONTRACT_QUOTE),
    )
    with pytest.raises(RemediationAssessmentError, match="仍然缺少什么"):
        finalize_assessment(
            _draft(status="partially_resolved", confirmed_points=[point]), _packet()
        )
    with pytest.raises(RemediationAssessmentError, match="下一步最省事"):
        finalize_assessment(
            _draft(
                status="partially_resolved",
                confirmed_points=[point],
                remaining_gaps=["尚未确认生产环境配置生效"],
            ),
            _packet(),
        )


def test_attachment_quote_must_exist_verbatim_in_the_attachment() -> None:
    packet = _packet(
        attachments=(
            RereviewAttachment(
                evidence_id="evidence_1", kind="file", label="settings.png", text="AI settings: enabled"
            ),
        )
    )
    draft = _draft(
        confirmed_points=[
            _point(
                "训练已关闭",
                AssessmentBasisDraft(source="attachment", reference="evidence_1", quote=TRAINING_QUOTE),
            )
        ]
    )
    with pytest.raises(RemediationAssessmentError, match="找不到"):
        finalize_assessment(draft, packet)


def test_unknown_or_unparsed_attachment_cannot_be_used_as_basis() -> None:
    draft = _draft(
        confirmed_points=[
            _point(
                "训练已关闭",
                AssessmentBasisDraft(source="attachment", reference="evidence_missing", quote=TRAINING_QUOTE),
            )
        ]
    )
    with pytest.raises(RemediationAssessmentError, match="不存在或无法解析"):
        finalize_assessment(draft, _packet())

    link_only = _packet(
        attachments=(
            RereviewAttachment(
                evidence_id="evidence_link", kind="link", label="官网", uri="https://example.com"
            ),
        )
    )
    with pytest.raises(RemediationAssessmentError, match="不存在或无法解析"):
        finalize_assessment(draft, link_only)


def test_basis_cannot_reference_foreign_issue_or_uncitable_chunk() -> None:
    with pytest.raises(RemediationAssessmentError, match="原审查问题"):
        finalize_assessment(
            _draft(
                status="not_resolved",
                remaining_gaps=["仍未确认"],
                next_request="请补充配置截图",
                confirmed_points=[_point("其他问题", AssessmentBasisDraft(source="issue", reference="issue_9"))],
            ),
            _packet(),
        )
    with pytest.raises(RemediationAssessmentError, match="法条"):
        finalize_assessment(
            _draft(
                status="not_resolved",
                remaining_gaps=["仍未确认"],
                next_request="请补充配置截图",
                confirmed_points=[
                    _point("法定义务", AssessmentBasisDraft(source="legal_basis", reference="chunk_999"))
                ],
            ),
            _packet(),
        )


def test_accepted_revision_is_reused_without_reupload() -> None:
    accepted = [
        {
            "id": "revision_1",
            "status": "accepted",
            "target_quote": CONTRACT_QUOTE,
            "accepted_text": ACCEPTED_QUOTE,
            "result_text": ACCEPTED_QUOTE,
        }
    ]
    packet = _packet(accepted=accepted)
    assert "【已接受的修改】" in packet.text
    assert "无需用户重复上传" in packet.text
    payload = finalize_assessment(
        _draft(
            confirmed_points=[
                _point(
                    "合同文本已删除训练用途授权",
                    AssessmentBasisDraft(
                        source="accepted_revision", reference="revision_1", quote=ACCEPTED_QUOTE
                    ),
                )
            ]
        ),
        packet,
    )
    assert payload["status"] == "resolved"
    assert payload["grounded_evidence"][0]["reference"] == "revision_1"


# --- same Agent loop ----------------------------------------------------------


def test_rereview_loop_can_search_then_finish() -> None:
    packet = _packet(
        attachments=(
            RereviewAttachment(
                evidence_id="evidence_1", kind="file", label="settings.png", text=TRAINING_QUOTE
            ),
        )
    )
    seen: list[str] = []

    def decide(state: AgentState, rule: dict[str, Any]) -> RemediationDecision:
        seen.append(state.steps[-1].action if state.steps else "start")
        if not state.searches:
            return RemediationDecision(
                action="search_evidence",
                summary="补充委托处理法条",
                queries=[
                    RetrievalQuery(
                        query_id="q1", query_type="legal_issue", text="个人信息委托处理"
                    )
                ],
            )
        return RemediationDecision(
            action="finish",
            summary="给出复核结论",
            draft=_draft(
                confirmed_points=[
                    _point(
                        "生产环境训练功能已关闭",
                        AssessmentBasisDraft(
                            source="attachment", reference="evidence_1", quote=TRAINING_QUOTE
                        ),
                    )
                ]
            ),
        )

    state = execute_rereview(
        packet=packet,
        goal="复核整改任务",
        state=None,
        model_id="stub",
        chunks_path=Path("unused.jsonl"),
        decide=decide,
        search=lambda queries, facts: [],
    )
    assert state.status == "completed"
    assert state.searches == 1
    assert seen == ["start", "search_evidence"]
    assert state.result is not None and state.result["status"] == "resolved"


def test_rereview_loop_can_request_input_and_resume_the_same_run() -> None:
    packet = _packet()
    prompts: list[str] = []

    def decide(state: AgentState, rule: dict[str, Any]) -> RemediationDecision:
        if state.status == "running" and not state.steps:
            return RemediationDecision(
                action="request_input",
                summary="追问训练关闭范围",
                question="该设置是对全部用户生效，还是只针对测试账号？",
            )
        prompts.append(state.steps[-1].observation.get("answer", ""))
        return RemediationDecision(
            action="finish",
            summary="给出复核结论",
            draft=_draft(
                status="insufficient_evidence",
                summary="只有负责人陈述，缺少可核实依据。",
                remaining_gaps=["无法确认生产环境配置已经生效"],
                next_request="请提供后台设置页面截图。",
                confirmed_points=[],
            ),
        )

    waiting = execute_rereview(
        packet=packet,
        goal="复核整改任务",
        state=None,
        model_id="stub",
        chunks_path=Path("unused.jsonl"),
        decide=decide,
        search=lambda queries, facts: [],
    )
    assert waiting.status == "waiting_input"
    assert waiting.gate_id == "input_1"

    from law_agent.review.agent import answer_agent

    resumed = answer_agent(waiting, gate_id=waiting.gate_id or "", answer="对全部用户生效。")
    final = execute_rereview(
        packet=packet,
        goal="复核整改任务",
        state=resumed,
        model_id="stub",
        chunks_path=Path("unused.jsonl"),
        decide=decide,
        search=lambda queries, facts: [],
    )
    assert final.status == "completed"
    assert prompts == ["对全部用户生效。"]
    assert final.result is not None
    assert final.result["status"] == "insufficient_evidence"


# --- Agent-drafted remediation tasks ------------------------------------------


def _draftable_result() -> dict[str, Any]:
    return {
        **_review_result(),
        "recommended_actions": ["补充合同用途条款限制。", "留存供应商确认邮件。"],
    }


def _task_draft(**overrides: Any) -> RemediationTaskDraft:
    payload: dict[str, Any] = {
        "title": "确认训练用途状态",
        "description": "确认供应商未使用客户数据训练模型。",
        "acceptance_criteria": "能确认生产环境训练功能已关闭。",
        "source_issue_id": "issue_1",
    }
    payload.update(overrides)
    return RemediationTaskDraft(**payload)


def test_task_drafts_resolve_real_sources_and_keep_suggestions() -> None:
    result = validate_task_drafts(
        RemediationTaskDraftSet(
            tasks=[
                _task_draft(),
                _task_draft(
                    title="留存供应商确认邮件",
                    source_issue_id=None,
                    source_recommendation_index=1,
                    suggested_assignee_role="reviewer",
                    suggested_due_days=30,
                ),
            ]
        ),
        _draftable_result(),
    )
    assert [item["source_recommendation"] for item in result] == [None, "留存供应商确认邮件。"]
    assert result[0]["source_issue_id"] == "issue_1"
    assert result[1]["suggested_assignee_role"] == "reviewer"
    assert result[1]["suggested_due_days"] == 30


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        (_task_draft(source_issue_id="issue_missing"), "审查问题"),
        (_task_draft(source_issue_id=None, source_recommendation_index=5), "审查建议"),
        (_task_draft(source_issue_id=None), "绑定真实的审查问题或审查建议"),
    ],
)
def test_task_drafts_reject_sources_the_case_does_not_have(
    draft: RemediationTaskDraft, message: str
) -> None:
    with pytest.raises(RemediationAssessmentError, match=message):
        validate_task_drafts(RemediationTaskDraftSet(tasks=[draft]), _draftable_result())


class _ScriptedClient:
    """Replays canned JSON payloads so the retry prompt can be inspected."""

    def __init__(self, outputs: list[dict[str, Any]]):
        self.outputs = list(outputs)
        self.calls: list[list[ChatMessage]] = []

    def chat_json(self, messages: list[ChatMessage], **_: Any) -> dict[str, Any]:
        self.calls.append(list(messages))
        return self.outputs.pop(0)


def test_task_draft_retry_uses_a_generic_hint_and_recovers() -> None:
    client = _ScriptedClient(
        [
            {
                "tasks": [
                    {
                        "title": "确认训练用途状态",
                        "description": "",
                        "acceptance_criteria": "",
                        "source_issue_id": "issue_does_not_exist",
                    }
                ],
                "summary": "",
            },
            {
                "tasks": [
                    {
                        "title": "确认训练用途状态",
                        "description": "确认供应商未使用客户数据训练模型。",
                        "acceptance_criteria": "能确认生产环境训练功能已关闭。",
                        "source_issue_id": "issue_1",
                    }
                ],
                "summary": "",
            },
        ]
    )
    drafter = RemediationTaskDrafter(model_id="test-model", client=client)

    items = drafter(_draftable_result())

    assert [item["source_issue_id"] for item in items] == ["issue_1"]
    retry_prompt = client.calls[1][-1].content
    assert "业务校验失败" in retry_prompt
    assert "审查问题 id 或审查建议下标" in retry_prompt
    # The retry prompt must not leak the claim-grounding wording from other nodes.
    assert "claim" not in retry_prompt
    assert "supporting_chunk_id" not in retry_prompt


# --- HTTP behaviour -----------------------------------------------------------


class _FakeRereview:
    """Stand-in for ``execute_rereview`` so HTTP tests need no LLM or corpus."""

    def __init__(self, *, result: dict[str, Any] | None = None, question: str | None = None):
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.question = question
        self.pending = question is not None

    def __call__(
        self,
        *,
        packet: Any,
        goal: str,
        state: AgentState | None,
        model_id: str,
        chunks_path: Path,
        rerank_mode: str = "off",
    ) -> AgentState:
        self.calls.append({"packet": packet, "goal": goal, "state": state})
        if self.pending:
            self.pending = False
            return AgentState(
                goal=goal,
                status="waiting_input",
                turns=1,
                gate_id="input_1",
                pending_question=self.question,
            )
        return AgentState(goal=goal, status="completed", turns=2, result=self.result)


class _FakeTaskDrafter:
    """Stand-in for ``draft_remediation_tasks`` so HTTP tests need no LLM."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.drafts = RemediationTaskDraftSet(
            tasks=[
                _task_draft(
                    priority="high",
                    suggested_assignee_role="requester",
                    suggested_due_days=7,
                )
            ]
        )

    def __call__(
        self, review_result: dict[str, Any], *, model_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append({"review_result": review_result, "model_id": model_id})
        return validate_task_drafts(self.drafts, review_result)


def _completed(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "partially_resolved",
        "summary": "生产配置已确认，合同用途条款仍待处理。",
        "confirmed_points": [{"text": "训练功能已关闭", "basis": []}],
        "remaining_gaps": ["合同仍允许服务改进用途"],
        "next_request": "请处理合同用途条款。",
        "grounded_evidence": [],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def task_drafter() -> _FakeTaskDrafter:
    return _FakeTaskDrafter()


@pytest.fixture
def workbench(tmp_path: Path, task_drafter: _FakeTaskDrafter, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("law_agent.config.load_env_file", lambda: None)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "test-model")
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    runner = _FakeRereview(result=_completed())
    app = create_app(
        chunks_path=chunks,
        case_store=InMemoryCaseStore(seed_password="pw"),
        enterprise_store=InMemoryEnterpriseStore(),
        rereview=runner,
        task_drafter=task_drafter,
    )
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"})
    case_id = client.post(
        "/api/cases",
        json={
            "question": "这个业务是否需要数据出境安全评估？",
            "material_text": CONTRACT_QUOTE,
            "intake": {"business_activity": "推荐系统", "cross_border_transfer": True},
        },
    ).json()["case"]["id"]
    app.state.case_store.update_case(case_id, status="approved")
    app.state.case_store.update_case(
        case_id, response_json={"review_result": _review_result()}
    )
    reviewer_id = client.get("/api/auth/me").json()["user"]["id"]
    plan = client.post(
        f"/api/cases/{case_id}/remediation-plan",
        json={
            "tasks": [
                {
                    "title": "关闭训练用途",
                    "description": "确认供应商不使用客户数据训练模型。",
                    "acceptance_criteria": "能确认生产环境训练功能关闭。",
                    "assignee_id": reviewer_id,
                    "due_date": "2026-12-31",
                    "source_review_result_id": "result_test",
                    "source_issue_id": "issue_1",
                }
            ]
        },
    )
    assert plan.status_code == 200, plan.text
    task_id = plan.json()["tasks"][0]["id"]
    assert client.post(f"/api/remediation-plans/{plan.json()['id']}/activate").status_code == 200
    assert client.post(f"/api/remediation-tasks/{task_id}/start").status_code == 200
    return client, app, runner, case_id, task_id


def test_reviewer_can_ask_the_agent_to_draft_remediation_tasks(
    workbench, task_drafter: _FakeTaskDrafter
) -> None:
    client, _app, _runner, case_id, _task_id = workbench
    response = client.post(f"/api/cases/{case_id}/remediation-task-drafts")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["source_issue_id"] == "issue_1"
    assert body["items"][0]["suggested_due_days"] == 7
    assert body["items"][0]["source_recommendation"] is None
    assert task_drafter.calls[0]["review_result"]["issues"][0]["id"] == "issue_1"
    assert task_drafter.calls[0]["model_id"] == "test-model"


def test_drafting_tasks_never_creates_a_plan(workbench) -> None:
    client, _app, _runner, case_id, _task_id = workbench
    before = client.get(f"/api/cases/{case_id}/remediation-plan").json()
    assert client.post(f"/api/cases/{case_id}/remediation-task-drafts").status_code == 200
    after = client.get(f"/api/cases/{case_id}/remediation-plan").json()
    assert len(after["tasks"]) == len(before["tasks"]) == 1


def test_drafting_tasks_rejects_sources_the_case_does_not_have(
    workbench, task_drafter: _FakeTaskDrafter
) -> None:
    client, _app, _runner, case_id, _task_id = workbench
    task_drafter.drafts = RemediationTaskDraftSet(
        tasks=[_task_draft(source_issue_id="issue_missing")]
    )
    response = client.post(f"/api/cases/{case_id}/remediation-task-drafts")
    assert response.status_code == 422
    assert "审查问题" in response.json()["detail"]


def test_applicant_cannot_ask_for_task_drafts(workbench) -> None:
    _client, app, _runner, case_id, _task_id = workbench
    requester = TestClient(app)
    requester.post(
        "/api/auth/login",
        json={"username": "requester@crosscomply.local", "password": "pw"},
    )
    assert requester.post(f"/api/cases/{case_id}/remediation-task-drafts").status_code == 403


def test_progress_submission_needs_only_a_note(workbench) -> None:
    client, _app, runner, _case_id, task_id = workbench
    response = client.post(
        f"/api/remediation-tasks/{task_id}/submissions",
        json={"note": "我已经关闭了生产环境的模型训练功能。"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submission"]["note"] == "我已经关闭了生产环境的模型训练功能。"
    assert body["submission"]["evidence"] == []
    assert body["assessment"]["run_status"] == "completed"
    assert body["assessment"]["status"] == "partially_resolved"
    assert runner.calls[0]["packet"].issue_id == "issue_1"
    assert "验收标准：" in runner.calls[0]["packet"].text


def test_progress_submission_rejects_a_blank_note(workbench) -> None:
    client, _app, runner, _case_id, task_id = workbench
    response = client.post(
        f"/api/remediation-tasks/{task_id}/submissions", json={"note": "   "}
    )
    assert response.status_code == 422
    assert runner.calls == []


def test_agent_assessment_never_closes_the_task(workbench) -> None:
    client, _app, runner, _case_id, task_id = workbench
    runner.result = _completed(status="resolved", remaining_gaps=[], next_request="")
    client.post(f"/api/remediation-tasks/{task_id}/submissions", json={"note": "已处理。"})
    detail = client.get(f"/api/remediation-tasks/{task_id}").json()["task"]
    assert detail["status"] == "pending_review"
    assert detail["latest_assessment"]["status"] == "resolved"
    assert detail["submissions"][-1]["assessment"]["status"] == "resolved"


def test_waiting_assessment_is_answered_through_the_input_endpoint(workbench) -> None:
    client, _app, runner, _case_id, task_id = workbench
    runner.question = "该设置是对全部用户生效，还是只针对测试账号？"
    runner.pending = True
    submitted = client.post(
        f"/api/remediation-tasks/{task_id}/submissions", json={"note": "已关闭训练。"}
    ).json()
    assessment = submitted["assessment"]
    assert assessment["run_status"] == "waiting_input"
    assert assessment["question"] == runner.question

    answered = client.post(
        f"/api/remediation-assessments/{assessment['id']}/input",
        json={"gate_id": "input_1", "answer": "对全部用户生效。"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["run_status"] == "completed"
    assert answered.json()["status"] == "partially_resolved"

    stale = client.post(
        f"/api/remediation-assessments/{assessment['id']}/input",
        json={"gate_id": "input_1", "answer": "再答一次。"},
    )
    assert stale.status_code == 409


def test_previous_assessment_and_human_note_reach_the_next_review(workbench) -> None:
    client, _app, runner, _case_id, task_id = workbench
    first = client.post(
        f"/api/remediation-tasks/{task_id}/submissions", json={"note": "已关闭训练。"}
    ).json()
    rejected = client.post(
        f"/api/remediation-submissions/{first['submission']['id']}/review",
        json={"decision": "rejected", "review_note": "请补充供应商确认邮件。"},
    )
    assert rejected.status_code == 200, rejected.text

    second = client.post(
        f"/api/remediation-tasks/{task_id}/submissions", json={"note": "已补充供应商确认邮件。"}
    )
    assert second.status_code == 200, second.text
    packet_text = runner.calls[-1]["packet"].text
    assert "【历史整改进展】" in packet_text
    assert "已关闭训练。" in packet_text
    assert "partially_resolved" in packet_text
    assert "请处理合同用途条款。" in packet_text
    assert "请补充供应商确认邮件。" in packet_text


def test_link_attachment_is_never_claimed_as_verified(workbench) -> None:
    client, _app, runner, _case_id, task_id = workbench
    response = client.post(
        f"/api/remediation-tasks/{task_id}/submissions",
        json={
            "note": "已处理。",
            "evidence": [
                {"kind": "link", "label": "供应商公告", "uri": "https://example.com/notice"}
            ],
        },
    )
    assert response.status_code == 200, response.text
    evidence = response.json()["submission"]["evidence"][0]
    assert evidence["parse_status"] == "not_applicable"
    assert evidence["parsed_text"] is None
    assert runner.calls[-1]["packet"].attachment_texts == {}


def test_case_material_evidence_is_grounded_from_frozen_materials(workbench) -> None:
    client, app, runner, case_id, task_id = workbench
    enterprise = app.state.enterprise_store
    version = enterprise.create_material_version(
        case_id=case_id,
        logical_name="vendor_dpa",
        filename="dpa.txt",
        content_type="text/plain",
        object_key=f"cases/{case_id}/dpa.txt",
        sha256="c" * 64,
        byte_size=40,
        uploaded_by="user_test",
        parse_status="ready",
        parsed_text=CONTRACT_QUOTE,
    )
    response = client.post(
        f"/api/remediation-tasks/{task_id}/submissions",
        json={
            "note": "合同已在工作稿中修订。",
            "evidence": [
                {"kind": "case_material", "label": "vendor_dpa", "object_key": version.id}
            ],
        },
    )
    assert response.status_code == 200, response.text
    evidence = response.json()["submission"]["evidence"][0]
    assert evidence["parse_status"] == "ready"
    assert runner.calls[-1]["packet"].attachment_texts == {evidence["id"]: CONTRACT_QUOTE}


def test_frozen_case_material_counts_as_verifiable_evidence_on_review(workbench) -> None:
    """Readable case materials verify a submission just like an upload, so no human note is forced."""
    client, app, _runner, case_id, task_id = workbench
    enterprise = app.state.enterprise_store
    version = enterprise.create_material_version(
        case_id=case_id,
        logical_name="vendor_dpa",
        filename="dpa.txt",
        content_type="text/plain",
        object_key=f"cases/{case_id}/dpa.txt",
        sha256="d" * 64,
        byte_size=40,
        uploaded_by="user_test",
        parse_status="ready",
        parsed_text=CONTRACT_QUOTE,
    )
    submitted = client.post(
        f"/api/remediation-tasks/{task_id}/submissions",
        json={
            "note": "已按合同条款完成整改。",
            "response_choice": "completed",
            "evidence": [
                {"kind": "case_material", "label": "vendor_dpa", "object_key": version.id}
            ],
        },
    ).json()
    accepted = client.post(
        f"/api/remediation-submissions/{submitted['submission']['id']}/review",
        json={"decision": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"


def test_declaration_only_acceptance_still_requires_a_recorded_basis(workbench) -> None:
    client, _app, _runner, _case_id, task_id = workbench
    submitted = client.post(
        f"/api/remediation-tasks/{task_id}/submissions",
        json={"note": "已处理。", "response_choice": "completed"},
    ).json()
    blocked = client.post(
        f"/api/remediation-submissions/{submitted['submission']['id']}/review",
        json={"decision": "accepted"},
    )
    assert blocked.status_code == 422
    assert "人工核验依据" in blocked.json()["detail"]
    accepted = client.post(
        f"/api/remediation-submissions/{submitted['submission']['id']}/review",
        json={"decision": "accepted", "review_note": "已人工核实供应商后台设置。"},
    )
    assert accepted.status_code == 200, accepted.text
