import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Menu } from 'lucide-react';
import { isReviewFailedResponse } from './types/api';
import type { CaseIntake, CaseTemplateApi, ComplianceFactsApi, DashboardSummaryApi, WorkbenchUser } from './types/api';
import type { Page } from './components/Sidebar';
import Sidebar from './components/Sidebar';
import WorkbenchPage from './components/WorkbenchPage';
import LoginPage from './components/LoginPage';
import { ApiError, createCase, freezeMaterialSnapshot, getCurrentUser, getDashboardSummary, login, logout, updateCase, updateCaseStatus, uploadMaterial } from './api/client';
import { EMPTY_INTAKE, initializeDemoCase, openCase, refreshCases, useCaseStore } from './store/caseStore';

const PUBLIC_DEMO_ENABLED = import.meta.env.VITE_PUBLIC_DEMO === 'true';

const GovernanceConsolePage = lazy(() => import('./components/GovernanceConsolePage'));
const CaseDetailPage = lazy(() => import('./components/CaseDetailPage'));
const TemplateCenterPage = lazy(() => import('./components/TemplateCenterPage'));
const RemediationPlanPage = lazy(() => import('./components/RemediationPlanPage'));
const MyRemediationsPage = lazy(async () => {
  const module = await import('./components/RemediationPlanPage');
  return { default: module.MyRemediationsPage };
});

function confirmedBoolean<T extends string>(
  value: T,
  positive: T,
  negative: T,
): boolean | null {
  if (value === positive) return true;
  if (value === negative) return false;
  return null;
}

function confirmedCount(value: string): number | null {
  const normalized = value.trim();
  if (!/^\d+$/.test(normalized)) return null;
  const count = Number(normalized);
  return Number.isSafeInteger(count) ? count : null;
}

function toComplianceFacts(intake: CaseIntake): ComplianceFactsApi {
  return {
    cross_border_transfer: intake.cross_border_transfer,
    is_ciio: confirmedBoolean(intake.ciio_status, 'ciio', 'not_ciio'),
    important_data: confirmedBoolean(
      intake.important_data_status,
      'important',
      'not_important',
    ),
    contains_personal_information: intake.data_types.length > 0 ? true : null,
    contains_sensitive_personal_information: intake.sensitive_personal_info,
    cumulative_personal_information_subjects: confirmedCount(intake.annual_non_sensitive_count),
    cumulative_sensitive_personal_information_subjects: confirmedCount(intake.annual_sensitive_count),
    claimed_exemption: null,
    exemption_facts_confirmed: null,
    special_regimes: [],
  };
}

function materialOriginal(material: string, file?: File | null): File {
  if (file) return file;
  return new File([material], 'case-material.txt', { type: 'text/plain;charset=utf-8' });
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
  const [question, setQuestion] = useState('');
  const [material, setMaterial] = useState('');
  const [intake, setIntake] = useState<CaseIntake>({ ...EMPTY_INTAKE });
  const [reviewMode, setReviewMode] = useState<'llm' | 'multi_agent'>('llm');
  const [rerankMode, setRerankMode] = useState<'off' | 'embedding'>('off');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dashboardSummary, setDashboardSummary] = useState<DashboardSummaryApi | null>(null);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const cases = useCaseStore();
  const activeCase = useMemo(() => activeCaseId ? cases.find((item) => item.id === activeCaseId) ?? null : null, [cases, activeCaseId]);

  useEffect(() => {
    if (PUBLIC_DEMO_ENABLED) {
      let mounted = true;
      void import('./demo/demoCase').then(({ DEMO_CASE, DEMO_SUMMARY, DEMO_USER }) => {
        if (!mounted) return;
        initializeDemoCase(DEMO_CASE);
        setUser(DEMO_USER);
        setDashboardSummary(DEMO_SUMMARY);
        setActiveCaseId(DEMO_CASE.id);
        setPage('case-detail');
        setBooting(false);
      });
      return () => { mounted = false; };
    }
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
    if (PUBLIC_DEMO_ENABLED) return;
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

  const handleSubmit = useCallback(async (q: string, m: string, confirmedIntake: CaseIntake, file?: File | null): Promise<void> => {
    if (!user) return;
    if (PUBLIC_DEMO_ENABLED) {
      setError('当前是公开演示模式。要提交自己的问题，请部署 CrossComply 服务端并配置模型、知识库和对象存储 Key。');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const saved = editingCaseId
        ? await updateCase(editingCaseId, {
          question: q,
          material_text: m,
          intake: confirmedIntake,
        })
        : await createCase({ question: q, materialText: m, intake: confirmedIntake, reviewMode, rerankMode, file });
      setEditingCaseId(null);
      setActiveCaseId(saved.case.id);
      setPage('case-detail');
      const version = await uploadMaterial(
        saved.case.id,
        'review_material',
        materialOriginal(m, file),
      );
      const frozen = await freezeMaterialSnapshot(
        saved.case.id,
        [version.id],
        toComplianceFacts(confirmedIntake),
      );
      if (frozen.rule_decision.determination.needs_info.length > 0) {
        await openCase(saved.case.id);
        setDashboardSummary(await getDashboardSummary());
        return;
      }
      const pending = await updateCaseStatus(saved.case.id, 'pending_review');
      await openCase(pending.case.id);
      setDashboardSummary(await getDashboardSummary());
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : '提交案件时发生未知错误');
    } finally {
      setLoading(false);
    }
  }, [editingCaseId, rerankMode, reviewMode, user]);

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
    setReviewMode(template.review_mode);
    setRerankMode(template.rerank_mode);
    setEditingCaseId(null);
    setActiveCaseId(null);
    setError(null);
    setPage('workbench');
  }, []);

  const remediationRecommendations = activeCase?.response && !isReviewFailedResponse(activeCase.response)
    ? activeCase.response.review_result.recommended_actions
    : [];

  const handleEditCase = useCallback((saved: NonNullable<typeof activeCase>): void => {
    setQuestion(saved.question);
    setMaterial(saved.materialText);
    setIntake({ ...saved.intake, data_types: [...saved.intake.data_types] });
    setEditingCaseId(saved.id);
    setError(null);
    setPage('workbench');
  }, []);

  const handleRerun = useCallback((q: string, m: string): void => {
    setQuestion(q);
    setMaterial(m);
    setIntake({ ...EMPTY_INTAKE });
    setEditingCaseId(null);
    setActiveCaseId(null);
    setPage('workbench');
  }, []);

  if (booting) {
    return <div className="app-loading"><img src="/crosscomply-logo.svg" alt="" className="app-loading__mark" /><span>正在连接 CrossComply 新建案件…</span></div>;
  }
  if (!user) return <LoginPage onLogin={handleLogin} error={authError} />;

  return (
    <div className="app-shell">
      <Sidebar currentPage={page} onPageChange={setPage} onOpenCase={handleOpenCase} activeCaseId={activeCaseId} cases={cases} user={user} demoMode={PUBLIC_DEMO_ENABLED} onLogout={() => void handleLogout()} onOpenGovernance={handleOpenGovernance} isMobileOpen={mobileSidebarOpen} onCloseMobile={() => setMobileSidebarOpen(false)} />
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
            <span className="app-mobile-surface">{page === 'governance' ? '用户管理' : page === 'my-remediations' ? '我的整改' : page === 'remediation-plan' ? '整改计划' : page === 'case-detail' ? '案件详情' : page === 'case-templates' ? '使用模板' : '新建案件'}</span>
          </div>
        </div>
        {error && page !== 'workbench' ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}
        {page === 'governance' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载用户管理…</div></div>}><GovernanceConsolePage user={user} /></Suspense> : null}
        {page === 'my-remediations' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载我的整改…</div></div>}><MyRemediationsPage user={user} /></Suspense> : null}
        {page === 'remediation-plan' && remediationCaseId ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载整改计划…</div></div>}><RemediationPlanPage caseId={remediationCaseId} user={user} recommendations={remediationRecommendations} /></Suspense> : null}
        {page === 'case-detail' && activeCase ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载案件详情…</div></div>}><CaseDetailPage saved={activeCase} demoMode={PUBLIC_DEMO_ENABLED} canEdit={user.role === 'requester' && !PUBLIC_DEMO_ENABLED} canManageActions={user.role === 'reviewer' || user.role === 'admin'} viewerRole={user.role} onEdit={handleEditCase} onRerun={handleRerun} onBack={() => setPage('workbench')} onOpenRemediationPlan={() => handleOpenRemediationPlan(activeCase.id)} /></Suspense> : null}
        {page === 'case-templates' ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载使用模板…</div></div>}><TemplateCenterPage onUseTemplate={handleUseTemplate} demoMode={PUBLIC_DEMO_ENABLED} /></Suspense> : null}
        {page === 'workbench' ? <WorkbenchPage question={question} material={material} intake={intake} reviewMode={reviewMode} rerankMode={rerankMode} editingCaseId={editingCaseId} demoMode={PUBLIC_DEMO_ENABLED} onQuestionChange={setQuestion} onMaterialChange={setMaterial} onIntakeChange={setIntake} onReviewModeChange={setReviewMode} onRerankModeChange={setRerankMode} onSubmit={(q, m, confirmedIntake, file) => void handleSubmit(q, m, confirmedIntake, file)} loading={loading} error={error} historyCount={cases.length} summary={dashboardSummary} /> : null}
        {page === 'case-detail' && !activeCase ? <div className="state-block card"><h2>正在加载案件</h2><p>请从最近案件中选择一个案件。</p></div> : null}
      </main>
    </div>
  );
}
