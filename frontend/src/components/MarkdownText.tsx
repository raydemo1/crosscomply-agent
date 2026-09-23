import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";

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
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={allowRawHtml ? [rehypeRaw] : []}
        components={{
          sup: ({ node, children: supChildren, ...props }) => {
            const rawProps = props as typeof props & Record<string, unknown>;
            const citationRef =
              typeof rawProps["data-citation-ref"] === "string"
                ? rawProps["data-citation-ref"]
                : "";
            if (!citationRef || !onCitationClick) {
              return <sup {...props}>{supChildren}</sup>;
            }
            return (
              <button
                type="button"
                className="cite-marker"
                data-citation-ref={citationRef}
                aria-label={`查看引用 ${citationRef}`}
                title={`查看 ${citationRef}`}
                onClick={() => onCitationClick(citationRef)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onCitationClick(citationRef);
                  }
                }}
              >
                {supChildren}
              </button>
            );
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
