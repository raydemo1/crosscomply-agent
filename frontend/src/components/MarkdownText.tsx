import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";

interface MarkdownTextProps {
  children: string;
  variant?: "report" | "inline" | "note";
  className?: string;
  onCitationClick?: (citationRef: string) => void;
}

export default function MarkdownText({
  children,
  variant = "report",
  className,
  onCitationClick,
}: MarkdownTextProps) {
  return (
    <div className={`markdown-body markdown-body--${variant} ${className || ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw]}
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
