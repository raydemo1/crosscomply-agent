"""Fact provenance boundaries, exercised without model or retrieval services."""

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from law_agent.review import agent_runtime
from law_agent.review.agent import AgentDecision, AgentModel, AgentState, answer_agent, run_agent
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.fact_provenance import append_fact, confirmed_intake_ledger
from law_agent.review.schemas import FactLedgerEntry, ReviewFacts
from law_agent.review.semantic_grounding import SemanticGroundingVerifier, SemanticVerdict
from law_agent.review.worker import ReviewWorker
from tests.test_review_agent import _draft, _tools_with
from tests.test_review_llm import FakeClient


def _material_entry(field="contains_personal_information", value=True, source_ref="material_1"):
    return FactLedgerEntry(
        field=field, value=value, source_type="material", source_ref=source_ref, status="extracted",
    )


@pytest.mark.parametrize("field,confirmed,extracted,conflict", [
    ("ciio_status", "not_ciio", "ciio", True),
    ("important_data_status", "not_important", "important", True),
    ("ciio_status", "under_review", "ciio", False),
    ("count_period", "annual_estimate", "current_year_cumulative", False),
    ("annual_sensitive_count", "0", "100", False),
])
def test_closed_identity_enums_detect_only_direct_opposites(field, confirmed, extracted, conflict):
    ledger = confirmed_intake_ledger({field: confirmed}, "intake")
    append_fact(ledger, _material_entry(field, extracted))
    assert (ledger[-1].status == "conflicted") is conflict


def test_expanded_working_model_keeps_raw_counts():
    facts = ReviewFacts(ciio_status="not_ciio", important_data_status="under_review", annual_sensitive_count="约 100 人（全年估算）", count_period="annual_estimate", destination_region="日本", exemption_facts="跨境人力资源管理所必需")
    restored = AgentState.model_validate({"goal": "审查", "facts": facts.model_dump(mode="json")})
    assert restored.facts == facts and restored.fact_ledger == [] and restored.fact_questions == []
    assert "legal_path" not in ReviewFacts.model_fields


def _run(state, *decisions, material_snapshot_id="material_1"):
    pending = iter([*decisions, AgentDecision(action="finish", summary="交付", draft=_draft())])
    return run_agent(
        state, material="冻结材料", intake={}, material_snapshot_id=material_snapshot_id,
        decide=lambda *_: next(pending), search=lambda *_: [], web_search=lambda *_: [],
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )


def _report_tools(verifier):
    tools = _tools_with([])
    tools._neighbor_hits = {}
    tools._top_k = 10
    tools._material_text = "冻结材料"
    tools._material_versions_by_id = {}
    tools._semantic_verifier = verifier
    tools.close = lambda: None
    return tools


def _queued_task(store, intake):
    version = store.create_material_version(
        case_id="case_1", logical_name="业务说明", filename="material.txt",
        content_type="text/plain", object_key="case_1/material.txt",
        sha256="a" * 64, byte_size=12, uploaded_by="user_1",
    )
    material = store.create_material_snapshot(
        case_id="case_1", version_ids=[version.id], created_by="user_1",
    )
    snapshot = store.create_intake_snapshot(
        case_id="case_1", material_snapshot_id=material.id, intake=intake, created_by="user_1",
    )
    task = store.enqueue_review_task(
        case_id="case_1", material_snapshot_id=material.id, intake_snapshot_id=snapshot.id,
        model_id="fake-model", data_boundary_summary={},
    )
    return task, snapshot


@pytest.fixture
def runtime(monkeypatch):
    store = InMemoryEnterpriseStore()
    task, snapshot = _queued_task(store, {"cross_border_transfer": False})
    seen = []
    decisions = []

    def decide(state, _intake):
        saved = store.get_task(task.id).agent_state
        assert saved["fact_ledger"] == state.model_dump(mode="json")["fact_ledger"]
        seen.append(state)
        return decisions.pop(0) if decisions else AgentDecision(
            action="request_input", summary="补充事实", question="请说明业务情况",
        )

    tools = _report_tools(lambda **_: SemanticVerdict(
        status="supported", claim_checks=[], conclusion_reason="暂定报告保持证据边界",
    ))
    monkeypatch.setattr(agent_runtime, "AgentModel", lambda **_: decide)
    monkeypatch.setattr(agent_runtime, "ComplianceAgentTools", lambda **_: tools)

    def execute(current):
        return agent_runtime.execute_agent_task(
            current, store=store, goal="审查", material="冻结材料",
        )

    return store, task, snapshot, seen, decisions, execute


def test_intake_initialization_preserves_false_zero_and_uncertainty():
    values = {
        "cross_border_transfer": False, "annual_sensitive_count": "0", "numeric_zero": 0,
        "ciio_status": "under_review", "count_period": "annual_estimate",
        "transfer_mechanism": "标准合同", "notes": "申请人备注",
        "data_types": ["姓名"], "followup_answers": {"question": "待补充"},
        "empty": "  ", "unknown": "unknown", "none": None, "list": [], "dict": {},
    }
    entries = confirmed_intake_ledger(values, "intake_1")
    restored = AgentState.model_validate_json(AgentState(goal="审查", fact_ledger=entries).model_dump_json())
    actual = {entry.field: entry.value for entry in restored.fact_ledger}
    assert actual == {key: values[key] for key in list(values)[:9]}
    assert actual["cross_border_transfer"] is False
    assert type(actual["numeric_zero"]) is int
    assert all(e.status == "confirmed" and e.source_ref == "intake_1" for e in entries)


def test_runtime_initializes_before_decision_and_resume_does_not_repeat(runtime):
    store, task, snapshot, seen, _decisions, execute = runtime
    worker = ReviewWorker(queue=store, worker_id="test", execute=execute)
    paused = worker.run_once()
    assert seen[0].fact_ledger == confirmed_intake_ledger(snapshot.intake, snapshot.id)
    resumed = answer_agent(
        AgentState.model_validate(paused.agent_state), gate_id=paused.agent_state["gate_id"],
        answer="需要进一步核对", provenance="applicant_statement",
    )
    expected = resumed.model_dump(mode="json")["fact_ledger"]
    store.resume_task(task.id, state=resumed.model_dump(mode="json"))
    paused_again = worker.run_once()
    assert seen[1].model_dump(mode="json")["fact_ledger"] == expected
    assert paused_again.agent_state["fact_ledger"] == expected


def test_runtime_resumes_empty_ledger_without_reinitializing(runtime):
    store, task, _snapshot, seen, _decisions, execute = runtime
    state = AgentState(goal="旧任务", facts=ReviewFacts(cross_border_transfer=True))
    checkpoint = state.model_dump(mode="json")
    task.agent_state = checkpoint
    worker = ReviewWorker(queue=store, worker_id="test", execute=execute)
    paused = worker.run_once()
    assert seen[0].goal == "旧任务"
    assert seen[0].facts.cross_border_transfer is True
    assert seen[0].fact_ledger == []
    assert paused.agent_state["fact_ledger"] == []


def test_runtime_rejects_wrong_material_binding_before_model_setup(runtime):
    store, task, snapshot, seen, _decisions, execute = runtime
    task.material_snapshot_id = "different_material"
    with pytest.raises(RuntimeError, match="不匹配"):
        execute(task)
    assert seen == []
    assert store.get_task(task.id).agent_state is None
    task.material_snapshot_id = snapshot.material_snapshot_id


def test_runtime_material_entries_use_task_snapshot_and_preserve_intake_conflict(runtime):
    store, task, snapshot, _seen, decisions, execute = runtime
    decisions.extend([
        AgentDecision(
            action="record_facts", summary="材料显示跨境",
            facts=ReviewFacts(cross_border_transfer=True),
            material_facts=[{"field": "cross_border_transfer", "value": True}],
        ),
        AgentDecision(action="finish", summary="有冲突，暂不判断", draft=_draft()),
    ])
    completed = ReviewWorker(queue=store, worker_id="test", execute=execute).run_once()
    assert completed.status == "succeeded"
    ledger = completed.result["fact_ledger"]
    assert [e["source_ref"] for e in ledger] == [snapshot.id, task.material_snapshot_id]
    assert [e["value"] for e in ledger] == [False, True]
    assert all(e["status"] == "conflicted" for e in ledger)


def test_explicit_material_origin_is_recorded_and_deduplicated_without_changing_projection():
    state = AgentState(goal="审查", facts=ReviewFacts(contains_personal_information=True))
    decision = AgentDecision(
        action="record_facts", summary="材料支持当前事实",
        facts=ReviewFacts(contains_personal_information=True, business_activity="供应商服务"),
        material_facts=[{"field": "contains_personal_information", "value": True}],
    )
    result = _run(state, decision, decision)
    assert result.facts == decision.facts
    assert result.fact_ledger == [_material_entry()]
    assert all(entry.field != "business_activity" for entry in result.fact_ledger)


def test_material_identity_survives_unresolved_working_projection():
    ledger = confirmed_intake_ledger({"ciio_status": "not_ciio"}, "intake_1")
    decision = AgentDecision(
        action="record_facts", summary="材料身份与填报不同，工作事实待核实",
        facts=ReviewFacts(ciio_status="under_review"),
        material_facts=[{"field": "ciio_status", "value": "ciio"}],
    )
    result = _run(AgentState(goal="审查", fact_ledger=ledger), decision, decision)
    restored = AgentState.model_validate_json(result.model_dump_json())

    assert restored.facts.ciio_status == "under_review"
    assert [entry.value for entry in restored.fact_ledger] == ["not_ciio", "ciio"]
    assert [entry.source_ref for entry in restored.fact_ledger] == ["intake_1", "material_1"]
    assert all(entry.status == "conflicted" for entry in restored.fact_ledger)


@pytest.mark.parametrize("observation", [
    {"field": "ciio_status", "value": "perhaps"},
    {"field": "contains_personal_information", "value": ["姓名"]},
    {"field": "annual_sensitive_count", "value": True},
    {"field": "ciio_status", "value": "ciio", "status": "confirmed"},
    {"field": "ciio_status", "value": "ciio", "source_ref": "forged"},
])
def test_material_observation_uses_existing_fact_types_and_program_owned_source(observation):
    with pytest.raises(ValidationError):
        AgentDecision(action="record_facts", summary="提取", facts={}, material_facts=[observation])


def test_applicant_update_without_material_declaration_is_not_relabelled():
    state = AgentState(goal="审查", status="waiting_input", gate_id="input_1")
    answer_agent(state, gate_id="input_1", answer="有个人信息", intake_snapshot_id="intake_1")
    result = _run(state, AgentDecision(
        action="record_facts", summary="更新工作事实",
        facts=ReviewFacts(contains_personal_information=True),
    ))
    assert result.facts.contains_personal_information is True
    assert len(result.fact_ledger) == 1
    entry = result.fact_ledger[0]
    assert (entry.field, entry.source_type, entry.status) == (
        "supplemental_statement", "applicant_statement", "unverified",
    )
    assert entry.source_ref == "input_1"
    assert state.steps[0].observation["gate_id"] == "input_1"


def test_clear_and_control_fields_are_not_material_observations():
    state = AgentState(goal="审查", facts=ReviewFacts(contains_personal_information=True))
    result = _run(state, AgentDecision(
        action="record_facts", summary="未知事实",
        facts=ReviewFacts(missing_information=["人数"], as_of_date="2025-01-01"),
        material_facts=[{"field": "contains_personal_information", "value": None}, {"field": "data_types", "value": []}],
    ))
    assert result.facts.contains_personal_information is None
    assert result.fact_ledger == []
    for field in ["missing_information", "as_of_date", "transfer_mechanism"]:
        with pytest.raises(ValidationError):
            AgentDecision(action="record_facts", summary="无效来源", facts={}, material_facts=[{"field": field, "value": "unknown"}])


def test_missing_material_context_rejects_attribution_without_mutation():
    state = AgentState(goal="审查", facts=ReviewFacts(contains_personal_information=False))
    result = _run(state, AgentDecision(
        action="record_facts", summary="提取材料",
        facts=ReviewFacts(contains_personal_information=True),
        material_facts=[{"field": "contains_personal_information", "value": True}],
    ), material_snapshot_id=None)
    assert result.fact_ledger == []
    assert result.facts.contains_personal_information is False
    assert "冻结材料快照" in result.steps[0].observation["error"]


@pytest.mark.parametrize("forbidden", [
    {"fact_ledger": [{"status": "confirmed"}]}, {"source_ref": "forged"}, {"status": "confirmed"},
])
def test_decision_cannot_choose_trust_or_snapshot_ids(forbidden):
    with pytest.raises(ValidationError):
        AgentDecision(action="record_facts", summary="无效来源", facts={}, **forbidden)


def test_narrow_boolean_conflict_preserves_both_sources_and_survives_checkpoint():
    ledger = confirmed_intake_ledger({"contains_personal_information": False}, "intake_1")
    state = _run(AgentState(goal="审查", fact_ledger=ledger), AgentDecision(
        action="record_facts", summary="材料提取",
        facts=ReviewFacts(contains_personal_information=True),
        material_facts=[{"field": "contains_personal_information", "value": True}],
    ))
    restored = AgentState.model_validate_json(state.model_dump_json())
    assert [e.value for e in restored.fact_ledger] == [False, True]
    assert [e.source_ref for e in restored.fact_ledger] == ["intake_1", "material_1"]
    assert all(e.status == "conflicted" for e in restored.fact_ledger)
    assert state.facts.contains_personal_information is True
    append_fact(restored.fact_ledger, _material_entry())
    assert len(restored.fact_ledger) == 2
    assert all(e.status == "conflicted" for e in restored.fact_ledger)


@pytest.mark.parametrize("field,left,right", [
    ("overseas_recipient", "ABC Singapore", "ABC Pte."),
    ("data_types", ["姓名"], ["姓名", "邮箱"]),
    ("contains_personal_information", "false", True),
    ("contains_personal_information", 0, True),
])
def test_ambiguous_or_differently_typed_values_do_not_trigger_conflict(field, left, right):
    ledger = confirmed_intake_ledger({field: left}, "intake_1")
    append_fact(ledger, _material_entry(field, right))
    assert [e.status for e in ledger] == ["confirmed", "extracted"]


def test_same_value_different_sources_remain_separate_without_promoting_statement():
    ledger = confirmed_intake_ledger({"contains_personal_information": True}, "intake_1")
    statement = FactLedgerEntry(
        field="supplemental_statement", value="材料也支持这一事实",
        source_type="applicant_statement", source_ref="input_1", status="unverified",
    )
    append_fact(ledger, statement)
    append_fact(ledger, _material_entry())
    append_fact(ledger, _material_entry(source_ref="material_2"))
    assert [e.status for e in ledger] == ["confirmed", "unverified", "extracted", "extracted"]


def test_reviewer_and_stale_answers_cannot_add_facts():
    state = AgentState(goal="审查", status="waiting_input", gate_id="input_1")
    before = state.model_dump(mode="json")
    with pytest.raises(ValueError):
        answer_agent(state, gate_id="wrong", answer="补充事实")
    assert state.model_dump(mode="json") == before
    answer_agent(state, gate_id="input_1", answer="请重新阅读材料", provenance="reviewer_instruction")
    assert state.fact_ledger == []
    assert state.steps[0].observation["provenance"] == "reviewer_instruction"
    with pytest.raises(ValueError):
        answer_agent(state, gate_id="input_1", answer="再次回答")
    assert len(state.steps) == 1


def test_model_and_verifier_receive_the_ledger_as_structured_data():
    ledger = confirmed_intake_ledger({"cross_border_transfer": False}, "intake_1")
    state = AgentState(goal="审查", fact_ledger=ledger)
    decider_client = FakeClient(outputs=[{"action": "request_input", "summary": "澄清", "question": "是否出境？"}])
    AgentModel(model_id="fake-model", client=decider_client)(state, {"facts": {}})
    client = FakeClient(outputs=[{"status": "supported", "claim_checks": [], "conclusion_reason": "暂定"}])
    SemanticGroundingVerifier(model_id="fake-model", client=client)(
        review_goal=state.goal,
        draft=_draft(), confirmed_intake={"cross_border_transfer": False},
        extracted_facts=state.facts, material="材料", evidence=[], fact_ledger=ledger,
    )
    expected = state.model_dump(mode="json")["fact_ledger"]
    assert json.loads(decider_client.calls[0][1].content)["state"]["fact_ledger"] == expected
    payload = json.loads(client.calls[0][1].content)
    assert payload["fact_ledger"] == expected
    assert payload["confirmed_intake"]["cross_border_transfer"] is False


@pytest.mark.parametrize("system_abstention", [False, True])
def test_finalizer_sends_ledger_to_verifier_and_preserves_it_in_payload(system_abstention):
    seen = []

    def verifier(**kwargs):
        seen.append(kwargs)
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="暂定")

    tools = _report_tools(verifier)
    ledger = confirmed_intake_ledger({"cross_border_transfer": False}, "intake_1")
    state = AgentState(goal="审查", fact_ledger=ledger)
    result = tools.finalize(
        _draft(), state, case_id="case_1", intake_snapshot={"facts": {}},
        system_abstention=system_abstention,
    )
    expected = state.model_dump(mode="json")["fact_ledger"]
    assert result["fact_ledger"] == expected
    if system_abstention:
        assert seen == []
    else:
        assert seen[0]["fact_ledger"] == ledger
        assert seen[0]["review_goal"] == state.goal
    result["fact_ledger"][0]["status"] = "conflicted"
    assert state.fact_ledger[0].status == "confirmed"


@pytest.mark.parametrize("exhaust_budget", [False, True])
def test_runtime_final_result_and_checkpoint_keep_ledger(runtime, exhaust_budget):
    store, task, snapshot, _seen, decisions, execute = runtime
    if exhaust_budget:
        decisions.append(AgentDecision(action="propose_plan", summary="调查", plan=["核对资料"]))
        task.agent_state = AgentState(
            goal="审查", max_turns=1,
            fact_ledger=confirmed_intake_ledger(snapshot.intake, snapshot.id),
        ).model_dump(mode="json")
    else:
        decisions.append(AgentDecision(action="finish", summary="交付", draft=_draft()))
    worker = ReviewWorker(queue=store, worker_id="test", execute=execute)
    completed = worker.run_once()
    assert completed.status == "succeeded"
    assert completed.result["fact_ledger"] == completed.agent_state["fact_ledger"]
    assert completed.result["fact_ledger"] == [e.model_dump(mode="json") for e in confirmed_intake_ledger(snapshot.intake, snapshot.id)]
    if exhaust_budget:
        assert completed.agent_state["steps"][-1]["action"] == "budget_abstention"


def test_runtime_rejects_intake_from_another_case(runtime):
    store, task, snapshot, seen, _decisions, execute = runtime
    store.intake_snapshots[snapshot.id] = replace(snapshot, case_id="other_case")
    with pytest.raises(RuntimeError, match="不存在"):
        execute(task)
    assert seen == []
