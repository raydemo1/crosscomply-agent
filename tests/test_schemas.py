import pytest

from law_agent.data.schemas import SourceRecord


def test_source_record_parses_manifest_strings() -> None:
    record = SourceRecord.model_validate(
        {
            "source_id": "flk_npc_pipl_2021",
            "title": "中华人民共和国个人信息保护法",
            "source_url": "https://flk.npc.gov.cn/",
            "download_url": "https://wb.flk.npc.gov.cn/",
            "source_site": "flk.npc.gov.cn",
            "doc_type": "law",
            "authority": "national_law",
            "law_status": "effective",
            "publish_date": "2021-08-20",
            "effective_date": "2021-11-01",
            "topic_tags": "个人信息保护;数据合规",
            "language": "zh",
            "file_format": "docx",
            "include_in_mvp": "true",
            "review_note": "核心法律",
        }
    )

    assert record.topic_tags == ["个人信息保护", "数据合规"]
    assert record.include_in_mvp is True


def test_source_record_accepts_judicial_interpretation_doc_type() -> None:
    record = SourceRecord(
        source_id="court_face_recognition_rules_2021",
        title="最高人民法院关于审理使用人脸识别技术处理个人信息相关民事案件适用法律若干问题的规定",
        source_url="https://www.court.gov.cn/fabu/xiangqing/315851.html",
        source_site="court.gov.cn",
        doc_type="judicial_interpretation",
        authority="judicial_interpretation",
        citation_role="primary_legal_basis",
        law_status="effective",
    )

    assert record.doc_type == "judicial_interpretation"
    assert record.authority == "judicial_interpretation"


def test_source_record_rejects_unparseable_validity_date() -> None:
    with pytest.raises(ValueError):
        SourceRecord(
            source_id="example", title="示例法", source_url="https://flk.npc.gov.cn/example",
            source_site="flk.npc.gov.cn", doc_type="law", valid_to="明年",
        )
