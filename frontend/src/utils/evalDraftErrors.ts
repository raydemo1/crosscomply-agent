import type { DraftError } from '../types/evaluation';

export function groupDraftErrors(errors: DraftError[], fields: readonly string[]): Record<string, DraftError[]> {
  const entries = new Map<string, DraftError[]>();
  const ordered = [...fields].sort((left, right) => right.length - left.length);
  for (const error of errors) {
    const key = ordered.find((field) => error.field === field || error.field.startsWith(`${field}.`) || error.field.startsWith(`${field}[`)) ?? 'form';
    const group = entries.get(key) ?? [];
    group.push(error);
    entries.set(key, group);
  }
  return Object.fromEntries(entries);
}
