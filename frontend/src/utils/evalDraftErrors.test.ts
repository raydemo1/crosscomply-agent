import assert from 'node:assert/strict';
import test from 'node:test';
import { groupDraftErrors } from './evalDraftErrors.ts';

test('validation errors are placed beside the closest visible field', () => {
  const errors = [
    { field: 'intake.business_entity', message: '疑似残留原申报主体', offset: 8, masked: '****' },
    { field: 'rubric.allowed_judgments[0]', message: '请填写此项' },
    { field: 'unexpected', message: '字段值无效' },
  ];
  assert.deepEqual(groupDraftErrors(errors, ['intake', 'rubric', 'rubric.allowed_judgments']), {
    intake: [errors[0]], 'rubric.allowed_judgments': [errors[1]], form: [errors[2]],
  });
});
