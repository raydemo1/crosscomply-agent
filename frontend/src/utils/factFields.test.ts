import assert from 'node:assert/strict';
import test from 'node:test';
import type { SavedCase } from '../types/case';
import { caseFactLedger, factLabel, factValue } from './factFields.ts';

test('false and zero remain facts, unknown and estimates keep their meaning', () => {
  assert.equal(factValue('contains_personal_information', false), '否');
  assert.equal(factValue('annual_sensitive_count', 0), '0');
  assert.equal(factValue('ciio_status', 'unknown'), '尚未确认');
  assert.equal(factValue('count_period', 'annual_estimate'), '全年估算');
  assert.equal(factLabel('important_data_status'), '重要数据情况');
});

test('conversation proposals remain unverified and isolate task and reviewer sources', () => {
  const saved = {
    materialSnapshot: { id: 'material' }, intakeSnapshot: { id: 'intake' },
    reviewTask: { id: 'task', material_snapshot_id: 'material', intake_snapshot_id: 'intake', agent_state: { fact_ledger: [] } },
    events: [
      { id: 'statement', event_type: 'matter_agent_turn', payload: { task_id: 'task', input_provenance: 'applicant_statement', reply: { proposed_facts: { destination_region: '日本' } } } },
      { id: 'old', event_type: 'matter_agent_turn', payload: { task_id: 'old-task', input_provenance: 'applicant_statement', reply: { proposed_facts: { destination_region: '美国' } } } },
      { id: 'instruction', event_type: 'matter_agent_turn', payload: { task_id: 'task', input_provenance: 'reviewer_instruction', reply: { proposed_facts: { destination_region: '新加坡' } } } },
    ],
  } as unknown as SavedCase;
  assert.deepEqual(caseFactLedger(saved), [{ field: 'destination_region', value: '日本', source_type: 'applicant_statement', status: 'unverified', source_ref: 'statement' }]);
  saved.intakeSnapshot!.id = 'new-intake';
  assert.deepEqual(caseFactLedger(saved), []);
});
