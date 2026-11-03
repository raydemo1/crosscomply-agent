"""Collect actual costs and visible-evidence traces without invoking providers."""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from law_agent.review.retrieval.corpus import load_corpus
from scripts.assess_qa_retrieval_chain import audit_case
from scripts.eval_candidate_navigation import OUT, CASES, baseline, read, write


def main():
    corpus = {c.chunk_id: c.model_dump(mode='json') for c in load_corpus()}
    rows = []
    for mode in ['baseline', 'A', 'B', 'C']:
        for cid in CASES:
            dest = baseline(cid) if mode == 'baseline' else OUT / mode / cid
            if not (dest / 'conversation.json').exists():
                continue
            calls = read(dest / 'calls.json')
            research = read(dest / 'research.json')
            audit, registry = audit_case(dest, corpus)
            phases = {}
            for phase in sorted({c.get('phase', 'unknown') for c in calls}):
                selected = [c for c in calls if c.get('phase', 'unknown') == phase]
                phases[phase] = {'requests': len(selected), 'prompt_tokens': sum(c.get('prompt_tokens') or 0 for c in selected),
                                 'completion_tokens': sum(c.get('completion_tokens') or 0 for c in selected),
                                 'missing_usage': sum(not c.get('usage_available') for c in selected),
                                 'latency_ms': sum(c.get('latency_ms') or 0 for c in selected)}
            decisions = read(dest / 'decisions.json')
            nav = [loc for r in research if r['kind'] == 'search' for s in r.get('navigation', []) for loc in s.get('locations', [])]
            row = {'mode': mode, 'case_id': cid, 'status': audit['native_status'], 'history_retained': audit['history_retained'],
                   'phases': phases, 'requests': len(calls), 'total_tokens': sum((c.get('prompt_tokens') or 0) + (c.get('completion_tokens') or 0) for c in calls),
                   'missing_usage_requests': sum(not c.get('usage_available') for c in calls),
                   'tool_counts': dict(Counter(r['kind'] for r in research)),
                   'read_batch_sizes': [len(d.get('read_requests', [])) for d in decisions if d['action'] == 'read_evidence'],
                   'navigation_items': len(nav), 'navigation_preview_chars': sum(len(x.get('preview', '')) for x in nav),
                   'initial_counts': audit['initial_counts'], 'pre_answer_counts': audit['pre_answer_counts'],
                   'pre_answer_seen_ids': audit['pre_answer_seen_ids'],
                   'originals': [{'id': k, 'source_id': v.get('source_id'), 'article_no': v.get('article_no')} for k, v in registry.items()]}
            if cid == 'agent_small_sensitive_001':
                target = 'cac_cross_border_data_flow_rules_2024:52ec835a9967001c8934f41e'
                row['future_threshold_article7'] = {'candidate_visible': any(x['chunk_id'] == target for x in nav),
                                                    'original_seen': any(k.split('#')[0] == target for k in audit['pre_answer_seen_ids']),
                                                    'core_requirement': False}
            rows.append(row)
    preparation = [read(p) for p in sorted((OUT / 'summary_calls').glob('*.result.json'))]
    static = [r for r in preparation if r.get('phase') == 'navigation_static_preparation']
    totals = []
    for mode in ['baseline', 'A', 'B', 'C']:
        rs = [r for r in rows if r['mode'] == mode]
        totals.append({'mode': mode, 'completed_cases': len(rs), 'requests': sum(r['requests'] for r in rs),
                       'total_tokens': sum(r['total_tokens'] for r in rs),
                       'missing_usage_requests': sum(r['missing_usage_requests'] for r in rs),
                       'initial_body_chars': sum(r['initial_counts']['body_chars'] for r in rs),
                       'pre_answer_body_chars': sum(r['pre_answer_counts']['body_chars'] for r in rs),
                       'read_batch_sizes': [n for r in rs for n in r['read_batch_sizes']]})
    result = {'rows': rows, 'totals': totals, 'static_preparation': {
              'requests': len(static), 'failed_requests': sum(r['status'] != 'completed' for r in static),
              'missing_usage_requests': sum(not u.get('usage_available') for r in static for u in r['usage']),
              'prompt_tokens': sum(u.get('prompt_tokens') or 0 for r in static for u in r['usage']),
              'completion_tokens': sum(u.get('completion_tokens') or 0 for r in static for u in r['usage'])},
              'limits': 'Visible evidence and cost only, not correctness. Static preparation excluded from runtime totals and listed separately. Old generations reused.'}
    write(OUT / 'comparison.json', result)
    print(json.dumps({'totals': totals, 'static_preparation': result['static_preparation']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
