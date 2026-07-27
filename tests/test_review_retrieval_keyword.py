"""Shared retrieval fixtures and utility tests."""

from pathlib import Path

import pytest

from law_agent.data.io import write_jsonl
from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.corpus import CorpusError, load_corpus
from law_agent.review.retrieval.hits import merge_hits_by_chunk_id
from law_agent.review.retrieval.text import normalize_text, tokenize
from law_agent.review.schemas import RetrievalHit


# ---------------------------------------------------------------------------
# Chunk fixtures mirroring real corpus structure
# ---------------------------------------------------------------------------

def _make_chunk(
    *,
    chunk_id: str,
    doc_id: str = "doc_001",
    source_id: str = "src_001",
    title: str = "数据出境安全评估办法",
    text: str = "",
    heading_path: list[str] | None = None,
    citation_label: str | None = "数据出境安全评估办法 第一条",
    citation_role: str = "primary_legal_basis",
    can_cite_clause: bool = True,
    source_url: str = "https://example.gov.cn/doc/001",
    applicable_region: str = "CN",
    chunk_index: int = 0,
    prev_chunk_id: str | None = None,
    next_chunk_id: str | None = None,
    topic_tags: list[str] | None = None,
    legal_domain: list[str] | None = None,
    applicable_subjects: list[str] | None = None,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        source_id=source_id,
        title=title,
        text=text or "第一条　为了规范数据出境活动，制定本办法。",
        chunk_index=chunk_index,
        doc_type="regulation",
        heading_path=heading_path or [title, "第一条"],
        article_no="第一条",
        citation_label=citation_label,
        citation_role=citation_role,
        can_cite_clause=can_cite_clause,
        source_url=source_url,
        applicable_region=applicable_region,
        prev_chunk_id=prev_chunk_id,
        next_chunk_id=next_chunk_id,
        topic_tags=topic_tags or [],
        legal_domain=legal_domain or [],
        applicable_subjects=applicable_subjects or [],
        char_count=len(text or "第一条　为了规范数据出境活动，制定本办法。"),
    )


FIXTURE_CHUNKS: list[Chunk] = [
    _make_chunk(
        chunk_id="chunk_assessment",
        title="数据出境安全评估办法",
        text="第四条　数据处理者向境外提供数据，符合下列情形之一的，应当申报数据出境安全评估：处理100万人以上个人信息。",
    ),
    _make_chunk(
        chunk_id="chunk_contract",
        doc_id="doc_002",
        source_id="src_002",
        title="个人信息出境标准合同办法",
        text="第二条　个人信息处理者通过订立标准合同的方式向境外提供个人信息的，适用本办法。",
        citation_label="个人信息出境标准合同办法 第二条",
    ),
    _make_chunk(
        chunk_id="chunk_pipl",
        doc_id="doc_003",
        source_id="src_003",
        title="中华人民共和国个人信息保护法",
        text="第三十八条　个人信息处理者因业务等需要，确需向中华人民共和国境外提供个人信息的，应当具备下列条件之一。",
        citation_label="中华人民共和国个人信息保护法 第三十八条",
    ),
    _make_chunk(
        chunk_id="chunk_automotive",
        doc_id="doc_004",
        source_id="src_004",
        title="汽车数据安全管理若干规定（试行）",
        text="第三条　汽车数据处理者开展汽车数据处理活动，应当遵守相关法律、行政法规和本规定的要求。",
        citation_label="汽车数据安全管理若干规定 第三条",
        citation_role="conditional_industry_basis",
        can_cite_clause=True,
    ),
    _make_chunk(
        chunk_id="chunk_shanghai",
        doc_id="doc_005",
        source_id="src_005",
        title="中国（上海）自由贸易试验区数据出境管理清单（负面清单）（2024版）",
        text="一、上海自贸区企业数据出境负面清单，涉及个人信息出境的，按本清单管理。",
        citation_label=None,
        citation_role="conditional_local_basis",
        can_cite_clause=False,
        applicable_region="CN-SH",
    ),
    _make_chunk(
        chunk_id="chunk_faq",
        doc_id="doc_006",
        source_id="src_006",
        title="《数据出境安全评估办法》答记者问",
        text="问：数据出境安全评估的适用范围是什么？答：适用于重要数据出境和处理100万人以上个人信息的数据出境。",
        citation_label=None,
        citation_role="interpretation_auxiliary",
        can_cite_clause=False,
    ),
]


# ---------------------------------------------------------------------------
# Tokenization tests
# ---------------------------------------------------------------------------

def test_normalize_text_converts_chinese_punctuation() -> None:
    assert normalize_text("数据出境，安全评估。") == "数据出境 安全评估"


def test_tokenize_chinese_bigrams() -> None:
    tokens = tokenize("数据出境")
    assert "数据" in tokens
    assert "据出" in tokens
    assert "出境" in tokens


def test_tokenize_keeps_ascii_runs() -> None:
    tokens = tokenize("GB/T 35273-2020 标准")
    assert "gb/t" in tokens or "gb" in tokens
    assert "35273-2020" in tokens or "35273" in tokens


def test_tokenize_single_cjk_char_kept_as_unigram() -> None:
    tokens = tokenize("据")
    assert "据" in tokens


# ---------------------------------------------------------------------------
# Merge hits tests
# ---------------------------------------------------------------------------

def test_merge_hits_deduplicates_by_chunk_id() -> None:
    hit_a = RetrievalHit(
        chunk_id="chunk_1", doc_id="d1", source_id="s1", title="t1", text="x",
        score=2.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u1",
        matched_query_type="legal_issue",
    )
    hit_b = RetrievalHit(
        chunk_id="chunk_1", doc_id="d1", source_id="s1", title="t1", text="x",
        score=3.0, rank=1, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u1",
        matched_query_type="material_fact",
    )
    hit_c = RetrievalHit(
        chunk_id="chunk_2", doc_id="d2", source_id="s2", title="t2", text="y",
        score=1.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u2",
        matched_query_type="legal_issue",
    )

    merged = merge_hits_by_chunk_id([[hit_a], [hit_b], [hit_c]], top_k=10)

    assert len(merged) == 2
    assert merged[0].chunk_id == "chunk_1"
    assert merged[0].score == 3.0  # best score kept
    assert merged[1].chunk_id == "chunk_2"
    assert merged[0].rank == 0
    assert merged[1].rank == 1


def test_merge_hits_respects_top_k() -> None:
    hits = [
        [RetrievalHit(
            chunk_id=f"chunk_{i}", doc_id=f"d{i}", source_id=f"s{i}",
            title=f"t{i}", text="x", score=float(i), rank=0,
            retriever="keyword", citation_role="primary_legal_basis",
            can_cite_clause=True, source_url=f"u{i}",
        )]
        for i in range(5)
    ]
    merged = merge_hits_by_chunk_id(hits, top_k=3)
    assert len(merged) == 3


# ---------------------------------------------------------------------------
# Corpus loader tests
# ---------------------------------------------------------------------------

def _write_chunks_jsonl(path: Path, chunks: list[Chunk]) -> None:
    write_jsonl(path, chunks)


def test_load_corpus_reads_chunks(tmp_path: Path) -> None:
    chunks_path = tmp_path / "chunks.jsonl"
    _write_chunks_jsonl(chunks_path, FIXTURE_CHUNKS)

    chunks = load_corpus(chunks_path)

    assert len(chunks) == len(FIXTURE_CHUNKS)
    assert chunks[0].chunk_id == "chunk_assessment"


def test_load_corpus_missing_file_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="does not exist"):
        load_corpus(tmp_path / "nonexistent.jsonl")


def test_load_corpus_empty_file_raises_error(tmp_path: Path) -> None:
    chunks_path = tmp_path / "empty.jsonl"
    chunks_path.write_text("", encoding="utf-8")

    with pytest.raises(CorpusError, match="empty"):
        load_corpus(chunks_path)
