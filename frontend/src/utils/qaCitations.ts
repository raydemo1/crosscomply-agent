import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import type { QACitation } from '../types/qa';

export interface CitationGroup {
  key: string;
  number: number;
  source: QACitation;
  citations: QACitation[];
}

export function groupQACitations(citations: QACitation[]): CitationGroup[] {
  const groups = new Map<string, CitationGroup>();
  for (const citation of citations) {
    const key = JSON.stringify([citation.source_id || citation.url || citation.title,
      citation.publish_date ?? null, citation.effective_date ?? null, citation.kind]);
    let group = groups.get(key);
    if (!group) {
      group = { key, number: groups.size + 1, source: citation, citations: [] };
      groups.set(key, group);
    }
    group.citations.push(citation);
  }
  return [...groups.values()];
}

export function citationLocation(citation: QACitation): string {
  return citation.article_no || citation.heading_path?.filter(Boolean).join(' / ') || '原文片段';
}

export function citationURL(url: string | null): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
}

const citationMarkdown = unified().use(remarkParse).use(remarkGfm);
const literalNodes = new Set(['code', 'inlineCode', 'link', 'linkReference', 'html']);

function* citationMatches(text: string, allowed: Set<string>) {
  for (const match of text.matchAll(/\[依据\d+\](?:[ \t]*\[依据\d+\])*/g)) {
    const refs = [...new Set([...match[0].matchAll(/\[(依据\d+)\]/g)].map((ref) => ref[1]))];
    if (refs.every((ref) => allowed.has(ref))) yield { match, refs };
  }
}

export function citedQACitationGroups(answer: string, groups: CitationGroup[]): CitationGroup[] {
  const allowed = new Set(groups.flatMap((group) => group.citations.map((citation) => citation.ref)));
  const cited = new Set<string>();
  function visit(node: MarkdownNode): void {
    if (literalNodes.has(node.type)) return;
    if (node.type === 'text' && node.value) {
      for (const { refs } of citationMatches(node.value, allowed)) refs.forEach((ref) => cited.add(ref));
    }
    node.children?.forEach(visit);
  }
  visit(citationMarkdown.parse(answer));
  return groups.map((group) => ({ ...group, citations: group.citations.filter((citation) => cited.has(citation.ref)) }))
    .filter((group) => group.citations.length > 0);
}

export function remarkQACitations(options: { refs: string[] }) {
  const allowed = new Set(options.refs);
  function transform(node: MarkdownNode): void {
    if (!node.children || literalNodes.has(node.type)) return;
    node.children = node.children.flatMap((child): MarkdownNode[] => {
      if (child.type !== 'text' || !child.value) {
        transform(child);
        return [child];
      }
      const text = child.value;
      const nodes: MarkdownNode[] = [];
      let offset = 0;
      for (const { match, refs } of citationMatches(text, allowed)) {
        if (match.index > offset) nodes.push({ type: 'text', value: text.slice(offset, match.index) });
        nodes.push({ type: 'link', url: '#qa-cite=' + refs.map((ref) => ref.slice(2)).join(','),
          children: [{ type: 'text', value: refs.join('、') }] });
        offset = match.index + match[0].length;
      }
      if (offset < text.length) nodes.push({ type: 'text', value: text.slice(offset) });
      return nodes;
    });
  }
  return transform;
}
