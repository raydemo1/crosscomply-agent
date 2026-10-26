import { createElement, type ComponentPropsWithoutRef } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

function ExportTable({ children }: ComponentPropsWithoutRef<'table'>) {
  return createElement('div', { className: 'review-table-scroll', role: 'region', 'aria-label': '报告表格，可横向滚动', tabIndex: 0 }, createElement('table', null, children));
}

export function renderReportMarkdown(value: string): string {
  const markdown = value.replace(
    /<sup\b[^<>]*\bdata-citation-ref="([\p{L}\p{N}_.:-]+)"[^<>]*>([^<>]+)<\/sup>/gu,
    (_, ref: string, label: string) => `[${label.replace(/[\[\]\\]/gu, '\\$&')}](#${encodeURIComponent(ref)})`,
  );
  return renderToStaticMarkup(createElement(ReactMarkdown, {
    remarkPlugins: [remarkGfm], skipHtml: true, children: markdown, components: { table: ExportTable },
  }));
}
