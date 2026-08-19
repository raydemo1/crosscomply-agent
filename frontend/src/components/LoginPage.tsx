import { useState } from 'react';
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
      <div className="login-ornament login-ornament--one" />
      <div className="login-ornament login-ornament--two" />
      <section className="login-card">
        <div className="login-brand"><img src="/crosscomply-logo.svg" alt="" className="login-brand__mark" /><div><strong><span>Cross</span><em>Comply</em></strong><small>跨境数据合规案件管理</small></div></div>
        <div className="login-card__copy"><h1><span className="login-word login-word--navy" tabIndex={0}>让合规判断</span><br /><span className="login-word login-word--accent" tabIndex={0}>有据可依</span></h1></div>
        <form onSubmit={submit} className="login-form">
          <label className="form-field"><span>登录账号</span><input type="text" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="例如：admin@example.com" autoComplete="username" autoFocus required /></label>
          <label className="form-field"><span>访问密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="输入账户密码" autoComplete="current-password" required /></label>
          <div className="login-form__hint">账号角色由系统权限决定，无需单独选择角色。</div>
          {error ? <div className="form-error" role="alert" aria-live="assertive">{error}</div> : null}
          <button type="submit" className="btn-primary login-form__submit" disabled={loading}>{loading ? '正在验证…' : '进入案件管理 →'}</button>
        </form>
        <div className="login-card__note">系统仅提供合规决策辅助，不替代专业法律意见。</div>
      </section>
      <aside className="login-aside" aria-label="CrossComply 产品理念">
        <div className="login-aside__mark" aria-hidden="true">“</div>
        <blockquote>好的合规工具不是替人下结论，而是让每个结论都能回到事实、来源和下一步行动。</blockquote>
      </aside>
    </main>
  );
}
