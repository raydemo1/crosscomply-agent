/**
 * Case-store domain types for the server-backed CrossComply workbench.
 */

import type { ReviewApiResponse, RiskLevel } from './api';
import type {
  CaseAction,
  CaseEvent,
  CaseIntake,
  CaseStatus,
  FeishuApprovalApi,
  MaterialSnapshotApi,
  ReportRecordApi,
  ReviewTaskApi,
  RuleDecisionApi,
  RemediationPlanApi,
  SignedDecisionApi,
} from './api';

/** Per-citation human feedback (keyed by `chunk_id`). */
export type CitationVerdict = 'correct' | 'wrong';

/** Human feedback attached to a saved review case. */
export interface CaseFeedback {
  /** Whether the overall conclusion was useful. `null` = not yet rated. */
  conclusionUseful: boolean | null;
  /** Per-citation verdicts, keyed by `chunk_id`. */
  citationVerdicts: Record<string, CitationVerdict>;
  /** Free-text describing sources the user felt were missing. */
  missingSources: string;
  /** General reviewer notes. */
  notes: string;
  /** ISO timestamp of the last feedback update. */
  updatedAt: string;
}

/**
 * A case record loaded from the CrossComply API.
 */
export interface SavedCase {
  /** Stable case id from the backend. */
  id: string;
  /** Trace id from the latest backend response. */
  traceId: string;
  /** Last server update timestamp. */
  savedAt: string;
  /** The case title/question shown in the workbench. */
  question: string;
  /** Server-persisted material text. */
  materialText: string;
  /** Source name for uploaded files. */
  materialSource: string | null;
  /** Full result when the case has been run; null for a draft. */
  response: ReviewApiResponse | null;
  /** Persisted risk level, available from both list and detail responses. */
  riskLevel: RiskLevel | null;
  status: CaseStatus;
  intake: CaseIntake;
  actions: CaseAction[];
  events: CaseEvent[];
  feedback: CaseFeedback | null;
  materialSnapshot: MaterialSnapshotApi | null;
  ruleDecision: RuleDecisionApi | null;
  reviewTask: ReviewTaskApi | null;
  feishuApproval: FeishuApprovalApi | null;
  signedDecision: SignedDecisionApi | null;
  report: ReportRecordApi | null;
  /** Independent remediation plan summary, when one has been established. */
  remediationPlan?: RemediationPlanApi | null;
}

/** A lightweight summary used to render the sidebar history list. */
export interface CaseSummary {
  id: string;
  savedAt: string;
  question: string;
  riskLevel: string | null;
  status: CaseStatus;
  hasFeedback: boolean;
  conclusionUseful: boolean | null;
}

/** Default empty feedback for a freshly saved case. */
export function emptyFeedback(): CaseFeedback {
  return {
    conclusionUseful: null,
    citationVerdicts: {},
    missingSources: '',
    notes: '',
    updatedAt: '',
  };
}
