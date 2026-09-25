from law_agent.data.citation_policy import (
    can_cite_clause,
    can_cite_clause_chunk,
    citation_role_for_source,
    default_retrievable_for_source,
    frontend_direct_reference_for_source,
)
from law_agent.data.filters import filter_chunks, filter_clause_citable_chunks
from law_agent.data.schemas import Chunk, SourceRecord


def test_source_record_parses_legal_metadata_lists() -> None:
    record = SourceRecord.model_validate(
        {
            "source_id": "flk_npc_pipl_2021",
            "title": "中华人民共和国个人信息保护法",
            "source_url": "https://flk.npc.gov.cn/",
            "source_site": "flk.npc.gov.cn",
            "doc_type": "law",
            "issuing_body": "全国人民代表大会常务委员会",
            "applicable_region": "CN",
            "legal_domain": "数据合规;个人信息保护",
            "applicable_subjects": "个人信息处理者;境外接收方",
        }
    )

    assert record.legal_domain == ["数据合规", "个人信息保护"]
    assert record.applicable_subjects == ["个人信息处理者", "境外接收方"]


def test_filter_chunks_by_legal_metadata() -> None:
    effective_data_chunk = Chunk(
        chunk_id="doc:0000",
        doc_id="doc",
        source_id="source",
        title="中华人民共和国个人信息保护法",
        text="第一条 为了保护个人信息权益。",
        chunk_index=0,
        doc_type="law",
        heading_path=["中华人民共和国个人信息保护法", "第一条"],
        article_no="第一条",
        authority="national_law",
        law_status="effective",
        source_url="https://flk.npc.gov.cn/",
        topic_tags=["个人信息保护"],
        applicable_region="CN",
        issuing_body="全国人民代表大会常务委员会",
        legal_domain=["数据合规", "个人信息保护"],
        applicable_subjects=["个人信息处理者"],
        char_count=15,
    )
    repealed_chunk = effective_data_chunk.model_copy(
        update={
            "chunk_id": "doc:0001",
            "law_status": "repealed",
            "legal_domain": ["历史法规"],
        }
    )

    matched = filter_chunks(
        [effective_data_chunk, repealed_chunk],
        law_status="effective",
        applicable_region="CN",
        legal_domain="数据合规",
        applicable_subjects="个人信息处理者",
    )

    assert [chunk.chunk_id for chunk in matched] == ["doc:0000"]


def test_citation_policy_marks_only_primary_sources_clause_citable() -> None:
    primary = SourceRecord(
        source_id="new_official_regulation",
        title="新规",
        source_url="https://example.gov.cn/rule",
        source_site="example.gov.cn",
        doc_type="regulation",
        citation_role="primary_legal_basis",
    )
    auxiliary = primary.model_copy(update={"citation_role": "interpretation_auxiliary"})
    conditional = primary.model_copy(update={"citation_role": "conditional_industry_basis"})

    assert citation_role_for_source(primary) == "primary_legal_basis"
    assert can_cite_clause(primary) is True
    assert can_cite_clause_chunk(primary, "第一条") is True
    assert can_cite_clause_chunk(primary, None) is False
    assert can_cite_clause(auxiliary) is False
    assert can_cite_clause_chunk(conditional, "第一条") is False
    assert can_cite_clause(primary.model_copy(update={"library_kind": "internal_policy"})) is False


def test_frontend_and_default_retrieval_policy_are_separate() -> None:
    source = SourceRecord(
        source_id="cac_data_export_security_assessment_filing_guide_v3_2025",
        title="备案指南",
        source_url="https://www.cac.gov.cn/guide",
        source_site="cac.gov.cn",
        doc_type="guideline",
    )
    assert default_retrievable_for_source(source) is True
    assert (
        frontend_direct_reference_for_source(
            "cac_data_export_security_assessment_filing_guide_v3_2025"
        )
        is True
    )

    assert default_retrievable_for_source(source.model_copy(update={"source_id": "other"})) is True
    assert frontend_direct_reference_for_source("cac_cross_border_data_flow_rules_2024") is False


def test_filter_clause_citable_chunks_only_returns_primary_effective_evidence() -> None:
    primary = Chunk(
        chunk_id="primary:0000",
        doc_id="primary",
        source_id="flk_npc_ff8081817b6472a3017b656cc2040044",
        title="中华人民共和国个人信息保护法",
        text="第一条 为了保护个人信息权益。",
        chunk_index=0,
        doc_type="law",
        heading_path=["中华人民共和国个人信息保护法", "第一条"],
        article_no="第一条",
        citation_label="中华人民共和国个人信息保护法 第一条",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        authority="national_law",
        law_status="effective",
        source_url="https://flk.npc.gov.cn/",
        char_count=15,
    )
    qna = primary.model_copy(
        update={
            "chunk_id": "qna:0000",
            "source_id": "cac_data_export_assessment_qna_2022",
            "title": "《数据出境安全评估办法》答记者问",
            "citation_role": "interpretation_auxiliary",
            "can_cite_clause": False,
        }
    )
    implementation_reference = primary.model_copy(
        update={
            "chunk_id": "implementation:0000",
            "source_id": "tc260_gbt_35273_2020_pip_security_spec",
            "title": "GB/T 35273-2020 信息安全技术 个人信息安全规范",
            "citation_role": "implementation_reference",
            "can_cite_clause": False,
        }
    )

    matched = filter_clause_citable_chunks([primary, qna, implementation_reference])

    assert [chunk.chunk_id for chunk in matched] == ["primary:0000"]
