import assert from 'node:assert/strict';
import test from 'node:test';

import { allocateUploadNames, PASTED_MATERIAL_LOGICAL_NAME } from './materialNames.ts';

/** Mimics the submit path: names are claimed one by one against a shared "taken" set. */
function allocate(names: string[], alreadyTaken: string[] = []): string[] {
  return allocateUploadNames(names, alreadyTaken, false);
}

test('repeated file names get numbered instead of replacing each other', () => {
  assert.deepEqual(allocate(['a.pdf', 'a.pdf', 'a.pdf']), ['a.pdf', 'a (2).pdf', 'a (3).pdf']);
});

test('numbering also avoids names already frozen for the case', () => {
  assert.deepEqual(allocate(['a.pdf'], ['a.pdf']), ['a (2).pdf']);
  assert.deepEqual(allocate(['a.pdf'], ['a.pdf', 'a (2).pdf']), ['a (3).pdf']);
});

test('numbering fills the lowest free slot', () => {
  assert.deepEqual(allocate(['a.pdf', 'a.pdf'], ['a (2).pdf']), ['a.pdf', 'a (3).pdf']);
});

test('names without an extension stay readable', () => {
  assert.deepEqual(allocate(['notes', 'notes']), ['notes', 'notes (2)']);
  assert.deepEqual(allocate(['.env', '.env']), ['.env', '.env (2)']);
  assert.deepEqual(allocate(['a.b.pdf', 'a.b.pdf']), ['a.b.pdf', 'a.b (2).pdf']);
});

test('distinct names are left untouched', () => {
  assert.deepEqual(allocate(['contract.pdf', 'attachment.md']), ['contract.pdf', 'attachment.md']);
});

test('upload names avoid the names already frozen on the case', () => {
  assert.deepEqual(allocateUploadNames(['contract.pdf'], ['contract.pdf'], false), ['contract (2).pdf']);
  assert.deepEqual(
    allocateUploadNames(['contract.pdf'], ['contract.pdf', 'contract (2).pdf'], false),
    ['contract (3).pdf'],
  );
});

test('re-uploaded prose reserves its stable name before the files are numbered', () => {
  assert.deepEqual(
    allocateUploadNames([PASTED_MATERIAL_LOGICAL_NAME], [], true),
    [`${PASTED_MATERIAL_LOGICAL_NAME} (2)`],
  );
  // Without prose to re-upload the file keeps the plain name.
  assert.deepEqual(allocateUploadNames([PASTED_MATERIAL_LOGICAL_NAME], [], false), [PASTED_MATERIAL_LOGICAL_NAME]);
});