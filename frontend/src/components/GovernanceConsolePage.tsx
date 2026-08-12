import type { WorkbenchUser } from '../types/api';
import EvalPage from './EvalPage';

interface GovernanceConsolePageProps {
  user: WorkbenchUser;
  onBack: () => void;
  onLogout: () => void;
}

export default function GovernanceConsolePage({ user, onBack, onLogout }: GovernanceConsolePageProps): JSX.Element {
  return (
    <div className="governance-shell">
      <header className="governance-topbar">
        <div className="governance-topbar__brand">
          <span className="governance-topbar__mark" aria-hidden="true">CC</span>
          <div>
            <strong>CrossComply</strong>
            <span>治理控制台</span>
          </div>
        </div>
        <div className="governance-topbar__actions">
          <span className="governance-topbar__role">管理员 · 受控环境</span>
          <button type="button" className="governance-topbar__back" onClick={onBack}>返回案件工作台</button>
          <button type="button" className="governance-topbar__logout" onClick={onLogout}>退出登录</button>
        </div>
      </header>

      <main className="governance-main">
        <section className="governance-intro" aria-labelledby="governance-title">
          <div>
            <p className="governance-intro__kicker">ADMINISTRATION / EVIDENCE QUALITY</p>
            <h1 id="governance-title">评测与知识治理</h1>
            <p>集中查看检索、引用和拒答能力的离线评测结果，复盘坏例，为案件工作台维护可信的证据基础。</p>
          </div>
          <div className="governance-intro__scope">
            <span className="governance-scope-badge">仅管理员可见</span>
            <span>评测结果只读</span>
          </div>
        </section>

        <EvalPage />
      </main>
    </div>
  );
}
