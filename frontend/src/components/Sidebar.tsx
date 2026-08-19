import { useMemo, useState } from 'react';
import { BookOpen, ClipboardCheck, Files, LogOut, Scale, ShieldCheck, X } from 'lucide-react';
import type { WorkbenchUser } from '../types/api';
import type { SavedCase } from '../types/case';
import { relativeTime, truncate } from '../utils/display';
import { CASE_STATUS_LABELS } from '../utils/workflow';

/**
 * Top-level shell destinations. The library destinations are intentionally
 * part of the shell contract before their full pages ship, so the navigation
 * can grow without changing the sidebar API again.
 */
export type Page =
  | 'workbench'
  | 'governance'
  | 'case-detail'
  | 'my-remediations'
  | 'remediation-plan'
  | 'legal-library'
  | 'policy-management';

interface SidebarProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
  onScenarioClick: (scenario: string) => void;
  onOpenCase: (caseId: string) => void;
  activeCaseId?: string | null;
  cases: SavedCase[];
  user: WorkbenchUser;
  demoMode?: boolean;
  onLogout: () => void;
  onOpenGovernance: () => void;
  isMobileOpen: boolean;
  onCloseMobile: () => void;
}

const SCENARIOS = [
  '是否需要数据出境安全评估？',
  '个人信息出境应走哪条合规路径？',
];

const RISK_DISPLAY: Record<string, { label: string; className: string }> = {
  high: { label: '高风险', className: 'history-item__risk history-item__risk--high' },
  medium: { label: '中风险', className: 'history-item__risk history-item__risk--medium' },
  low: { label: '低风险', className: 'history-item__risk history-item__risk--low' },
  insufficient_evidence: { label: '证据不足', className: 'history-item__risk history-item__risk--insufficient' },
  pending: { label: '待评估', className: 'history-item__risk history-item__risk--pending' },
};

function riskDisplay(item: SavedCase): { label: string; className: string } {
  const level = item.riskLevel ?? (
    item.response && 'review_result' in item.response
      ? item.response.review_result.risk_level
      : null
  );
  return level ? (RISK_DISPLAY[level] ?? RISK_DISPLAY.pending) : RISK_DISPLAY.pending;
}

export default function Sidebar({
  currentPage,
  onPageChange,
  onScenarioClick,
  onOpenCase,
  activeCaseId,
  cases,
  user,
  demoMode = false,
  onLogout,
  onOpenGovernance,
  isMobileOpen,
  onCloseMobile,
}: SidebarProps): JSX.Element {
  const [query, setQuery] = useState('');
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return cases;
    return cases.filter((item) => item.question.toLowerCase().includes(needle) || item.id.toLowerCase().includes(needle));
  }, [cases, query]);

  return (
    <aside id="primary-sidebar" className={'app-sidebar sidebar' + (isMobileOpen ? ' is-mobile-open' : '')}>
      <div className="sidebar-brand">
        <div className="sidebar-brand-lockup">
          <img src="/crosscomply-logo.svg" alt="" className="sidebar-brand-mark" />
          <div className="sidebar-brand-copy">
            <div className="sidebar-brand-title">CrossComply</div>
            <div className="sidebar-brand-subtitle">跨境数据合规系统</div>
          </div>
        </div>
        <button type="button" className="sidebar-mobile-close" onClick={onCloseMobile} aria-label="关闭案件导航"><X size={18} strokeWidth={1.8} /></button>
      </div>

      <nav className="sidebar-section" aria-label="主导航">
        <div className="sidebar-nav">
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'workbench' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('workbench'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><Files size={18} strokeWidth={1.8} /></span>
            <span>案件管理</span>
          </button>
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'my-remediations' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('my-remediations'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><ClipboardCheck size={18} strokeWidth={1.8} /></span>
            <span>我的整改</span>
          </button>
          <button type="button" className="sidebar-nav-item sidebar-nav-item--coming-soon" disabled aria-disabled="true">
            <span className="sidebar-nav-item-icon" aria-hidden="true"><Scale size={18} strokeWidth={1.8} /></span>
            <span>法律法规库</span>
            <span className="sidebar-nav-item-status">即将开放</span>
          </button>
          <button type="button" className="sidebar-nav-item sidebar-nav-item--coming-soon" disabled aria-disabled="true">
            <span className="sidebar-nav-item-icon" aria-hidden="true"><BookOpen size={18} strokeWidth={1.8} /></span>
            <span>政策制度</span>
            <span className="sidebar-nav-item-status">即将开放</span>
          </button>
          {user.role === 'admin' ? (
            <button type="button" className={'sidebar-nav-item' + (currentPage === 'governance' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onOpenGovernance(); }}>
              <span className="sidebar-nav-item-icon" aria-hidden="true"><ShieldCheck size={18} strokeWidth={1.8} /></span>
              <span>用户治理</span>
            </button>
          ) : null}
        </div>
      </nav>

      <details className="sidebar-section sidebar-scenarios-disclosure">
        <summary className="sidebar-section-label">更多审查场景</summary>
        <div className="sidebar-scenarios">
          {SCENARIOS.map((scenario) => (
            <button key={scenario} type="button" className="sidebar-scenario" onClick={() => { onCloseMobile(); onScenarioClick(scenario); }}>
              {scenario}
            </button>
          ))}
        </div>
      </details>

      <div className="sidebar-section sidebar-history">
        <div className="sidebar-section-label">
          案件记录 <span className="sidebar-history__count">{cases.length}</span>
        </div>
        {cases.length > 0 ? (
          <input type="search" className="sidebar-history__search" placeholder="搜索案件或编号" value={query} onChange={(event) => setQuery(event.target.value)} />
        ) : null}
        <div className="sidebar-history__list">
          {filtered.length === 0 ? (
            <div className="sidebar-history__empty">暂无匹配案件。</div>
          ) : filtered.map((item) => {
            const risk = riskDisplay(item);
            return (
              <button key={item.id} type="button" className={'history-item history-item--button' + (item.id === activeCaseId ? ' is-active' : '')} onClick={() => { onCloseMobile(); onOpenCase(item.id); }}>
                <div className="history-item__top">
                  <span className="history-item__question">{truncate(item.question, 32)}</span>
                </div>
                <div className="history-item__meta">
                  <span className={risk.className}>{risk.label}</span>
                  <span>{CASE_STATUS_LABELS[item.status]}</span>
                  <span>{relativeTime(item.savedAt)}</span>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      <div className="sidebar-user-card sidebar-user-card--bottom">
        <div className="sidebar-user-card__avatar" aria-hidden="true">
          <svg viewBox="0 0 24 24" role="presentation">
            <circle cx="12" cy="8" r="3.25" />
            <path d="M5.5 19.25c.55-3.35 2.82-5.25 6.5-5.25s5.95 1.9 6.5 5.25" />
          </svg>
        </div>
        <div className="sidebar-user-card__identity">
          <strong>{user.display_name}</strong>
          <span>{user.role === 'admin' ? '管理员' : user.role === 'reviewer' ? '合规审核人' : '业务申请人'}</span>
        </div>
        {demoMode ? <span className="sidebar-user-card__demo">演示</span> : <button type="button" className="sidebar-user-card__logout" onClick={() => { onCloseMobile(); onLogout(); }} title="退出登录" aria-label="退出登录"><LogOut size={16} strokeWidth={1.8} /></button>}
      </div>
    </aside>
  );
}
