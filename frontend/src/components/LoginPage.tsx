import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import '../styles/login.css';
import type { FormEvent } from 'react';

interface LoginPageProps {
  onLogin: (username: string, password: string) => Promise<void>;
  error: string | null;
}

const REPORT_VIEWS = [
  { id: 'facts', ordinal: '一', label: '业务事实', heading: '业务材料', items: [{ label: '业务场景', value: '跨境客服工单系统' }, { label: '涉及数据', value: '客户姓名、手机号' }, { label: '传输方式', value: 'API 调用境外服务器' }] },
  { id: 'sources', ordinal: '二', label: '法律依据', heading: '引用条文', items: [{ label: '出境条件', value: '《个人信息保护法》第三十八条' }, { label: '告知同意', value: '《个人信息保护法》第三十九条' }, { label: '原文定位', value: '业务材料第 2 段' }] },
  { id: 'questions', ordinal: '三', label: '待核实事项', heading: '事实缺口', items: [{ label: '出境规模', value: '年度出境人数（决定申报路径）' }, { label: '补充材料', value: '境外接收方合同' }, { label: '人工复核', value: '审核人确认后生效' }] },
] as const;

export default function LoginPage({ onLogin, error }: LoginPageProps): JSX.Element {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [showRecovery, setShowRecovery] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [reportView, setReportView] = useState(0);
  const activeReport = REPORT_VIEWS[reportView];

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
      <section className="login-access" aria-labelledby="login-title">
        <div className="login-brand">
          <img src="/crosscomply-logo.svg" alt="" className="login-brand__mark" />
          <strong>CrossComply</strong>
        </div>
        <div className="login-card">
          <h1 id="login-title">登录 CrossComply</h1>
          <form onSubmit={submit} className="login-form">
            <label className="form-field"><span>账号</span><input type="text" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="请输入邮箱或账号" autoComplete="username" autoFocus required disabled={loading} /></label>
            <div className="login-password">
              <div className="login-password__heading"><label htmlFor="login-password">密码</label><button type="button" className="login-recovery" aria-expanded={showRecovery} aria-controls="login-recovery" onClick={() => setShowRecovery((value) => !value)}>忘记密码？</button></div>
              <div className="login-password__input"><input id="login-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="请输入密码" autoComplete="current-password" required disabled={loading} /><button type="button" className="login-password__toggle" aria-label={showPassword ? '隐藏密码' : '显示密码'} aria-pressed={showPassword} aria-controls="login-password" title={showPassword ? '隐藏密码' : '显示密码'} disabled={loading} onClick={() => setShowPassword((value) => !value)}>{showPassword ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}</button></div>
            </div>
            {showRecovery ? <p id="login-recovery" className="login-recovery__help" role="status">请联系企业管理员重置密码，再使用新密码登录。</p> : null}
            {error ? <div className="form-error" role="alert" aria-live="assertive">{error}</div> : null}
            <button type="submit" className="login-form__submit" disabled={loading}>{loading ? '正在登录…' : '登录'}</button>
          </form>
        </div>
        <p className="login-access__note">本系统提供合规审查辅助，不替代正式法律意见。</p>
      </section>
      <aside className="login-showcase" aria-labelledby="login-value">
        <div className="login-showcase__inner">
          <div className="login-showcase__copy">
            <h2 id="login-value">让合规判断<br />有据可依</h2>
          </div>
          <div className="login-dossier">
          <span className="login-dossier__sheet login-dossier__sheet--far" aria-hidden="true" />
          <span className="login-dossier__sheet login-dossier__sheet--near" aria-hidden="true" />
          <div className="login-report">
            <div className="login-report__heading"><span>合规审查报告</span></div>
            <div className="login-report__workspace">
              <div className="login-report__flow" role="group" aria-label="审查报告内容">
                {REPORT_VIEWS.map((view, index) => <button type="button" key={view.id} className={index === reportView ? 'is-active' : ''} aria-pressed={index === reportView} aria-controls="login-report-preview" onClick={() => setReportView(index)}><span className="login-report__ordinal" aria-hidden="true">{view.ordinal}、</span><span>{view.label}</span></button>)}
              </div>
              <div className="login-report__paper" id="login-report-preview" aria-live="polite">
                <div className="login-report__paper-content" key={activeReport.id}>
                  <div className="login-report__paper-heading"><span>{activeReport.heading}</span></div>
                  <div className="login-report__entries">{activeReport.items.map((item) => <div className="login-report__entry" key={item.label}><span>{item.label}</span><span className="login-report__leader" aria-hidden="true" /><span className="login-report__entry-value">{item.value}</span></div>)}</div>
                </div>
              </div>
            </div>
          </div>
          </div>
        </div>
      </aside>
    </main>
  );
}
