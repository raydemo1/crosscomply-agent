import { useEffect, useState } from 'react';
import { assignManagedUserRole, createManagedUser, listManagedUsers, resetManagedUserPassword, setManagedUserState } from '../api/client';
import type { ManagedUserApi, UserRole, WorkbenchUser } from '../types/api';

interface GovernanceConsolePageProps {
  user: WorkbenchUser;
}

export default function GovernanceConsolePage({ user }: GovernanceConsolePageProps): JSX.Element {
  const [users, setUsers] = useState<ManagedUserApi[]>([]);
  const [form, setForm] = useState({ username: '', display_name: '', password: '', role: 'requester' as UserRole });
  const [resetFor, setResetFor] = useState<string | null>(null);
  const [resetPassword, setResetPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = async (): Promise<void> => {
    const result = await listManagedUsers();
    setUsers(result.items);
  };

  useEffect(() => {
    void refresh().catch((reason) => setMessage(reason instanceof Error ? reason.message : '无法加载用户列表'));
  }, []);

  const createUser = async (): Promise<void> => {
    setBusy(true);
    setMessage(null);
    try {
      await createManagedUser(form);
      setForm({ username: '', display_name: '', password: '', role: 'requester' });
      await refresh();
      setMessage('用户已创建。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '创建用户失败');
    } finally {
      setBusy(false);
    }
  };

  const mutate = async (operation: () => Promise<ManagedUserApi>, success: string): Promise<void> => {
    setBusy(true);
    setMessage(null);
    try {
      await operation();
      await refresh();
      setMessage(success);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '用户操作失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="governance-main">
      <section className="governance-intro" aria-labelledby="governance-title">
        <div>
          <h1 id="governance-title" className="page-title">用户管理</h1>
        </div>
      </section>

      <section className="card user-admin" aria-labelledby="user-admin-title">
        <div className="user-admin__heading"><div><h2 id="user-admin-title">企业用户管理</h2></div><strong>{users.length} 个账户</strong></div>
        <div className="user-admin__create">
          <label className="form-field"><span>登录名</span><input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} placeholder="buyer@example.com" /></label>
          <label className="form-field"><span>显示名称</span><input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} placeholder="采购申请人" /></label>
          <label className="form-field"><span>初始密码</span><input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} placeholder="至少 12 位" /></label>
          <label className="form-field"><span>角色</span><select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as UserRole })}><option value="requester">申请人</option><option value="reviewer">审核人</option><option value="admin">管理员</option></select></label>
          <button type="button" className="btn-primary" disabled={busy || !form.username.trim() || !form.display_name.trim() || form.password.length < 12} onClick={() => void createUser()}>创建用户</button>
        </div>
        {message ? <div className="user-admin__message" role="status">{message}</div> : null}
        <div className="user-admin__list">
          {users.length === 0 ? <div className="state-block__hint">尚未创建企业用户。</div> : users.map((item) => (
            <div className="user-admin__row" key={item.id}>
              <div className="user-admin__identity"><span className={item.active ? 'is-active' : ''} aria-hidden="true" /><div><strong>{item.display_name}</strong><small>{item.username}</small></div></div>
              <select aria-label={`${item.display_name}的角色`} value={item.role} disabled={busy} onChange={(event) => void mutate(() => assignManagedUserRole(item.id, event.target.value as UserRole), '角色已更新。')}><option value="requester">申请人</option><option value="reviewer">审核人</option><option value="admin">管理员</option></select>
              <button type="button" className="case-header__action-btn" disabled={busy} onClick={() => void mutate(() => setManagedUserState(item.id, !item.active), item.active ? '账户已停用。' : '账户已启用。')}>{item.active ? '停用' : '启用'}</button>
              <button type="button" className="case-header__action-btn" disabled={busy} onClick={() => { setResetFor(resetFor === item.id ? null : item.id); setResetPassword(''); }}>重置密码</button>
              {resetFor === item.id ? <div className="user-admin__reset"><input type="password" value={resetPassword} onChange={(event) => setResetPassword(event.target.value)} placeholder="输入至少 12 位新密码" /><button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={busy || resetPassword.length < 12} onClick={() => void mutate(async () => { const updated = await resetManagedUserPassword(item.id, resetPassword); setResetFor(null); setResetPassword(''); return updated; }, '密码已重置，原会话已失效。')}>确认重置</button></div> : null}
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}
