import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import '../styles/case-center.css';

export default function CaseCenterHeader({ action }: { action?: ReactNode }): JSX.Element {
  return <header className="case-center__heading">
    <h1 className="page-title">案件中心</h1>
    <div className="case-center__navigation">
      <nav aria-label="案件中心视图">
        <NavLink to="/cases" end>案件列表</NavLink>
        <NavLink to="/cases/business">出境业务</NavLink>
      </nav>
      {action ? <div className="case-center__navigation-action">{action}</div> : null}
    </div>
  </header>;
}
