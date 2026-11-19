import type { ReactNode } from 'react';

interface ReportHeadingProps {
  title: string;
  count?: string;
  action?: ReactNode;
  /** 章节序号（一、二、三…）；不传时用朱砂方点。 */
  index?: string;
}

export default function ReportHeading({ title, count, action, index }: ReportHeadingProps) {
  return <div className={'report-heading' + (index ? ' report-heading--numbered' : '')}>
    <h2>{index ? <span className="report-heading__index" aria-hidden="true">{index}、</span> : null}{title}</h2>
    {count || action ? <div className="report-heading__meta">{count ? <span className="report-heading__count">{count}</span> : null}{action}</div> : null}
  </div>;
}
