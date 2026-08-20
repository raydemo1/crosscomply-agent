import { useEffect, useMemo, useState } from 'react';
import { BookOpen, ChevronDown, ChevronRight, ClipboardCheck, Database, FilePlus2, Files, LayoutTemplate, LogOut, Search, Scale, ShieldCheck, X } from 'lucide-react';
import type { WorkbenchUser } from '../types/api';
import type { SavedCase } from '../types/case';
import { relativeTime, truncate } from '../utils/display';
import { CASE_STATUS_LABELS } from '../utils/workflow';

/**
 * Top-level shell destinations. Legal sources and internal policies share one
 * governance surface, with the library kind carried by the child destination.
 */
export type Page =
  | 'workbench'
  | 'governance'
  | 'case-detail'
  | 'case-templates'
  | 'my-remediations'
  | 'remediation-plan'
  | 'knowledge-legal'
  | 'knowledge-policy';

interface SidebarProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
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

function summarizeCaseTitle(question: string): string {
  const normalized = question.replace(/\s+/g, ' ').trim();
  const cleaned = normalized.replace(/^(?:例如|请判断|请问)[：:\s]*/u, '');
  const boundary = cleaned.search(/[?？;；。！!]/u);
  const candidate = boundary > 0 ? cleaned.slice(0, boundary) : cleaned;
  return truncate(candidate || normalized, 40);
}

interface CasePreviewPosition {
  item: SavedCase;
  top: number;
  left: number;
}

export default function Sidebar({
  currentPage,
  onPageChange,
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
  const [searchOpen, setSearchOpen] = useState(false);
  const [showAllCases, setShowAllCases] = useState(false);
  const [casePreview, setCasePreview] = useState<CasePreviewPosition | null>(null);
  const createPagesActive = currentPage === 'workbench' || currentPage === 'case-templates';
  const [createMenuOpen, setCreateMenuOpen] = useState(createPagesActive);
  const knowledgePagesActive = currentPage === 'knowledge-legal' || currentPage === 'knowledge-policy';
  const [knowledgeMenuOpen, setKnowledgeMenuOpen] = useState(knowledgePagesActive);

  useEffect(() => {
    if (createPagesActive) setCreateMenuOpen(true);
  }, [createPagesActive]);

  useEffect(() => {
    if (knowledgePagesActive) setKnowledgeMenuOpen(true);
  }, [knowledgePagesActive]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return cases;
    return cases.filter((item) => item.question.toLowerCase().includes(needle) || item.id.toLowerCase().includes(needle));
  }, [cases, query]);
  const visibleCases = query.trim() || showAllCases ? filtered : filtered.slice(0, 5);

  const showCasePreview = (item: SavedCase, element: HTMLElement): void => {
    const rect = element.getBoundingClientRect();
    setCasePreview({
      item,
      top: Math.max(12, Math.min(rect.top, window.innerHeight - 148)),
      left: rect.right + 12,
    });
  };

  const clearCasePreview = (caseId: string): void => {
    setCasePreview((current) => current?.item.id === caseId ? null : current);
  };

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
          <button type="button" className={'sidebar-nav-item sidebar-nav-item--group' + (createPagesActive ? ' is-active' : '')} aria-expanded={createMenuOpen} onClick={() => { setCreateMenuOpen((open) => !open); if (!createPagesActive) onPageChange('workbench'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><Files size={18} strokeWidth={1.8} /></span>
            <span>新建案件</span>
            <span className="sidebar-nav-item-chevron" aria-hidden="true">{createMenuOpen ? <ChevronDown size={15} strokeWidth={1.8} /> : <ChevronRight size={15} strokeWidth={1.8} />}</span>
          </button>
          {createMenuOpen ? (
            <div className="sidebar-nav-submenu">
              <button type="button" className={'sidebar-nav-subitem' + (currentPage === 'workbench' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('workbench'); }}>
                <span className="sidebar-nav-subitem-icon" aria-hidden="true"><FilePlus2 size={16} strokeWidth={1.8} /></span>
                <span>新建案件</span>
              </button>
              <button type="button" className={'sidebar-nav-subitem' + (currentPage === 'case-templates' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('case-templates'); }}>
                <span className="sidebar-nav-subitem-icon" aria-hidden="true"><LayoutTemplate size={16} strokeWidth={1.8} /></span>
                <span>使用模板</span>
              </button>
            </div>
          ) : null}
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'my-remediations' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('my-remediations'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><ClipboardCheck size={18} strokeWidth={1.8} /></span>
            <span>我的整改</span>
          </button>
          {user.role === 'admin' ? (
            <>
              <button type="button" className={'sidebar-nav-item sidebar-nav-item--group' + (knowledgePagesActive ? ' is-active' : '')} aria-expanded={knowledgeMenuOpen} onClick={() => { setKnowledgeMenuOpen((open) => !open); if (!knowledgePagesActive) onPageChange('knowledge-legal'); }}>
                <span className="sidebar-nav-item-icon" aria-hidden="true"><Database size={18} strokeWidth={1.8} /></span>
                <span>知识库</span>
                <span className="sidebar-nav-item-chevron" aria-hidden="true">{knowledgeMenuOpen ? <ChevronDown size={15} strokeWidth={1.8} /> : <ChevronRight size={15} strokeWidth={1.8} />}</span>
              </button>
              {knowledgeMenuOpen ? (
                <div className="sidebar-nav-submenu">
                  <button type="button" className={'sidebar-nav-subitem' + (currentPage === 'knowledge-legal' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('knowledge-legal'); }}>
                    <span className="sidebar-nav-subitem-icon" aria-hidden="true"><Scale size={16} strokeWidth={1.8} /></span>
                    <span>法律法规</span>
                  </button>
                  <button type="button" className={'sidebar-nav-subitem' + (currentPage === 'knowledge-policy' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onPageChange('knowledge-policy'); }}>
                    <span className="sidebar-nav-subitem-icon" aria-hidden="true"><BookOpen size={16} strokeWidth={1.8} /></span>
                    <span>规章制度</span>
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
          {user.role === 'admin' ? (
            <button type="button" className={'sidebar-nav-item' + (currentPage === 'governance' ? ' is-active' : '')} onClick={() => { onCloseMobile(); onOpenGovernance(); }}>
              <span className="sidebar-nav-item-icon" aria-hidden="true"><ShieldCheck size={18} strokeWidth={1.8} /></span>
              <span>用户管理</span>
            </button>
          ) : null}
        </div>
      </nav>

      <div className="sidebar-section sidebar-history">
        <div className="sidebar-section-label sidebar-history__header">
          <span>最近案件 <span className="sidebar-history__count">{cases.length}</span></span>
          {cases.length > 0 ? (
            <button type="button" className={'sidebar-history__search-trigger' + (searchOpen ? ' is-active' : '')} onClick={() => { setSearchOpen((open) => !open); if (searchOpen) setQuery(''); }} aria-label={searchOpen ? '收起案件搜索' : '搜索案件'} aria-expanded={searchOpen}>
              <Search size={16} strokeWidth={1.8} aria-hidden="true" />
            </button>
          ) : null}
        </div>
        {cases.length > 0 && searchOpen ? (
          <input type="search" className="sidebar-history__search" placeholder="搜索案件或编号" value={query} onChange={(event) => setQuery(event.target.value)} />
        ) : null}
        <div className="sidebar-history__list">
          {filtered.length === 0 ? (
            <div className="sidebar-history__empty">暂无匹配案件。</div>
          ) : visibleCases.map((item) => {
            const risk = riskDisplay(item);
            return (
              <button
                key={item.id}
                type="button"
                className={'history-item history-item--button' + (item.id === activeCaseId ? ' is-active' : '')}
                aria-label={item.question}
                aria-describedby={casePreview?.item.id === item.id ? 'recent-case-tooltip' : undefined}
                onMouseEnter={(event) => showCasePreview(item, event.currentTarget)}
                onMouseLeave={() => clearCasePreview(item.id)}
                onFocus={(event) => showCasePreview(item, event.currentTarget)}
                onBlur={() => clearCasePreview(item.id)}
                onClick={() => { onCloseMobile(); onOpenCase(item.id); }}
              >
                <div className="history-item__top">
                  <span className="history-item__question">{summarizeCaseTitle(item.question)}</span>
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
        {casePreview ? (
          <div
            id="recent-case-tooltip"
            className="history-hover-card"
            role="tooltip"
            style={{ top: casePreview.top, left: casePreview.left }}
          >
            <strong>{casePreview.item.question}</strong>
            <span>{riskDisplay(casePreview.item).label} · {CASE_STATUS_LABELS[casePreview.item.status]} · {relativeTime(casePreview.item.savedAt)}</span>
          </div>
        ) : null}
        {!query.trim() && cases.length > 5 ? (
          <button type="button" className="sidebar-history__toggle" onClick={() => setShowAllCases((shown) => !shown)}>
            {showAllCases ? '收起案件' : `展开全部（${cases.length}）`}
          </button>
        ) : null}
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
