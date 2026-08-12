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
          </div>
        </div>
        <div className="governance-topbar__actions">
          <button type="button" className="governance-topbar__back" onClick={onBack}>返回案件工作台</button>
          <button type="button" className="governance-topbar__logout" onClick={onLogout}>退出登录</button>
        </div>
      </header>

      <main className="governance-main">
        <section className="governance-intro" aria-labelledby="governance-title">
          <div>
            <h1 id="governance-title">评测与知识治理</h1>
          </div>
        </section>

        <EvalPage />
      </main>
    </div>
  );
}
