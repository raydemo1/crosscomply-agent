"""Independent semantic check of a proposed legal conclusion against frozen facts and law."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from law_agent.config import require_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import RetrievalHit, ReviewFacts


class ClaimCheck(StrictModel):
    claim_index: int = Field(ge=0)
    status: Literal["supported", "unsupported", "uncertain"]
    reason: str
    missing_facts: list[str] = Field(default_factory=list)


class SemanticVerdict(StrictModel):
    status: Literal["supported", "unsupported", "uncertain"]
    claim_checks: list[ClaimCheck]
    conclusion_reason: str
    missing_facts: list[str] = Field(default_factory=list)


class SemanticGroundingRejected(ValueError):
    def __init__(self, verdict: SemanticVerdict):
        self.verdict = verdict
        super().__init__(verdict.conclusion_reason)


SYSTEM_PROMPT = """你是独立的法律证据校验员。只检查候选报告是否由给定的完整法条和已确认事实支持，不另行创造法律规则。
逐条检查 claim，同时审查 decision_summary、conclusion、trigger_reasons 和 issues 中的法律断言；不能只看引用编号。
特别核对同条的并列条件、例外、前提、否定范围，以及人数和日期的统计口径。某一个免予分支不成立，不能推出所有分支均不成立。
申请人填报的拟采用路径只是主张；未知或未确认字段不是否定事实。材料摘录和 Agent 提取事实若与申请人确认事实冲突，标为 uncertain。
如引用只覆盖法条的一部分、正文被截断、事实不足或其他可能适用的分支未调查，标为 uncertain；如结论与法条或事实矛盾，标为 unsupported。
只在所有法律断言均获得支持，且结论没有遗漏会改变路径的条件时，overall status 才可为 supported。
claim_checks 必须恰好覆盖每个 claim_index，从 0 开始。证据不足结论可以没有 claim，但仍需核查其措辞是否保持暂定。
只输出符合 schema 的 JSON。"""


class SemanticGroundingVerifier:
    def __init__(
        self, *, model_id: str,
        client: OpenAICompatibleClient | None = None,
    ) -> None:
        self.node = StructuredLLMNode(
            node_name="semantic_grounding",
            output_model=SemanticVerdict,
            client=client or OpenAICompatibleClient(require_llm_config()),
            structured_output_mode="json_object",
            max_retries=1,
        )
        self.node.model = model_id

    def __call__(
        self, *, draft: LLMReviewResultDraft, confirmed_intake: dict[str, object],
        extracted_facts: ReviewFacts, material: str, evidence: Sequence[RetrievalHit],
    ) -> SemanticVerdict:
        cited_ids = {chunk_id for claim in draft.claims for chunk_id in claim.supporting_chunk_ids}
        cited = [hit for hit in evidence if hit.chunk_id in cited_ids]
        payload = {
            "confirmed_intake": confirmed_intake,
            "agent_extracted_facts": extracted_facts.model_dump(mode="json"),
            "frozen_material": material,
            "draft": draft.model_dump(mode="json"),
            "cited_authorities": [
                {"chunk_id": hit.chunk_id, "title": hit.title,
                 "article_text": hit.full_article_text or hit.text,
                 "citation_role": hit.citation_role, "source_url": hit.source_url}
                for hit in cited
            ],
        }
        verdict = self.node.run([
            ChatMessage(role="system", content=SYSTEM_PROMPT + "\n" + json.dumps(
                SemanticVerdict.model_json_schema(), ensure_ascii=False,
            )),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ])
        indices = [item.claim_index for item in verdict.claim_checks]
        if sorted(indices) != list(range(len(draft.claims))):
            raise ValueError("语义校验未覆盖全部法律主张")
        if verdict.status == "supported" and any(item.status != "supported" for item in verdict.claim_checks):
            raise ValueError("语义校验整体结论与逐条结果冲突")
        return verdict
