"""Exercise production local-evidence paths with real retrieval and scripted decisions."""
import json
from unittest.mock import patch

from law_agent.config import require_agent_llm_config
from law_agent.review import agent_runtime
from law_agent.review.agent import AgentDecision, EvidenceReadRequest
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.remediation import RemediationDecision, RereviewPacket, execute_rereview
from law_agent.review.schemas import RetrievalQuery


def actor(decision_type, receipt):
    def decide(state, _intake):
        if not state.searches:
            return decision_type(action='search_evidence', summary='验证正式检索入口', max_chunks=2,
                queries=[RetrievalQuery(query_id='q1', query_type='legal_issue',
                    text='促进和规范数据跨境流动规定 第七条 第八条 安全评估 标准合同 门槛'),
                    RetrievalQuery(query_id='q2', query_type='legal_issue',
                    text='个人信息保护法 第十三条 单独同意 例外 人力资源管理')])
        if not state.reads:
            observation = state.steps[-1].observation
            assert 'error' not in observation, observation.get('error')
            assert len(state.evidence) <= 2
            candidates = [(s['source_id'], loc) for s in observation['candidate_navigation'] for loc in s['locations']]
            assert len(candidates) >= 2
            for _, loc in candidates:
                assert 0 < len(loc['preview']) <= 100
                assert all(1 <= q['rank'] <= 8 for q in loc['query_matches'])
            selected = candidates[:2]
            receipt['selected_chunks'] = [loc['chunk_id'] for _, loc in selected]
            receipt['navigation_items'] = len(candidates)
            return decision_type(action='read_evidence', summary='批量定位两处原文',
                read_requests=[EvidenceReadRequest(source_id=source, chunk_id=loc['chunk_id']) for source, loc in selected])
        results = state.steps[-1].observation['read_results']
        assert [r.get('chunk_ids') for r in results] == [[k] for k in receipt['selected_chunks']], results
        assert state.reads == 2 and len(state.read_history) == 2
        receipt.update(searches=state.searches, reads=state.reads, chunk_only=True, ordered_merge=True)
        return decision_type(action='request_input', summary='验证完成，未作业务结论', question='此项为内存中的正式入口机制校验。')
    return decide


def main():
    cfg = require_agent_llm_config()
    store = InMemoryEnterpriseStore()
    version = store.create_material_version(case_id='navigation_probe', logical_name='校验材料', filename='probe.txt',
        content_type='text/plain', object_key='probe', sha256='a' * 64, byte_size=0, uploaded_by='probe')
    material = store.create_material_snapshot(case_id='navigation_probe', version_ids=[version.id], created_by='probe')
    intake = store.create_intake_snapshot(case_id='navigation_probe', material_snapshot_id=material.id, intake={}, created_by='probe')
    store.enqueue_review_task(case_id='navigation_probe', material_snapshot_id=material.id,
        intake_snapshot_id=intake.id, model_id=cfg.model, data_boundary_summary={})
    task = store.claim_next_task(worker_id='in_memory_probe')
    review = {}
    with patch.object(agent_runtime, 'AgentModel', lambda **_: actor(AgentDecision, review)):
        state = agent_runtime.execute_agent_task(task, store=store, goal='正式本地检索连接校验', material='机制校验')
    assert state.status == 'waiting_input'
    remediation = {}
    state = execute_rereview(packet=RereviewPacket(text='机制校验，不包含整改实施证据'), goal='复核本地检索连接校验',
        state=None, model_id=cfg.model, chunks_path='/app/data/corpus/legal_docs_20260702/chunks.jsonl',
        decide=actor(RemediationDecision, remediation))
    assert state.status == 'waiting_input'
    print(json.dumps({'review': review, 'remediation': remediation, 'chat_model_calls': 0,
                      'persistent_business_records_written': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
