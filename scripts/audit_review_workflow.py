"""Replay saved report contracts without model calls or rewriting old results."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from law_agent.review.remediation import guided_actions_from_review
from law_agent.review.workflow import (
    review_has_missing_information,
    review_needs_source_verification,
    review_requires_retry,
)


def audit_payload(payload: dict) -> dict:
    review = payload['review_result']
    laws = {str(q).strip() for q in review.get('legal_basis_gaps') or []}
    questions = {str(q).strip() for q in review.get('missing_information') or [] if str(q).strip()} - laws
    actions = guided_actions_from_review(review, requester_id='offline-audit')
    fact_actions = [a for a in actions if a['task_kind'] == 'fact_confirmation']
    unlinked = [{'title': issue.get('title'), 'kind': issue.get('kind'),
                 'unknowns': issue.get('unknowns') or [],
                 'unlisted': [q for q in issue.get('unknowns') or [] if str(q).strip() not in questions]}
                for issue in review.get('issues') or []
                if issue.get('kind') in {'missing_information', 'material_conflict'}
                and (not issue.get('unknowns') or any(str(q).strip() not in questions for q in issue['unknowns']))]
    return {'risk_level': review.get('risk_level'), 'missing_information': sorted(questions),
            'legal_basis_gaps': sorted(laws), 'unlinked_issues': unlinked,
            'fact_action_count': len(fact_actions), 'action_count': len(actions),
            'blocked_without_fact_or_source_gap': bool(review_has_missing_information(payload)
                and not review_needs_source_verification(payload) and not questions),
            'requires_retry': review_requires_retry(payload),
            'note': 'Exact unlinked text is a structural discrepancy, not proof of semantic omission. Review against the complete list; do not fuzzy-match or rewrite saved reports.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('data/review_runs'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.root.rglob('payload.json')):
        raw = path.read_bytes()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get('review_result'), dict):
            continue
        rows.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), **audit_payload(payload)})
        assert path.read_bytes() == raw
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'reports': len(rows), 'unlinked_reports': sum(bool(r['unlinked_issues']) for r in rows),
                      'blocked_without_fact_or_source_gap': sum(r['blocked_without_fact_or_source_gap'] for r in rows)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
