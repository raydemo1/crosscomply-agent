import type { CaseStatus } from './api';

export type WorkKind = 'supplement' | 'review' | 'source_review' | 'remediation' | 'remediation_review'
  | 'approval' | 'transfer_draft' | 'transfer_due' | 'impact_review';

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
