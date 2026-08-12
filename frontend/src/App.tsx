import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import type { CaseIntake, CaseStatus, DashboardSummaryApi, WorkbenchUser } from './types/api';
import type { Page } from './components/Sidebar';
import Sidebar from './components/Sidebar';
import WorkbenchPage from './components/WorkbenchPage';
import LoginPage from './components/LoginPage';
import { ApiError, createCase, getCurrentUser, getDashboardSummary, login, logout, runCase, updateCase, updateCaseStatus } from './api/client';
import { EMPTY_INTAKE, fromDetail, openCase, refreshCases, useCaseStore } from './store/caseStore';

const GovernanceConsolePage = lazy(() => import('./components/GovernanceConsolePage'));
const CaseDetailPage = lazy(() => import('./components/CaseDetailPage'));

export default function App(): JSX.Element {
  const [user, setUser] = useState<WorkbenchUser | null>(null);
  const [booting, setBooting] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>('workbench');
  const [activeCaseId, setActiveCaseId] = useState<string | null>(null);
  const [editingCaseId, setEditingCaseId] = useState<string | null>(null);
  const [question, setQuestion] = useState('');
  const [material, setMaterial] = useState('');
  const [intake, setIntake] = useState<CaseIntake>({ ...EMPTY_INTAKE });
  const [reviewMode, setReviewMode] = useState<'llm' | 'multi_agent'>('llm');
  const [rerankMode, setRerankMode] = useState<'off' | 'embedding'>('off');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dashboardSummary, setDashboardSummary] = useState<DashboardSummaryApi | null>(null);
  const cases = useCaseStore();
  const activeCase = useMemo(() => activeCaseId ? cases.find((item) => item.id === activeCaseId) ?? null : null, [cases, activeCaseId]);

  useEffect(() => {
    let mounted = true;
    void getCurrentUser().then(async (current) => {
      if (!mounted) return;
      setUser(current);
      if (current) {
        const [, summary] = await Promise.all([refreshCases(), getDashboardSummary()]);
        if (mounted) setDashboardSummary(summary);
      }
    }).catch((reason) => {
      if (mounted) setAuthError(reason instanceof Error ? reason.message : '无法连接到工作台');
    }).finally(() => {
      if (mounted) setBooting(false);
    });
    return () => { mounted = false; };
  }, []);

  const handleLogin = useCallback(async (username: string, password: string): Promise<void> => {
    setAuthError(null);
    const current = await login(username, password);
    setUser(current);
    const [, summary] = await Promise.all([refreshCases(), getDashboardSummary()]);
    setDashboardSummary(summary);
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

  const handleSubmit = useCallback(async (q: string, m: string, confirmedIntake: CaseIntake, file?: File | null): Promise<void> => {
    if (!user) return;
    setLoading(true);
    setError(null);
    try {
      const saved = editingCaseId
        ? await updateCase(editingCaseId, {
          question: q,
          material_text: m,
          intake: confirmedIntake,
          facts_confirmed: true,
        })
        : await createCase({ question: q, materialText: m, intake: confirmedIntake, reviewMode, rerankMode, file });
      const submitted = await updateCaseStatus(saved.case.id, 'submitted');
      if (user.role === 'reviewer' || user.role === 'admin') {
        const result = await runCase(submitted.case.id);
        const detail = fromDetail(result);
        // The store is refreshed from the server so response, actions and events stay together.
        await openCase(detail.id);
      } else {
        await openCase(submitted.case.id);
      }
      setDashboardSummary(await getDashboardSummary());
      setEditingCaseId(null);
      setActiveCaseId(saved.case.id);
      setPage('case-detail');
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

  const handleScenarioClick = useCallback((scenario: string): void => {
    setQuestion(scenario);
    setPage('workbench');
  }, []);

  const handleEditCase = useCallback((saved: NonNullable<typeof activeCase>): void => {
    setQuestion(saved.question);
    setMaterial(saved.materialText);
    setIntake({ ...saved.intake, data_types: [...saved.intake.data_types] });
    setEditingCaseId(saved.id);
    setError(null);
    setPage('workbench');
  }, []);

  const handleStatusChange = useCallback(async (caseId: string, status: CaseStatus): Promise<void> => {
    try {
      await updateCaseStatus(caseId, status);
      await openCase(caseId);
      setDashboardSummary(await getDashboardSummary());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法更新案件状态');
    }
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
    return <div className="app-loading"><div className="app-loading__mark">CC</div><span>正在连接 CrossComply 工作台…</span></div>;
  }
  if (!user) return <LoginPage onLogin={handleLogin} error={authError} />;

  if (page === 'governance') {
    return (
      <Suspense fallback={<div className="app-loading"><div className="app-loading__mark">CC</div><span>正在加载治理控制台…</span></div>}>
        <GovernanceConsolePage user={user} onBack={() => setPage('workbench')} onLogout={() => void handleLogout()} />
      </Suspense>
    );
  }

  return (
    <div className="app-shell">
      <Sidebar currentPage={page} onPageChange={setPage} onScenarioClick={handleScenarioClick} onOpenCase={handleOpenCase} activeCaseId={activeCaseId} cases={cases} user={user} onLogout={() => void handleLogout()} onOpenGovernance={handleOpenGovernance} />
      <main className="app-center">
        <div className="app-mobile-nav">
          <span className="app-mobile-brand">CrossComply</span>
          <div className="app-mobile-actions">
            <span className="app-mobile-surface">案件工作台</span>
            {user.role === 'admin' ? <button type="button" className="app-mobile-utility" onClick={handleOpenGovernance}>治理控制台 ↗</button> : null}
          </div>
        </div>
        {error && page !== 'workbench' ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}
        {page === 'case-detail' && activeCase ? <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载案件详情…</div></div>}><CaseDetailPage saved={activeCase} canEdit={user.role === 'requester'} canManageActions={user.role === 'reviewer' || user.role === 'admin'} viewerRole={user.role} onEdit={handleEditCase} onRerun={handleRerun} onBack={() => setPage('workbench')} onStatusChange={(id, status) => void handleStatusChange(id, status)} /></Suspense> : null}
        {page === 'workbench' ? <WorkbenchPage question={question} material={material} intake={intake} reviewMode={reviewMode} rerankMode={rerankMode} editingCaseId={editingCaseId} onQuestionChange={setQuestion} onMaterialChange={setMaterial} onIntakeChange={setIntake} onReviewModeChange={setReviewMode} onRerankModeChange={setRerankMode} onSubmit={(q, m, confirmedIntake, file) => void handleSubmit(q, m, confirmedIntake, file)} loading={loading} error={error} historyCount={cases.length} summary={dashboardSummary} /> : null}
        {page === 'case-detail' && !activeCase ? <div className="state-block card"><h2>正在加载案件</h2><p>请从左侧案件队列选择一个案件。</p></div> : null}
      </main>
    </div>
  );
}
