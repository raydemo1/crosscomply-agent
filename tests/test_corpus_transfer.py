from dataclasses import replace
import json
from unittest.mock import MagicMock
import zipfile

import pytest

from law_agent.config import load_service_config
from law_agent.kb.service import InMemoryIndex, KnowledgeBase
from scripts import transfer_corpus as transfer
from tests.test_kb import _chunk, _source


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "ROOT", tmp_path)
    config = load_service_config()
    config = replace(config, embedding=replace(config.embedding, model="test-model", dimension=2))
    monkeypatch.setattr(transfer, "load_service_config", lambda: config)
    monkeypatch.setattr(transfer.subprocess, "check_output", lambda *a, **kw: "test-commit\n")
    corpus = tmp_path / "data/corpus/test"
    kb = KnowledgeBase(corpus, index=InMemoryIndex(), embed_texts=lambda texts: [[0.5, 0.25] for _ in texts])
    kb.ingest_prepared(_source(), "第一条 数据处理者应当遵守本规定。", [_chunk("第一条 数据处理者应当遵守本规定。")])
    (corpus / "raw").mkdir(exist_ok=True)
    (corpus / "raw/source.txt").write_text("原文", encoding="utf-8")
    archive = tmp_path / "corpus.zip"
    transfer.pack(corpus, archive)
    destination = tmp_path / "restored"
    with zipfile.ZipFile(archive) as stream:
        stream.extractall(destination)
    return destination / "data/corpus/test", archive


def test_package_preserves_hidden_state_and_checks_content(package):
    corpus, archive = package
    result = transfer.restore(corpus)
    assert result == {"chunks": 1, "sources": 1, "embedding_model": "test-model",
                      "model_calls": 0, "executed": False}
    assert transfer.sha256(archive) in archive.with_suffix(".zip.sha256").read_text(encoding="utf-8")
    (corpus / "raw/source.txt").write_text("被改动", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        transfer.verify(corpus)


def test_manifest_cannot_skip_required_files_or_escape_corpus(package):
    corpus, _ = package
    path = corpus / transfer.MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="omits required"):
        transfer.verify(corpus)
    manifest["files"] = [{"path": "../outside", "bytes": 0, "sha256": ""}]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid manifest path"):
        transfer.verify(corpus)


def test_missing_vector_is_rejected_without_embedding_request(package):
    corpus, _ = package
    state_path = corpus / ".knowledge_base_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["embedding_cache"] = {}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing compatible cached vector"):
        transfer.cached_vectors(corpus, 2)


def test_restore_preserves_generation_and_refuses_existing_indexes(package, monkeypatch):
    corpus, _ = package
    index = MagicMock()
    index.es.count.return_value = {"count": 0}
    index.pg.cursor.return_value.__enter__.return_value.fetchone.return_value = (0,)
    monkeypatch.setattr("law_agent.kb.service_index.ServiceGenerationIndex", lambda _: index)
    result = transfer.restore(corpus, execute=True)
    assert result["executed"] and result["model_calls"] == 0
    state = json.loads((corpus / ".knowledge_base_state.json").read_text(encoding="utf-8"))
    index.activate.assert_called_once_with("law_001", state["sources"]["law_001"]["generation_id"])
    index.verify.assert_called_once()
    index.close.assert_called_once()
    index.reset_mock()
    index.es.count.return_value = {"count": 1}
    with pytest.raises(ValueError, match="requires empty"):
        transfer.restore(corpus, execute=True)
    index.stage.assert_not_called()
    index.close.assert_called_once()


def test_model_mismatch_stops_before_connecting(package, monkeypatch):
    corpus, _ = package
    config = transfer.load_service_config()
    monkeypatch.setattr(transfer, "load_service_config", lambda: replace(
        config, embedding=replace(config.embedding, model="other-model"),
    ))
    factory = MagicMock()
    monkeypatch.setattr("law_agent.kb.service_index.ServiceGenerationIndex", factory)
    with pytest.raises(ValueError, match="model/dimension differs"):
        transfer.restore(corpus, execute=True)
    factory.assert_not_called()
