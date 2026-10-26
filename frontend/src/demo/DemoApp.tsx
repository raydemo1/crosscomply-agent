import { useEffect, useState } from 'react';
import { Link, Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom';
import { ArrowUpRight, BookOpen, Check, ChevronRight, Menu, Moon, Sun, X } from 'lucide-react';
import CaseDetailPage from '../components/CaseDetailPage';
import RiskBadge from '../components/RiskBadge';
import { exampleCases, scenarios } from './examples';
import './demo.css';

export default function DemoApp() {
  const [menu, setMenu] = useState(false);
  const [dark, setDark] = useState(() => localStorage.getItem('crosscomply-demo-theme') === 'dark');
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    localStorage.setItem('crosscomply-demo-theme', dark ? 'dark' : 'light');
    document.title = 'CrossComply · 示例演示';
  }, [dark]);
  return <div className="demo-shell">
    <aside className={`demo-sidebar${menu ? ' is-open' : ''}`} aria-label="示例导航">
      <Link className="demo-brand" to="/" onClick={() => setMenu(false)}><img src="/crosscomply-logo.svg" alt="" /><span><strong>CrossComply</strong><small>跨境数据合规工作台</small></span></Link>
      <Link className="demo-overview-link" to="/" onClick={() => setMenu(false)}><BookOpen size={17} />示例工作台</Link>
      <div className="demo-nav-label">探索示例案件</div>
      <nav>{scenarios.map((item, index) => <Link key={item.id} to={`/cases/${item.id}`} onClick={() => setMenu(false)}><span className="demo-nav-index">0{index + 1}</span><span>{item.title}<small>{item.category}</small></span><ChevronRight size={14} /></Link>)}</nav>
      <div className="demo-sidebar-footer"><span className="demo-badge">示例演示</span><p>虚构业务 · 预编写内容<br />无需登录，即可探索。</p><button type="button" onClick={() => setDark((value) => !value)}>{dark ? <Sun size={16} /> : <Moon size={16} />}{dark ? '浅色模式' : '深色模式'}</button></div>
    </aside>
    {menu ? <button type="button" className="demo-scrim" aria-label="关闭示例导航" onClick={() => setMenu(false)} /> : null}
    <main className="demo-main">
      <div className="demo-mobile-bar"><button type="button" aria-label={menu ? '关闭示例导航' : '打开示例导航'} aria-expanded={menu} onClick={() => setMenu(!menu)}>{menu ? <X size={21} /> : <Menu size={21} />}</button><span>CrossComply</span><span className="demo-badge">示例演示</span></div>
      <div className="demo-notice" role="note"><span className="demo-badge">示例演示</span><p>案例、材料与回答均为预编写示例，仅展示产品流程，不提供真实审查。</p></div>
      <Routes>
        <Route path="/" element={<DemoOverview />} />
        <Route path="/cases/:caseId" element={<DemoCase />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </main>
  </div>;
}

function DemoOverview() {
  return <div className="demo-overview">
    <header className="demo-hero"><p className="demo-eyebrow">CROSSCOMPLY / PRODUCT EXPERIENCE</p><h1>从业务事实，<br />读懂合规判断。</h1><p>在三个虚构场景中，体验有依据、有前提、可追问的审查报告。</p><Link className="demo-primary" to={`/cases/${scenarios[0].id}`}>打开一份示例报告<ArrowUpRight size={18} /></Link></header>
    <section className="demo-examples" aria-labelledby="demo-examples-heading"><div className="demo-section-head"><h2 id="demo-examples-heading">选择一个场景开始</h2><span>03 / 示例案件</span></div>
      {scenarios.map((item, index) => <Link className="demo-example" key={item.id} to={`/cases/${item.id}`}><span className="demo-example-number">0{index + 1}</span><div><div className="demo-example-title"><h3>{item.title}</h3><RiskBadge level={item.risk} /></div><p>{item.description}</p><span className="demo-example-question">{item.question}</span></div><ArrowUpRight size={21} className="demo-example-arrow" /></Link>)}
    </section>
    <section className="demo-capabilities" aria-label="可以体验的功能">{['分层阅读审查结论', '查看事实与原文', '点击引用核对法条', '划词体验流式追问'].map((text) => <span key={text}><Check size={15} />{text}</span>)}</section>
    <p className="demo-footnote">示例中的名称与业务均为虚构。演示不接收材料上传，不连接真实后端或模型 API。</p>
  </div>;
}

function DemoCase() {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const saved = exampleCases.find((item) => item.id === caseId);
  if (!saved) return <Navigate to="/" replace />;
  return <CaseDetailPage key={saved.id} saved={saved} canEdit={false} canManageActions={false} viewerRole="reviewer" onEdit={() => navigate('/')} onRerun={() => navigate('/')} onBack={() => navigate('/')} />;
}
