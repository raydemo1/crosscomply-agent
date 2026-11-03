import pytest
from pydantic import ValidationError

from law_agent.review.agent import EvidenceReadRequest, INVESTIGATION_TOOLS
from law_agent.review.remediation import RemediationAgentModel, RemediationDecision, execute_rereview
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import RetrievalQuery
from tests.test_remediation_assessment import _draft, _packet
from tests.test_review_agent import _chunk, _search_tools
from tests.test_review_llm import FakeClient


def test_remediation_registers_shared_search_and_batch_read_tools():
    model = RemediationAgentModel(model_id='test', client=FakeClient())
    tools = {t['function']['name']: t['function'] for t in model.protocol.tools}
    assert 'search_web' not in tools
    for name in ['search_evidence', 'read_evidence']:
        assert tools[name]['description'] == INVESTIGATION_TOOLS[name].description
    schema = tools['read_evidence']['parameters']['properties']['read_requests']
    assert schema['maxItems'] == 4
    with pytest.raises(ValidationError):
        RemediationDecision(action='read_evidence', summary='读原文')
    with pytest.raises(ValidationError):
        RemediationDecision(action='finish', summary='交付', draft=_draft(),
                            read_requests=[EvidenceReadRequest(source_id='s1', article_no='第二条')])


def test_remediation_default_tools_deliver_navigation_and_read_original(monkeypatch):
    from law_agent.review import remediation

    chunks = [_chunk('main', '第一条', '正文' * 80, index=0),
              _chunk('candidate', '第二条', '需要核对的适用条件。', index=1)]
    hits = [hit_from_chunk(c, i).model_copy(update={'score': 100 - i}) for i, c in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *_a, **_kw: [hits], top_k=1)
    closed = []
    tools.close = lambda: closed.append(True)
    monkeypatch.setattr(remediation, 'ComplianceAgentTools', lambda **_: tools)

    def decide(state, _intake):
        if not state.steps:
            return RemediationDecision(action='search_evidence', summary='核对法律条件', max_chunks=1,
                queries=[RetrievalQuery(query_id='q', text='适用条件', query_type='legal_issue')])
        if not state.reads:
            source = state.steps[0].observation['candidate_navigation'][0]
            location = source['locations'][0]
            assert location['chunk_id'] == 'candidate'
            assert location['preview'] == chunks[1].text
            assert location['query_matches'][0]['rank'] == 2
            assert {h.chunk_id for h in state.evidence} == {'main'}
            return RemediationDecision(action='read_evidence', summary='取得候选原文',
                read_requests=[EvidenceReadRequest(source_id=source['source_id'], chunk_id=location['chunk_id'])])
        assert [h.chunk_id for h in state.evidence] == ['main', 'candidate']
        return RemediationDecision(action='finish', summary='仍需实施证明', draft=_draft(
            status='insufficient_evidence', confirmed_points=[], remaining_gaps=['缺少实施材料'],
            next_request='请提供设置截图。'))

    result = execute_rereview(packet=_packet(), goal='复核', state=None, model_id='test',
                             chunks_path='unused', decide=decide)
    assert result.status == 'completed' and result.reads == 1
    assert result.result['status'] == 'insufficient_evidence'
    assert closed == [True]
