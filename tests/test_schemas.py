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
