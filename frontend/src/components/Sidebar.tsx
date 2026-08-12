import { useMemo, useState } from 'react';
import type { WorkbenchUser } from '../types/api';
import type { SavedCase } from '../types/case';
import { relativeTime, truncate } from '../utils/display';

export type Page = 'workbench' | 'governance' | 'case-detail';

interface SidebarProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
  onScenarioClick: (scenario: string) => void;
  onOpenCase: (caseId: string) => void;
  activeCaseId?: string | null;
  cases: SavedCase[];
  user: WorkbenchUser;
  onLogout: () => void;
  onOpenGovernance: () => void;
  isMobileOpen: boolean;
  onCloseMobile: () => void;
}

const SCENARIOS = [
  '这个业务是否需要数据出境安全评估？',
  '数据出境安全评估的申报条件是什么？',
  '智能网联汽车数据出境有什么特殊要求？',
  '上海自贸区数据出境负面清单有什么要求？',
];

const STATUS_LABELS: Record<SavedCase['status'], string> = {
  draft: '草稿',
  submitted: '待审核',
  in_review: '审查中',
  needs_info: '待补充',
  completed: '已完成',
  review_failed: '运行失败',
};

const RISK_DOT_CLASS: Record<string, string> = {
  high: 'risk-dot risk-dot--high',
  medium: 'risk-dot risk-dot--medium',
  low: 'risk-dot risk-dot--low',
  insufficient_evidence: 'risk-dot risk-dot--insufficient',
};

export default function Sidebar({
  currentPage,
  onPageChange,
  onScenarioClick,
  onOpenCase,
  activeCaseId,
  cases,
  user,
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
        <div className="sidebar-brand-title">CrossComply</div>
        <button type="button" className="sidebar-mobile-close" onClick={onCloseMobile} aria-label="关闭案件导航">×</button>
      </div>

      <div className="sidebar-user-card">
        <div className="sidebar-user-card__avatar">{user.display_name.slice(0, 1)}</div>
        <div>
          <strong>{user.display_name}</strong>
          <span>{user.role === 'admin' ? '管理员' : user.role === 'reviewer' ? '合规审核人' : '业务申请人'}</span>
        </div>
        <button type="button" className="sidebar-user-card__logout" onClick={() => { onCloseMobile(); onLogout(); }} title="退出登录">↗</button>
      </div>

      <nav className="sidebar-section" aria-label="主导航">
        <div className="sidebar-nav">
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'workbench' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('workbench'); }}>
            <span className="sidebar-nav-item-icon">⌂</span>
            <span>案件工作台</span>
          </button>
        </div>
      </nav>

      {user.role === 'admin' ? (
        <section className="sidebar-governance-entry" aria-label="管理员入口">
          <button type="button" className="sidebar-governance-entry__button" onClick={() => { onCloseMobile(); onOpenGovernance(); }}>
            <span className="sidebar-governance-entry__icon" aria-hidden="true">◒</span>
            <span>
              <strong>治理控制台</strong>
            </span>
            <span className="sidebar-governance-entry__arrow" aria-hidden="true">↗</span>
          </button>
        </section>
      ) : null}

      <div className="sidebar-section">
        <div className="sidebar-section-label">常用审查场景</div>
        <div className="sidebar-scenarios">
          {SCENARIOS.map((scenario) => (
            <button key={scenario} type="button" className="sidebar-scenario" onClick={() => { onCloseMobile(); onScenarioClick(scenario); }}>
              {scenario}
            </button>
          ))}
        </div>
      </div>

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
          ) : filtered.map((item) => (
            <button key={item.id} type="button" className={'history-item history-item--button' + (item.id === activeCaseId ? ' is-active' : '')} onClick={() => { onCloseMobile(); onOpenCase(item.id); }}>
              <div className="history-item__top">
                <span className={RISK_DOT_CLASS[item.response && 'review_result' in item.response ? item.response.review_result.risk_level : 'insufficient_evidence']} aria-hidden="true" />
                <span className="history-item__question">{truncate(item.question, 32)}</span>
              </div>
              <div className="history-item__meta">
                <span>{STATUS_LABELS[item.status]}</span>
                <span>{relativeTime(item.savedAt)}</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </aside>
  );
}
