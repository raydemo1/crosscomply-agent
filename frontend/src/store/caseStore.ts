/** Server-backed case store for the CrossComply workbench. */

import { useSyncExternalStore } from 'react';
import {
  getCaseDetail,
  listCases,
  saveFeedback,
} from '../api/client';
import type { CaseDetailApi, CaseIntake, CaseSummaryApi } from '../types/api';
import type { CaseFeedback, CitationVerdict, SavedCase } from '../types/case';

const listeners = new Set<() => void>();
let snapshot: SavedCase[] = [];

export const EMPTY_INTAKE: CaseIntake = {
  business_activity: '',
  data_types: [],
  sensitive_personal_info: null,
  cross_border_transfer: null,
  important_data_status: 'unknown',
  ciio_status: 'unknown',
  annual_non_sensitive_count: '',
  annual_sensitive_count: '',
  overseas_recipient: '',
  destination_region: '',
  processing_purpose: '',
  transfer_mechanism: '',
  vendor_name: '',
  contract_status: '',
  legal_basis_or_consent: '',
  notes: '',
};

function notify(): void {
  listeners.forEach((listener) => listener());
}

/**
 * The demo fixture is injected at runtime instead of being imported, so the
 * production bundle never carries demo data. When the public demo is enabled,
 * the store holds the fixture in memory; otherwise `demoCase` stays null and
 * `openCase` never touches it.
 */
let demoCase: SavedCase | null = null;

export function initializeDemoCase(demo: SavedCase): void {
  demoCase = demo;
  snapshot = [demo];
  notify();
}

function toFeedback(detail: CaseDetailApi): CaseFeedback | null {
  if (!detail.feedback) return null;
  return {
    conclusionUseful: detail.feedback.conclusion_useful,
    citationVerdicts: detail.feedback.citation_verdicts,
    missingSources: detail.feedback.missing_sources,
    notes: detail.feedback.notes,
    updatedAt: detail.feedback.updated_at,
  };
}

function fromSummary(item: CaseSummaryApi): SavedCase {
  return {
    id: item.id,
    traceId: '',
    savedAt: item.updated_at,
    question: item.question,
    materialText: '',
    materialSource: null,
    response: null,
    riskLevel: item.risk_level,
    status: item.status,
    intake: { ...EMPTY_INTAKE },
    actions: [],
    events: [],
    feedback: null,
    materialSnapshot: null,
    ruleDecision: null,
    reviewTask: null,
    feishuApproval: null,
    signedDecision: null,
    report: null,
    remediationPlan: null,
  };
}

export function fromDetail(detail: CaseDetailApi): SavedCase {
  const item = detail.case;
  return {
    id: item.id,
    traceId: item.trace_id ?? '',
    savedAt: item.updated_at,
    question: item.question,
    materialText: item.material_text,
    materialSource: item.material_source,
    response: item.response,
    riskLevel: item.risk_level ?? (
      item.response && 'review_result' in item.response
        ? item.response.review_result.risk_level
        : null
    ),
    status: item.status,
    intake: item.intake,
    actions: detail.actions ?? [],
    events: detail.events,
    feedback: toFeedback(detail),
    materialSnapshot: detail.material_snapshot,
    ruleDecision: detail.rule_decision,
    reviewTask: detail.review_task,
    feishuApproval: detail.feishu_approval,
    signedDecision: detail.signed_decision,
    report: detail.report,
    remediationPlan: detail.remediation_plan ?? null,
  };
}

function mergeCase(next: SavedCase): void {
  const index = snapshot.findIndex((item) => item.id === next.id);
  if (index < 0) {
    snapshot = [next, ...snapshot];
  } else {
    const copy = [...snapshot];
    copy[index] = next;
    snapshot = copy;
  }
  notify();
}

export async function refreshCases(): Promise<SavedCase[]> {
  const result = await listCases();
  const summaries = result.items.map(fromSummary);
  snapshot = summaries.map((summary) => snapshot.find((item) => item.id === summary.id) ?? summary);
  notify();
  return snapshot;
}

export async function openCase(id: string): Promise<SavedCase> {
  if (demoCase && id === demoCase.id) {
    mergeCase(demoCase);
    return demoCase;
  }
  const detail = await getCaseDetail(id);
  const next = fromDetail(detail);
  mergeCase(next);
  return next;
}

export function getCase(id: string): SavedCase | null {
  return snapshot.find((item) => item.id === id) ?? null;
}

export function useCaseStore(): SavedCase[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}

async function persistFeedback(id: string, patch: Partial<CaseFeedback>): Promise<void> {
  const current = getCase(id);
  if (!current) return;
  const feedback = current.feedback;
  const saved = await saveFeedback(id, {
    conclusion_useful: patch.conclusionUseful ?? feedback?.conclusionUseful ?? null,
    missing_sources: patch.missingSources ?? feedback?.missingSources ?? '',
    notes: patch.notes ?? feedback?.notes ?? '',
    citation_verdicts: patch.citationVerdicts ?? feedback?.citationVerdicts ?? {},
  });
  const detail = await getCaseDetail(id);
  mergeCase(fromDetail({ ...detail, feedback: saved }));
}

export function setCitationVerdict(id: string, chunkId: string, verdict: CitationVerdict | null): void {
  const current = getCase(id);
  if (!current) return;
  const verdicts = { ...(current.feedback?.citationVerdicts ?? {}) };
  if (verdict === null) delete verdicts[chunkId];
  else verdicts[chunkId] = verdict;
  void persistFeedback(id, { citationVerdicts: verdicts });
}

export function setConclusionUseful(id: string, useful: boolean | null): void {
  void persistFeedback(id, { conclusionUseful: useful });
}

export function setFeedbackText(id: string, field: 'missingSources' | 'notes', value: string): void {
  void persistFeedback(id, { [field]: value });
}
