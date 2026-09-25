import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Menu } from 'lucide-react';
import { isReviewFailedResponse } from './types/api';
import type { CaseIntake, CaseTemplateApi, DashboardSummaryApi, WorkbenchUser } from './types/api';
import type { Page } from './components/Sidebar';
import Sidebar from './components/Sidebar';
import WorkbenchPage from './components/WorkbenchPage';
import LoginPage from './components/LoginPage';
import { ApiError, createCase, extractIntake, freezeMaterialSnapshot, getCurrentUser, getDashboardSummary, listMaterialVersions, login, logout, updateCase, updateCaseStatus, uploadMaterial } from './api/client';
import { EMPTY_INTAKE, openCase, refreshCases, useCaseStore } from './store/caseStore';
import { allocateUploadNames, PASTED_MATERIAL_LOGICAL_NAME } from './utils/materialNames';

const GovernanceConsolePage = lazy(() => import('./components/GovernanceConsolePage'));
const KnowledgeBasePage = lazy(() => import('./components/KnowledgeBasePage'));
const CaseDetailPage = lazy(() => import('./components/CaseDetailPage'));
const TemplateCenterPage = lazy(() => import('./components/TemplateCenterPage'));
const RemediationPlanPage = lazy(() => import('./components/RemediationPlanPage'));
const MyRemediationsPage = lazy(async () => {
  const module = await import('./components/RemediationPlanPage');
  return { default: module.MyRemediationsPage };
});

function materialOriginal(material: string): File {
  return new File([material], 'case-material.txt', { type: 'text/plain;charset=utf-8' });
}

const MATERIAL_FALLBACK_PREFIX = '材料以附件形式提供：';

/** Editing the prose is an update to the pasted material, not a brand new upload. */
function replacesPastedMaterial(text: string, originalText: string): boolean {
  return Boolean(text.trim()) && text !== originalText;
}

function materialFallback(files: File[]): string {
  return files.length ? `${MATERIAL_FALLBACK_PREFIX}${files.map((file) => file.name).join('、')}` : '';
}

interface MaterialUpload {
  logicalName: string;
  file: File;
}

interface KnownVersion {
  id: string;
  logicalName: string;
  versionNumber: number;
}

/**
 * Only the versions already frozen for this case may carry over. Reading every historical
 * version would silently resurrect superseded drafts and discarded attachments.
 */
async function loadFrozenVersions(
  caseId: string,
  keptVersionIds: string[],
): Promise<KnownVersion[]> {
  if (!keptVersionIds.length) return [];
  const keptIds = new Set(keptVersionIds);
  const kept = (await listMaterialVersions(caseId))
    .filter((version) => keptIds.has(version.id))
    .map((version) => ({
      id: version.id,
      logicalName: version.logical_name,
      versionNumber: version.version_number,
    }));
  if (kept.length !== keptIds.size) {
    throw new ApiError(0, '本案已有材料发生变化，请刷新页面后重新提交。', '/api/cases');
  }
  return kept;
}

/**
 * A snapshot holds at most one version per logical material, so a higher version of a frozen
 * material replaces it instead of freezing both drafts side by side.
 */
function frozenVersionIds(versions: KnownVersion[]): string[] {
  const newest = new Map<string, KnownVersion>();
  for (const version of versions) {
    const current = newest.get(version.logicalName);
    if (!current || version.versionNumber > current.versionNumber) {
      newest.set(version.logicalName, version);
    }
  }
  return [...newest.values()].map((version) => version.id);
}

/**
 * A file-only case stores a filename summary in material_text. Keep it out of the
 * editable box so it can never be re-submitted as if it were the real material.
 */
function editableMaterialText(materialText: string): string {
  return materialText.startsWith(MATERIAL_FALLBACK_PREFIX) ? '' : materialText;
}

function linkedCaseId(): string | null {
  const value = new URLSearchParams(window.location.search).get('case')?.trim();
  return value || null;
}

function linkedRemediationPlan(): boolean {
  return new URLSearchParams(window.location.search).get('remediation') === 'plan';
}

export default function App(): JSX.Element {
  const [user, setUser] = useState<WorkbenchUser | null>(null);
  const [booting, setBooting] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>('workbench');
  const [activeCaseId, setActiveCaseId] = useState<string | null>(null);
  const [remediationCaseId, setRemediationCaseId] = useState<string | null>(null);
  const [editingCaseId, setEditingCaseId] = useState<string | null>(null);
  const [editingVersionIds, setEditingVersionIds] = useState<string[]>([]);
  /** Names already frozen on the edited case; new uploads are numbered to avoid them. */
  const [editingMaterialNames, setEditingMaterialNames] = useState<string[]>([]);
  const [editingMaterialText, setEditingMaterialText] = useState('');
  const [question, setQuestion] = useState('');
  const [material, setMaterial] = useState('');
  const [intake, setIntake] = useState<CaseIntake>({ ...EMPTY_INTAKE });
  const [loading, setLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [missingFactKeys, setMissingFactKeys] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dashboardSummary, setDashboardSummary] = useState<DashboardSummaryApi | null>(null);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const cases = useCaseStore();
  const activeCase = useMemo(() => activeCaseId ? cases.find((item) => item.id === activeCaseId) ?? null : null, [cases, activeCaseId]);

  // Numbering of new uploads depends on the names already frozen on the case, so fetch them up
  // front. Reading them only at submit time would make the preview disagree with what gets frozen.
  useEffect(() => {
    if (!editingCaseId || !editingVersionIds.length) {
      setEditingMaterialNames([]);
      return;
    }
    let cancelled = false;
    const keptIds = new Set(editingVersionIds);
    void listMaterialVersions(editingCaseId)
      .then((versions) => {
        if (cancelled) return;
        setEditingMaterialNames(
          versions.filter((version) => keptIds.has(version.id)).map((version) => version.logical_name),
        );
      })
      .catch(() => {
        if (!cancelled) setEditingMaterialNames([]);
      });
    return () => {
      cancelled = true;
    };
  }, [editingCaseId, editingVersionIds]);

  useEffect(() => {
    let mounted = true;
    void getCurrentUser().then(async (current) => {
      if (!mounted) return;
      setUser(current);
      if (current) {
        const [, summary] = await Promise.all([refreshCases(), getDashboardSummary()]);
        if (mounted) setDashboardSummary(summary);
        const caseId = linkedCaseId();
        if (caseId) {
          try {
            await openCase(caseId);
            if (mounted) {
              setActiveCaseId(caseId);
              if (linkedRemediationPlan()) {
                setRemediationCaseId(caseId);
                setPage('remediation-plan');
              } else {
                setPage('case-detail');
              }
            }
          } catch (reason) {
            if (mounted) setError(reason instanceof Error ? reason.message : '无法打开飞书关联案件');
          }
        }
      }
    }).catch((reason) => {
      if (mounted) setAuthError(reason instanceof Error ? reason.message : '无法连接到 CrossComply 服务');
    }).finally(() => {
      if (mounted) setBooting(false);
    });
    return () => { mounted = false; };
  }, []);

  const handleLogin = useCallback(async (username: string, password: string): Promise<void> => {
    setAuthError(null);
    let current: WorkbenchUser;
    try {
      current = await login(username, password);
    } catch (reason) {
      setAuthError(
        reason instanceof ApiError && reason.status === 401
          ? '用户名或密码错误，请检查后重试。'
          : reason instanceof Error
            ? reason.message
            : '登录失败，请稍后重试。',
      );
      return;
    }
    setUser(current);
    const [, summary] = await Promise.all([refreshCases(), getDashboardSummary()]);
    setDashboardSummary(summary);
    const caseId = linkedCaseId();
    if (caseId) {
      await openCase(caseId);
      setActiveCaseId(caseId);
      if (linkedRemediationPlan()) {
        setRemediationCaseId(caseId);
        setPage('remediation-plan');
      } else {
        setPage('case-detail');
      }
    }
  }, []);

  const handleLogout = useCallback(async (): Promise<void> => {
    await logout();
    setUser(null);
    setDashboardSummary(null);
    setActiveCaseId(null);
    setEditingCaseId(null);
    setPage('workbench');
  }, []);

  const handleOpenGovernance = useCallback((): void => {
    if (user?.role !== 'admin') return;
    setError(null);
    setPage('governance');
  }, [user]);

  const handleAnalyze = useCallback(async (q: string, m: string, files: File[]): Promise<boolean> => {
    setAnalyzing(true);
    setError(null);
    try {
      const result = await extractIntake(q, m, files);
      setIntake((current) => ({ ...current, ...result.intake, data_types: [...result.intake.data_types] }));
      setMissingFactKeys(result.missing.map((item) => item.key));
      return true;
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : 'Agent 无法读取材料，请检查文件后重试');
      return false;
    } finally {
      setAnalyzing(false);
    }
  }, []);

  const handleSubmit = useCallback(async (q: string, m: string, confirmedIntake: CaseIntake, files: File[]): Promise<void> => {
    if (!user) return;
    setLoading(true);
    setError(null);
    const textMaterial = m.trim();
    const keptVersionIds = editingCaseId ? editingVersionIds : [];
    // Only genuinely new material is uploaded. Supplementing facts must never re-upload the
    // frozen originals, and pasted prose replaces its own previous version instead of adding one.
    const updatesPastedMaterial = replacesPastedMaterial(textMaterial, editingMaterialText);
    const clearEditingState = (): void => {
      setEditingCaseId(null);
      setEditingVersionIds([]);
      setEditingMaterialText('');
    };
    try {
      if (!keptVersionIds.length && !files.length && !updatesPastedMaterial) {
        throw new ApiError(0, '请提供至少一份待审查材料。', '/api/cases');
      }
      const saved = editingCaseId
        ? await updateCase(editingCaseId, {
          question: q,
          ...(updatesPastedMaterial ? { material_text: textMaterial } : {}),
          intake: confirmedIntake,
        })
        : await createCase({ question: q, materialText: textMaterial || materialFallback(files), intake: confirmedIntake });
      const frozenVersions = await loadFrozenVersions(saved.case.id, keptVersionIds);
      const fileNames = allocateUploadNames(
        files.map((file) => file.name),
        frozenVersions.map((version) => version.logicalName),
        updatesPastedMaterial,
      );
      const uploads: MaterialUpload[] = files.map((file, index) => ({
        logicalName: fileNames[index],
        file,
      }));
      if (updatesPastedMaterial) {
        uploads.push({ logicalName: PASTED_MATERIAL_LOGICAL_NAME, file: materialOriginal(textMaterial) });
      }
      const uploaded: KnownVersion[] = [];
      for (const upload of uploads) {
        const created = await uploadMaterial(saved.case.id, upload.logicalName, upload.file);
        uploaded.push({
          id: created.id,
          logicalName: created.logical_name,
          versionNumber: created.version_number,
        });
      }
      await freezeMaterialSnapshot(
        saved.case.id,
        frozenVersionIds([...frozenVersions, ...uploaded]),
      );
      const pending = await updateCaseStatus(saved.case.id, 'pending_review');
      clearEditingState();
      await openCase(pending.case.id);
      setDashboardSummary(await getDashboardSummary());
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : '提交案件时发生未知错误');
    } finally {
      setLoading(false);
    }
  }, [editingCaseId, editingVersionIds, editingMaterialText, user, openCase]);

  const handleOpenCase = useCallback(async (caseId: string): Promise<void> => {
    setError(null);
    try {
      await openCase(caseId);
      setActiveCaseId(caseId);
      setPage('case-detail');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法打开案件');
    }
  }, []);

  const handleOpenRemediationPlan = useCallback((caseId: string): void => {
    setRemediationCaseId(caseId);
    setActiveCaseId(caseId);
    setPage('remediation-plan');
  }, []);

  const handleUseTemplate = useCallback((template: CaseTemplateApi): void => {
    setQuestion(template.question);
    setMaterial('');
    setIntake({ ...EMPTY_INTAKE, ...template.intake, data_types: [...(template.intake.data_types ?? [])] });
    setEditingCaseId(null);
    setEditingVersionIds([]);
    setEditingMaterialText('');
    setActiveCaseId(null);
    setError(null);
    setMissingFactKeys([]);
    setPage('workbench');
  }, []);

  const remediationRecommendations = activeCase?.response && !isReviewFailedResponse(activeCase.response)
    ? activeCase.response.review_result.recommended_actions
    : [];

  const remediationIssues = activeCase && activeCase.id === remediationCaseId && activeCase.response && !isReviewFailedResponse(activeCase.response)
    ? activeCase.response.review_result.issues
    : [];

  const handleEditCase = useCallback((saved: NonNullable<typeof activeCase>): void => {
    const editableMaterial = editableMaterialText(saved.materialText);
    setQuestion(saved.question);
    setMaterial(editableMaterial);
    setIntake({ ...saved.intake, data_types: [...saved.intake.data_types] });
    setEditingCaseId(saved.id);
    // Keep the frozen material versions so supplementing facts never replaces them.
    setEditingVersionIds(saved.materialSnapshot?.version_ids ?? []);
    setEditingMaterialText(editableMaterial);
    setError(null);
    setMissingFactKeys([]);
    setPage('workbench');
  }, []);

  const handleRerun = useCallback((q: string, m: string): void => {
    setQuestion(q);
    setMaterial(editableMaterialText(m));
    setIntake({ ...EMPTY_INTAKE });
    setEditingCaseId(null);
    setEditingVersionIds([]);
    setEditingMaterialText('');
    setActiveCaseId(null);
    setMissingFactKeys([]);
    setPage('workbench');
  }, []);

  if (booting) {
    return <div className="app-loading"><img src="/crosscomply-logo.svg" alt="" className="app-loading__mark" /><span>正在连接 CrossComply 新建案件…</span></div>;
  }
  if (!user) return <LoginPage onLogin={handleLogin} error={authError} />;

  return (
    <div className="app-shell">
      <Sidebar currentPage={page} onPageChange={setPage} onOpenCase={handleOpenCase} activeCaseId={activeCaseId} cases={cases} user={user} onLogout={() => void handleLogout()} onOpenGovernance={handleOpenGovernance} isMobileOpen={mobileSidebarOpen} onCloseMobile={() => setMobileSidebarOpen(false)} />
      {mobileSidebarOpen ? <button type="button" className="sidebar-scrim" onClick={() => setMobileSidebarOpen(false)} aria-label="关闭案件导航" /> : null}
      <main className="app-center">
        <div className="app-mobile-nav">
          <button type="button" className="app-mobile-menu" onClick={() => setMobileSidebarOpen(true)} aria-expanded={mobileSidebarOpen} aria-controls="primary-sidebar" aria-label="打开案件导航">
            <Menu size={20} strokeWidth={1.8} aria-hidden="true" />
          </button>
          <div className="app-mobile-brand">
            <img src="/crosscomply-logo.svg" alt="" className="app-mobile-brand__mark" />
            <span>CrossComply</span>
          </div>
          <div className="app-mobile-actions">
            <span className="app-mobile-surface">{page === 'governance' ? '用户管理' : page === 'knowledge-legal' ? '知识库 · 法律法规' : page === 'knowledge-policy' ? '知识库 · 规章制度' : page === 'my-remediations' ? '我的整改' : page === 'remediation-plan' ? '整改计划' : page === 'case-detail' ? '案件详情' : page === 'case-templates' ? '使用模板' : '新建案件'}</span>
          </div>
        </div>
        {error && page !== 'workbench' ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}
        {page === 'governance' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载用户管理…</div></div>}><GovernanceConsolePage user={user} /></Suspense> : null}
        {page === 'knowledge-legal' || page === 'knowledge-policy' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载知识库…</div></div>}><KnowledgeBasePage user={user} initialLibraryKind={page === 'knowledge-legal' ? 'legal' : 'internal_policy'} /></Suspense> : null}
        {page === 'my-remediations' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载我的整改…</div></div>}><MyRemediationsPage user={user} /></Suspense> : null}
        {page === 'remediation-plan' && remediationCaseId ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载整改计划…</div></div>}><RemediationPlanPage caseId={remediationCaseId} user={user} recommendations={remediationRecommendations} issues={remediationIssues} /></Suspense> : null}
        {page === 'case-detail' && activeCase ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载案件详情…</div></div>}><CaseDetailPage saved={activeCase} canEdit={user.role === 'requester'} canManageActions={user.role === 'reviewer' || user.role === 'admin'} viewerRole={user.role} onEdit={handleEditCase} onRerun={handleRerun} onBack={() => setPage('workbench')} onOpenRemediationPlan={() => handleOpenRemediationPlan(activeCase.id)} /></Suspense> : null}
        {page === 'case-templates' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载使用模板…</div></div>}><TemplateCenterPage user={user} onUseTemplate={handleUseTemplate} /></Suspense> : null}
        {page === 'workbench' ? <WorkbenchPage question={question} material={material} intake={intake} editingCaseId={editingCaseId} existingMaterialNames={editingMaterialNames} reservePastedMaterial={replacesPastedMaterial(material, editingMaterialText)} onQuestionChange={setQuestion} onMaterialChange={setMaterial} onIntakeChange={setIntake} onAnalyze={handleAnalyze} onSubmit={(q, m, confirmedIntake, files) => void handleSubmit(q, m, confirmedIntake, files)} loading={loading} analyzing={analyzing} error={error} missingFactKeys={missingFactKeys} historyCount={cases.length} summary={dashboardSummary} /> : null}
        {page === 'case-detail' && !activeCase ? <div className="state-block card"><h2>正在加载案件</h2><p>请从最近案件中选择一个案件。</p></div> : null}
      </main>
    </div>
  );
}
