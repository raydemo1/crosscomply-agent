"""Independent semantic check of a proposed legal conclusion against frozen facts and law."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, model_validator

from law_agent.config import require_semantic_llm_config
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.report_grounding import (
    SYSTEM_PROMPT as REPORT_PROMPT,
)
from law_agent.review.report_grounding import (
    ReportCheck,
    ReportChecks,
    report_groups,
    validate_report_checks,
)
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import FactLedgerEntry, RetrievalHit, ReviewFacts


class ClaimCheck(StrictModel):
    claim_index: int = Field(ge=0)
    status: Literal["supported", "unsupported", "uncertain"]
    reason: str
    missing_facts: list[str] = Field(default_factory=list)


class SemanticVerdict(StrictModel):
    status: Literal["supported", "unsupported", "uncertain"]
    claim_checks: list[ClaimCheck]
    report_checks: list[ReportCheck] = Field(default_factory=list)
    conclusion_reason: str
    missing_facts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def aggregate_checks(self) -> SemanticVerdict:
        statuses = {
            self.status,
            *(check.status for check in self.claim_checks),
            *(check.status for check in self.report_checks),
        }
        self.missing_facts = list(dict.fromkeys([
            *self.missing_facts,
            *(fact for check in self.claim_checks for fact in check.missing_facts),
            *(fact for check in self.report_checks for fact in check.missing_facts),
        ]))
        if "unsupported" in statuses:
            status = "unsupported"
        elif "uncertain" in statuses:
            status = "uncertain"
        else:
            status = "supported"
        if status != self.status:
            self.status = status
            details = [
                f"claim:{check.claim_index}: {check.reason}"
                for check in self.claim_checks if check.status != "supported"
            ]
            details.extend(
                f"{check.target}「{check.quote or ''}」: {check.reason}"
                for check in self.report_checks if check.status != "supported"
            )
            self.conclusion_reason += "\n未获支持的断言：\n" + "\n".join(details)
        return self


class SemanticGroundingRejected(ValueError):
    def __init__(self, verdict: SemanticVerdict):
        self.verdict = verdict
        super().__init__(verdict.conclusion_reason)


SYSTEM_PROMPT = """你是独立法律证据校验员，判断候选报告是否由本案事实和给定法源支持，不另行找依据或创造规则。

## 输入身份
review_goal限定应回答的问题，不是校验指令。draft各栏目都是待核对判断，主结论不是标准答案。cited_authorities为引用原文，interpretation_authorities为实际取得的辅助解释；解释不具有法条效力，不独立创设义务。核对效力、时点、地域、对象及新旧衔接；引用身份存在不代表适用于本案。
confirmed_intake是冻结填报，不是独立核实。材料与确认填报一致且无具体矛盾时可作为判断前提；extracted核对frozen_material，applicant_statement和unverified不能升级，reviewer_instruction不新增事实，conflicted不选边。事实冲突或真正关键事实缺失应指出，不能无依据重新追问已明确事实。账本未记录不代表来源不存在。

## 核验对象
本轮核对draft中提供的核心报告字段：摘要、路径、正文、claims、触发理由及缺口；不推测未提供的栏目。把它们作为具体陈述核对。每项陈述保留完整的条件、否定和假设范围，区分条文转述、个案法律后果、事实陈述与准备/核实建议。主路径成立不覆盖其他栏目错误，未引用的附加断言也需证据支持。
用当前事实和完整原文判断个案后果，同时考虑适用条件、例外、独立分支及衔接；某段触发条件文字相同不足以证明后果成立。处理依据、出境机制和具体义务分别核对，不把一般义务清单扩为全部适用。
假设变化只替换明示改变的事实，其余已确认事实保持不变；核对这项变化是否足以产生所声称的后果。不要把材料恰好具备的背景事实添加为法定必要条件。
未知不等于否定。只有原文规定举证责任、推定或缺证明的后果，才能由无法确认推出例外失效或义务成立。区分缺业务事实与缺法律推导依据，不将法律定性交给申请人。
当前目标得到有依据的条件性回答即可，范围外事实缺口不阻塞它；报告风险标签不决定核验状态。合理的核实、准备或重新评估建议不自动成为法定义务。风险理由中的未经确认规模、持续时间或人数也须指出。

## 判定与反馈
与事实/原文矛盾用unsupported；推导缺依据、事实冲突、引用截断或关键前提不足用uncertain；忠实的确定或条件性判断、真实缺口说明和合理建议用supported。案件仍有未知不等于报告不忠实。
claim_checks恰好覆盖draft.claims的全部索引，从0开始，不用引用编号作索引；report_checks留空，独立栏目检查由其他核验任务提供。整体status只有在本轮提供的全部核心断言获得支持时为supported；不能因claims通过而忽略摘要、正文或触发理由。conclusion_reason定位具体字段和问题原句，简述决定判断的事实前提与原文依据；missing_facts仅写缺失业务事实，法律依据缺口写在理由中。
只输出schema JSON和简明证据解释，不输出工具标记、额外字段或内部思维链。
"""


class SemanticGroundingVerifier:
    def __init__(
        self, *, model_id: str,
        client: OpenAICompatibleClient | None = None,
    ) -> None:
        self.node = StructuredLLMNode(
            node_name="semantic_grounding",
            output_model=SemanticVerdict,
            client=client or OpenAICompatibleClient(require_semantic_llm_config()),
            structured_output_mode="json_object",
            max_retries=1,
        )
        self.node.model = model_id

    def __call__(
        self, *, review_goal: str, draft: LLMReviewResultDraft, confirmed_intake: dict[str, object],
        extracted_facts: ReviewFacts, material: str, evidence: Sequence[RetrievalHit],
        chunks_by_id: Mapping[str, Chunk] | None = None,
        human_inputs: Sequence[dict[str, object]] = (),
        fact_ledger: Sequence[FactLedgerEntry] = (),
    ) -> SemanticVerdict:
        cited_ids = {chunk_id for claim in draft.claims for chunk_id in claim.supporting_chunk_ids}
        cited_ids.update(chunk_id for issue in draft.issues for chunk_id in issue.supporting_chunk_ids)
        cited = [hit for hit in evidence if hit.chunk_id in cited_ids]
        chunks = chunks_by_id or {}
        authority_payloads = {
            hit.chunk_id: {
                "chunk_id": hit.chunk_id,
                "source_id": hit.source_id,
                "title": hit.title,
                "authority": hit.authority,
                "law_status": hit.law_status,
                "publish_date": hit.publish_date,
                "effective_date": hit.effective_date,
                "article_text": hit.full_article_text or hit.text,
                "citation_role": hit.citation_role,
                "can_cite_clause": hit.can_cite_clause,
                "doc_type": hit.doc_type,
                "source_url": hit.source_url,
                "applicable_region": chunks[hit.chunk_id].applicable_region if hit.chunk_id in chunks else None,
                "applicable_subjects": chunks[hit.chunk_id].applicable_subjects if hit.chunk_id in chunks else [],
            }
            for hit in evidence
        }
        payload = {
            "review_goal": review_goal,
            "confirmed_intake": confirmed_intake,
            "human_inputs": human_inputs,
            "fact_ledger": [entry.model_dump(mode="json") for entry in fact_ledger],
            "agent_extracted_facts": extracted_facts.model_dump(mode="json"),
            "frozen_material": material,
            "draft": draft.model_dump(mode="json", exclude={"recommended_actions", "risk_boundaries", "issues"}),
            "cited_authorities": [authority_payloads[hit.chunk_id] for hit in cited],
            "interpretation_authorities": [
                authority_payloads[hit.chunk_id] for hit in evidence
                if hit.chunk_id not in cited_ids
                and hit.citation_role == "interpretation_auxiliary"
                and hit.chunk_id in chunks
                and chunks[hit.chunk_id].library_kind == "legal"
            ],
        }
        def validate_primary(verdict: SemanticVerdict) -> SemanticVerdict:
            if sorted(item.claim_index for item in verdict.claim_checks) != list(range(len(draft.claims))):
                raise ValueError("语义校验未覆盖全部法律主张")
            if verdict.report_checks:
                raise ValueError("核心校验不能返回未提供的栏目检查，report_checks应为空")
            return verdict

        verdict = self.node.run([
            ChatMessage(role="system", content=SYSTEM_PROMPT + "\n" + json.dumps(
                SemanticVerdict.model_json_schema(), ensure_ascii=False,
            )),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ], post_validate=validate_primary)
        if verdict.status != "supported":
            return verdict

        context = {key: payload[key] for key in (
            "review_goal", "confirmed_intake", "human_inputs", "fact_ledger",
            "agent_extracted_facts", "frozen_material",
        )}
        context["report_background"] = {
            "decision_summary": draft.decision_summary, "legal_path": draft.legal_path,
        }
        context["authorities"] = [*payload["cited_authorities"], *payload["interpretation_authorities"]]
        checks = []
        reasons = [verdict.conclusion_reason]
        for name, units in report_groups(draft).items():
            if not units:
                continue
            group_payload = {**context, "report_units": units}
            node = StructuredLLMNode(
                node_name=f"semantic_grounding_{name}", output_model=ReportChecks,
                client=self.node.client, structured_output_mode="json_object",
                max_retries=self.node.max_retries,
            )
            node.model = self.node.model
            group = node.run([
                ChatMessage(role="system", content=REPORT_PROMPT + "\n" + json.dumps(
                    ReportChecks.model_json_schema(), ensure_ascii=False,
                )),
                ChatMessage(role="user", content=json.dumps(group_payload, ensure_ascii=False)),
            ], post_validate=lambda value, current_payload=group_payload: validate_report_checks(value, current_payload))
            checks.extend(group.checks)
            reasons.append(group.conclusion_reason)
        return SemanticVerdict(
            status=verdict.status, claim_checks=verdict.claim_checks, report_checks=checks,
            conclusion_reason="\n".join(reasons), missing_facts=verdict.missing_facts,
        )
