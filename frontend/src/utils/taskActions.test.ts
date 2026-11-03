import assert from 'node:assert/strict';
import test from 'node:test';
import { actionableTasks } from './taskActions.ts';
import type { RemediationTaskApi, WorkbenchUser } from '../types/api.ts';
const user = { id: 'reviewer', role: 'reviewer' } as WorkbenchUser;
const task = (patch: Partial<RemediationTaskApi>) => ({ id: 'task', status: 'open', phase: 'pre_approval', is_current: true, submissions: [], ...patch }) as RemediationTaskApi;
test('badge counts own execution and independent review without double counting', () => {
  const items = [task({ assignee_id: user.id }), task({ status: 'pending_review', submissions: [{ submitted_by: 'other' }] as RemediationTaskApi['submissions'] }), task({ status: 'pending_review', submissions: [{ submitted_by: user.id }] as RemediationTaskApi['submissions'] }), task({ assignee_id: user.id, is_current: false }), task({ assignee_id: 'other' }), task({ assignee_id: user.id, phase: 'post_approval', case_status: 'needs_info' })];
  assert.equal(actionableTasks(items, user).length, 2);
  assert.equal(actionableTasks(items, { ...user, role: 'requester' }).length, 1);
});
test('post-approval work only counts once approved, completed work stays out', () => {
  assert.equal(actionableTasks([task({ assignee_id: user.id, phase: 'post_approval', case_status: 'approved' }), task({ assignee_id: user.id, status: 'completed' })], user).length, 1);
});
