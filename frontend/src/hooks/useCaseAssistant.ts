import { createContext, useContext } from 'react';
import type { MatterSelection } from '../types/api';

export type AuxiliaryPanel = 'conversation' | 'document' | 'evidence';
export interface CaseAssistantState {
  active: AuxiliaryPanel | null;
  selection: MatterSelection | undefined;
  setSelection: (selection: MatterSelection | undefined) => void;
  activate: (panel: AuxiliaryPanel, citationRef?: string) => boolean;
  close: (panel: AuxiliaryPanel) => void;
  askAbout: (selection?: MatterSelection) => void;
  registerDocument: (guard: () => boolean, annotate: (selection: MatterSelection) => void) => () => void;
  available: boolean;
}
export const CaseAssistantContext = createContext<CaseAssistantState | null>(null);
export function useCaseAssistant(): CaseAssistantState {
  const context = useContext(CaseAssistantContext);
  if (!context) throw new Error('Case assistant must be inside its provider');
  return context;
}
