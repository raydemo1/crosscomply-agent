from threading import Barrier, Event, get_ident

from law_agent.review.agent import AgentState, EvidenceRead, EvidenceReadRequest, _read_evidence_batch
from law_agent.review.agent_context import search_observation_context
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from law_agent.review.retrieval.neighbors import hit_from_chunk
from tests.test_review_agent import _chunk, _search_tools


def test_hybrid_navigation_retains_per_query_top8_and_shared_memberships():
    chunks = [_chunk(f'c{i}', f'第{i}条', f'正文{i}', index=i) for i in range(10)]
    hits = [hit_from_chunk(c, i) for i, c in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *_a, **_kw: [[hits[0]], [hits[0]]], top_k=1)
    queries = [RetrievalQuery(query_id=q, query_type='legal_issue', text=q) for q in ['q1', 'q2']]
    entries = tools._search_navigation_entries(queries, [hits, hits], [hits[0]])
    assert [e['chunk_id'] for e in entries] == [f'c{i}' for i in range(1, 8)]
    assert entries[5]['query_matches'] == [{'query_id': 'q1', 'rank': 7}, {'query_id': 'q2', 'rank': 7}]
    tools._adapters.vector.search_many = lambda *_a, **_kw: [hits, hits]
    tools.search(queries, ReviewFacts())
    assert any(loc['chunk_id'] == 'c1' for s in tools.search_navigation() for loc in s['locations'])


def test_parallel_reads_merge_in_request_order_and_keep_partial_success():
    hit = hit_from_chunk(_chunk('known', '第一条', '原文', index=0), 0)
    state = AgentState(goal='核对', evidence=[hit], max_reads=4)
    main_thread = get_ident()
    checkpoint_threads = []
    barrier = Barrier(3)
    completed_second = Event()

    def read(source, article, chunk, facts, offset):
        assert get_ident() != main_thread
        barrier.wait(timeout=3)
        if article == '第一条':
            assert completed_second.wait(timeout=3)
        if article == '第二条':
            completed_second.set()
        if article == '第三条':
            raise ValueError('条文不存在')
        return EvidenceRead(hits=[hit.model_copy(update={'chunk_id': article})])

    requests = [EvidenceReadRequest(source_id='s1', article_no=a) for a in ['第一条', '第二条', '第三条']]
    results = _read_evidence_batch(state, requests + [requests[0]], read_evidence=read,
                                   checkpoint=lambda _: checkpoint_threads.append(get_ident()))
    assert [r.get('chunk_ids') for r in results[:2]] == [['第一条'], ['第二条']]
    assert '条文不存在' in results[2]['error']
    assert '重复' in results[3]['error']
    assert state.reads == 3
    assert [r.returned_chunk_ids for r in state.read_history] == [['第一条'], ['第二条']]
    assert [h.chunk_id for h in state.evidence] == ['known', '第一条', '第二条']
    assert set(checkpoint_threads) == {main_thread}


def test_read_budget_is_reserved_before_parallel_dispatch():
    hit = hit_from_chunk(_chunk('known', '第一条', '原文', index=0), 0)
    state = AgentState(goal='核对', evidence=[hit], max_reads=1)
    calls = []

    def read(*args):
        calls.append(args[1])
        return EvidenceRead(hits=[hit])

    results = _read_evidence_batch(state, [EvidenceReadRequest(source_id='s1', article_no=a)
                                          for a in ['第一条', '第二条']], read_evidence=read, checkpoint=lambda _: None)
    assert calls == ['第一条'] and state.reads == 1
    assert '预算已用尽' in results[1]['error']


def test_query_association_update_does_not_repeat_preview():
    from law_agent.review.agent import AgentStep

    old = {'source_id': 's', 'title': '法', 'locations': [{'chunk_id': 'c', 'preview': '原文',
           'query_matches': [{'query_id': 'q1', 'rank': 6}]}]}
    new = {**old, 'locations': [{**old['locations'][0], 'query_matches': [{'query_id': 'q2', 'rank': 2}]}]}
    steps = [AgentStep(number=1, action='search_evidence', summary='发现', observation={'candidate_navigation': [old]})]
    result = search_observation_context({'candidate_navigation': [new]}, steps, [])
    loc = result['candidate_navigation'][0]['locations'][0]
    assert loc['query_matches'] == [{'query_id': 'q2', 'rank': 2}] and 'preview' not in loc
