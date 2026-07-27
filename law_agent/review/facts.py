"""LLM-owned review fact extraction."""

from __future__ import annotations

import json
from collections.abc import Callable

from law_agent.config import require_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.schemas import ReviewFacts

FactsExtractor = Callable[[str, str | None], ReviewFacts]
_LLM_PROMPT_VERSION = "0.1.0"


class LLMReviewFacts(StrictModel):
    """Required-field schema for LLM fact extraction output."""

    business_activity: str | None
    data_types: list[str]
    sensitive_personal_info: bool | None
    cross_border_transfer: bool | None
    overseas_recipient: str | None
    processing_purpose: str | None
    legal_basis_or_consent: str | None
    industry: str | None
    region: str | None
    missing_information: list[str]


def build_fact_extraction_messages(
    material_text: str,
    question: str | None = None,
) -> list[ChatMessage]:
    """Build a DeepSeek JSON prompt for fact extraction."""

    json_example = {
        "business_activity": "移动 App 个性化推荐和数据分析",
        "data_types": ["手机号", "定位信息", "设备标识"],
        "sensitive_personal_info": True,
        "cross_border_transfer": True,
        "overseas_recipient": "新加坡数据分析服务商",
        "processing_purpose": "推荐优化和行为分析",
        "legal_basis_or_consent": None,
        "industry": None,
        "region": "CN",
        "missing_information": ["legal_basis_or_consent", "data_volume_threshold"],
    }
    user_payload = {
        "prompt_version": _LLM_PROMPT_VERSION,
        "question": question,
        "material_text": material_text[:6000],
        "json_example": json_example,
        "instructions": [
            "只基于用户材料抽取事实，不要推测材料中没有的信息。",
            "问题里的假设或法名不等于材料事实；例如用户问“是否出境/是否触发安全评估”时，除非材料明确说明出境安排，否则 cross_border_transfer 为 null。",
            "必须输出合法 json object，字段必须与 json_example 完全一致。",
            "未检测到的事实用 null，列表字段用 []。",
            "missing_information 只列出仍需用户补充的事实键。",
            "当材料含「一些」「某些」「未确定」「没有说明」等模糊限定词时，不要将对应字段设为 true；应把缺口放入 missing_information。",
        ],
    }
    return [
        ChatMessage(
            role="system",
            content=(
                "你是法律合规审查事实抽取助手。"
                "只输出 json，不输出解释、markdown 或自然语言。"
            ),
        ),
        ChatMessage(role="user", content=json.dumps(user_payload, ensure_ascii=False)),
    ]


def extract_facts_with_deepseek(
    material_text: str,
    question: str | None = None,
    *,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
) -> ReviewFacts:
    """Extract ``ReviewFacts`` using DeepSeek with strict validation."""

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=LLMReviewFacts,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
    )
    output = node.run(build_fact_extraction_messages(material_text, question))
    return ReviewFacts.model_validate(output.model_dump(), strict=True)


extract_facts_with_llm = extract_facts_with_deepseek
