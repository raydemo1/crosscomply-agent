"""Assess the four frozen preview variants, separating retrieval from answer quality."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from law_agent.config import load_env_file, require_agent_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.usage import usage_scope
from law_agent.review.retrieval.corpus import load_corpus
from scripts.assess_qa_retrieval_chain import audit_case, reference_texts, validate, PROMPT as RETRIEVAL_PROMPT
from scripts.assess_qa_answer_changes import PROMPT as ANSWER_PROMPT
from scripts.eval_candidate_navigation import OUT, CASES, read, write, baseline
from scripts.eval_qa_retrieval_chain import FIXTURE


def main():
    load_env_file()
    cfg = require_agent_llm_config()
    assert cfg.model == 'deepseek-flash'
    cfg = replace(cfg, timeout_seconds=180, read_timeout_seconds=180, total_timeout_seconds=300)
    client = OpenAICompatibleClient(cfg)
    corpus = {c.chunk_id: c.model_dump(mode='json') for c in load_corpus()}
    fixtures = {c['case_id']: c for c in read(FIXTURE)['cases']}
    results = []
    for index, cid in enumerate(CASES):
        order = ['baseline', 'A', 'B', 'C']
        random.Random(20261030 + index).shuffle(order)
        packages = []
        audits = []
        for label, mode in zip('ABCD', order, strict=True):
            dest = baseline(cid) if mode == 'baseline' else OUT / mode / cid
            if not (dest / 'conversation.json').exists():
                raise RuntimeError(f'Run incomplete: {mode}/{cid}')
            audit, registry = audit_case(dest, corpus)
            assert audit['history_retained']
            package = {'label': label, 'source_text_registry': registry,
                       **{k: audit[k] for k in ['initial_visible_ids', 'pre_answer_seen_ids', 'native_status', 'awaiting_user']},
                       'answer': (dest / 'answer.md').read_text(encoding='utf-8'),
                       'citation_mapping': [{k: c.get(k) for k in ['ref', 'chunk_id', 'url', 'kind']}
                                            for c in (read(dest / 'conversation.json')['runs'][-1].get('result') or {}).get('citations', [])]}
            packages.append(package)
            audits.append({'mode': mode, **audit})
        material = read(baseline(cid) / 'case.json')
        case = fixtures[cid]
        for stage in ['retrieval', 'answer']:
            dest = OUT / 'assessment' / stage / cid
            prompt = RETRIEVAL_PROMPT if stage == 'retrieval' else ANSWER_PROMPT.replace('两个匿名方案', '四个匿名方案').replace('A和B均且仅一次', 'A、B、C、D均且仅一次')
            prompt += '\n本次比较四个匿名方案A、B、C、D；各自的source_text_registry位于方案内部，不得跨方案使用。逐个方案评价，不要求固定检索路径。'
            inputs = [{k: v for k, v in p.items() if stage == 'answer' or k not in {'answer', 'citation_mapping'}} for p in packages]
            payload = {'question': material['question'], 'material': material['material'],
                       'rubric': case['case']['rubric'], 'requirement_drafts': case['requirement_drafts'],
                       'packages': inputs, 'reference_only_not_retrieved': reference_texts(cid, corpus)}
            signature = hashlib.sha256(json.dumps({'prompt': prompt, 'input': payload}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            mapping = dict(zip('ABCD', order, strict=True))
            if (dest / 'raw_result.json').exists():
                assert read(dest / 'input_sha256.json') == signature
                result = read(dest / 'raw_result.json')
            else:
                write(dest / 'input.json', payload)
                write(dest / 'input_sha256.json', signature)
                write(dest / 'labels.json', mapping)
                usage = []
                print('EVALUATE', stage, cid, flush=True)
                try:
                    with usage_scope('candidate_navigation_' + stage + '_assessment', usage.append):
                        result = client.chat_json([ChatMessage('system', prompt), ChatMessage('user', json.dumps(payload, ensure_ascii=False))],
                                                  structured_output_mode='json_object', stream=True)
                    write(dest / 'raw_result.json', result)
                finally:
                    write(dest / 'usage.json', usage)
            errors = validate(result, payload)
            if stage == 'answer':
                for p in result.get('packages', []):
                    original = next(x for x in packages if x['label'] == p['label'])
                    for issue in p.get('claim_issues', []):
                        if not issue.get('quote') or issue['quote'] not in original['answer']:
                            errors.append(p['label'] + ': claim quote is not literal')
                        if set(issue.get('supporting_ids', [])) - set(original['pre_answer_seen_ids']):
                            errors.append(p['label'] + ': claim references unseen evidence')
            row = {'case_id': cid, 'stage': stage, 'labels': mapping, 'review': result, 'qualification_errors': errors}
            write(dest / 'audit.json', audits)
            results.append(row)
            write(OUT / 'assessment/summary.json', results)
            print('DONE', stage, cid, 'qualification_errors', len(errors), flush=True)
    write(OUT / 'assessment/manifest.json', {'status': 'completed', 'model': cfg.model, 'cases': CASES,
          'reviews': len(results), 'separate_retrieval_and_answer': True, 'baseline_generation_repeated': False,
          'limits': 'Single evaluator and draft evidence groups; manual original-text audit required; not formal accuracy.'})


if __name__ == '__main__':
    main()
