import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { BookOpen, ClipboardCheck, Database, FilePlus2, FolderOpen, LayoutTemplate, LogOut, MessageSquare, Moon, ShieldCheck, Sun, X } from 'lucide-react';
import type { WorkbenchUser } from '../types/api';
import { useCaseStore } from '../store/caseStore';
import { listMyRemediations } from '../api/client';
import { actionableTasks } from '../utils/taskActions';
import { CASE_STATUS_LABELS } from '../utils/workflow';
import { currentTheme, setTheme as persistTheme, watchSystemTheme, type Theme } from '../utils/theme';

interface SidebarProps {
  user: WorkbenchUser;
  onLogout: () => void | Promise<void>;
  isMobileOpen: boolean;
  onCloseMobile: () => void;
}

export default function Sidebar({ user, onLogout, isMobileOpen, onCloseMobile }: SidebarProps): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const cases = useCaseStore();
  const [theme, setThemeState] = useState<Theme>(() => currentTheme());
  const [pending, setPending] = useState<number | null>(null);
  const visitKey = `crosscomply:recent-cases:${user.id}`;
  const [visits, setVisits] = useState<string[]>(() => {
    try { return JSON.parse(sessionStorage.getItem(visitKey) ?? '[]'); } catch { return []; }
  });
  useEffect(() => { watchSystemTheme(); }, []);
  useEffect(() => {
    const id = location.pathname.match(/^\/cases\/([^/]+)$/)?.[1];
    if (!id) return;
    setVisits((current) => {
      const next = [decodeURIComponent(id), ...current.filter((item) => item !== decodeURIComponent(id))].slice(0, 3);
      sessionStorage.setItem(visitKey, JSON.stringify(next));
      return next;
    });
  }, [location.pathname, visitKey]);
  useEffect(() => {
    let alive = true;
    const refresh = () => { void listMyRemediations({ scope: user.role === 'requester' ? 'mine' : 'review' })
      .then(({ items }) => { if (alive) setPending(actionableTasks(items, user).length); })
      .catch(() => { if (alive) setPending(null); }); };
    refresh();
    const timer = window.setInterval(refresh, 30000);
    window.addEventListener('remediation-changed', refresh);
    return () => { alive = false; window.clearInterval(timer); window.removeEventListener('remediation-changed', refresh); };
  }, [user.id, user.role, location.pathname]);
  const go = (path: string) => { onCloseMobile(); navigate(path); };
  const toggleTheme = (): void => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    persistTheme(next); setThemeState(next);
  };
  const nav = (path: string, label: string, icon: React.ReactNode, badge?: number | null) => <button type="button" key={path} className={'sidebar-nav-item' + (location.pathname.startsWith(path) ? ' is-active' : '')} onClick={() => go(path)}>{icon}<span>{label}</span>{badge ? <span className="sidebar-task-count" aria-label={`${badge} 项待处理`}>{badge}</span> : null}</button>;
  return <aside id="primary-sidebar" className={'app-sidebar sidebar' + (isMobileOpen ? ' is-mobile-open' : '')}>
    <div className="sidebar-brand"><button type="button" className="sidebar-brand-lockup sidebar-brand-home" onClick={() => go('/cases')} aria-label="CrossComply 案件中心"><img src="/crosscomply-logo.svg" alt="" className="sidebar-brand-mark" /><span className="sidebar-brand-title">CrossComply</span></button><button type="button" className="sidebar-mobile-close" onClick={onCloseMobile} aria-label="关闭案件导航"><X size={18} /></button></div>
    <div className="sidebar-create"><button className="btn-primary" type="button" onClick={() => go('/workbench')}><FilePlus2 size={18} />新建案件</button></div>
    <nav className="sidebar-section sidebar-nav" aria-label="主导航">
      {nav('/qa', '合规问答', <MessageSquare size={18} />)}
      {nav('/cases', '案件中心', <FolderOpen size={18} />)}
      {nav('/reviews', user.role === 'requester' ? '我的待办' : '待办与复核', <ClipboardCheck size={18} />, pending)}
      {nav('/templates', '场景模板', <LayoutTemplate size={18} />)}
    </nav>
    {visits.length ? <section className="sidebar-section sidebar-recent" aria-label="最近访问的案件"><div className="sidebar-section-label">最近访问</div>{visits.map((id) => cases.find((item) => item.id === id)).filter((item) => item !== undefined).map((item) => <button type="button" key={item.id} className="history-item history-item--button" onClick={() => go(`/cases/${item.id}`)}><span className="sidebar-recent-title">{item.question}</span><small>{CASE_STATUS_LABELS[item.status]}</small></button>)}</section> : null}
    <div className="sidebar-bottom">
      {user.role === 'admin' ? <nav className="sidebar-section sidebar-nav sidebar-admin" aria-label="管理导航">{nav('/knowledge', '知识库', <Database size={18} />)}{location.pathname.startsWith('/knowledge') ? <div className="sidebar-nav-submenu">{nav('/knowledge/legal', '法律法规', <BookOpen size={16} />)}{nav('/knowledge/internal_policy', '规章制度', <BookOpen size={16} />)}</div> : null}{nav('/governance', '用户管理', <ShieldCheck size={18} />)}</nav> : null}
      <div className="sidebar-user-card sidebar-user-card--bottom">
        <div className="sidebar-user-card__avatar" aria-hidden="true">
          <svg viewBox="0 0 24 24" role="presentation">
            <circle cx="12" cy="8" r="3.25" />
            <path d="M5.5 19.25c.55-3.35 2.82-5.25 6.5-5.25s5.95 1.9 6.5 5.25" />
          </svg>
        </div>
        <div className="sidebar-user-card__identity">
          {user.role === 'admin' ? (
            <button
              type="button"
              className="sidebar-user-card__profile"
              title="打开用户管理"
              onClick={() => { onCloseMobile(); navigate('/governance'); }}
            >
              <strong>{user.display_name}</strong>
              <span>管理员</span>
            </button>
          ) : (
            <div>
              <strong>{user.display_name}</strong>
              <span>{user.role === 'reviewer' ? '审核人' : '申报人'}</span>
            </div>
          )}
        </div>
        <button type="button" className="sidebar-user-card__logout" onClick={toggleTheme} title={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'} aria-label={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'}>{theme === 'dark' ? <Sun size={16} strokeWidth={1.8} /> : <Moon size={16} strokeWidth={1.8} />}</button>
        <button type="button" className="sidebar-user-card__logout" onClick={() => { onCloseMobile(); onLogout(); }} title="退出登录" aria-label="退出登录"><LogOut size={16} strokeWidth={1.8} /></button>
      </div>
    </div>
  </aside>;
}
