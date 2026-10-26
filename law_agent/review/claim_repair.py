"""引用簿记类驳回的定向修复：只重写 claims，不整份重生成报告。

成文后的引用护栏驳回（supporting_chunk_ids 指向不可作为正式法条的材料）是
机械性簿记错误：报告正文与法律推理仍然成立，只有 claims 的引用集合需要调整。
走整份重写一轮约 80-100 秒，且重写可能在新段落引入新的引用错误；这里用一次
小型结构化调用只修正 claims 数组，再走同一护栏校验，失败则回落到原有的
全量修订循环。
"""

from __future__ import annotations

import json
from typing import Callable

from pydantic import Field

from law_agent.config import require_agent_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.result_builder import LLMReviewResultDraft, validate_grounded_claims
from law_agent.review.schemas import GroundedClaimDraft, RetrievalHit

# result_builder.validate_grounded_claims 抛出的引用簿记类 ValueError 前缀。
CLAIM_CITATION_ERROR_PREFIXES = (
    "法律主张引用了不可作为正式法条的材料",
    "法律主张引用了未检索到的法条",
    "法律主张必须引用至少一条正式法条",
)

_CITABLE_CHUNK_LIMIT = 60
_CITABLE_TEXT_PREFIX = 200

_REPAIR_SYSTEM_PROMPT = """你是审查报告的引用修复器。输入包含：一条引用护栏驳回原因、被驳回的 claims 列表、本次审查可引用的法条 chunk 清单（均为 can_cite_clause=true）。
任务：只修正 claims 的 supporting_chunk_ids；某条 claim 确实没有清单内法条支持时整条删除。
规则：
- claim 的 text 一字不改，不新增 claim，不发明 chunk_id。
- 只能引用清单内的 chunk_id；每条 claim 至少保留一条该 claim 文本实际依据的条文。
- claim 的判断若实际依据是参考性材料而非正式法条，删除该 claim，不强行挂靠。
只输出 JSON：{"claims": [{"text": "...", "supporting_chunk_ids": ["..."]}]}'"""


def is_claim_citation_error(message: str) -> bool:
    return message.startswith(CLAIM_CITATION_ERROR_PREFIXES)


class RepairedClaims(StrictModel):
    claims: list[GroundedClaimDraft] = Field(default_factory=list)


def make_claim_repairer(
    *, model_id: str, client: OpenAICompatibleClient | None = None
) -> Callable[[LLMReviewResultDraft, str, list[RetrievalHit]], LLMReviewResultDraft]:
    """构造 run_agent 使用的 repair_claims 回调；失败时抛 ReviewWorkflowFailed。"""

    def repair_claims(
        draft: LLMReviewResultDraft, error_message: str, evidence_hits: list[RetrievalHit]
    ) -> LLMReviewResultDraft:
        node = StructuredLLMNode(
            node_name="claim_repair",
            output_model=RepairedClaims,
            client=client or OpenAICompatibleClient(require_agent_llm_config()),
            max_retries=1,
        )
        citable = [
            {
                "chunk_id": hit.chunk_id,
                "article_no": hit.article_no,
                "title": hit.title,
                "text": hit.text[:_CITABLE_TEXT_PREFIX],
            }
            for hit in evidence_hits
            if hit.can_cite_clause
        ][:_CITABLE_CHUNK_LIMIT]
        messages = [
            ChatMessage(role="system", content=_REPAIR_SYSTEM_PROMPT),
            ChatMessage(role="user", content=json.dumps({
                "rejection": error_message,
                "claims": [claim.model_dump(mode="json") for claim in draft.claims],
                "citable_chunks": citable,
            }, ensure_ascii=False)),
        ]
        parsed = node.run(
            messages,
            post_validate=lambda out: RepairedClaims(
                claims=validate_grounded_claims(out.claims, evidence_hits)
            ),
            post_validation_reason="claim_repair_guardrail",
            post_validation_hint="claims 只能引用清单内 can_cite_clause=true 的法条 chunk_id",
        )
        return draft.model_copy(update={"claims": [
            GroundedClaimDraft(text=claim.text, supporting_chunk_ids=list(claim.supporting_chunk_ids))
            for claim in parsed.claims
        ]})

    return repair_claims
