import type { ReactNode } from 'react';

interface ReportHeadingProps {
  title: string;
  count?: string;
  action?: ReactNode;
}

export default function ReportHeading({ title, count, action }: ReportHeadingProps) {
  return <div className="report-heading">
    <h2>{title}</h2>
    {count || action ? <div className="report-heading__meta">{count ? <span className="report-heading__count">{count}</span> : null}{action}</div> : null}
  </div>;
}
