/** Client for the CrossComply persisted case workbench API. */

import type {
  CaseAction,
  CaseDetailApi,
  CaseFeedbackApi,
  CaseIntake,
  CaseListApi,
  CaseStatus,
  DashboardSummaryApi,
  EvalJobResponse,
  EvalRerankMode,
  EvalRunOptions,
  EvalSummary,
  HealthResponse,
  ReviewApiResponse,
  WorkbenchUser,
} from '../types/api';

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL?.replace(/\/+$/, '') ?? '';
const DEFAULT_TIMEOUT_MS = 30_000;
const REVIEW_TIMEOUT_MS = 120_000;

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
export const ALLOWED_UPLOAD_SUFFIXES = [
  '.txt', '.md', '.markdown', '.pdf', '.docx', '.html', '.htm', '.json',
] as const;

export class ApiError extends Error {
  readonly status: number;
  readonly endpoint: string;
  readonly detail: unknown;

  constructor(status: number, message: string, endpoint: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.endpoint = endpoint;
    this.detail = detail;
  }
}

export function validateUploadFile(file: File): void {
  const name = file.name || 'unknown';
  const lowerName = name.toLowerCase();
  const dotIndex = lowerName.lastIndexOf('.');
  const dotSuffix = dotIndex >= 0 ? lowerName.slice(dotIndex) : '';
  if (!ALLOWED_UPLOAD_SUFFIXES.some((suffix) => lowerName.endsWith(suffix))) {
    throw new ApiError(422, `不支持的文件类型 ${dotSuffix || '（无后缀）'}`, '/api/cases');
  }
  if (file.size === 0) {
    throw new ApiError(422, `文件 ${name} 为空，无法解析。`, '/api/cases');
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new ApiError(422, `文件 ${name} 超过 20 MB 上限。`, '/api/cases');
  }
}

function buildUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

function extractDetail(body: unknown): unknown {
  if (body && typeof body === 'object' && 'detail' in body) {
    return (body as { detail: unknown }).detail;
  }
  return body;
}

function detailToString(detail: unknown): string {
  if (detail == null) return '';
  if (typeof detail === 'string') return detail;
  if (typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') {
    return detail.message;
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
}

async function request<T>(
  path: string,
  options: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeoutId = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : undefined;
  const isFormData = typeof FormData !== 'undefined' && fetchOptions.body instanceof FormData;
  let response: Response;
  try {
    response = await fetch(buildUrl(path), {
      ...fetchOptions,
      credentials: 'include',
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(fetchOptions.body !== undefined && !isFormData ? { 'Content-Type': 'application/json' } : {}),
        ...fetchOptions.headers,
      },
    });
  } catch (error) {
    throw new ApiError(
      0,
      controller.signal.aborted
        ? `请求超时：${path}`
        : `无法连接到 CrossComply 服务：${error instanceof Error ? error.message : String(error)}`,
      path,
    );
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }

  const rawText = await response.text();
  let parsed: unknown = undefined;
  if (rawText) {
    try {
      parsed = JSON.parse(rawText);
    } catch {
      parsed = rawText;
    }
  }
  if (!response.ok) {
    const detail = extractDetail(parsed);
    throw new ApiError(
      response.status,
      detailToString(detail) || `API 请求失败（${response.status}）`,
      path,
      detail,
    );
  }
  return parsed as T;
}

export async function login(username: string, password: string): Promise<WorkbenchUser> {
  const response = await request<{ user: WorkbenchUser }>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
  return response.user;
}

export async function logout(): Promise<void> {
  await request('/api/auth/logout', { method: 'POST' });
}

export async function getCurrentUser(): Promise<WorkbenchUser | null> {
  try {
    const response = await request<{ user: WorkbenchUser }>('/api/auth/me');
    return response.user;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export interface CreateCaseInput {
  title?: string;
  question: string;
  materialText: string;
  materialSource?: string | null;
  intake: CaseIntake;
  reviewMode: 'llm' | 'multi_agent';
  rerankMode: 'off' | 'embedding';
  file?: File | null;
}

export async function createCase(input: CreateCaseInput): Promise<CaseDetailApi> {
  if (!input.question.trim()) throw new ApiError(0, '请输入审查问题。', '/api/cases');
  if (!input.file && !input.materialText.trim()) throw new ApiError(0, '请输入待审查材料。', '/api/cases');
  if (input.file) validateUploadFile(input.file);

  if (input.file) {
    const formData = new FormData();
    formData.append('question', input.question);
    formData.append('material_text', input.materialText);
    formData.append('material_source', input.file.name);
    formData.append('intake_json', JSON.stringify(input.intake));
    formData.append('review_mode', input.reviewMode);
    formData.append('rerank_mode', input.rerankMode);
    formData.append('file', input.file);
    return request<CaseDetailApi>('/api/cases', {
      method: 'POST',
      body: formData,
      timeoutMs: REVIEW_TIMEOUT_MS,
    });
  }
  return request<CaseDetailApi>('/api/cases', {
    method: 'POST',
    body: JSON.stringify({
      title: input.title,
      question: input.question,
      material_text: input.materialText,
      material_source: input.materialSource ?? null,
      intake: input.intake,
      review_mode: input.reviewMode,
      rerank_mode: input.rerankMode,
    }),
  });
}

export async function listCases(query = ''): Promise<CaseListApi> {
  const suffix = query ? `?query=${encodeURIComponent(query)}` : '';
  return request<CaseListApi>(`/api/cases${suffix}`);
}

export async function getCaseDetail(caseId: string): Promise<CaseDetailApi> {
  return request<CaseDetailApi>(`/api/cases/${encodeURIComponent(caseId)}`);
}

export async function updateCase(
  caseId: string,
  payload: Partial<Pick<CaseDetailApi['case'], 'title' | 'question' | 'material_text' | 'facts_confirmed' | 'owner_id'>> & { intake?: CaseIntake },
): Promise<CaseDetailApi> {
  return request<CaseDetailApi>(`/api/cases/${encodeURIComponent(caseId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function updateCaseStatus(caseId: string, status: CaseStatus, note = ''): Promise<CaseDetailApi> {
  return request<CaseDetailApi>(`/api/cases/${encodeURIComponent(caseId)}/status`, {
    method: 'POST',
    body: JSON.stringify({ status, note }),
  });
}

export async function runCase(caseId: string): Promise<CaseDetailApi & { run_status: CaseStatus | 'review_failed' }> {
  return request<CaseDetailApi & { run_status: CaseStatus | 'review_failed' }>(
    `/api/cases/${encodeURIComponent(caseId)}/run`,
    { method: 'POST', timeoutMs: REVIEW_TIMEOUT_MS },
  );
}

export async function createAction(caseId: string, payload: Omit<CaseAction, 'id' | 'case_id' | 'created_at' | 'updated_at'>): Promise<CaseAction> {
  return request<CaseAction>(`/api/cases/${encodeURIComponent(caseId)}/actions`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function updateAction(actionId: string, payload: Partial<Pick<CaseAction, 'title' | 'description' | 'owner_role' | 'priority' | 'status' | 'due_date'>>): Promise<CaseAction> {
  return request<CaseAction>(`/api/actions/${encodeURIComponent(actionId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function saveFeedback(caseId: string, payload: {
  conclusion_useful?: boolean | null;
  missing_sources?: string;
  notes?: string;
  citation_verdicts?: Record<string, 'correct' | 'wrong'>;
}): Promise<CaseFeedbackApi> {
  return request<CaseFeedbackApi>(`/api/cases/${encodeURIComponent(caseId)}/feedback`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getDashboardSummary(): Promise<DashboardSummaryApi> {
  return request<DashboardSummaryApi>('/api/dashboard/summary');
}

export async function runEvaluation(options: EvalRunOptions = {
  retrieval_mode: 'service', review_mode: 'llm', top_k: 10, max_workers: 4, rerank_mode: 'off', suite: 'full',
}): Promise<EvalJobResponse> {
  return request<EvalJobResponse>('/api/eval/run', {
    method: 'POST',
    body: JSON.stringify(options),
  });
}

export async function getEvalStatus(rerankMode: EvalRerankMode = 'off'): Promise<EvalJobResponse> {
  return request<EvalJobResponse>(`/api/eval/status?rerank_mode=${encodeURIComponent(rerankMode)}`);
}

export async function getLatestEval(rerankMode: EvalRerankMode = 'off'): Promise<EvalSummary | null> {
  try {
    return await request<EvalSummary>(`/api/eval/latest?rerank_mode=${encodeURIComponent(rerankMode)}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export async function checkHealth(): Promise<boolean> {
  try {
    const result = await request<HealthResponse>('/api/health', { timeoutMs: 5_000 });
    return result?.status === 'ok';
  } catch {
    return false;
  }
}
