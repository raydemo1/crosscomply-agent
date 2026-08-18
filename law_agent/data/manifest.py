"""Build candidate source manifests for data compliance topics."""

from __future__ import annotations

from law_agent.data.fetchers.flk_npc import search_sources
from law_agent.data.schemas import SourceRecord

DATA_COMPLIANCE_SEEDS = [
    SourceRecord(
        source_id="flk_npc_pipl_2021",
        title="中华人民共和国个人信息保护法",
        source_url="https://flk.npc.gov.cn/",
        download_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        publish_date="2021-08-20",
        effective_date="2021-11-01",
        issuing_body="全国人民代表大会常务委员会",
        applicable_region="CN",
        legal_domain=["数据合规", "个人信息保护"],
        applicable_subjects=["个人信息处理者", "境外接收方"],
        topic_tags=["个人信息保护", "数据合规"],
        language="zh",
        file_format="html",
        include_in_mvp=True,
        review_note="第一批核心法律；真实 URL 后续由 flk 采集器补全",
    ),
    SourceRecord(
        source_id="flk_npc_data_security_2021",
        title="中华人民共和国数据安全法",
        source_url="https://flk.npc.gov.cn/",
        download_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        publish_date="2021-06-10",
        effective_date="2021-09-01",
        issuing_body="全国人民代表大会常务委员会",
        applicable_region="CN",
        legal_domain=["数据合规", "数据安全"],
        applicable_subjects=["数据处理者", "重要数据处理者"],
        topic_tags=["数据安全", "数据合规"],
        language="zh",
        file_format="html",
        include_in_mvp=True,
        review_note="第一批核心法律；真实 URL 后续由 flk 采集器补全",
    ),
    SourceRecord(
        source_id="flk_npc_cybersecurity_2016",
        title="中华人民共和国网络安全法",
        source_url="https://flk.npc.gov.cn/",
        download_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        publish_date="2016-11-07",
        effective_date="2017-06-01",
        issuing_body="全国人民代表大会常务委员会",
        applicable_region="CN",
        legal_domain=["数据合规", "网络安全"],
        applicable_subjects=["网络运营者", "关键信息基础设施运营者"],
        topic_tags=["网络安全", "数据合规"],
        language="zh",
        file_format="html",
        include_in_mvp=True,
        review_note="第一批核心法律；真实 URL 后续由 flk 采集器补全",
    ),
]


def build_seed_manifest(topic: str) -> list[SourceRecord]:
    if topic != "data_compliance":
        raise ValueError(f"Unsupported topic: {topic}")
    return list(DATA_COMPLIANCE_SEEDS)


def build_manifest(
    topic: str,
    *,
    from_flk: bool = False,
    terms: list[str] | None = None,
    limit: int | None = None,
    timeout_seconds: int = 30,
) -> list[SourceRecord]:
    if from_flk:
        records = search_sources(terms=terms, timeout_seconds=timeout_seconds)
    else:
        raise ValueError("A real source must be selected. Use --from-flk for phase 1 ingestion.")
    return records[:limit] if limit else records
