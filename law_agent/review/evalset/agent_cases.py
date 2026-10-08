"""Production Agent evaluation cases.

Case material is NOT copied: ``case_id``/``question``/``material_text`` are
resolved from the existing retrieval ``EvalScenario`` golden set, and only the
Agent-specific rubric is defined here.

Slice 2A ships the ``smoke`` suite (framework self-check). The 15-20 hand-picked
``core`` cases (article-5 exemption, quantity thresholds, sensitive PI,
important data, CIIO, local/industry rules, missing facts, material conflicts,
historical points in time, freshness, must-abstain, ...) are added in Slice 2B
after reviewing first real-model runs.
"""

from __future__ import annotations

from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric, ScriptedAnswer
from law_agent.review.evalset.cases import get_scenarios

# Smoke rubrics stay small: they exercise the harness end to end, including a
# case the Agent must abstain on.
_SMOKE_RUBRICS: dict[str, AgentRubric] = {
    "eval_cross_border_001": AgentRubric(
        # This smoke case validates the pause/resume mechanism, not a
        # definitive legal conclusion. The scripted reply is an unverified
        # applicant_statement: under production fact-provenance rules it cannot
        # upgrade the missing counts into confirmed facts, so the honest close
        # after resume is still insufficient_evidence. Cases where confirmed
        # intake/frozen facts justify a definitive conclusion arrive in 2B;
        # "追问后事实升级为 confirmed → 确定结论" waits for Slice 3 Fact
        # Provenance.
        should_abstain=True,
        acceptable_outcomes=["insufficient_evidence"],
        must_not_assume=[
            "不得把 50 万用户直接等同于已满足或已不满足任何申报门槛；人数口径与统计期间未经确认不得当作已确认事实",
            "申请人在追问中的口头补充属于 applicant_statement，未经核实不得作为确定法律路径的前提",
        ],
        clarification_expectations=[
            "应追问统计期间、个人信息与敏感个人信息的具体数量、是否为 CIIO、是否涉及重要数据",
        ],
        notes="50 万用户、手机号与定位信息出境新加坡。验证 request_input → 预设回答 → resume 后以证据不足诚实收口。",
    ),
    "eval_out_of_corpus_001": AgentRubric(
        should_abstain=True,
        acceptable_outcomes=["insufficient_evidence"],
        notes="EU AI Act 不在受控库；Agent 必须 abstain，不得用中国法源强行作答。",
    ),
}

_SMOKE_ANSWERS: dict[str, list[ScriptedAnswer]] = {
    # Drives the pause/resume path: the Agent asks for the missing counts, the
    # harness supplies this answer, and the run resumes. The answer stays an
    # unverified applicant_statement, so the resumed run is expected to close
    # with insufficient_evidence rather than treating it as confirmed fact.
    "eval_cross_border_001": [
        ScriptedAnswer(
            answer=(
                "统计期间为上一年度 1 月 1 日至 12 月 31 日；"
                "该期间向境外提供个人信息累计约 50 万人，"
                "其中含持续采集的精确位置信息，敏感个人信息累计约 50 万人，"
                "手机号按一般个人信息处理；"
                "公司不是关键信息基础设施运营者，未处理重要数据。"
            ),
            provenance="applicant_statement",
        )
    ],
}

# Slice 2B: register hand-picked core case ids here, one AgentRubric each.
_CORE_RUBRICS: dict[str, AgentRubric] = {}
_CORE_ANSWERS: dict[str, list[ScriptedAnswer]] = {}

SMOKE_CASE_IDS: tuple[str, ...] = tuple(_SMOKE_RUBRICS)


def _build_cases(
    rubrics: dict[str, AgentRubric],
    answers: dict[str, list[ScriptedAnswer]],
) -> list[AgentCase]:
    scenarios = {scenario.case_id: scenario for scenario in get_scenarios("full")}
    cases: list[AgentCase] = []
    for case_id, rubric in rubrics.items():
        scenario = scenarios.get(case_id)
        if scenario is None:
            raise ValueError(f"agent eval rubric references unknown scenario: {case_id}")
        cases.append(
            AgentCase(
                case_id=scenario.case_id,
                question=scenario.question,
                material_text=scenario.material_text,
                rubric=rubric,
                scripted_answers=list(answers.get(case_id, ())),
                tags=list(scenario.tags),
            )
        )
    return cases


def get_agent_cases(suite: str) -> list[AgentCase]:
    if suite == "smoke":
        return _build_cases(_SMOKE_RUBRICS, _SMOKE_ANSWERS)
    if suite == "core":
        return _build_cases(_CORE_RUBRICS, _CORE_ANSWERS)
    raise ValueError(f"unknown agent eval suite: {suite!r}")
