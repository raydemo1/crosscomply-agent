import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArrowUpRight, BookOpen, ChevronRight, FileText, X } from 'lucide-react';
import type { QACitation, QAResult } from '../types/qa';
import { citationLocation, citationURL, citedQACitationGroups, groupQACitations, remarkQACitations } from '../utils/qaCitations';

interface Props {
  answer: string;
  result: QAResult | null;
  streaming: boolean;
  incomplete?: boolean;
  children?: ReactNode;
}

const KINDS = { legal: '法律依据', context: '参考资料', web: '网页线索' };

function CitationPassage({ citation, selected }: { citation: QACitation; selected: boolean }): JSX.Element {
  return <article className="qa-citation-passage" data-selected={selected}>
    <h4>{citationLocation(citation)}</h4>
    {citation.available_at_answer_date === false ? <p>非回答时点（{citation.answer_as_of_date}）有效依据，仅供历史或版本比较。</p> : null}
    <blockquote>{citation.excerpt || '当前来源未提供正文摘录。'}</blockquote>
  </article>;
}

export default function QAAnswer({ answer, result, streaming, incomplete = false, children }: Props): JSX.Element {
  const citations = result?.citations;
  const groups = useMemo(() => groupQACitations(citations ?? []), [citations]);
  const citedGroups = useMemo(() => citedQACitationGroups(answer, groups), [answer, groups]);
  const [selectedRefs, setSelectedRefs] = useState<string[]>([]);
  const dialog = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  const activeGroups = citedGroups.filter((group) => group.citations.some((citation) => selectedRefs.includes(citation.ref)));
  const components = useMemo<Components>(() => ({ a({ href, children }) {
    if (href && /^#qa-cite=\d+(?:,\d+)*$/.test(href)) {
      const refs = href.slice('#qa-cite='.length).split(',').map((number) => `依据${number}`);
      const sources = groups.filter((group) => group.citations.some((citation) => refs.includes(citation.ref)));
      if (sources.length && refs.every((ref) => sources.some((group) => group.citations.some((citation) => citation.ref === ref)))) {
        return <span className="qa-citation-group" role="group" aria-label="引用来源">
          {sources.map((group) => {
            const passages = group.citations.filter((citation) => refs.includes(citation.ref));
            const label = `${group.source.title} · ${passages.map(citationLocation).join('、')}`;
            return <button key={group.key} type="button" className="qa-citation-chip" title={label}
              aria-label={`查看引用：${label}`} aria-haspopup="dialog" onClick={() => setSelectedRefs(passages.map((citation) => citation.ref))}>
              {group.number}
            </button>;
          })}
        </span>;
      }
    }
    return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
  } }), [groups]);
  const materials = new Map<string, QAResult['material_citations']>();
  for (const citation of result?.material_citations ?? []) {
    const entries = materials.get(citation.material_version_id) ?? [];
    entries.push(citation);
    materials.set(citation.material_version_id, entries);
  }

  useEffect(() => {
    if (!selectedRefs.length || !dialog.current) return;
    if (!dialog.current.open) dialog.current.showModal();
    dialog.current.querySelector('[data-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [selectedRefs]);

  if (incomplete) return <>
    {children}
    {answer ? <details className="qa-incomplete-draft"><summary>查看未完成草稿（可能缺少条件和例外）</summary><pre>{answer}</pre></details> : null}
  </>;

  return <>
    {answer ? <div className={'qa-prose' + (streaming ? ' is-streaming' : '')}>
      <ReactMarkdown remarkPlugins={[remarkGfm, [remarkQACitations, { refs: (citations ?? []).map((citation) => citation.ref) }]]} components={components}>
        {answer}
      </ReactMarkdown>
    </div> : null}

    {children}

    {citedGroups.length || materials.size ? <details className="qa-sources">
      <summary>参考来源 <span>{citedGroups.length + materials.size}</span></summary>
      <div className="qa-source-list">
        {citedGroups.map((group) => <button key={group.key} type="button" className="qa-source-row"
          aria-haspopup="dialog" onClick={() => setSelectedRefs(group.citations.map((citation) => citation.ref))}>
          <span className="qa-source-number">{group.number}</span>
          <span className="qa-source-row-content"><strong>{group.source.title}</strong>
            {group.source.kind !== 'legal' ? <small>{KINDS[group.source.kind]}</small> : null}</span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>)}
        {[...materials.entries()].map(([id, entries]) => <details className="qa-material-source" key={id}>
          <summary><FileText size={14} aria-hidden="true" />{entries[0].filename}</summary>
          {entries.map((citation, index) => <blockquote key={index}>{citation.quote}</blockquote>)}
        </details>)}
      </div>
    </details> : null}

    <dialog ref={dialog} className="qa-citation-dialog" aria-labelledby={headingId}
      onClose={() => setSelectedRefs([])} onClick={(event) => {
        if (event.target === event.currentTarget) {
          const rect = event.currentTarget.getBoundingClientRect();
          if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.current?.close();
        }
      }}>
      <header className="qa-citation-dialog-header"><div><BookOpen size={15} aria-hidden="true" /><h2 id={headingId}>引用依据</h2></div>
        <button type="button" className="qa-citation-close" aria-label="关闭引用依据" onClick={() => dialog.current?.close()}><X size={19} /></button>
      </header>
      <div className="qa-citation-dialog-body">
        {activeGroups.map((group) => <section className="qa-citation-document" key={group.key}>
          {group.source.kind !== 'legal' ? <span className="qa-citation-kind">{KINDS[group.source.kind]}</span> : null}
          <h3>{group.source.title}</h3>
          {citedGroups.some((other) => other.key !== group.key && other.source.title === group.source.title)
            && (group.source.effective_date || group.source.publish_date) ? <p className="qa-citation-version">{group.source.effective_date ? `施行于 ${group.source.effective_date}` : `发布于 ${group.source.publish_date}`}</p> : null}
          {citationURL(group.source.url) ? <a className="qa-citation-original" href={citationURL(group.source.url)} target="_blank" rel="noreferrer">查看原文<ArrowUpRight size={13} /></a> : null}
          {group.citations.filter((citation) => selectedRefs.includes(citation.ref)).map((citation) => <CitationPassage key={citation.ref} citation={citation} selected />)}
          {group.citations.some((citation) => !selectedRefs.includes(citation.ref)) ? <details className="qa-citation-more">
            <summary>同一文件的其他引用</summary>
            {group.citations.filter((citation) => !selectedRefs.includes(citation.ref)).map((citation) => <CitationPassage key={citation.ref} citation={citation} selected={false} />)}
          </details> : null}
        </section>)}
      </div>
    </dialog>
  </>;
}
