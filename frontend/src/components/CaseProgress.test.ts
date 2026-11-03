import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { CaseProgressProps } from './CaseProgress';
let server: ViteDevServer;
let Component: (props: CaseProgressProps) => JSX.Element;
before(async () => { server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' }); Component = (await server.ssrLoadModule('/src/components/CaseProgress.tsx')).default; });
after(async () => { await server?.close(); });
const plan = (tasks: object[]) => ({ tasks }) as CaseProgressProps['remediationPlan'];
const draw = (patch: Partial<CaseProgressProps>) => renderToStaticMarkup(createElement(Component, { status: 'needs_info', viewerRole: 'requester', caseId: 'case', canRereview: true, ...patch }));
test('unfinished blocking work prevents re-review while completed required work unlocks it', () => {
  const pending = draw({ remediationPlan: plan([{ phase: 'pre_approval', blocking: true, status: 'pending_review' }]) });
  assert.doesNotMatch(pending, />重新审查<\/button>/);
  assert.match(pending, /0\/1 完成/);
  const ready = draw({ remediationPlan: plan([{ phase: 'pre_approval', blocking: true, status: 'completed' }, { phase: 'pre_approval', blocking: false, status: 'open' }]) });
  assert.match(ready, />重新审查<\/button>/);
  assert.match(ready, /1\/1 完成/);
});
test('non-owner and reviewer cannot re-review; live questions take priority', () => {
  const remediationPlan = plan([{ phase: 'pre_approval', blocking: true, status: 'completed' }]);
  for (const patch of [{ canRereview: false }, { viewerRole: 'reviewer' as const }]) assert.doesNotMatch(draw({ remediationPlan, ...patch }), />重新审查<\/button>/);
  const waiting = draw({ remediationPlan, reviewTaskStatus: 'waiting_input' });
  assert.match(waiting, /回答 CrossComply 问题/);
  assert.doesNotMatch(waiting, />重新审查<\/button>/);
});
