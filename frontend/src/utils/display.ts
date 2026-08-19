/**
 * Shared display helpers for the review workbench.
 *
 * Centralises the enum → Chinese label maps and formatting helpers so the
 * workbench, case detail page, evidence dossier, and report exporter all
 * render risk levels, citation roles, and evidence status consistently.
 */

import type {
  CitationUsage,
  ClauseCitationRole,
  EvidenceStatus,
  EvidenceIssueType,
  RetrievalQueryType,
  RiskLevel,
} from '../types/api';

// ---------------------------------------------------------------------------
// Label maps
// ---------------------------------------------------------------------------

export const RISK_LABELS: Record<RiskLevel, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
  insufficient_evidence: '证据不足',
};

export const RISK_BADGE_CLASS: Record<RiskLevel, string> = {
  high: 'badge badge-high',
  medium: 'badge badge-medium',
  low: 'badge badge-low',
  insufficient_evidence: 'badge badge-insufficient',
};

export const USAGE_LABELS: Record<CitationUsage, string> = {
  legal_basis: '直接法律依据',
  conditional_basis: '条件性法律依据',
  implementation_reference: '实施标准与指南',
  policy_explanation: '政策释义与背景材料',
};

export const AUTHORITY_LABELS: Record<string, string> = {
  national_law: '国家法律',
  administrative_regulation: '行政法规',
  ministry_policy: '部门规章/规范性文件',
  local_regulation: '地方性法规',
  judicial_interpretation: '司法解释',
  public_interpretation: '公开政策释义',
  privacy_policy: '隐私政策',
  simulated_internal_policy: '内部制度（模拟）',
  unknown: '权威等级未提供',
};

export const DOC_TYPE_LABELS: Record<string, string> = {
  unknown: '法源类型未提供',
  law: '法律',
  regulation: '法规/规章',
  policy: '政策文件',
  guideline: '标准与指南',
  faq: '问答/释义',
  privacy_policy: '隐私政策',
  internal_policy: '内部制度',
  case: '案例',
  contract: '合同',
};

export const LAW_STATUS_LABELS: Record<string, string> = {
  effective: '现行有效',
  not_yet_effective: '尚未生效',
  amended: '已修订',
  repealed: '已废止',
  unknown: '状态未知',
};

/** Stable display order for citation groups. */
export const USAGE_ORDER: CitationUsage[] = [
  'legal_basis',
  'conditional_basis',
  'implementation_reference',
  'policy_explanation',
];

export const CITATION_ROLE_LABELS: Record<ClauseCitationRole, string> = {
  primary_legal_basis: '主要法律依据',
  conditional_local_basis: '条件 · 地域依据',
  conditional_industry_basis: '条件 · 行业依据',
  implementation_reference: '实施参考',
  interpretation_auxiliary: '解释辅助',
};

export const EVIDENCE_STATUS_LABELS: Record<EvidenceStatus, string> = {
  not_checked: '未检查',
  sufficient: '证据充分',
  needs_second_retrieval: '需二次检索',
  insufficient: '证据不足',
};

export const EVIDENCE_STATUS_BADGE_CLASS: Record<EvidenceStatus, string> = {
  not_checked: 'badge badge-evidence-notchecked',
  sufficient: 'badge badge-evidence-sufficient',
  needs_second_retrieval: 'badge badge-evidence-second',
  insufficient: 'badge badge-evidence-insufficient',
};

export const EVIDENCE_ISSUE_LABELS: Record<EvidenceIssueType, string> = {
  no_primary_legal_basis: '缺少主要法律依据',
  region_mismatch: '地域不匹配',
  industry_mismatch: '行业不匹配',
  only_auxiliary_evidence: '仅有辅助证据',
  cross_border_mismatch: '跨境场景不匹配',
  critical_facts_missing: '关键事实缺失',
};

export const QUERY_TYPE_LABELS: Record<RetrievalQueryType, string> = {
  legal_issue: '法律议题',
  material_fact: '材料事实',
  region_condition: '地域条件',
  industry_condition: '行业条件',
  missing_information: '缺失信息',
};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/** Render a nullable boolean as 是 / 否 / —. */
export function renderBool(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return value ? '是' : '否';
}

/** Render a nullable string, falling back to —. */
export function renderText(value: string | null | undefined): string {
  return value && value.trim() ? value : '—';
}

/** Render a string array as a 、-joined list, falling back to —. */
export function renderList(values: string[] | null | undefined): string {
  if (!values || values.length === 0) return '—';
  return values.join('、');
}

/** Truncate text to a max length, appending an ellipsis. */
export function truncate(text: string, limit: number): string {
  if (text.length <= limit) return text;
  return text.slice(0, limit) + '…';
}

/** Format an ISO timestamp as a readable local string. */
export function formatTime(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Relative time like "3 分钟前" / "刚刚". */
export function relativeTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return '刚刚';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return formatTime(iso);
}

/** A short id suffix for display (e.g. `…a3f9`). */
export function shortId(id: string): string {
  if (!id) return '—';
  return id.length <= 8 ? id : `…${id.slice(-8)}`;
}

/** Remove the case-local citation number from user-facing legal references. */
function stripCitationRef(value: string): string {
  return value.replace(/^\s*法源[-－]\d+\s*/, '').trim();
}

/** Render a legal basis as the law name and, when available, its article. */
export function legalBasisLabel(title: string | null | undefined, article?: string | null): string {
  const cleanTitle = stripCitationRef(title || '').replace(/^《|》$/g, '').trim();
  if (!cleanTitle) return article?.trim() || '未提供法律依据';
  return `《${cleanTitle}》${article?.trim() || ''}`;
}

/** Prefer the backend's complete citation label while hiding internal refs. */
export function citationDisplayLabel(citation: {
  citation_label?: string | null;
  title: string;
  article_no?: string | null;
}): string {
  const label = citation.citation_label?.trim();
  if (label) return stripCitationRef(label);
  return legalBasisLabel(citation.title, citation.article_no);
}
