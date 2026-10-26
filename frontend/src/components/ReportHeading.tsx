import type { ReactNode } from 'react';

interface ReportHeadingProps {
  chapterNo?: string;
  title: string;
  count?: string;
  action?: ReactNode;
}

export default function ReportHeading({ chapterNo, title, count, action }: ReportHeadingProps) {
  return <div className="report-heading">
    <h2>{chapterNo ? <span className="report-chapter-no">{chapterNo}</span> : null}{title}</h2>
    {count || action ? <div className="report-heading__meta">{count ? <span className="report-heading__count">{count}</span> : null}{action}</div> : null}
  </div>;
}
