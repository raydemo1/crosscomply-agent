/** Client for the CrossComply persisted case workbench API. */

import type {
  CaseDetailApi,
  CaseFeedbackApi,
  CaseIntake,
  CaseListApi,
  CaseSummaryApi,
  CaseTemplateApi,
  CaseTemplatePayload,
  CaseStatus,
  ComplianceFactsApi,
  DashboardSummaryApi,
  EvalJobResponse,
  EvalRerankMode,
  EvalRunOptions,
  EvalSummary,
  HealthResponse,
  KnowledgeDeletePreviewApi,
  KnowledgeImportPreviewApi,
  KnowledgeJobApi,
  KnowledgeLibraryKind,
  KnowledgeSourceApi,
  KnowledgeSourceDetailApi,
  KnowledgeTrashRecordApi,
  FreezeMaterialSnapshotResponse,
  FeishuApprovalApi,
  MaterialVersionApi,
  ManagedUserApi,
  ReviewTaskApi,
  RemediationAssigneeApi,
  RemediationEvidenceUploadApi,
  RemediationInboxItemApi,
  RemediationPlanApi,
  RemediationPlanCreatePayload,
  RemediationReviewPayload,
  RemediationSubmissionApi,
  RemediationSubmissionPayload,
  RemediationTaskApi,
  RemediationTaskUpdatePayload,
  WorkbenchUser,
} from '../types/api';

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL?.replace(/\/+$/, '') ?? '';
const DEFAULT_TIMEOUT_MS = 30_000;
const REVIEW_TIMEOUT_MS = 120_000;
const TASK_POLL_INTERVAL_MS = 1_500;

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

export async function listManagedUsers(): Promise<{ items: ManagedUserApi[]; total: number }> {
  return request<{ items: ManagedUserApi[]; total: number }>('/api/admin/users');
}

export async function createManagedUser(payload: {
  username: string;
  display_name: string;
  password: string;
  role: WorkbenchUser['role'];
}): Promise<ManagedUserApi> {
  return request<ManagedUserApi>('/api/admin/users', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function setManagedUserState(userId: string, active: boolean): Promise<ManagedUserApi> {
  return request<ManagedUserApi>(`/api/admin/users/${encodeURIComponent(userId)}/state`, {
    method: 'PATCH',
    body: JSON.stringify({ active }),
  });
}

export async function assignManagedUserRole(
  userId: string,
  role: WorkbenchUser['role'],
): Promise<ManagedUserApi> {
  return request<ManagedUserApi>(`/api/admin/users/${encodeURIComponent(userId)}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });
}

export async function resetManagedUserPassword(userId: string, password: string): Promise<ManagedUserApi> {
  return request<ManagedUserApi>(`/api/admin/users/${encodeURIComponent(userId)}/reset-password`, {
    method: 'POST',
    body: JSON.stringify({ password }),
  });
}

// ---------------------------------------------------------------------------
// Knowledge-base administration
// ---------------------------------------------------------------------------

export async function listKnowledgeSources(
  libraryKind: KnowledgeLibraryKind,
  params: { query?: string; status?: string } = {},
): Promise<{ items: KnowledgeSourceApi[]; total: number }> {
  const query = new URLSearchParams({ library_kind: libraryKind });
  if (params.query?.trim()) query.set('query', params.query.trim());
  if (params.status) query.set('status', params.status);
  return request<{ items: KnowledgeSourceApi[]; total: number }>(`/api/admin/knowledge-sources?${query.toString()}`);
}

export async function getKnowledgeSource(sourceId: string): Promise<KnowledgeSourceDetailApi> {
  return request<KnowledgeSourceDetailApi>(`/api/admin/knowledge-sources/${encodeURIComponent(sourceId)}`);
}

export function knowledgeSourceDownloadUrl(sourceId: string): string {
  return buildUrl(`/api/admin/knowledge-sources/${encodeURIComponent(sourceId)}/raw`);
}

export async function previewKnowledgeImport(
  libraryKind: KnowledgeLibraryKind,
  files: File[],
  metadata: Array<Record<string, unknown>> = [],
): Promise<KnowledgeImportPreviewApi> {
  if (files.length === 0) throw new ApiError(422, '请选择至少一个来源文件。', '/api/admin/knowledge-import-previews');
  if (files.length > 50) throw new ApiError(422, '一次最多导入 50 个来源文件。', '/api/admin/knowledge-import-previews');
  files.forEach(validateUploadFile);
  const formData = new FormData();
  formData.append('library_kind', libraryKind);
  formData.append('metadata_json', JSON.stringify(metadata));
  files.forEach((file) => formData.append('files', file));
  return request<KnowledgeImportPreviewApi>('/api/admin/knowledge-import-previews', {
    method: 'POST',
    body: formData,
    timeoutMs: REVIEW_TIMEOUT_MS,
  });
}

export async function commitKnowledgeImport(previewId: string): Promise<{ job: KnowledgeJobApi }> {
  return request<{ job: KnowledgeJobApi }>('/api/admin/knowledge-import-jobs', {
    method: 'POST',
    body: JSON.stringify({ preview_id: previewId }),
  });
}

export async function updateKnowledgeMetadata(
  sourceId: string,
  payload: Record<string, unknown>,
): Promise<{ job: KnowledgeJobApi }> {
  return request<{ job: KnowledgeJobApi }>(`/api/admin/knowledge-sources/${encodeURIComponent(sourceId)}/metadata`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function previewKnowledgeDelete(
  libraryKind: KnowledgeLibraryKind,
  sourceIds: string[],
): Promise<KnowledgeDeletePreviewApi> {
  return request<KnowledgeDeletePreviewApi>('/api/admin/knowledge-delete-previews', {
    method: 'POST',
    body: JSON.stringify({ library_kind: libraryKind, source_ids: sourceIds }),
  });
}

export async function commitKnowledgeDelete(token: string, confirmation: string): Promise<{ job: KnowledgeJobApi }> {
  return request<{ job: KnowledgeJobApi }>('/api/admin/knowledge-delete-jobs', {
    method: 'POST',
    body: JSON.stringify({ token, confirmation }),
  });
}

export async function listKnowledgeTrash(libraryKind: KnowledgeLibraryKind): Promise<{ items: KnowledgeTrashRecordApi[]; total: number }> {
  const query = new URLSearchParams({ library_kind: libraryKind });
  return request<{ items: KnowledgeTrashRecordApi[]; total: number }>(`/api/admin/knowledge-trash?${query.toString()}`);
}

export async function restoreKnowledgeSource(sourceId: string, libraryKind: KnowledgeLibraryKind): Promise<{ job: KnowledgeJobApi }> {
  return request<{ job: KnowledgeJobApi }>(`/api/admin/knowledge-trash/${encodeURIComponent(sourceId)}/restore`, {
    method: 'POST',
    body: JSON.stringify({ library_kind: libraryKind }),
  });
}

export async function getKnowledgeJob(jobId: string): Promise<{ job: KnowledgeJobApi }> {
  return request<{ job: KnowledgeJobApi }>(`/api/admin/knowledge-jobs/${encodeURIComponent(jobId)}`);
}

export async function waitForKnowledgeJob(
  jobId: string,
  onUpdate?: (job: KnowledgeJobApi) => void,
): Promise<KnowledgeJobApi> {
  while (true) {
    const { job } = await getKnowledgeJob(jobId);
    onUpdate?.(job);
    if (job.status === 'succeeded' || job.status === 'partially_succeeded' || job.status === 'failed') return job;
    await new Promise<void>((resolve) => window.setTimeout(resolve, TASK_POLL_INTERVAL_MS));
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

export async function listCaseTemplates(query = ''): Promise<{ items: CaseTemplateApi[]; total: number }> {
  const suffix = query.trim() ? `?query=${encodeURIComponent(query.trim())}` : '';
  return request<{ items: CaseTemplateApi[]; total: number }>(`/api/case-templates${suffix}`);
}

export async function createCaseTemplate(payload: CaseTemplatePayload): Promise<CaseTemplateApi> {
  return request<CaseTemplateApi>('/api/case-templates', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function updateCaseTemplate(
  templateId: string,
  payload: Partial<CaseTemplatePayload>,
): Promise<CaseTemplateApi> {
  return request<CaseTemplateApi>(`/api/case-templates/${encodeURIComponent(templateId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function archiveCaseTemplate(templateId: string): Promise<CaseTemplateApi> {
  return request<CaseTemplateApi>(`/api/case-templates/${encodeURIComponent(templateId)}/archive`, {
    method: 'POST',
  });
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

export async function uploadMaterial(
  caseId: string,
  logicalName: string,
  file: File,
): Promise<MaterialVersionApi> {
  validateUploadFile(file);
  const formData = new FormData();
  formData.append('logical_name', logicalName);
  formData.append('file', file);
  return request<MaterialVersionApi>(`/api/cases/${encodeURIComponent(caseId)}/materials`, {
    method: 'POST',
    body: formData,
    timeoutMs: REVIEW_TIMEOUT_MS,
  });
}

export async function freezeMaterialSnapshot(
  caseId: string,
  versionIds: string[],
  facts: ComplianceFactsApi,
): Promise<FreezeMaterialSnapshotResponse> {
  return request<FreezeMaterialSnapshotResponse>(
    `/api/cases/${encodeURIComponent(caseId)}/material-snapshots`,
    {
      method: 'POST',
      body: JSON.stringify({ version_ids: versionIds, facts }),
    },
  );
}

export interface EnqueueReviewResponse {
  task_id: string;
  status: ReviewTaskApi['status'];
}

export async function runCase(caseId: string): Promise<EnqueueReviewResponse> {
  return request<EnqueueReviewResponse>(
    `/api/cases/${encodeURIComponent(caseId)}/run`,
    { method: 'POST' },
  );
}

export async function getReviewTask(taskId: string): Promise<ReviewTaskApi> {
  return request<ReviewTaskApi>(`/api/tasks/${encodeURIComponent(taskId)}`);
}

export async function retryReviewTask(taskId: string): Promise<ReviewTaskApi> {
  return request<ReviewTaskApi>(`/api/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: 'POST',
  });
}

export async function createFeishuApproval(caseId: string): Promise<FeishuApprovalApi> {
  return request<FeishuApprovalApi>(`/api/cases/${encodeURIComponent(caseId)}/feishu-approval`, {
    method: 'POST',
  });
}

export function caseReportDownloadUrl(caseId: string): string {
  return buildUrl(`/api/cases/${encodeURIComponent(caseId)}/reports/download`);
}

export async function waitForReviewTask(
  taskId: string,
  onUpdate?: (task: ReviewTaskApi) => void | Promise<void>,
): Promise<ReviewTaskApi> {
  while (true) {
    const task = await getReviewTask(taskId);
    await onUpdate?.(task);
    if (task.status === 'succeeded' || task.status === 'failed') return task;
    await new Promise<void>((resolve) => window.setTimeout(resolve, TASK_POLL_INTERVAL_MS));
  }
}

// ---------------------------------------------------------------------------
// Independent remediation plan
// ---------------------------------------------------------------------------

export async function getRemediationPlan(caseId: string): Promise<RemediationPlanApi> {
  return request<RemediationPlanApi>(`/api/cases/${encodeURIComponent(caseId)}/remediation-plan`);
}

export async function createRemediationPlan(caseId: string, payload: RemediationPlanCreatePayload): Promise<RemediationPlanApi> {
  return request<RemediationPlanApi>(`/api/cases/${encodeURIComponent(caseId)}/remediation-plan`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function activateRemediationPlan(planId: string): Promise<RemediationPlanApi> {
  return request<RemediationPlanApi>(`/api/remediation-plans/${encodeURIComponent(planId)}/activate`, { method: 'POST' });
}

export async function cancelRemediationPlan(planId: string): Promise<RemediationPlanApi> {
  return request<RemediationPlanApi>(`/api/remediation-plans/${encodeURIComponent(planId)}/cancel`, { method: 'POST' });
}

export async function listMyRemediations(params: { scope?: 'mine' | 'review'; status?: string; overdue?: boolean } = {}): Promise<{ items: RemediationInboxItemApi[]; total: number }> {
  const query = new URLSearchParams();
  query.set('scope', params.scope ?? 'mine');
  if (params.status) query.set('status', params.status);
  if (params.overdue) query.set('overdue', 'true');
  return request<{ items: RemediationInboxItemApi[]; total: number }>(`/api/remediations?${query.toString()}`);
}

export async function getRemediationTask(taskId: string): Promise<{ task: RemediationTaskApi; case: CaseSummaryApi | null }> {
  return request<{ task: RemediationTaskApi; case: CaseSummaryApi | null }>(`/api/remediation-tasks/${encodeURIComponent(taskId)}`);
}

export async function updateRemediationTask(taskId: string, payload: RemediationTaskUpdatePayload): Promise<RemediationTaskApi> {
  return request<RemediationTaskApi>(`/api/remediation-tasks/${encodeURIComponent(taskId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function startRemediationTask(taskId: string): Promise<RemediationTaskApi> {
  return request<RemediationTaskApi>(`/api/remediation-tasks/${encodeURIComponent(taskId)}/start`, {
    method: 'POST',
  });
}

export async function uploadRemediationEvidence(taskId: string, file: File): Promise<RemediationEvidenceUploadApi> {
  validateUploadFile(file);
  const formData = new FormData();
  formData.append('file', file);
  return request<RemediationEvidenceUploadApi>(`/api/remediation-tasks/${encodeURIComponent(taskId)}/evidence`, {
    method: 'POST',
    body: formData,
  });
}

export async function submitRemediationTask(taskId: string, payload: RemediationSubmissionPayload): Promise<RemediationSubmissionApi> {
  return request<RemediationSubmissionApi>(`/api/remediation-tasks/${encodeURIComponent(taskId)}/submissions`, {
    method: 'POST',
    body: JSON.stringify({ note: payload.note, evidence: payload.evidence ?? [] }),
  });
}

export async function reviewRemediationSubmission(submissionId: string, payload: RemediationReviewPayload): Promise<RemediationSubmissionApi> {
  return request<RemediationSubmissionApi>(`/api/remediation-submissions/${encodeURIComponent(submissionId)}/review`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function listAssignableUsers(): Promise<{ items: RemediationAssigneeApi[]; total: number }> {
  return request<{ items: RemediationAssigneeApi[]; total: number }>('/api/users/assignable');
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
