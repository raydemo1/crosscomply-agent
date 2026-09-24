/**
 * Put the auto-number before the extension so the result still reads as a filename:
 * `contract.pdf` → `contract (2).pdf` → `contract (3).pdf`.
 */
export function uniqueLogicalName(filename: string, taken: Set<string>): string {
  if (!taken.has(filename)) return filename;
  const dot = filename.lastIndexOf('.');
  const base = dot > 0 ? filename.slice(0, dot) : filename;
  const suffix = dot > 0 ? filename.slice(dot) : '';
  let ordinal = 2;
  while (taken.has(`${base} (${ordinal})${suffix}`)) ordinal += 1;
  return `${base} (${ordinal})${suffix}`;
}

/** Pasted prose keeps one stable identity so editing it replaces its previous version. */
export const PASTED_MATERIAL_LOGICAL_NAME = '材料正文';

/**
 * Names claimed by one upload batch, in submission order: the pasted prose keeps its reserved
 * name first, then every dropped file takes the next free name. Callers use this both to preview
 * the list and to submit it, so what the user reads is what gets frozen.
 */
export function allocateUploadNames(
  filenames: string[],
  frozenNames: Iterable<string>,
  reservePastedMaterial: boolean,
): string[] {
  const taken = new Set(frozenNames);
  if (reservePastedMaterial) taken.add(PASTED_MATERIAL_LOGICAL_NAME);
  return filenames.map((filename) => {
    const chosen = uniqueLogicalName(filename, taken);
    taken.add(chosen);
    return chosen;
  });
}