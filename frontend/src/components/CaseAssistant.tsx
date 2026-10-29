import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { MessageCircle, PencilLine } from 'lucide-react';
import type { MatterSelection, UserRole } from '../types/api';
import type { SavedCase } from '../types/case';
import { CaseAssistantContext, useCaseAssistant, type AuxiliaryPanel } from '../hooks/useCaseAssistant';
import MatterConversation from './MatterConversation';
import '../styles/case-assistant.css';

export function CaseAssistantTrigger() {
  const assistant = useCaseAssistant();
  return assistant.available ? <button type="button" data-assistant-trigger className="case-header__action-btn case-assistant-trigger" aria-label="向 CrossComply 提问" aria-controls="case-conversation-panel" aria-expanded={assistant.active === 'conversation'} onClick={() => assistant.askAbout()}><MessageCircle size={16} aria-hidden="true" />追问案件</button> : null;
}

export default function CaseAssistant({ saved, viewerRole, canManageActions, children }: {
  saved: SavedCase; viewerRole: UserRole; canManageActions: boolean; children: ReactNode;
}) {
  const [active, setActive] = useState<AuxiliaryPanel | null>(null);
  const [selection, setSelection] = useState<MatterSelection>();
  const [passage, setPassage] = useState<{ selection: MatterSelection; rect: DOMRect; top: number; left: number } | null>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef(active);
  activeRef.current = active;
  const documentActions = useRef<{ guard: () => boolean; annotate: (selection: MatterSelection) => void } | null>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const evidenceTrigger = useRef<HTMLElement | null>(null);
  const evidenceCitation = useRef<string | null>(null);
  const restoreEvidenceFocus = useRef(false);
  const task = saved.reviewTask;
  const available = Boolean(task && saved.materialSnapshot?.id === task.material_snapshot_id && saved.intakeSnapshot?.id === task.intake_snapshot_id);
  const activate = useCallback((panel: AuxiliaryPanel, citationRef?: string) => {
    if (activeRef.current === 'document' && panel !== 'document' && documentActions.current?.guard() === false) return false;
    if (panel === 'evidence' && activeRef.current !== panel && document.activeElement instanceof HTMLElement) {
      evidenceTrigger.current = document.activeElement;
      evidenceCitation.current = citationRef ?? document.activeElement.closest('[data-citation-ref]')?.getAttribute('data-citation-ref') ?? null;
    }
    activeRef.current = panel;
    setActive(panel);
    setPassage(null);
    return true;
  }, []);
  const close = useCallback((panel: AuxiliaryPanel) => {
    if (activeRef.current !== panel) return;
    if (panel === 'evidence') restoreEvidenceFocus.current = true;
    if (panel === 'evidence' && document.activeElement instanceof HTMLElement && document.activeElement.closest('#evidence-sidebar')) document.activeElement.blur();
    activeRef.current = null;
    setActive(null);
    if (panel === 'conversation') requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
  }, []);
  useLayoutEffect(() => {
    if (active !== null || !restoreEvidenceFocus.current) return;
    restoreEvidenceFocus.current = false;
    const trigger = evidenceCitation.current
      ? hostRef.current?.querySelector<HTMLElement>(`.cite-marker[data-citation-ref="${CSS.escape(evidenceCitation.current)}"]`)
      : evidenceTrigger.current;
    if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  }, [active]);
  const askAbout = useCallback((next?: MatterSelection) => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!available || !activate('conversation')) return;
    triggerRef.current = trigger?.closest('.passage-actions') ? hostRef.current?.querySelector<HTMLElement>('[data-assistant-trigger]') ?? null : trigger;
    if (next) setSelection(next);
    window.getSelection()?.removeAllRanges();
    requestAnimationFrame(() => hostRef.current?.querySelector<HTMLTextAreaElement>('#matter-question')?.focus());
  }, [activate, available]);
  const registerDocument = useCallback((guard: () => boolean, annotate: (selection: MatterSelection) => void) => {
    const actions = { guard, annotate };
    documentActions.current = actions;
    return () => { if (documentActions.current === actions) documentActions.current = null; };
  }, []);

  useLayoutEffect(() => {
    if (!passage || !toolbarRef.current) return;
    const { width, height } = toolbarRef.current.getBoundingClientRect();
    const rect = passage.rect;
    const top = Math.max(8, Math.min(rect.top >= height + 16 ? rect.top - height - 8 : rect.bottom + 8, window.innerHeight - height - 8));
    const left = Math.max(12, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 12));
    setPassage((current) => current ? { ...current, top, left } : current);
  }, [passage?.selection]);

  useEffect(() => {
    const capture = () => {
      const selected = window.getSelection();
      if (!available || !selected?.rangeCount || selected.isCollapsed) { setPassage(null); return; }
      const range = selected.getRangeAt(0);
      const element = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement;
      const source = element?.closest<HTMLElement>('[data-matter-source]');
      if (!source || !hostRef.current?.contains(source) || !source.contains(range.endContainer)) { setPassage(null); return; }
      const quote = range.toString();
      if (!quote.trim() || Array.from(quote).length > 2000) { setPassage(null); return; }
      let reference: MatterSelection;
      if (source.dataset.matterSource === 'material') {
        const prefix = range.cloneRange();
        prefix.selectNodeContents(source);
        prefix.setEnd(range.startContainer, range.startOffset);
        const start = Array.from(prefix.toString()).length;
        reference = { kind: 'material', material_version_id: source.dataset.materialId!, quote, start_offset: start, end_offset: start + Array.from(quote).length, label: source.dataset.materialLabel };
      } else {
        if (element?.closest('button, input, textarea, summary, a') || range.cloneContents().querySelector('button, input, textarea')) { setPassage(null); return; }
        reference = { kind: 'report', review_result_id: source.dataset.reportId!, quote, label: '审查报告' };
      }
      const rect = range.getBoundingClientRect();
      if (rect.bottom < 8 || rect.top > window.innerHeight - 8) { setPassage(null); return; }
      const width = Math.min(canManageActions && reference.kind === 'material' ? 340 : 236, window.innerWidth - 24);
      const below = rect.top < 64;
      setPassage((current) => current && JSON.stringify(current.selection) === JSON.stringify(reference) && current.rect.top === rect.top && current.rect.left === rect.left ? current : {
        selection: reference, rect,
        top: Math.max(8, Math.min(below ? rect.bottom + 8 : rect.top - 52, window.innerHeight - 60)),
        left: Math.max(12, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 12)),
      });
    };
    const pointer = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest('.passage-actions')) capture();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setPassage(null); if (activeRef.current === 'conversation') close('conversation'); }
      else if (event.shiftKey || event.key.startsWith('Arrow')) capture();
    };
    let frame = 0;
    const reposition = () => { window.cancelAnimationFrame(frame); frame = window.requestAnimationFrame(capture); };
    let timer = 0;
    const selectionChanged = () => { window.clearTimeout(timer); timer = window.setTimeout(capture, 120); };
    document.addEventListener('pointerup', pointer);
    document.addEventListener('keyup', key);
    document.addEventListener('selectionchange', selectionChanged);
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    return () => {
      document.removeEventListener('pointerup', pointer);
      document.removeEventListener('keyup', key);
      document.removeEventListener('selectionchange', selectionChanged);
      window.clearTimeout(timer);
      window.removeEventListener('scroll', reposition, true);
      window.removeEventListener('resize', reposition);
      window.cancelAnimationFrame(frame);
    };
  }, [available, canManageActions, close]);

  return <CaseAssistantContext.Provider value={{ active, selection, setSelection, activate, close, askAbout, registerDocument, available }}>
    <div className={'case-assistant-host' + (active === 'conversation' ? ' has-conversation' : '')} ref={hostRef}>
      <div className="case-assistant-main">{children}</div>
      {active === 'conversation' ? <button type="button" className="matter-panel-scrim" aria-label="收起问答面板" tabIndex={-1} onClick={() => close('conversation')} /> : null}
      {available ? <MatterConversation saved={saved} viewerRole={viewerRole} /> : null}
    </div>
    {passage ? createPortal(<div className="passage-actions" ref={toolbarRef} role="toolbar" aria-label="选中文字操作" style={{ top: passage.top, left: passage.left }} onPointerDown={(event) => event.preventDefault()}>
      <button type="button" onClick={() => askAbout(passage.selection)}><MessageCircle size={16} aria-hidden="true" />向 CrossComply 追问</button>
      {canManageActions && passage.selection.kind === 'material' ? <button type="button" onClick={() => {
        const reference = passage.selection;
        if (activate('document')) documentActions.current?.annotate(reference);
        window.getSelection()?.removeAllRanges();
      }}><PencilLine size={15} aria-hidden="true" />添加批注</button> : null}
    </div>, document.body) : null}
  </CaseAssistantContext.Provider>;
}
