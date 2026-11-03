"""Validate the remaining seven QA cases against cached adopted-version results."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.eval_candidate_navigation import OUT, baseline, read, write
from scripts.eval_qa_retrieval_chain import FIXTURE, source_hashes

SOURCE = OUT / 'A_remaining_20261030'
BASELINE = OUT / 'baseline_remaining_20261030'


def prepare():
    previous = read(OUT / 'A/manifest.json')
    assert source_hashes() == previous['source_hashes']
    cases = [c['case_id'] for c in read(FIXTURE)['cases'] if c['entry'] == 'qa'
             and c['case_id'] not in previous['cases']]
    assert len(cases) == 7
    manifest = {'cases': cases, 'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                'generation_repeated': False, 'sources': {}}
    for cid in cases:
        src, dst = baseline(cid), BASELINE / cid
        dst.mkdir(parents=True, exist_ok=True)
        for path in src.iterdir():
            if path.suffix in {'.json', '.md'}:
                target = dst / path.name
                if target.exists():
                    assert target.read_bytes() == path.read_bytes()
                else:
                    shutil.copy2(path, target)
        manifest['sources'][cid] = str(src.relative_to(ROOT))
    write(BASELINE / 'manifest.json', manifest)
    write(OUT / 'adoption.json', {'date': '2026-10-30', 'adopted': 'A',
          'preview': 'Original first100 characters; no summary calls',
          'navigation': 'Unreturned/unread positions among top8 fused ranks per query; no refill',
          'body_cap': 10, 'read_batch_cap': 4, 'chunk_read': 'Only selected chunk; no neighbors',
          'rerank': 'off', 'summary_enabled': False, 'new_generation_cases': cases,
          'previous_four_reused': previous['cases'], 'source_hashes': source_hashes(),
          'baseline_generation_repeated': False})
    print('PREPARED', len(cases), 'cached baselines; policy A adopted', flush=True)


def assess():
    for script, dest, extra in [
        ('assess_qa_retrieval_chain.py', 'remaining_retrieval_assessment_20261030', []),
        ('assess_qa_answer_changes.py', 'remaining_answer_assessment_20261030', ['--baseline', str(BASELINE)]),
    ]:
        command = [sys.executable, str(ROOT / 'scripts' / script), '--source', str(SOURCE),
                   '--output', str(OUT / dest), '--follow', *extra]
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            raise SystemExit(result.returncode)


def summarize():
    rows = []
    for cid in read(BASELINE / 'manifest.json')['cases']:
        if not (SOURCE / cid / 'summary.json').exists():
            continue
        new, old = read(SOURCE / cid / 'summary.json'), read(BASELINE / cid / 'summary.json')
        assert read(SOURCE / cid / 'case.json') == read(BASELINE / cid / 'case.json')
        calls = read(SOURCE / cid / 'calls.json')
        rows.append({'case_id': cid, 'status': new['status'], 'awaiting_user': new['awaiting_user'],
                     'old_requests': old['actual_model_requests'], 'new_requests': new['actual_model_requests'],
                     'old_tokens': old['prompt_tokens'] + old['completion_tokens'],
                     'new_tokens': new['prompt_tokens'] + new['completion_tokens'],
                     'missing_usage_requests': sum(not c.get('usage_available') for c in calls),
                     'tool_counts': new['tool_counts']})
    result = {'cases': rows, 'old_tokens': sum(r['old_tokens'] for r in rows),
              'new_tokens': sum(r['new_tokens'] for r in rows),
              'old_requests': sum(r['old_requests'] for r in rows), 'new_requests': sum(r['new_requests'] for r in rows),
              'missing_usage_requests': sum(r['missing_usage_requests'] for r in rows)}
    for stage, name in [('retrieval', 'remaining_retrieval_assessment_20261030'),
                        ('answer', 'remaining_answer_assessment_20261030')]:
        path = OUT / name / 'summary.json'
        if path.exists():
            result[stage + '_assessment'] = read(path)
    write(OUT / 'remaining_comparison_20261030.json', result)
    print(json.dumps({k: v for k, v in result.items() if not isinstance(v, list)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['prepare', 'assess', 'summarize'])
    args = parser.parse_args()
    globals()[args.stage]()
