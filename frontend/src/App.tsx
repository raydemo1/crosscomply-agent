import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate, useOutletContext, useParams } from 'react-router-dom';
import { Menu } from 'lucide-react';
import type { WorkbenchUser } from './types/api';
import type { QAHandoff, QASummary } from './types/qa';
import Sidebar from './components/Sidebar';
import WorkbenchPage from './components/WorkbenchPage';
import LoginPage from './components/LoginPage';
import { ApiError, getCurrentUser, listQAConversations, login, logout } from './api/client';
import { openCase, refreshCases, useCaseStore } from './store/caseStore';

const ComplianceQAPage = lazy(() => import('./components/ComplianceQAPage'));
const GovernanceConsolePage = lazy(() => import('./components/GovernanceConsolePage'));
const KnowledgeBasePage = lazy(() => import('./components/KnowledgeBasePage'));
const CaseDetailPage = lazy(() => import('./components/CaseDetailPage'));
const TemplateCenterPage = lazy(() => import('./components/TemplateCenterPage'));
const RemediationPlanPage = lazy(() => import('./components/RemediationPlanPage'));
const MyRemediationsPage = lazy(async () => {
  const module = await import('./components/RemediationPlanPage');
  return { default: module.MyRemediationsPage };
});

// needs_info 也要轮询：申请人补充、审核人应答都会让它随时回到审查运行中
const TRANSITIONAL_STATUSES = ['pending_review', 'review_running', 'needs_info'];

interface ShellContext {
  user: WorkbenchUser;
  refreshQaHistory: () => Promise<void>;
  closeMobileNav: () => void;
}

export default function App(): JSX.Element {
  const [user, setUser] = useState<WorkbenchUser | null>(null);
  const [booting, setBooting] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    void getCurrentUser().then((current) => {
      if (mounted) setUser(current);
    }).catch((reason) => {
      if (mounted) setAuthError(reason instanceof Error ? reason.message : '无法连接到 CrossComply 服务，请检查网络后重试。');
    }).finally(() => {
      if (mounted) setBooting(false);
    });
    return () => { mounted = false; };
  }, []);

  const handleLogin = useCallback(async (username: string, password: string): Promise<void> => {
    setAuthError(null);
    try {
      const current = await login(username, password);
      setUser(current);
    } catch (reason) {
      setAuthError(
        reason instanceof ApiError && reason.status === 401
          ? '用户名或密码错误，请检查后重试。'
          : reason instanceof Error
            ? reason.message
            : '登录失败，请稍后重试。',
      );
    }
  }, []);

  if (booting) {
    return <div className="app-loading"><img src="/crosscomply-logo.svg" alt="" className="app-loading__mark" /><span>正在连接 CrossComply…</span></div>;
  }
  if (!user) return <LoginPage onLogin={handleLogin} error={authError} />;

  return (
    <Routes>
      <Route element={<Shell user={user} onLogout={logout} onUserChanged={() => setUser(null)} />}>
        <Route path="/qa/:conversationId?" element={<QaRoute user={user} />} />
        <Route path="/workbench" element={<WorkbenchRoute user={user} />} />
        <Route path="/templates" element={<TemplateRoute user={user} />} />
        <Route path="/reviews" element={<MyRemediationsRoute user={user} />} />
        <Route path="/reviews/plan/:caseId" element={<RemediationPlanRoute user={user} />} />
        <Route path="/cases/:caseId" element={<CaseDetailRoute user={user} />} />
        <Route path="/knowledge/:kind" element={<KnowledgeRoute user={user} />} />
        <Route path="/governance" element={<GovernanceRoute user={user} />} />
        <Route path="/index.html" element={<LegacyRedirect />} />
        <Route path="*" element={<LegacyRedirect />} />
      </Route>
    </Routes>
  );
}

/** 旧版链接（/?case=…&remediation=plan、/?qa=…）与未知路径的重定向。 */
function LegacyRedirect(): JSX.Element {
  const params = new URLSearchParams(window.location.search);
  const caseId = params.get('case');
  const qaId = params.get('qa');
  if (caseId) {
    return <Navigate to={params.get('remediation') === 'plan' ? `/reviews/plan/${caseId}` : `/cases/${caseId}`} replace />;
  }
  if (qaId) return <Navigate to={`/qa/${qaId}`} replace />;
  return <Navigate to="/qa" replace />;
}

/** 已登录外壳：侧栏 + 主内容。问答历史等跨页状态在这里持有。 */
function Shell({ user, onLogout, onUserChanged }: { user: WorkbenchUser; onLogout: typeof logout; onUserChanged: () => void }): JSX.Element {
  const navigate = useNavigate();
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [qaHistory, setQaHistory] = useState<QASummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const location = useLocation();

  const refreshQaHistory = useCallback(async (): Promise<void> => {
    try {
      setQaHistory(await listQAConversations());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取问答历史，请稍后重试。');
    }
  }, []);

  useEffect(() => { void refreshQaHistory(); void refreshCases(); }, [refreshQaHistory]);

  // 页面切换时关闭移动端抽屉
  useEffect(() => { setMobileSidebarOpen(false); }, [location.pathname]);

  const handleLogout = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      await onLogout();
      onUserChanged();
      navigate('/qa', { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '退出登录失败，请稍后重试。');
    }
  }, [onLogout, onUserChanged, navigate]);

  return (
    <div className="app-shell">
      <Sidebar
        user={user}
        qaHistory={qaHistory}
        onLogout={handleLogout}
        isMobileOpen={mobileSidebarOpen}
        onCloseMobile={() => setMobileSidebarOpen(false)}
      />
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
        </div>
        {error ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}
        <Suspense fallback={<div className="card state-block"><div className="state-block__title">正在加载…</div></div>}>
          <Outlet context={{ user, refreshQaHistory, closeMobileNav: () => setMobileSidebarOpen(false) } satisfies ShellContext} />
        </Suspense>
      </main>
    </div>
  );
}

function useShellContext(): ShellContext {
  return useOutletContext<ShellContext>();
}

// ---------------------------------------------------------------------------
// 路由包装组件：按路由参数加载 store 数据并组装页面 props
// ---------------------------------------------------------------------------

function QaRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const { refreshQaHistory, closeMobileNav } = useShellContext();
  return <ComplianceQAPage
    conversationId={conversationId ?? null}
    onConversationChange={(id) => navigate(id ? `/qa/${id}` : '/qa', { replace: true })}
    onNewConversation={() => { closeMobileNav(); navigate('/qa'); }}
    onHistoryChange={() => void refreshQaHistory()}
    onHandoff={(handoff: QAHandoff) => { closeMobileNav(); navigate('/workbench', { state: { handoff } }); }}
  />;
}

function WorkbenchRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  const { caseId } = useParams();
  const location = useLocation();
  const cases = useCaseStore();
  const saved = caseId ? cases.find((item) => item.id === caseId) ?? null : null;
  const [loading, setLoading] = useState(Boolean(caseId));

  useEffect(() => {
    if (!caseId) { setLoading(false); return; }
    let mounted = true;
    setLoading(true);
    void openCase(caseId).catch(() => {}).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [caseId]);

  if (loading) return <div className="card state-block"><div className="state-block__title">正在读取案件…</div></div>;
  if (caseId && !saved) return <div className="card state-block"><div className="state-block__title">未找到案件</div><p>请从最近案件中选择一个案件。</p></div>;

  const seed = (location.state ?? null) as { handoff?: QAHandoff; template?: import('./types/api').CaseTemplateApi; rerun?: { question: string; material: string } } | null;
  return <WorkbenchPage editSeed={caseId ? saved : null} seed={seed} />;
}

function TemplateRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  const navigate = useNavigate();
  const { closeMobileNav } = useShellContext();
  return <TemplateCenterPage
    user={user}
    onUseTemplate={(template) => { closeMobileNav(); navigate('/workbench', { state: { template } }); }}
  />;
}

function MyRemediationsRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  return <MyRemediationsPage user={user} />;
}

function RemediationPlanRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const cases = useCaseStore();
  const saved = cases.find((item) => item.id === caseId) ?? null;
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    void openCase(caseId ?? '').catch(() => {}).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [caseId]);

  if (loading) return <div className="card state-block"><div className="state-block__title">正在读取案件…</div></div>;
  if (!saved) return <div className="card state-block"><div className="state-block__title">未找到案件</div><p>请从最近案件中选择一个案件。</p></div>;

  const report = saved.response !== null && 'review_result' in saved.response
    ? saved.response.review_result
    : null;
  const recommendations = report?.recommended_actions ?? [];
  const issues = report?.issues ?? [];
  return <RemediationPlanPage
    key={saved.id}
    caseId={saved.id}
    user={user}
    recommendations={recommendations}
    issues={issues}
    onOpenAgentQuestion={() => navigate(`/cases/${saved.id}`, { state: { focusAgentAnswer: true } })}
  />;
}

function CaseDetailRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const cases = useCaseStore();
  const saved = cases.find((item) => item.id === caseId) ?? null;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const dirtyRef = useRef(false);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);
    void openCase(caseId ?? '').then(() => {
      if (mounted) dirtyRef.current = false;
    }).catch((reason) => {
      if (mounted) setError(reason instanceof Error ? reason.message : '无法打开案件');
    }).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [caseId]);

  // 排队/运行中的案件每 8 秒自动刷新，完成后停止
  const status = saved?.status ?? null;
  useEffect(() => {
    if (!caseId || !status || !TRANSITIONAL_STATUSES.includes(status)) return undefined;
    const timer = window.setInterval(() => { void openCase(caseId).catch(() => {}); }, 8000);
    return () => window.clearInterval(timer);
  }, [caseId, status]);

  useEffect(() => {
    document.title = status === 'review_running' ? '审查运行中… · CrossComply'
      : status === 'pending_review' ? '等待审查 · CrossComply'
        : status === 'needs_info' ? '待补充信息 · CrossComply'
          : 'CrossComply - 数据出境合规智能体';
    return () => { document.title = 'CrossComply - 数据出境合规智能体'; };
  }, [status]);

  if (loading) return <div className="card state-block"><div className="state-block__title">正在读取案件详情…</div></div>;
  if (error) return <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div>;
  if (!saved) return <div className="card state-block"><div className="state-block__title">未找到案件</div><p>请从最近案件中选择一个案件。</p></div>;

  const guard = (): boolean => {
    if (!dirtyRef.current) return true;
    return window.confirm('原文审阅有未提交的修改，离开将丢失。确定离开吗？');
  };

  return <CaseDetailPage
    key={saved.id}
    saved={saved}
    canEdit={user.role === 'requester'}
    canManageActions={user.role === 'reviewer' || user.role === 'admin'}
    viewerRole={user.role}
    focusAgentAnswer={Boolean((location.state as { focusAgentAnswer?: boolean } | null)?.focusAgentAnswer)}
    onAgentAnswerFocused={() => navigate(`/cases/${saved.id}`, { replace: true, state: null })}
    onDocumentDirtyChange={(dirty) => { dirtyRef.current = dirty; }}
    onEdit={() => { if (guard()) navigate(`/workbench?case=${saved.id}`); }}
    onRerun={(question, material) => navigate('/workbench', { state: { rerun: { question, material } } })}
    onBack={() => { if (guard()) navigate('/workbench'); }}
    onOpenRemediationPlan={() => navigate(`/reviews/plan/${saved.id}`)}
    onReviewSettled={() => { if (caseId) void openCase(caseId).catch(() => {}); }}
  />;
}

function KnowledgeRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  const { kind } = useParams();
  return <KnowledgeBasePage user={user} initialLibraryKind={kind === 'internal_policy' ? 'internal_policy' : 'legal'} />;
}

function GovernanceRoute({ user }: { user: WorkbenchUser }): JSX.Element {
  return <GovernanceConsolePage user={user} />;
}
