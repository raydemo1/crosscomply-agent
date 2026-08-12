import { useState } from 'react';
import type { FormEvent } from 'react';
import type { WorkbenchUser } from '../types/api';

interface LoginPageProps {
  onLogin: (username: string, password: string) => Promise<void>;
  error: string | null;
}

const ACCOUNTS: Array<{ username: string; label: string; role: WorkbenchUser['role'] }> = [
  { username: 'reviewer@crosscomply.local', label: '合规审核人', role: 'reviewer' },
  { username: 'requester@crosscomply.local', label: '业务申请人', role: 'requester' },
  { username: 'admin@crosscomply.local', label: '系统管理员', role: 'admin' },
];

export default function LoginPage({ onLogin, error }: LoginPageProps): JSX.Element {
  const [username, setUsername] = useState(ACCOUNTS[0].username);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setLoading(true);
    try {
      await onLogin(username, password);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="login-shell">
      <div className="login-ornament login-ornament--one" />
      <div className="login-ornament login-ornament--two" />
      <section className="login-card">
        <div className="login-brand"><img src="/crosscomply-logo.svg" alt="" className="login-brand__mark" /><div><strong>CrossComply</strong><small>跨境数据合规案件工作台</small></div></div>
        <div className="login-card__copy"><div className="report-kicker">Governed compliance review</div><h1><span className="login-word login-word--navy" tabIndex={0}>让合规判断</span><br /><span className="login-word login-word--accent" tabIndex={0}>有据可依</span><span className="login-word login-word--period" tabIndex={0}>。</span></h1><p>从业务材料、关键事实到法规依据和整改动作，保留每一次判断的上下文。</p></div>
        <form onSubmit={submit} className="login-form">
          <label className="form-field"><span>工作台角色</span><select value={username} onChange={(event) => setUsername(event.target.value)}>{ACCOUNTS.map((account) => <option key={account.username} value={account.username}>{account.label}</option>)}</select></label>
          <label className="form-field"><span>访问密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="输入工作台密码" autoFocus required /></label>
          {error ? <div className="form-error" role="alert">{error}</div> : null}
          <button type="submit" className="btn-primary login-form__submit" disabled={loading}>{loading ? '正在验证…' : '进入工作台 →'}</button>
        </form>
        <div className="login-card__note">系统仅提供合规决策辅助，不替代专业法律意见。</div>
      </section>
      <aside className="login-aside"><div className="login-aside__quote">“</div><p>好的合规工具不是替人下结论，而是让每个结论都能回到事实、来源和下一步行动。</p><span>CrossComply / Case Workbench</span></aside>
    </main>
  );
}
