import { useState } from 'react';
import '../styles/login.css';
import type { FormEvent } from 'react';

interface LoginPageProps {
  onLogin: (username: string, password: string) => Promise<void>;
  error: string | null;
}

export default function LoginPage({ onLogin, error }: LoginPageProps): JSX.Element {
  const [username, setUsername] = useState('');
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
      <div className="login-brand-panel">
        <div className="login-brand"><img src="/crosscomply-logo.svg" alt="" className="login-brand__mark" /><strong>CrossComply</strong></div>
        <h1>让合规判断<br /><span>有据可依</span></h1>
        <span className="login-brand-panel__caption">跨境数据合规系统</span>
      </div>
      <section className="login-card" aria-labelledby="login-title">
        <h2 id="login-title">登录</h2>
        <form onSubmit={submit} className="login-form">
          <label className="form-field"><span>登录账号</span><input type="text" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="输入登录账号" autoComplete="username" autoFocus required /></label>
          <label className="form-field"><span>访问密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="输入账户密码" autoComplete="current-password" required /></label>
          {error ? <div className="form-error" role="alert" aria-live="assertive">{error}</div> : null}
          <button type="submit" className="btn-primary login-form__submit" disabled={loading}>{loading ? '正在验证…' : '进入系统 →'}</button>
        </form>
        <div className="login-card__note">系统仅提供合规决策辅助，不替代专业法律意见。</div>
      </section>
    </main>
  );
}
