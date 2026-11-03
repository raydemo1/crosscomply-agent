"""Frozen navigation replay and isolated preview experiments; no production model changes."""
from __future__ import annotations

import argparse
import faulthandler
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from law_agent.config import require_agent_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.usage import usage_scope
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.retrieval.corpus import load_corpus, DEFAULT_CHUNKS_PATH
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import RetrievalQuery
from scripts import eval_qa_retrieval_chain as chain

RUN = ROOT / 'data/review_runs/scu_v41_validation_20261027'
OUT = RUN / 'candidate_navigation_20261030'
CASES = ['agent_small_sensitive_001', 'eval_shanghai_001',
         'agent_hr_exception_missing_001', 'eval_standard_contract_003']
VERSION = 'navigation-topic-v1-100'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def baseline(cid):
    folder = 'qa_scope_remaining_20261030' if (RUN / 'qa_scope_remaining_20261030' / cid).exists() else 'qa_scope_batch1_ready_20261029'
    return RUN / folder / cid


def offline():
    corpus = {c.chunk_id: c for c in load_corpus(DEFAULT_CHUNKS_PATH)}
    tools = object.__new__(ComplianceAgentTools)
    tools._sources_with_articles = {c.source_id for c in corpus.values() if c.article_no}
    rows = []
    for item in read(chain.FIXTURE)['cases']:
        if item['entry'] != 'qa':
            continue
        cid = item['case_id']
        base = baseline(cid)
        windows = read(base / 'candidate_windows.json')
        searches = [a for a in read(base / 'research.json') if a['kind'] == 'search']
        for index, (w, action) in enumerate(zip(windows, searches, strict=True)):
            queries = [RetrievalQuery.model_validate(q) for q in action['queries']]
            batches = [[hit_from_chunk(corpus[h['chunk_id']], i) for i, h in enumerate(q)] for q in w['per_query_rankings']]
            selected = [hit_from_chunk(corpus[k], i) for i, k in enumerate(w['selected_ids'])]
            entries = tools._search_navigation_entries(queries, batches, selected)
            old = action.get('navigation', [])
            targets = [t['target'] for t in read(RUN / 'production_unified_live' / f'{cid}.json')['targets']]
            def covered(ids):
                return sum(any(corpus[k].source_id == t['source_id'] and
                               (not t.get('article_no') or corpus[k].article_no == t['article_no']) for k in ids) for t in targets)
            selected_ids = w['selected_ids']
            old_ids = [loc['chunk_id'] for s in old for loc in s.get('locations', [])]
            rows.append({'case_id': cid, 'search_index': index, 'selected_ids': selected_ids,
                         'old_navigation': old, 'new_navigation': tools._group_search_navigation(entries),
                         'observations': len(targets), 'selected_observations': covered(selected_ids),
                         'old_discovered_observations': covered(selected_ids + old_ids),
                         'new_discovered_observations': covered(selected_ids + [e['chunk_id'] for e in entries]),
                         'old_preview_chars': sum(len(loc.get('preview', '')) for s in old for loc in s.get('locations', [])),
                         'new_preview_chars': sum(len(e['preview']) for e in entries)})
    write(OUT / 'offline.json', {'cases': 11, 'search_cases': len({r['case_id'] for r in rows}),
                                'rows': rows, 'provider_calls': 0,
                                'limits': 'Discovery is not acquired evidence. Two latest cases did not search. Old navigation is cached, no regeneration.'})
    print('OFFLINE', len(rows), 'search batches', flush=True)


class Summaries:
    def __init__(self):
        self.client = OpenAICompatibleClient(require_agent_llm_config())
        assert self.client.config.model == 'DeepSeek-V4.1-Flash-1017'
        self.cache_path = OUT / 'static_summaries.json'
        self.cache = read(self.cache_path) if self.cache_path.exists() else {}
        self.counter = len(list((OUT / 'summary_calls').glob('*.json'))) // 2

    def generate(self, chunks, *, question=None, queries=None):
        self.counter += 1
        dest = OUT / 'summary_calls' / f'{self.counter:04d}'
        records = []
        payload = {'question': question, 'queries': queries,
                   'candidates': [{'chunk_id': c.chunk_id, 'title': c.title,
                                   'article_no': c.article_no, 'text': c.text} for c in chunks]}
        messages = [ChatMessage('system', '为法律检索候选生成中文导航摘要，说明原文主题、适用对象以及需要进一步核对的条件或例外。不判断当前案例，不补充原文没有的信息，不给最终法律结论。每项1至100个字符。严格返回JSON：{"summaries":[{"chunk_id":"输入ID","preview":"摘要"}]}。每个输入ID恰好一次。'),
                    ChatMessage('user', json.dumps(payload, ensure_ascii=False))]
        write(dest.with_suffix('.request.json'), payload)
        phase = 'navigation_dynamic_summary' if question is not None else 'navigation_static_preparation'
        try:
            with usage_scope(phase, records.append):
                result = self.client.chat_json(messages, stream=True, structured_output_mode='json_object')
            items = result['summaries']
            mapping = {x['chunk_id']: x['preview'] for x in items}
            if len(items) != len(chunks) or set(mapping) != {c.chunk_id for c in chunks}:
                raise ValueError('摘要ID缺失、重复或多出')
            if any(not isinstance(s, str) or not s.strip() or len(s) > 100 for s in mapping.values()):
                raise ValueError('摘要为空、格式错误或超过100字符')
            write(dest.with_suffix('.result.json'), {'status': 'completed', 'phase': phase, 'summaries': mapping, 'usage': records})
            return mapping
        except Exception as exc:
            write(dest.with_suffix('.result.json'), {'status': 'failed', 'phase': phase, 'error': type(exc).__name__, 'usage': records})
            raise

    def prepare(self):
        corpus = {c.chunk_id: c for c in load_corpus(DEFAULT_CHUNKS_PATH)}
        ids = set()
        for row in read(OUT / 'offline.json')['rows']:
            if row['case_id'] in CASES:
                ids.update(loc['chunk_id'] for s in row['new_navigation'] for loc in s['locations'])
        for variant in ['A', 'C']:
            for path in (OUT / variant).glob('*/candidate_windows.json'):
                # Include the actual experiment's candidate pool to prepare for fresh B planning.
                for window in read(path):
                    ids.update(window['candidate_ids'])
        pending = [corpus[k] for k in sorted(ids) if self.cache.get(k, {}).get('text_sha256') != digest(corpus[k].text)
                   or self.cache.get(k, {}).get('version') != VERSION]
        print('STATIC_PREPARATION', len(pending), 'new candidates', flush=True)
        for offset in range(0, len(pending), 10):
            batch = pending[offset:offset + 10]
            values = self.generate(batch)
            for c in batch:
                self.cache[c.chunk_id] = {'version': VERSION, 'text_sha256': digest(c.text),
                                         'title': c.title, 'article_no': c.article_no, 'preview': values[c.chunk_id]}
            write(self.cache_path, self.cache)
            print('STATIC_PREPARED', offset + len(batch), '/', len(pending), flush=True)

    def replace(self, tools, queries, mode):
        locations = [loc for source in tools._search_navigation for loc in source['locations']]
        if not locations:
            return
        chunks = [tools._chunks_by_id[loc['chunk_id']] for loc in locations]
        if mode == 'B':
            values = {}
            for c in chunks:
                entry = self.cache.get(c.chunk_id)
                if not entry or entry['text_sha256'] != digest(c.text) or entry['version'] != VERSION:
                    raise RuntimeError('静态摘要未预生成或已经失效：' + c.chunk_id)
                values[c.chunk_id] = entry['preview']
        else:
            values = self.generate(chunks, question=tools._question,
                                   queries=[q.model_dump(mode='json') for q in queries])
        for loc in locations:
            loc['preview'] = values[loc['chunk_id']]


def live(mode):
    summaries = Summaries() if mode != 'A' else None
    original = ComplianceAgentTools.search
    def search(self, queries, facts, **kw):
        hits = original(self, queries, facts, **kw)
        if summaries is not None:
            summaries.replace(self, queries, mode)
        return hits
    destination = OUT / mode
    argv = ['eval_qa_retrieval_chain', '--output', str(destination), '--cases', *CASES]
    if destination.exists():
        argv.append('--resume')
    started = time.monotonic()
    with patch.object(ComplianceAgentTools, 'search', search), patch.object(sys, 'argv', argv):
        chain.main()
    write(destination / 'elapsed.json', {'elapsed_seconds_this_invocation': time.monotonic() - started})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['offline', 'A', 'B', 'C', 'prepareB', 'all'])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    faulthandler.dump_traceback_later(120, repeat=True)
    if args.stage in ['offline', 'all']:
        offline()
    for stage in (['A', 'C', 'prepareB', 'B'] if args.stage == 'all' else [args.stage]):
        if stage in ['A', 'B', 'C']:
            live(stage)
        elif stage == 'prepareB':
            Summaries().prepare()


if __name__ == '__main__':
    main()
