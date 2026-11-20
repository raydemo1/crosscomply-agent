import type { CaseStatus } from './api';

export type WorkKind = 'supplement' | 'review' | 'source_review' | 'remediation' | 'remediation_review'
  | 'approval' | 'transfer_draft' | 'transfer_due' | 'impact_review' | 'portfolio_threshold' | 'regulation_impact';

export type ImpactStatus = 'open' | 'dismissed' | 'rereview_started' | 'resolved';
export type ImpactSeverity = 'blocking' | 'upcoming' | 'informational';
export type ImpactReason = 'cited_text_changed' | 'source_removed' | 'source_repealed' | 'superseded' | 'superseded_upcoming';

export interface RegulationFlag {
  id: string;
  case_id: string;
  case_title: string;
  task_id: string;
  activity_id: string | null;
  activity_title: string | null;
  title: string;
  article_nos: string[];
  chunk_ids: string[];
  reason: ImpactReason;
  severity: ImpactSeverity;
  status: ImpactStatus;
  effective_on: string | null;
  created_at: string;
  resolution_note: string | null;
  rereview_case_id: string | null;
}

export interface KnowledgeChange {
  id: string;
  change_type: 'added' | 'body_changed' | 'metadata_changed' | 'removed' | 'restored';
  detail: { title: string; changed_articles: Array<{ article_no: string | null }> };
  impact_status: 'pending' | 'computed' | 'failed';
  flagged_cases: number;
  created_at: string;
}

export interface KnowledgeImpactResult {
  status: 'failed' | 'computed';
  flagged_cases?: number;
  change_event_id?: string;
}

export type PortfolioBand = 'exempt' | 'contract_or_cert' | 'assessment';
export interface PortfolioContribution {
  activity_id: string;
  activity_title: string;
  baseline_case_id: string;
  intake_snapshot_id: string;
  snapshot_year: number;
  non_sensitive: number | null;
  sensitive: number | null;
  count_period: string;
  counts_toward_total: boolean;
  status: 'counted' | 'stale_year' | 'unparsed' | 'excluded';
  exclusion_note: string;
}

export interface PortfolioSummary {
  entity_key: string;
  entity_name: string;
  year: number;
  ciio_any: boolean;
  contributions: PortfolioContribution[];
  non_sensitive_sum: number;
  non_sensitive_max: number;
  sensitive_sum: number;
  sensitive_max: number;
  band_upper: PortfolioBand;
  band_lower: PortfolioBand;
  other_activity_count: number;
  counted_other_count: number;
  incomplete_other_count: number;
  scope_limited: boolean;
  fingerprint: string;
  threshold_alert?: boolean;
}

export interface PortfolioSnapshot {
  id: string;
  case_id: string;
  payload: PortfolioSummary;
  created_at: string;
}

export interface WorkItem {
  id: string;
  kind: WorkKind;
  title: string;
  context: string;
  reason: string;
  action: string;
  href: string;
  owner_name: string;
  due_on: string | null;
  urgency: 'overdue' | 'soon' | null;
}

export interface TransferActivity {
  regulation_impact_open?: boolean;
  id: string;
  title: string;
  business_entity: string;
  owner_name: string;
  recipient: string;
  destination: string;
  data_types: string[];
  next_review_on: string;
  approval: {
    case_id: string;
    title: string;
    status: 'approved' | 'conditionally_approved';
    decided_at: string | null;
  };
  active_status: CaseStatus | null;
}

export type ImpactOutcome = 'scope_changed' | 'scope_unchanged';
export type ScopeValue = string | string[] | boolean | null;

export interface TransferReview {
  case_id: string;
  baseline_case_id: string;
  title: string;
  change_note: string;
  created_at: string;
  status: CaseStatus;
  task_id: string | null;
  changes: Array<{ field: string; before: ScopeValue; after: ScopeValue }>;
  impact: {
    outcome: ImpactOutcome;
    note: string;
    reviewer_name: string | null;
    confirmed_at: string;
  } | null;
  can_confirm: boolean;
  can_edit_draft: boolean;
}

export interface TransferDetail extends TransferActivity {
  origin_case_id: string;
  reviews: TransferReview[];
}

export interface EligibleTransferCase {
  id: string;
  title: string;
  decided_at: string | null;
}
