export interface QAMaterial {
  id: string;
  filename: string;
  byte_size: number;
  created_at: string;
}

export interface QACitation {
  ref: string;
  title: string;
  url: string | null;
  excerpt: string;
  kind: 'legal' | 'context' | 'web';
  source_id?: string;
  chunk_id?: string;
  article_no?: string | null;
  heading_path?: string[];
  publish_date?: string | null;
  effective_date?: string | null;
  law_status?: string | null;
  issuing_body?: string | null;
  available_at_answer_date?: boolean;
  answer_as_of_date?: string;
  can_cite_clause?: boolean;
}

export interface QAQuestion {
  question: string;
  selection: 'single' | 'multiple';
  options: string[];
}

export interface QAResult {
  budget_exhausted?: boolean;
  evidence_gaps?: string[];
  generation_status?: 'streaming' | 'completed' | 'failed';
  questions?: QAQuestion[];
  answer?: string;
  citations: QACitation[];
  material_citations: Array<{ material_version_id: string; filename: string; quote: string }>;
  conditions: string[];
}

export interface QARun {
  id: string;
  question: string;
  material_ids: string[];
  status: 'queued' | 'running' | 'completed' | 'failed';
  answer: string;
  result: QAResult | null;
  progress: string;
  error: string | null;
  created_at: string;
}

export interface QASummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface QAConversation extends QASummary {
  materials: QAMaterial[];
  runs: QARun[];
}

export interface QAHandoff {
  conversationId: string;
  runId: string;
  question: string;
  files: File[];
  materialIds: string[];
  leads: string;
  requestId: string;
}
