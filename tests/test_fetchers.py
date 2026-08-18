import pytest

from law_agent.data.fetchers.flk_npc import _extract_issuing_body, get_download_url, strip_html
from law_agent.data.fetchers.generic import fetch_source
from law_agent.data.schemas import SourceRecord


def test_strip_html_removes_flk_highlight_tags() -> None:
    assert (
        strip_html("中华人民共和国<em class='highlight'>个人信息保护</em>法")
        == "中华人民共和国个人信息保护法"
    )


def test_flk_issuing_body_is_not_guessed_when_missing() -> None:
    assert _extract_issuing_body({"flxz": "法律"}) is None


def test_flk_issuing_body_uses_explicit_source_field() -> None:
    assert _extract_issuing_body({"zdjgName": "国务院"}) == "国务院"


def test_fetch_source_has_no_sample_fallback(tmp_path) -> None:
    record = SourceRecord(
        source_id="missing",
        title="Missing",
        source_url="https://127.0.0.1:9/missing.txt",
        source_site="example.test",
        doc_type="law",
        file_format="txt",
        include_in_mvp=True,
    )

    result = fetch_source(record, tmp_path, timeout_seconds=1, allow_network=True)

    assert result.ok is False
    assert not result.path.exists()


def test_flk_download_url_requires_api_url(monkeypatch) -> None:
    def fake_get_json(url, params, timeout_seconds):
        return {"code": 200, "data": {}}

    monkeypatch.setattr("law_agent.data.fetchers.flk_npc._get_json", fake_get_json)

    with pytest.raises(RuntimeError, match="returned no URL"):
        get_download_url("abc", timeout_seconds=1)


def test_fetch_flk_source_has_no_download_fallback(tmp_path, monkeypatch) -> None:
    def fail_download(*args, **kwargs):
        raise RuntimeError("official download unavailable")

    monkeypatch.setattr("law_agent.data.fetchers.flk_npc.download_document", fail_download)
    record = SourceRecord(
        source_id="flk_npc_abc",
        title="中华人民共和国个人信息保护法",
        source_url="https://flk.npc.gov.cn/law-search/search/flfgDetails?bbbs=abc",
        download_url="https://flk.npc.gov.cn/law-search/download/pc?format=docx&bbbs=abc",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        file_format="docx",
        include_in_mvp=True,
    )

    result = fetch_source(record, tmp_path, timeout_seconds=1)

    assert result.ok is False
    assert result.error == "official download unavailable"
    assert not result.path.exists()
