"""LLM-owned retrieval query planning."""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import Field, field_validator

from law_agent.config import require_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.schemas import (
    IssueDraft,
    RetrievalQuery,
    RetrievalQueryType,
    ReviewFacts,
)

QueryPlanner = Callable[[str, ReviewFacts, str | None], list[RetrievalQuery]]


class _QueryIdGenerator:
    """Deterministic counter-based query ID generator."""

    def __init__(self) -> None:
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"q_{self._counter}"


_VALID_QUERY_TYPES: tuple[RetrievalQueryType, ...] = (
    "legal_issue",
    "material_fact",
    "region_condition",
    "industry_condition",
    "missing_information",
)

_CONTROLLED_LEGAL_PATHWAY_ANCHORS: list[dict[str, object]] = [
    {
        "pathway": "cross_border_foundation",
        "when_to_use": "用户询问数据出境基础规则、总体合规路径、豁免边界、不同路径关系，或地方/行业规则与全国规则的关系。",
        "query_type": "legal_issue",
        "query_terms": [
            "促进和规范数据跨境流动规定",
            "数据出境",
            "基础规则",
            "豁免情形",
            "适用范围",
        ],
    },
    {
        "pathway": "security_assessment",
        "when_to_use": "问题或事实涉及申报、网信部门、安全评估、重要数据、数量阈值或大规模个人信息。",
        "query_type": "legal_issue",
        "query_terms": [
            "数据出境安全评估办法",
            "数据出境安全评估申报指南",
            "申报条件",
            "重要数据",
            "个人信息数量阈值",
        ],
    },
    {
        "pathway": "post_assessment_obligations",
        "when_to_use": "问题或事实涉及已通过安全评估后的持续义务、重新申报、变更、监督检查或评估有效期。",
        "query_type": "legal_issue",
        "query_terms": [
            "数据出境安全评估办法",
            "数据出境安全评估",
            "重新申报",
            "变更",
            "后续义务",
            "监督管理",
        ],
    },
    {
        "pathway": "standard_contract",
        "when_to_use": "问题或事实涉及标准合同、备案、合同文本或境外接收方义务。",
        "query_type": "legal_issue",
        "query_terms": [
            "个人信息出境标准合同办法",
            "个人信息出境标准合同范本",
            "备案指南",
            "境外接收方义务",
        ],
    },
    {
        "pathway": "certification",
        "when_to_use": "问题或事实涉及个人信息保护认证、集团内部跨境共享、认证路径、粤港澳/大湾区跨境个人信息或认证路径下的责任。",
        "query_type": "legal_issue",
        "query_terms": [
            "个人信息出境个人信息保护认证办法",
            "个人信息保护认证实施规则",
            "个人信息跨境处理活动安全认证规范",
            "认证路径",
            "境外接收方责任",
        ],
    },
    {
        "pathway": "sensitive_personal_info",
        "when_to_use": "问题或事实涉及敏感个人信息、人脸识别、生物识别、儿童个人信息、精确定位、行踪轨迹或敏感个人信息处理要求。",
        "query_type": "legal_issue",
        "query_terms": [
            "个人信息保护法",
            "敏感个人信息",
            "生物识别",
            "儿童个人信息",
            "精确定位",
            "敏感个人信息处理安全要求",
            "敏感个人信息识别指南",
        ],
    },
    {
        "pathway": "remote_access_boundary",
        "when_to_use": "问题或事实涉及境外远程访问境内数据、VPN 查看境内数据库、没有批量下载但境外可访问，或询问是否属于数据出境边界。",
        "query_type": "legal_issue",
        "query_terms": [
            "数据出境安全评估办法",
            "数据出境安全评估办法答记者问",
            "促进和规范数据跨境流动规定",
            "远程访问",
            "境外访问",
            "数据出境",
        ],
    },
    {
        "pathway": "pathway_conflict",
        "when_to_use": "问题或事实同时涉及标准合同、安全评估门槛、认证或多条个人信息出境路径，询问哪条路径优先或如何选择。",
        "query_type": "legal_issue",
        "query_terms": [
            "数据出境安全评估办法",
            "个人信息出境标准合同办法",
            "促进和规范数据跨境流动规定答记者问",
            "安全评估",
            "标准合同",
            "适用条件",
            "路径优先",
        ],
    },
]

_CONTROLLED_REGION_FACETS: list[dict[str, object]] = [
    {
        "label": "上海自贸区/临港新片区",
        "region_code": "CN-SH",
        "aliases": ["上海", "上海自贸区", "临港新片区"],
        "query_terms": ["上海自贸区", "临港新片区", "数据出境", "负面清单"],
    },
    {
        "label": "天津自贸区",
        "region_code": "CN-TJ",
        "aliases": ["天津", "天津自贸区"],
        "query_terms": ["天津自贸区", "数据出境", "负面清单", "管理清单"],
    },
    {
        "label": "重庆自贸区",
        "region_code": "CN-CQ",
        "aliases": ["重庆", "重庆自贸区"],
        "query_terms": ["重庆自贸区", "数据出境", "负面清单", "车联网"],
    },
    {
        "label": "浙江自贸区",
        "region_code": "CN-ZJ",
        "aliases": ["浙江", "浙江自贸区"],
        "query_terms": ["浙江自贸区", "数据出境", "负面清单", "跨境电商"],
    },
    {
        "label": "海南自由贸易港",
        "region_code": "CN-HI",
        "aliases": ["海南", "海南自由贸易港"],
        "query_terms": ["海南自由贸易港", "数据出境", "负面清单"],
    },
    {
        "label": "北京自贸区/服务业扩大开放示范区",
        "region_code": "CN-BJ",
        "aliases": ["北京", "北京自贸区"],
        "query_terms": ["北京自贸区", "服务业扩大开放", "数据出境", "负面清单"],
    },
    {
        "label": "广东自贸区",
        "region_code": "CN-GD",
        "aliases": ["广东", "广东自贸区"],
        "query_terms": ["广东自贸区", "数据出境", "负面清单"],
    },
    {
        "label": "深圳",
        "region_code": "CN-GD-SZ",
        "aliases": ["深圳"],
        "query_terms": ["深圳", "数据条例", "数据出境"],
    },
    {
        "label": "福建自贸区",
        "region_code": "CN-FJ",
        "aliases": ["福建", "福建自贸区"],
        "query_terms": ["福建自贸区", "数据出境", "负面清单"],
    },
    {
        "label": "广西自贸区",
        "region_code": "CN-GX",
        "aliases": ["广西", "广西自贸区"],
        "query_terms": ["广西自贸区", "数据出境", "负面清单"],
    },
    {
        "label": "江苏自贸区",
        "region_code": "CN-JS",
        "aliases": ["江苏", "江苏自贸区"],
        "query_terms": ["江苏自贸区", "数据出境", "负面清单"],
    },
]

_CONTROLLED_INDUSTRY_FACETS: list[dict[str, object]] = [
    {
        "label": "智能网联汽车/车联网",
        "aliases": ["汽车", "智能网联汽车", "车联网", "车辆位置", "行驶轨迹", "道路环境"],
        "query_terms": [
            "汽车数据安全管理若干规定",
            "汽车数据出境安全指引",
            "智能网联汽车",
            "测绘地理信息",
            "数据出境",
        ],
    },
    {
        "label": "跨境电商",
        "aliases": ["跨境电商", "电子商务", "电商", "订单数据", "物流数据"],
        "query_terms": ["跨境电商", "订单数据", "物流数据", "数据出境"],
    },
    {
        "label": "金融信息服务",
        "aliases": ["金融", "金融信息服务"],
        "query_terms": ["金融信息服务", "数据分类分级", "重要数据"],
    },
]


class LLMRetrievalQuery(StrictModel):
    query_type: RetrievalQueryType
    text: str
    pathway: str | None = None

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class LLMQueryPlan(StrictModel):
    queries: list[LLMRetrievalQuery] = Field(min_length=1)


def build_query_planning_messages(
    question: str,
    facts: ReviewFacts,
    material_text: str | None = None,
) -> list[ChatMessage]:
    """Build a DeepSeek JSON prompt for query planning."""

    json_example = {
        "queries": [
            {
                "query_type": "legal_issue",
                "text": "数据出境安全评估 申报条件",
                "pathway": "security_assessment",
            },
            {
                "query_type": "material_fact",
                "text": "手机号 定位信息 新加坡 数据出境",
                "pathway": None,
            },
            {
                "query_type": "missing_information",
                "text": "数据出境安全评估 数据规模 阈值 单独同意",
                "pathway": None,
            },
        ]
    }

    user_payload = {
        "question": question,
        "review_facts": facts.model_dump(),
        "material_text": (material_text or "")[:6000],
        "corpus_scope": {
            "jurisdiction": "中国大陆数据合规和个人信息保护语料",
            "includes": [
                "个人信息保护法、数据安全法、网络安全法、网络数据安全管理条例",
                "数据出境安全评估、个人信息出境标准合同、个人信息保护认证",
                "国家网信部门政策问答、TC260 标准、自贸区地方数据出境清单、汽车/金融等行业材料",
            ],
            "excludes": [
                "EU AI Act",
                "CCPA/CPRA",
                "其他外国法或非数据合规领域问题",
            ],
        },
        "allowed_query_types": list(_VALID_QUERY_TYPES),
        "controlled_legal_pathways": _CONTROLLED_LEGAL_PATHWAY_ANCHORS,
        "controlled_region_facets": _CONTROLLED_REGION_FACETS,
        "controlled_industry_facets": _CONTROLLED_INDUSTRY_FACETS,
        "json_example": json_example,
        "instructions": [
            "基于用户问题和审查事实生成多个类型化检索查询。",
            "所有查询都必须面向 corpus_scope 内的中国数据合规语料；不要把库外外国法问题改写成看似相关的中国法问题。",
            "如果问题明显超出 corpus_scope，仍输出最小查询用于验证语料缺口，但不要编造国内法替代查询。",
            "如果材料事实不足，生成 missing_information 查询以帮助确认缺口，而不是假设事实成立。",
            "legal_issue 查询优先从 controlled_legal_pathways 中选择相关 pathway，并用其 query_terms 作为文件名/制度锚点；可以加入用户问题或材料里的具体事实词，但不要发明该列表以外的外国法、非数据合规制度或文件名。",
            "不要因为 review_facts.cross_border_transfer 为 true 就机械选择 cross_border_foundation；如果问题已经明确落入 security_assessment、standard_contract 或 post_assessment_obligations，应优先生成具体 pathway 查询，让具体依据排在泛化基础规则之前。",
            "只有当用户询问出境基础规则、总体合规路径、豁免边界、路径关系，或地方/行业规则与全国规则关系时，才选择 cross_border_foundation pathway。",
            "如果 review_facts.cross_border_transfer 为 false，或材料明确“暂不出境/境内处理/不向境外提供”，不要因为问题里出现“是否触发安全评估”就选择 cross_border_foundation、security_assessment、standard_contract 或 certification；应优先选择 sensitive_personal_info 等境内处理相关 pathway，并保留否定边界。",
            "当问题或事实涉及申报、网信部门、安全评估、阈值、重要数据或大规模个人信息时，必须选择 security_assessment pathway。",
            "当问题或事实涉及已通过安全评估后的持续义务、重新申报、接收方/目的/范围变更、监督检查时，必须选择 post_assessment_obligations pathway。",
            "当问题或事实涉及标准合同文本、备案、境外接收方义务时，必须选择 standard_contract pathway。",
            "当问题或事实涉及认证、集团内部跨境共享、大湾区/香港个人信息跨境或认证路径责任时，必须选择 certification pathway。",
            "当问题或事实涉及人脸识别、生物识别、儿童个人信息、精确定位、行踪轨迹或敏感个人信息处理时，必须选择 sensitive_personal_info pathway。",
            "当问题或事实涉及境外远程访问境内数据库、VPN 查看或是否构成数据出境边界时，必须选择 remote_access_boundary pathway。",
            "当问题或事实同时涉及标准合同和安全评估门槛，并询问优先级/冲突时，必须选择 pathway_conflict pathway，同时选择 security_assessment 和 standard_contract。",
            "region_condition 只能从 controlled_region_facets 中选择一个和材料事实明确匹配的中国境内地区/自贸区 facet，并用其 query_terms 组合；没有明确命中时不要输出 region_condition。",
            "不要把境外接收方所在地（如新加坡、日本、美国、德国）写成 region_condition；它们只能出现在 material_fact 查询中。",
            "industry_condition 只能从 controlled_industry_facets 中选择一个和材料事实明确匹配的行业 facet，并用其 query_terms 组合；没有明确命中时不要输出 industry_condition。",
            "如果问题涉及自贸区、负面清单、地方口径或行业专项要求，同时保留全国基础规则查询，并追加匹配到的 region_condition 或 industry_condition。",
            "对否定式或边界问题要保留否定边界，例如“暂不出境”“境内处理”“是否一定触发”。",
            "优先输出 5 到 8 条互补查询；每条查询应短而具体，尽量包含可命中文件标题的中文关键词。",
            "必须输出合法 json object，字段必须与 json_example 完全一致。",
            "query_type 只能使用 allowed_query_types 中的值。",
            "不要输出 query_id，query_id 由程序生成。",
            "legal_issue 查询必须输出 pathway 字段，值为 controlled_legal_pathways 中对应条目的 pathway 名；其他 query_type 的 pathway 为 null。",
        ],
    }

    return [
        ChatMessage(
            role="system",
            content=(
                "你是法律合规检索 query planning 助手。"
                "只输出 json，不输出解释、markdown 或自然语言。"
            ),
        ),
        ChatMessage(role="user", content=json.dumps(user_payload, ensure_ascii=False)),
    ]


def build_issue_query_planning_messages(
    *,
    question: str,
    material_text: str,
    facts: ReviewFacts,
    issue: IssueDraft,
) -> list[ChatMessage]:
    """Build one bounded query-planning prompt for a single review issue."""

    json_example = {
        "queries": [
            {
                "query_type": "legal_issue",
                "text": "数据出境安全评估 申报条件 个人信息数量阈值",
                "pathway": "security_assessment",
            }
        ]
    }
    payload = {
        "case_question": question,
        "material_text": material_text[:6000],
        "review_facts": facts.model_dump(),
        "issue": issue.model_dump(),
        "controlled_legal_pathways": _CONTROLLED_LEGAL_PATHWAY_ANCHORS,
        "controlled_region_facets": _CONTROLLED_REGION_FACETS,
        "controlled_industry_facets": _CONTROLLED_INDUSTRY_FACETS,
        "json_example": json_example,
        "instructions": [
            "只为当前 issue 生成 1 到 3 条互补、可直接检索的查询。",
            "query_type 必须属于 issue.query_types；不要生成其他议题的查询。",
            "当前 issue 是唯一研究边界；不要回答 issue、给出法律结论、风险等级或建议措施。",
            "legal_issue 查询必须选择受控 legal pathway，并使用其制度名或文件名作为锚点。",
            "region_condition 和 industry_condition 只能在审查事实明确匹配受控 facet 时生成。",
            "missing_information 查询只针对当前 issue 所需但材料缺失的事实，不得假设该事实成立。",
            "保留材料中的否定或边界条件，例如暂不出境、仅境内处理、是否一定触发。",
            (
                "查询应短而具体，优先包含可命中文件标题的中文关键词；多条查询必须覆盖不同的"
                "法律要件、适用条件或事实边界，不得只替换同义词重复检索。"
            ),
            "严格输出 json object；不要输出 query_id，query_id 由系统分配。",
            "legal_issue 的 pathway 必须是受控 legal pathway 的名称；其他 query_type 的 pathway 必须为 null。",
        ],
    }
    return [
        ChatMessage(
            role="system",
            content="你是法律合规检索 query planning 助手。只输出 json。",
        ),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def plan_queries_with_deepseek(
    question: str,
    facts: ReviewFacts,
    material_text: str | None = None,
    *,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
) -> list[RetrievalQuery]:
    """Plan retrieval queries using DeepSeek with strict validation.

    This is the online LLM path. It does not fill omitted query types from
    rules and does not parse natural-language responses.
    """

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="query_planning",
        output_model=LLMQueryPlan,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
    )
    plan = node.run(build_query_planning_messages(question, facts, material_text))

    ids = _QueryIdGenerator()
    queries: list[RetrievalQuery] = []
    for query in plan.queries:
        queries.append(
            RetrievalQuery(
                query_id=ids.next_id(),
                query_type=query.query_type,
                text=query.text.strip(),
                pathway=query.pathway,
            )
        )

    return queries


def plan_issue_queries_with_deepseek(
    *,
    question: str,
    material_text: str,
    facts: ReviewFacts,
    issue: IssueDraft,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
) -> list[LLMRetrievalQuery]:
    """Plan bounded queries for one Case Analyst issue without assigning IDs."""

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="query_planning",
        output_model=LLMQueryPlan,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
    )

    def validate_issue_scope(plan: LLMQueryPlan) -> LLMQueryPlan:
        allowed_types = set(issue.query_types)
        invalid = [
            query.query_type for query in plan.queries if query.query_type not in allowed_types
        ]
        if invalid:
            raise ValueError(
                "issue query planner returned query types outside the issue scope: "
                + ", ".join(sorted(set(invalid)))
            )
        return plan

    plan = node.run(
        build_issue_query_planning_messages(
            question=question,
            material_text=material_text,
            facts=facts,
            issue=issue,
        ),
        post_validate=validate_issue_scope,
        post_validation_reason="issue_query_scope_invalid",
    )
    return plan.queries[:3]


plan_queries_with_llm = plan_queries_with_deepseek
