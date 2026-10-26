import { createContext, useContext, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";

const CitationClickContext = createContext<((citationRef: string) => void) | undefined>(undefined);

function CitationSup({ node: _node, children, ...props }: ComponentPropsWithoutRef<'sup'> & { node?: unknown; 'data-citation-ref'?: string }) {
  const onCitationClick = useContext(CitationClickContext);
  const citationRef = props['data-citation-ref'];
  if (!citationRef || !onCitationClick) return <sup {...props}>{children}</sup>;
  return <button type="button" className="cite-marker" data-citation-ref={citationRef}
    aria-label={`查看引用 ${citationRef}`} title={`查看 ${citationRef}`}
    onClick={() => onCitationClick(citationRef)}>{children}</button>;
}

function ReportTable({ node: _node, children, ...props }: ComponentPropsWithoutRef<'table'> & { node?: unknown }) {
  return <div className="report-table"><span className="report-table__hint" aria-hidden="true">可左右滑动查看完整表格</span><div className="report-table__scroll" role="region" aria-label="报告表格，可横向滚动" tabIndex={0}><table {...props}>{children}</table></div></div>;
}

const markdownComponents = { sup: CitationSup };

interface MarkdownTextProps {
  children: string;
  variant?: "report" | "inline" | "note";
  className?: string;
  /**
   * 是否把 Markdown 中的原始 HTML 渲染成活 DOM，默认 true：审查报告正文由
   * `result_builder.py` 生成，其中的 `<sup data-citation-ref>` 引用标记必须按
   * HTML 解析。渲染不可信的材料原文（如申请人上传的 `.md`）时必须显式传
   * `false`，避免原始 HTML 进入渲染链路生成活 DOM。
   */
  allowRawHtml?: boolean;
  onCitationClick?: (citationRef: string) => void;
}

export default function MarkdownText({
  children,
  variant = "report",
  className,
  allowRawHtml = true,
  onCitationClick,
}: MarkdownTextProps) {
  return (
    <div className={`markdown-body markdown-body--${variant} ${className || ""}`}>
      <CitationClickContext.Provider value={onCitationClick}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={allowRawHtml ? [rehypeRaw] : []}
        components={variant === 'report' ? { ...markdownComponents, table: ReportTable } : markdownComponents}
      >
        {children}
      </ReactMarkdown>
      </CitationClickContext.Provider>
    </div>
  );
}
