from types import SimpleNamespace

import pytest

from scripts import eval_candidate_navigation as experiment
from tests.test_review_agent import _chunk


@pytest.mark.parametrize('response', [
    {'summaries': []},
    {'summaries': [{'chunk_id': 'c', 'preview': '内容'}, {'chunk_id': 'c', 'preview': '重复'}]},
    {'summaries': [{'chunk_id': 'c', 'preview': '字' * 101}]},
    {'summaries': [{'chunk_id': 'c', 'preview': ''}]},
])
def test_invalid_summary_fails_without_fallback(tmp_path, monkeypatch, response):
    monkeypatch.setattr(experiment, 'OUT', tmp_path)
    summary = object.__new__(experiment.Summaries)
    summary.counter = 0
    summary.client = SimpleNamespace(chat_json=lambda *_a, **_kw: response)
    with pytest.raises(ValueError):
        summary.generate([_chunk('c', '第一条', '原文', index=0)])
    result = experiment.read(tmp_path / 'summary_calls/0001.result.json')
    assert result['status'] == 'failed' and 'summaries' not in result


def test_static_preview_rejects_changed_original_and_unknown_candidate():
    summary = object.__new__(experiment.Summaries)
    chunk = _chunk('c', '第一条', '原文', index=0)
    tools = SimpleNamespace(_chunks_by_id={'c': chunk}, _search_navigation=[{
        'locations': [{'chunk_id': 'c', 'preview': '原文'}]}])
    for entry in [None, {'text_sha256': 'stale', 'version': experiment.VERSION, 'preview': '摘要'}]:
        summary.cache = {'c': entry} if entry else {}
        with pytest.raises(RuntimeError, match='未预生成或已经失效'):
            summary.replace(tools, [], 'B')
        assert tools._search_navigation[0]['locations'][0]['preview'] == '原文'


def test_dynamic_preview_uses_one_batch_call_and_preserves_identity():
    summary = object.__new__(experiment.Summaries)
    calls = []
    chunk = _chunk('c', '第一条', '原文', index=0)
    tools = SimpleNamespace(_question='适用条件是什么', _chunks_by_id={'c': chunk}, _search_navigation=[{
        'locations': [{'chunk_id': 'c', 'query_matches': [{'query_id': 'q', 'rank': 6}], 'preview': '原文'}]}])
    def generate(chunks, **kw):
        calls.append((chunks, kw))
        return {'c': '关于适用条件的规定，需读原文核对例外'}
    summary.generate = generate
    summary.replace(tools, [], 'C')
    assert len(calls) == 1
    loc = tools._search_navigation[0]['locations'][0]
    assert loc['chunk_id'] == 'c' and loc['query_matches'][0]['rank'] == 6
