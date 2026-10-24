import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { BookOpen, ChevronDown, ChevronRight, ClipboardCheck, Database, FilePlus2, LayoutTemplate, LogOut, MessageSquare, Moon, Search, Scale, ShieldCheck, Sun, X } from 'lucide-react';
import type { QASummary } from '../types/qa';
import type { WorkbenchUser } from '../types/api';
import type { SavedCase } from '../types/case';
import { relativeTime, truncate, RISK_LABELS, RISK_HISTORY_CLASS } from '../utils/display';
import { CASE_STATUS_LABELS } from '../utils/workflow';
import { useCaseStore } from '../store/caseStore';
import { currentTheme, setTheme as persistTheme, watchSystemTheme, type Theme } from '../utils/theme';

interface SidebarProps {
  user: WorkbenchUser;
  qaHistory: QASummary[];
  onLogout: () => void | Promise<void>;
  isMobileOpen: boolean;
  onCloseMobile: () => void;
}

function riskDisplay(item: SavedCase): { label: string; className: string } {
  const level = item.riskLevel ?? (
    item.response && 'review_result' in item.response
      ? item.response.review_result.risk_level
      : null
  );
  return level
    ? { label: RISK_LABELS[level] ?? RISK_LABELS.pending, className: RISK_HISTORY_CLASS[level] ?? RISK_HISTORY_CLASS.pending }
    : { label: RISK_LABELS.pending, className: RISK_HISTORY_CLASS.pending };
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
  /** 相对侧栏：优先悬浮在列表项上方，顶部空间不足时落到下方 */
  top?: number;
  bottom?: number;
}

export default function Sidebar({
  user,
  qaHistory,
  onLogout,
  isMobileOpen,
  onCloseMobile,
}: SidebarProps): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const cases = useCaseStore();
  const activeCaseId = location.pathname.startsWith('/cases/') ? decodeURIComponent(location.pathname.slice('/cases/'.length)) : null;
  const activeQAId = location.pathname.match(/^\/qa\/(.+)$/)?.[1] ?? null;
  const currentPage = location.pathname.startsWith('/workbench') ? 'workbench'
    : location.pathname.startsWith('/templates') ? 'case-templates'
    : location.pathname.startsWith('/reviews') ? 'my-remediations'
    : location.pathname.startsWith('/knowledge/legal') ? 'knowledge-legal'
    : location.pathname.startsWith('/knowledge') ? 'knowledge-policy'
    : location.pathname.startsWith('/governance') ? 'governance'
    : 'home';
  const [query, setQuery] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const [showAllCases, setShowAllCases] = useState(false);
  const [casePreview, setCasePreview] = useState<CasePreviewPosition | null>(null);
  const previewTimer = useRef<number | null>(null);
  const knowledgePagesActive = currentPage === 'knowledge-legal' || currentPage === 'knowledge-policy';
  const [knowledgeMenuOpen, setKnowledgeMenuOpen] = useState(knowledgePagesActive);
  const [theme, setThemeState] = useState<Theme>(() => currentTheme());
  useEffect(() => { watchSystemTheme(); }, []);
  const toggleTheme = (): void => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    persistTheme(next);
    setThemeState(next);
  };

  useEffect(() => {
    if (knowledgePagesActive) setKnowledgeMenuOpen(true);
  }, [knowledgePagesActive]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return cases;
    return cases.filter((item) => item.question.toLowerCase().includes(needle) || item.id.toLowerCase().includes(needle));
  }, [cases, query]);
  const visibleCases = query.trim() || showAllCases ? filtered : filtered.slice(0, 5);

  const clearCasePreview = (): void => {
    if (previewTimer.current) {
      window.clearTimeout(previewTimer.current);
      previewTimer.current = null;
    }
    setCasePreview(null);
  };

  const showCasePreview = (item: SavedCase, element: HTMLElement): void => {
    if (previewTimer.current) window.clearTimeout(previewTimer.current);
    previewTimer.current = window.setTimeout(() => {
      const sidebar = element.closest('.app-sidebar');
      if (!sidebar) return;
      const sidebarRect = sidebar.getBoundingClientRect();
      const rect = element.getBoundingClientRect();
      const itemTop = rect.top - sidebarRect.top;
      // 优先悬浮在列表项上方；顶部空间不足则放到下方，始终留在侧栏内
      setCasePreview(itemTop > 220
        ? { item, bottom: sidebarRect.height - itemTop + 6 }
        : { item, top: itemTop + rect.height + 6 });
    }, 350);
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
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'home' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/qa'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><MessageSquare size={18} strokeWidth={1.8} /></span>
            <span>合规问答</span>
          </button>
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'workbench' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/workbench'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><FilePlus2 size={18} strokeWidth={1.8} /></span>
            <span>新建案件</span>
          </button>
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'case-templates' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/templates'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><LayoutTemplate size={18} strokeWidth={1.8} /></span>
            <span>使用模板</span>
          </button>
          <button type="button" className={'sidebar-nav-item' + (currentPage === 'my-remediations' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/reviews'); }}>
            <span className="sidebar-nav-item-icon" aria-hidden="true"><ClipboardCheck size={18} strokeWidth={1.8} /></span>
            <span>我的复核</span>
          </button>
          {user.role === 'admin' ? (
            <>
              <button type="button" className={'sidebar-nav-item sidebar-nav-item--group' + (knowledgePagesActive ? ' is-active' : '')} aria-expanded={knowledgeMenuOpen} onClick={() => { setKnowledgeMenuOpen((open) => !open); if (!knowledgePagesActive) navigate('/knowledge/legal'); }}>
                <span className="sidebar-nav-item-icon" aria-hidden="true"><Database size={18} strokeWidth={1.8} /></span>
                <span>知识库</span>
                <span className="sidebar-nav-item-chevron" aria-hidden="true">{knowledgeMenuOpen ? <ChevronDown size={15} strokeWidth={1.8} /> : <ChevronRight size={15} strokeWidth={1.8} />}</span>
              </button>
              {knowledgeMenuOpen ? (
                <div className="sidebar-nav-submenu">
                  <button type="button" className={'sidebar-nav-subitem' + (currentPage === 'knowledge-legal' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/knowledge/legal'); }}>
                    <span className="sidebar-nav-subitem-icon" aria-hidden="true"><Scale size={16} strokeWidth={1.8} /></span>
                    <span>法律法规</span>
                  </button>
                  <button type="button" className={'sidebar-nav-subitem' + (currentPage === 'knowledge-policy' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/knowledge/policy'); }}>
                    <span className="sidebar-nav-subitem-icon" aria-hidden="true"><BookOpen size={16} strokeWidth={1.8} /></span>
                    <span>规章制度</span>
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
          {user.role === 'admin' ? (
            <button type="button" className={'sidebar-nav-item' + (currentPage === 'governance' ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate('/governance'); }}>
              <span className="sidebar-nav-item-icon" aria-hidden="true"><ShieldCheck size={18} strokeWidth={1.8} /></span>
              <span>用户管理</span>
            </button>
          ) : null}
        </div>
      </nav>

      {qaHistory.length ? <div className="sidebar-section sidebar-history-group qa-sidebar-history"><div className="sidebar-section-label sidebar-history__header"><span>最近问答 <span className="sidebar-history__count">{qaHistory.length}</span></span></div><div className="sidebar-history__list">{qaHistory.slice(0, 5).map((item) => <button key={item.id} type="button" className={'history-item history-item--button' + (item.id === activeQAId ? ' is-active' : '')} onClick={() => { onCloseMobile(); navigate(`/qa/${item.id}`); }}><div className="history-item__top"><span className="history-item__question">{item.title}</span></div><div className="history-item__meta"><span>问答</span><span>{relativeTime(item.updated_at)}</span></div></button>)}</div>{qaHistory.length > 5 ? <details className="qa-history-more"><summary>查看更早问答</summary>{qaHistory.slice(5).map((item) => <button type="button" className="history-item history-item--button" key={item.id} onClick={() => { onCloseMobile(); navigate(`/qa/${item.id}`); }}>{item.title}</button>)}</details> : null}</div> : null}

      <div className="sidebar-section sidebar-history-group sidebar-history">
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
            <div className="sidebar-history__empty">{cases.length ? '暂无匹配案件。' : '暂无案件'}</div>
          ) : visibleCases.map((item) => {
            const risk = riskDisplay(item);
            return (
              <button
                key={item.id}
                type="button"
                className={'history-item history-item--button' + (item.id === activeCaseId ? ' is-active' : '')}
                aria-label={item.question}
                onMouseEnter={(event) => showCasePreview(item, event.currentTarget)}
                onMouseLeave={clearCasePreview}
                onFocus={(event) => showCasePreview(item, event.currentTarget)}
                onBlur={clearCasePreview}
                onClick={() => { clearCasePreview(); onCloseMobile(); navigate(`/cases/${item.id}`); }}
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
        {!query.trim() && cases.length > 5 ? (
          <button type="button" className="sidebar-history__toggle" onClick={() => setShowAllCases((shown) => !shown)}>
            {showAllCases ? '收起案件' : `展开全部（${cases.length}）`}
          </button>
        ) : null}
      </div>

      {casePreview ? (
        <div
          className="history-hover-card"
          role="tooltip"
          style={casePreview.top !== undefined
            ? { top: casePreview.top, left: 10, right: 10 }
            : { bottom: casePreview.bottom, left: 10, right: 10 }}
        >
          <strong>{casePreview.item.question}</strong>
          <span>{riskDisplay(casePreview.item).label} · {CASE_STATUS_LABELS[casePreview.item.status]} · {relativeTime(casePreview.item.savedAt)}</span>
        </div>
      ) : null}

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
              <span>管理员 · 用户管理</span>
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
    </aside>
  );
}
