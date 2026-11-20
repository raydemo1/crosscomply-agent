import type { ImpactStatus } from './operations';

export type EvalDraftStatus = 'draft' | 'ready' | 'exported' | 'discarded';
export type EvalSignal = {
  kind: 'conclusion_not_useful' | 'citation_wrong' | 'missing_sources' | 'approval_rejected' | 'rereview_changed';
  citation_refs?: string[];
};

export interface EvalDraftSummary {
  id: string;
  case_id: string;
  task_id: string;
  status: EvalDraftStatus;
  updated_at: string;
  exported_case_id: string | null;
}

export interface EvalRubric {
  allowed_judgments: string[];
  forbidden_judgments: string[];
  must_cover_sources: string[];
  forbidden_clause_sources: string[];
  required_exceptions: string[];
  must_not_assume: string[];
  clarification_expectations: string[];
  should_abstain: boolean | null;
}

export interface EvalDraftInput {
  question: string;
  material_text: string;
  intake: Record<string, unknown>;
  rubric: Partial<EvalRubric>;
  reference_basis: string[];
  selection_reason: string;
  reviewer_correction: string;
  deidentified: boolean;
}

export interface EvalDraft extends EvalDraftSummary, EvalDraftInput {
  signals: EvalSignal[];
  agent_output: {
    risk_level: string | null;
    decision_summary: string | null;
    legal_path: string | null;
    conclusion: string | null;
    citations: { source_id: string; chunk_id: string; article_no: string | null }[];
  };
}

export interface DraftError {
  field: string;
  message: string;
  offset?: number;
  masked?: string;
}

export interface QualityMetrics {
  reviewed_cases: number;
  conclusion_useful_rate: number | null;
  citation_checks: { correct: number; wrong: number; accuracy: number | null };
  approval_outcomes: Record<'approved' | 'conditionally_approved' | 'rejected', number>;
  eval_drafts: Record<EvalDraftStatus, number>;
  regulation_impact: Record<ImpactStatus, number>;
}
