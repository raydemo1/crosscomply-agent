"""Production Agent evaluation runner.

Runs golden cases through the real production runtime —
``agent_runtime.execute_agent_task`` — with an ``InMemoryEnterpriseStore``,
real frozen MaterialSnapshot/IntakeSnapshot/ReviewTask records and the same
pause → scripted human answer → resume sequence production uses
(``answer_agent`` + ``resume_task`` + ``claim_next_task``).

It never calls ``run_service_retrieval``: the point is to evaluate the Agent
delivered to users, not the search pipeline.

Two layers of metrics:
- deterministic checks, computed here without another model;
- optional eval-only structured judge (``StructuredAgentJudge``), never
  imported by production code.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from law_agent.config import RerankMode, require_llm_config
from law_agent.data.citation_policy import has_clause_locator, has_legal_effect
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.agent import AgentState, answer_agent
from law_agent.review.agent_runtime import execute_agent_task
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, ReviewTask
from law_agent.review.evalset.agent_cases import get_agent_cases
from law_agent.review.evalset.agent_schemas import (
    AgentCase,
    AgentCaseResult,
    AgentEvalSummary,
    IllegalCitation,
    JudgeVerdict,
    ScriptedAnswer,
)
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.ids import utc_now_iso
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.web_research import WebSearchResult, is_trusted_official_url

WORKER_ID = "agent-eval"

# execute(task, store) -> post-run AgentState. Tests inject a fake; production
# evaluation uses make_production_executor().
AgentExecutor = Callable[[ReviewTask, InMemoryEnterpriseStore], AgentState]
JudgeCall = Callable[[AgentCase, dict[str, Any], list[str], list[ScriptedAnswer]], JudgeVerdict]


def infrastructure_http_status(message: str) -> int | None:
    match = re.search(r"\bHTTP (401|402|403|408|429|5\d\d)\b", message)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Frozen-input setup (mirrors the production HTTP intake flow)
# ---------------------------------------------------------------------------


def build_case_task(
    store: InMemoryEnterpriseStore,
    case: AgentCase,
    *,
    model_id: str,
) -> ReviewTask:
    version = store.create_material_version(
        case_id=case.case_id,
        logical_name="eval_material",
        filename="eval_material.txt",
        content_type="text/plain",
        object_key=f"agent_eval/{case.case_id}/eval_material.txt",
        sha256=hashlib.sha256(case.material_text.encode("utf-8")).hexdigest(),
        byte_size=len(case.material_text.encode("utf-8")),
        uploaded_by="agent_eval",
        parse_status="ready",
        parser="plain",
        parser_version="eval",
        parsed_text=case.material_text,
    )
    snapshot = store.create_material_snapshot(
        case_id=case.case_id, version_ids=[version.id], created_by="agent_eval"
    )
    intake = IntakePayload().model_dump(mode="json")
    intake.update(case.intake)
    intake_snapshot = store.create_intake_snapshot(
        case_id=case.case_id,
        material_snapshot_id=snapshot.id,
        intake=intake,
        created_by="agent_eval",
    )
    return store.enqueue_review_task(
        case_id=case.case_id,
        material_snapshot_id=snapshot.id,
        intake_snapshot_id=intake_snapshot.id,
        model_id=model_id,
        data_boundary_summary={"evaluation": "production_agent_eval"},
    )


def make_production_executor(
    case: AgentCase,
    *,
    chunks_path: str | object = DEFAULT_CHUNKS_PATH,
    rerank_mode: RerankMode = "off",
    artifacts_dir: Path | None = None,
) -> AgentExecutor:
    """Executor that assembles inputs exactly like ``review.worker`` does."""

    def execute(task: ReviewTask, store: InMemoryEnterpriseStore) -> AgentState:
        snapshot = store.get_material_snapshot(task.material_snapshot_id)
        if snapshot is None:
            raise RuntimeError("material snapshot vanished during agent eval")
        versions = [
            item
            for item in (store.get_material_version(vid) for vid in snapshot.version_ids)
            if item is not None
        ]
        if any(item.parse_status != "ready" for item in versions):
            raise RuntimeError("agent eval material versions must be parsed/ready")
        frozen_material = "\n\n".join(
            f"【材料 {item.logical_name} v{item.version_number} | {item.id}】\n{item.parsed_text}"
            for item in versions
        )
        with ExitStack() as stack:
            effective_chunks = Path(chunks_path)
            if case.controlled_web:
                directory = artifacts_dir or Path(stack.enter_context(TemporaryDirectory()))
                effective_chunks = directory / "controlled_chunks.jsonl"
                prepare_controlled_corpus(case, Path(chunks_path), effective_chunks)
                client = FixedWebClient(case)
                stack.enter_context(patch("law_agent.review.agent_tools.build_web_search_client", return_value=client))
            return execute_agent_task(
                task,
                store=store,
                goal=case.question,
                material=frozen_material,
                material_versions=versions,
                chunks_path=effective_chunks,
                rerank_mode=rerank_mode,
                on_web_findings=lambda findings: None,
            )

    return execute


def prepare_controlled_corpus(case: AgentCase, chunks_path: Path, output: Path) -> None:
    fixture = case.controlled_web
    if fixture is None:
        raise ValueError("controlled corpus requires a Web fixture")
    if any(not is_trusted_official_url(r.url) for r in fixture.results):
        raise ValueError("controlled Web results must use trusted official URLs")
    rows = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    missing = set(fixture.held_out_source_ids) - {row["source_id"] for row in rows}
    if missing:
        raise ValueError(f"held-out sources are absent from the original corpus: {sorted(missing)}")
    remaining = [row for row in rows if row["source_id"] not in fixture.held_out_source_ids]
    if not remaining:
        raise ValueError("controlled corpus cannot be empty")
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in remaining), encoding="utf-8")


class FixedWebClient:
    def __init__(self, case: AgentCase):
        self.results = [WebSearchResult(**r.model_dump()) for r in case.controlled_web.results]

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]:
        return self.results[:max_results]


# ---------------------------------------------------------------------------
# Per-case harness
# ---------------------------------------------------------------------------


def _take_answer(
    answers: list[ScriptedAnswer], gate_id: str | None
) -> ScriptedAnswer | None:
    for answer in answers:
        if answer.gate_id is not None and answer.gate_id == gate_id:
            answers.remove(answer)
            return answer
    for answer in answers:
        if answer.gate_id is None:
            answers.remove(answer)
            return answer
    return None


def _counters_from_state(state: AgentState | None) -> dict[str, int]:
    """Counters from one state.

    Production ``execute_agent_task`` resumes from the checkpointed state, so
    the final state is cumulative: it already contains every step of earlier
    attempts (read_material, request_input, the inserted human_input, ...).
    Counters MUST therefore come from the latest state alone — concatenating
    per-attempt states would double-count the checkpointed history.
    """

    if state is None:
        return {
            "turns": 0,
            "searches": 0,
            "reads": 0,
            "web_searches": 0,
            "enrichment_submissions": 0,
            "request_inputs": 0,
            "duplicate_read_denials": 0,
            "budget_exhausted": 0,
        }
    return {
        "turns": state.turns,
        "searches": state.searches,
        "reads": state.reads,
        "web_searches": state.web_searches,
        "enrichment_submissions": state.enrichment_submissions,
        "request_inputs": sum(
            1 for step in state.steps if step.action == "request_input"
        ),
        "duplicate_read_denials": sum(
            1
            for step in state.steps
            if step.action == "read_evidence" and bool(step.observation.get("error"))
        ),
        "budget_exhausted": int(
            any(step.action == "budget_abstention" for step in state.steps)
        ),
    }


def _result_base(
    case: AgentCase, state: AgentState | None, *, status: str
) -> AgentCaseResult:
    counters = _counters_from_state(state)
    return AgentCaseResult(
        case_id=case.case_id,
        review_status=case.review_status,
        tags=list(case.tags),
        status=status,  # type: ignore[arg-type]
        turns=counters["turns"],
        searches=counters["searches"],
        reads=counters["reads"],
        web_searches=counters["web_searches"],
        enrichment_submissions=counters["enrichment_submissions"],
        request_inputs=counters["request_inputs"],
        duplicate_read_denials=counters["duplicate_read_denials"],
        budget_exhausted=bool(counters["budget_exhausted"]),
        questions_asked=[
            step.summary
            for step in (state.steps if state is not None else [])
            if step.action == "request_input"
        ],
    )


def _evaluate_deterministic(
    case: AgentCase, result: AgentCaseResult, payload: dict[str, Any]
) -> AgentCaseResult:
    rubric = case.rubric
    rr = payload.get("review_result") or {}
    citations = rr.get("citations") or []
    risk_level = rr.get("risk_level")
    result.risk_level = risk_level
    result.freshness_hold = bool(payload.get("freshness_hold"))

    cited_sources = {str(item.get("source_id")) for item in citations if item.get("source_id")}
    result.cited_source_ids = sorted(cited_sources)
    clause_sources: set[str] = set()
    illegal: list[IllegalCitation] = []
    for item in citations:
        if not item.get("can_cite_clause"):
            continue
        source_id = str(item.get("source_id"))
        source = SimpleNamespace(
            library_kind="legal",
            citation_role=item.get("citation_role"),
            doc_type=item.get("doc_type", "law"),
        )
        if source_id in rubric.forbidden_clause_sources:
            illegal.append(
                IllegalCitation(
                    source_id=source_id,
                    chunk_id=str(item.get("chunk_id")),
                    article_no=item.get("article_no"),
                    reason="rubric 禁止该来源作为条款级引用",
                )
            )
        elif not (
            has_legal_effect(source) and has_clause_locator(item.get("article_no"))
        ):
            illegal.append(
                IllegalCitation(
                    source_id=source_id,
                    chunk_id=str(item.get("chunk_id")),
                    article_no=item.get("article_no"),
                    reason="不具备法律效力或缺少条款定位，却被标记为可条款引用",
                )
            )
        else:
            clause_sources.add(source_id)
    result.illegal_clause_citations = illegal
    result.missing_required_sources = [
        source_id for source_id in rubric.must_cover_sources if source_id not in clause_sources
    ]

    abstained = risk_level == "insufficient_evidence"
    result.abstain_correct = (
        abstained == rubric.should_abstain if rubric.should_abstain is not None else None
    )
    if rubric.acceptable_outcomes:
        result.outcome_acceptable = risk_level in rubric.acceptable_outcomes
    result.freshness_hold_correct = result.freshness_hold == rubric.expect_freshness_hold

    reasons: list[str] = []
    if result.abstain_correct is False:
        reasons.append(
            "expected_abstention" if rubric.should_abstain else "unexpected_abstention"
        )
    if result.outcome_acceptable is False:
        reasons.append(f"outcome_not_acceptable:{risk_level}")
    if result.missing_required_sources:
        reasons.append(
            "missing_required_sources:" + ",".join(result.missing_required_sources)
        )
    if illegal:
        reasons.append(f"illegal_clause_citations:{len(illegal)}")
    if not result.freshness_hold_correct:
        reasons.append(
            "freshness_hold_unexpected" if result.freshness_hold else "freshness_hold_expected"
        )
    result.deterministic_fail_reasons = reasons
    result.deterministic_pass = not reasons
    result.overall_pass = result.deterministic_pass
    return result


def run_agent_case(
    case: AgentCase,
    *,
    execute: AgentExecutor,
    model_id: str = "eval-model",
    judge: JudgeCall | None = None,
    store: InMemoryEnterpriseStore | None = None,
) -> AgentCaseResult:
    store = store or InMemoryEnterpriseStore()
    build_case_task(store, case, model_id=model_id)
    pending_answers = list(case.scripted_answers)
    answers_used: list[ScriptedAnswer] = []
    asked_questions: list[str] = []
    started = time.perf_counter()
    # Latest state returned by the executor. Production resumes from the
    # checkpointed AgentState, so the final state is cumulative across
    # attempts; all counters are read from this single state.
    state: AgentState | None = None

    # At most one attempt per pause, plus the final attempt.
    for _ in range(len(case.scripted_answers) + 2):
        task = store.claim_next_task(worker_id=WORKER_ID)
        if task is None:
            raise RuntimeError("agent eval harness: no claimable review task")
        attempt = task.attempt_count
        try:
            state = execute(task, store)
        except ReviewWorkflowFailed as exc:
            latest = store.get_task(task.id)
            if latest and latest.agent_state:
                state = AgentState.model_validate(latest.agent_state)
            blocked = infrastructure_http_status(exc.message) is not None
            result = _result_base(case, state, status="blocked" if blocked else "failed")
            result.failure_node = exc.failed_node
            result.failure_category = exc.reason
            result.failure_message = exc.message
            result.deterministic_fail_reasons = [
                f"workflow_failed:{exc.failed_node}:{exc.reason}"
            ]
            result.overall_pass = None if blocked else False
            result.total_latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001 - eval must never crash the suite
            latest = store.get_task(task.id)
            if latest and latest.agent_state:
                state = AgentState.model_validate(latest.agent_state)
            blocked = infrastructure_http_status(str(exc)) is not None
            result = _result_base(case, state, status="blocked" if blocked else "failed")
            result.failure_node = "agent_runtime"
            result.failure_category = type(exc).__name__
            result.failure_message = str(exc)[:1000]
            result.deterministic_fail_reasons = [f"runtime_error:{type(exc).__name__}"]
            result.overall_pass = None if blocked else False
            result.total_latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        if state.status == "waiting_input":
            store.pause_task(
                task.id,
                state=state.model_dump(mode="json"),
                expected_attempt=attempt,
            )
            asked_questions.append(
                state.pending_question
                or next(
                    (
                        step.summary
                        for step in reversed(state.steps)
                        if step.action == "request_input"
                    ),
                    "",
                )
            )
            answer = _take_answer(pending_answers, state.gate_id)
            if answer is None:
                paused = _result_base(case, state, status="unanswered_gate")
                paused.questions_asked = list(asked_questions)
                paused.scripted_answers_used = len(answers_used)
                paused.scripted_answers_unused = len(pending_answers)
                paused.deterministic_fail_reasons = ["unanswered_gate"]
                paused.overall_pass = None
                paused.total_latency_ms = int((time.perf_counter() - started) * 1000)
                return paused
            answered = answer_agent(
                state,
                gate_id=state.gate_id,
                answer=answer.answer,
                provenance=answer.provenance,
                intake_snapshot_id=task.intake_snapshot_id,
            )
            store.resume_task(task.id, state=answered.model_dump(mode="json"))
            answers_used.append(answer)
            continue

        store.complete_task(
            task.id,
            result=state.result or {},
            final_node="completed",
            expected_attempt=attempt,
        )
        break
    else:
        raise RuntimeError("agent eval harness: attempt loop exceeded scripted answers")

    if state is None or not state.result:
        raise RuntimeError("agent eval harness: agent finished without a result payload")

    result = _result_base(case, state, status="succeeded")
    # The cumulative final state already contains the request_input steps in
    # production; prefer the questions captured at pause moments (one entry per
    # actual pause) as a guard against executors that lose step history.
    if asked_questions:
        result.questions_asked = asked_questions
    result.scripted_answers_used = len(answers_used)
    result.scripted_answers_unused = len(pending_answers)
    result = _evaluate_deterministic(case, result, state.result)
    if judge is not None:
        verdict = judge(case, state.result, result.questions_asked, answers_used)
        result.judge = verdict
        # Overall precedence: a deterministic failure is a proven Agent error
        # and must never be masked by a later judge infra failure.
        if not result.deterministic_pass:
            result.overall_pass = False
        elif verdict.error is not None or verdict.overall_pass is None:
            # Judge infrastructure failure: neither an Agent failure nor a pass.
            result.overall_pass = None
        else:
            result.overall_pass = bool(verdict.overall_pass)
    elif case.rubric.should_abstain is None and result.deterministic_pass:
        result.overall_pass = None
    result.total_latency_ms = int((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# Eval-only structured judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """你是企业数据出境合规评测员，只评测单个 Agent 的最终交付，不重新做审查。
依据：案件问题、申请人确认事实（如有）、Agent 的追问与回答记录、最终结论及其法源引用。
按 rubric 逐维判定：
- legal_correctness: 法律路径与结论是否正确（门槛、并列条件、路径选择）。
- exception_coverage: required_exceptions 中的每个豁免/例外是否被独立核对。
- fact_grounding: 是否把未经确认的事实当作已确认（对照 must_not_assume）。
- clarification_quality: 追问或报告明确披露的事实缺口与核实建议是否对准 clarification_expectations；它描述需澄清的内容，不自动要求调用 request_input。只有案件问题明确要求先追问时，才检查这一交互要求；无澄清必要时给 not_applicable。
allowed_judgments、forbidden_judgments 是法律判断边界，不是固定答案句式；不得按字面匹配评分。
逐项核对forbidden_judgments、required_exceptions及must_not_assume。明确违反判断边界或遗漏必要例外时，把具体断言与所违反的约束记入rubric_violations，相应维度标fail，总评不通过；不能因主机制正确就降为minor_issue。minor_issue只用于不违反这些约束且不改变义务或路径的轻微问题，不能用它接受报告自行增加的错误义务。
核查范围包括摘要、主结论、legal_path、问题发现、recommended_actions及risk_boundaries中的假设分支。事实变化时须保留未改变且仍成立的例外；不要将案情中恰好具备的事实当作法定必要条件。理由中识别的明确违反应与rubric_violations、维度和总评一致。
按问题限定的范围判断；确定一个出境机制不等于确定整个业务合规。风险等级不能替代法律路径正确性。
should_abstain=null 表示按 allowed_judgments 与 forbidden_judgments 评估是否诚实保留不确定性，不要求固定风险等级。法条引用准确仍须检查个案推理：未确认某个例外要件不等于已确认其不满足；区分条件性判断、暂行准备建议与确定法律义务。核对摘要、主结论、legal_path、issues 与条件说明是否一致，不能仅凭末尾“以后可能改变”的说明接受前文的确定结论。
不规定检索、阅读顺序或必须追问；直接披露阻塞性缺口并诚实abstain也可满足澄清要求。
申请人补充回答不自动成为已确认事实；不得据此奖赏无证据的确定结论。
fact_ledger是最终交付的来源账本，不是正确答案；confirmed只表示确认填报，extracted是材料提取，unverified未核实，conflicted须保留矛盾，不可据状态直接认定法律结论成立。
reference_basis是候选rubric的法源核对记录，不代表已有人类审定；判断不确定时说明，不凭模型记忆扩大法律义务。
不相关的维度给 not_applicable。reasons 每条一行短评，不输出思维链，只输出 schema JSON。"""


class StructuredAgentJudge:
    """LLM-as-judge used only by the agent eval CLI; never used in production."""

    def __init__(
        self,
        *,
        model_id: str | None = None,
        client: OpenAICompatibleClient | None = None,
        max_retries: int = 2,
        on_input: Callable[[list[ChatMessage]], None] | None = None,
    ) -> None:
        config = require_llm_config()
        self.node = StructuredLLMNode(
            node_name="agent_eval_judge",
            output_model=JudgeVerdict,
            client=client or OpenAICompatibleClient(config),
            max_retries=max_retries,
        )
        if model_id:
            self.node.model = model_id
        self.model_id = model_id or config.model
        self.on_input = on_input

    def __call__(
        self,
        case: AgentCase,
        payload: dict[str, Any],
        questions_asked: list[str],
        answers_used: list[ScriptedAnswer],
    ) -> JudgeVerdict:
        try:
            rr = payload.get("review_result") or {}
            rubric = case.rubric
            cited_chunk_ids = {c.get("chunk_id") for c in rr.get("citations", [])}
            cited_chunk_ids.update(
                chunk_id
                for claim in rr.get("claims", [])
                for chunk_id in claim.get("supporting_chunk_ids", [])
            )
            delivery = {
                "review_result": rr,
                "fact_ledger": payload.get("fact_ledger", []),
                "supporting_evidence": [
                    {key: chunk.get(key) for key in ("chunk_id", "source_id", "article_no", "text", "source_url", "can_cite_clause")}
                    for chunk in payload.get("evidence_chunks", [])
                    if chunk.get("chunk_id") in cited_chunk_ids
                ],
                "freshness_hold": payload.get("freshness_hold", False),
                "web_impact": payload.get("web_impact"),
                "material_web_urls": payload.get("material_web_urls", []),
                "web_findings": payload.get("web_findings", []),
            }
            user_lines = [
                f"【案件问题】{case.question}",
                f"【材料】{case.material_text}",
                f"【确认事实】{case.intake or '（无）'}",
                "【Rubric】",
                f"- should_abstain: {json.dumps(rubric.should_abstain)}",
                f"- acceptable_outcomes: {rubric.acceptable_outcomes or '（不限）'}",
                f"- must_cover_sources: {rubric.must_cover_sources}",
                f"- required_exceptions: {rubric.required_exceptions or '（无）'}",
                f"- must_not_assume: {rubric.must_not_assume or '（无）'}",
                f"- clarification_expectations: {rubric.clarification_expectations or '（无）'}",
                f"- allowed_judgments: {rubric.allowed_judgments or '（无）'}",
                f"- forbidden_judgments: {rubric.forbidden_judgments or '（无）'}",
                f"- notes: {rubric.notes}",
                f"【Rubric法源核对记录】{case.reference_basis}",
                f"【Agent 追问】{questions_asked or '（无）'}",
                "【预设人工回答】"
                + (
                    "；".join(f"{a.provenance}: {a.answer}" for a in answers_used)
                    or "（无）"
                ),
                "【最终结果】",
                f"- risk_level: {rr.get('risk_level')}",
                f"- legal_path: {rr.get('legal_path')}",
                f"- conclusion: {rr.get('conclusion')}",
                f"- trigger_reasons: {rr.get('trigger_reasons')}",
                f"- missing_information: {rr.get('missing_information')}",
                f"- citations: {[c.get('source_id') for c in rr.get('citations', [])]}",
                "【完整交付及引用内容】" + json.dumps(delivery, ensure_ascii=False, default=str),
            ]
            messages = [ChatMessage(role="system", content=_JUDGE_SYSTEM), ChatMessage(role="user", content="\n".join(user_lines))]
            if self.on_input:
                self.on_input(messages)
            verdict = self.node.run(messages)
            verdict.judge_model = self.model_id
            return verdict
        except Exception as exc:  # noqa: BLE001 - judge infra failure is diagnostic
            return JudgeVerdict(judge_model=self.model_id, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Suite driver
# ---------------------------------------------------------------------------


def run_agent_evaluation(
    *,
    suite: str = "smoke",
    cases: list[AgentCase] | None = None,
    agent_model: str,
    judge_model: str | None = None,
    use_judge: bool = True,
    chunks_path: str | object = DEFAULT_CHUNKS_PATH,
    rerank_mode: RerankMode = "off",
) -> AgentEvalSummary:
    custom_cases = cases is not None
    cases = cases if custom_cases else get_agent_cases(suite)
    judge: JudgeCall | None = None
    if use_judge:
        # Default the judge to the same model as the Agent under test; use
        # --judge-model for a deliberately different judge model.
        resolved_judge_model = judge_model or agent_model
        judge = StructuredAgentJudge(model_id=resolved_judge_model)
    else:
        resolved_judge_model = None

    results: list[AgentCaseResult] = []
    for case in cases:
        executor = make_production_executor(
            case, chunks_path=chunks_path, rerank_mode=rerank_mode
        )
        results.append(
            run_agent_case(case, execute=executor, model_id=agent_model, judge=judge)
        )

    completed = [item for item in results if item.status == "succeeded"]
    judged = [item for item in results if item.judge is not None]
    total = len(results)
    return AgentEvalSummary(
        generated_at=utc_now_iso(),
        suite="custom" if custom_cases else suite,
        agent_model=agent_model,
        judge_model=resolved_judge_model,
        rerank_mode=rerank_mode,
        chunks_path=str(chunks_path),
        results=results,
        total_cases=total,
        completed_cases=len(completed),
        deterministic_pass_count=sum(item.deterministic_pass for item in results),
        judge_pass_count=sum(
            1 for item in judged if item.judge is not None and item.judge.overall_pass is True
        ),
        judge_error_count=sum(
            1 for item in judged if item.judge is not None and item.judge.error is not None
        ),
        overall_pass_count=sum(1 for item in results if item.overall_pass is True),
        overall_fail_count=sum(1 for item in results if item.overall_pass is False),
        overall_unevaluated_count=sum(
            1 for item in results if item.overall_pass is None
        ),
        mean_turns=(sum(item.turns for item in results) / total) if total else 0.0,
        mean_searches=(sum(item.searches for item in results) / total) if total else 0.0,
        mean_reads=(sum(item.reads for item in results) / total) if total else 0.0,
        total_web_searches=sum(item.web_searches for item in results),
        total_request_inputs=sum(item.request_inputs for item in results),
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def format_summary_text(summary: AgentEvalSummary) -> str:
    lines = [
        "=" * 70,
        f"Production Agent Eval — suite={summary.suite} ({summary.generated_at})",
        f"Agent model: {summary.agent_model} | Judge: {summary.judge_model or 'disabled'}",
        f"Corpus: {summary.chunks_path} | Rerank: {summary.rerank_mode}",
        "=" * 70,
        f"Cases: {summary.total_cases} | completed: {summary.completed_cases}",
        "Review status: " + ", ".join(
            f"{status}={sum(r.review_status == status for r in summary.results)}"
            for status in ("candidate", "approved", "framework_check")
        ),
        f"Deterministic pass: {summary.deterministic_pass_count}/{summary.total_cases}",
        (
            f"Judge pass: {summary.judge_pass_count}/{summary.total_cases}"
            f" (judge errors: {summary.judge_error_count})"
        ),
        (
            f"Overall: PASS {summary.overall_pass_count} / "
            f"FAIL {summary.overall_fail_count} / "
            f"UNEVALUATED {summary.overall_unevaluated_count} "
            f"(of {summary.total_cases})"
        ),
        (
            f"Means — turns: {summary.mean_turns:.1f}, "
            f"searches: {summary.mean_searches:.1f}, "
            f"reads: {summary.mean_reads:.1f} | web: {summary.total_web_searches}, "
            f"request_input: {summary.total_request_inputs}"
        ),
    ]
    for item in summary.results:
        if item.judge is None:
            verdict = "judge=skip"
        elif item.judge.error or item.judge.overall_pass is None:
            verdict = "judge=unev"
        else:
            verdict = "judge=pass" if item.judge.overall_pass else "judge=fail"
        overall_label = (
            "PASS" if item.overall_pass is True
            else "UNEV" if item.overall_pass is None
            else "FAIL"
        )
        lines.append(
            f"  [{overall_label}] {item.case_id} "
            f"status={item.status} risk={item.risk_level or '-'} {verdict} "
            f"turns={item.turns} asks={item.request_inputs} "
            f"reasons={item.deterministic_fail_reasons}"
        )
    return "\n".join(lines)


def format_summary_markdown(summary: AgentEvalSummary) -> str:
    lines = [
        f"# Production Agent Evaluation — `{summary.suite}`",
        "",
        f"- Generated: `{summary.generated_at}`",
        f"- Agent model: `{summary.agent_model}`",
        f"- Judge model: `{summary.judge_model or 'disabled'}`",
        f"- Corpus: `{summary.chunks_path}` (rerank: `{summary.rerank_mode}`)",
        "- Review status: " + ", ".join(
            f"`{status}`={sum(r.review_status == status for r in summary.results)}"
            for status in ("candidate", "approved", "framework_check")
        ),
        "- 候选案例的 PASS 仅代表满足当前候选 rubric；未经人工审定不得称为法律可靠性 Golden 指标。",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total cases | {summary.total_cases} |",
        f"| Completed | {summary.completed_cases} |",
        f"| Deterministic pass | {summary.deterministic_pass_count}/{summary.total_cases} |",
        f"| Judge pass | {summary.judge_pass_count}/{summary.total_cases} |",
        f"| Judge errors | {summary.judge_error_count} |",
        (
            f"| Overall — PASS / FAIL / UNEVALUATED | "
            f"{summary.overall_pass_count} / {summary.overall_fail_count} / "
            f"{summary.overall_unevaluated_count} (of {summary.total_cases}) |"
        ),
        f"| Mean turns / searches / reads | {summary.mean_turns:.1f} / {summary.mean_searches:.1f} / {summary.mean_reads:.1f} |",
        f"| Web searches / request_input (total) | {summary.total_web_searches} / {summary.total_request_inputs} |",
        "",
        "## Cases",
        "",
        "| Case | Overall | Status | Risk | Det. | Judge | Turns | Searches | Reads | Asks | Reasons |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for item in summary.results:
        if item.judge is None:
            judge_cell = "-"
        elif item.judge.error or item.judge.overall_pass is None:
            judge_cell = "unevaluated"
        else:
            judge_cell = "pass" if item.judge.overall_pass else "fail"
        overall_cell = (
            "PASS" if item.overall_pass is True
            else "UNEVALUATED" if item.overall_pass is None
            else "FAIL"
        )
        lines.append(
            f"| {item.case_id} | {overall_cell} | {item.status} | {item.risk_level or '-'} | "
            f"{'pass' if item.deterministic_pass else 'fail'} | {judge_cell} | "
            f"{item.turns} | {item.searches} | {item.reads} | {item.request_inputs} | "
            f"{'<br>'.join(item.deterministic_fail_reasons) or '-'} |"
        )
    judge_rows = [item for item in summary.results if item.judge is not None]
    if judge_rows:
        lines.extend(["", "## Judge detail", ""])
        for item in judge_rows:
            verdict = item.judge
            assert verdict is not None
            head = f"### {item.case_id}"
            if verdict.error:
                lines.extend([head, "", f"- judge error: `{verdict.error}`", ""])
                continue
            lines.extend(
                [
                    head,
                    "",
                    f"- legal_correctness: `{verdict.legal_correctness}`",
                    f"- exception_coverage: `{verdict.exception_coverage}`",
                    f"- fact_grounding: `{verdict.fact_grounding}`",
                    f"- clarification_quality: `{verdict.clarification_quality}`",
                    f"- overall_pass: **{verdict.overall_pass}**",
                ]
            )
            lines.extend(f"  - {reason}" for reason in verdict.reasons)
            lines.append("")
    return "\n".join(lines)
