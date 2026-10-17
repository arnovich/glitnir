import {createHash} from 'node:crypto';
import type * as vscode from 'vscode';
import type {Reply} from './backend';

/** A checked decision block, as the backend serves it for the saved text `sha256`. */
export interface Candidate {sha256: string; insert_after_line: number; block: string}

/** Insert `text` at a zero-based editor position. */
export interface Insertion {line: number; character: number; text: string}

/** The parts of the VS Code API that accepting uses, so tests can pass a double. */
export type EditorApi = Pick<typeof vscode, 'Position' | 'WorkspaceEdit'> & {workspace: Pick<typeof vscode.workspace, 'applyEdit'>};

const SHA256 = /^[0-9a-f]{64}$/;
const MAX_BLOCK_BYTES = 200_000;
export const DIRTY = 'Save the notebook before accepting a decision.';
export const CHANGED = 'The notebook changed since this decision ran; run it again.';
const INVALID = 'Glitnir returned an invalid decision; nothing was inserted.';
const BOM = 'Notebooks starting with a byte-order mark cannot accept decisions yet; remove the BOM.';
const RACED = 'The notebook changed while accepting; review and save it yourself.';

/** The only thing the preview names: a decision key. Nothing else it sends is read. */
export function decisionKey(value: unknown): string {
  if (typeof value !== 'string' || !SHA256.test(value)) throw new Error('Invalid decision key; nothing was inserted.');
  return value;
}

/** Read the backend's answer: exactly a candidate, or its reason for refusing. */
export function servedCandidate(reply: Reply): Candidate {
  if (reply.status !== 200) {
    const reason = reply.json?.error;
    throw new Error(typeof reason === 'string' && reason ? reason : 'This decision cannot be accepted.');
  }
  const value = reply.json;
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== 'block,insert_after_line,sha256'
    || typeof value.sha256 !== 'string' || !SHA256.test(value.sha256)
    || !Number.isSafeInteger(value.insert_after_line) || value.insert_after_line < 1
    || typeof value.block !== 'string' || !value.block || /[\r\0]/.test(value.block)
    || Buffer.byteLength(value.block) > MAX_BLOCK_BYTES) throw new Error(INVALID);
  return {sha256: value.sha256, insert_after_line: value.insert_after_line, block: value.block};
}

/**
 * Place the block on the lines right after the directive's last line, one-based
 * `insert_after_line`, in exactly the saved text it was checked against. The
 * backend compares text with CRLF normalized, as Lean reads it.
 */
export function insertion(text: string, dirty: boolean, candidate: Candidate): Insertion {
  if (dirty) throw new Error(DIRTY);
  const normalized = text.replace(/\r\n/g, '\n');
  const digest = (value: string) => createHash('sha256').update(value).digest('hex');
  if (digest(normalized) !== candidate.sha256) {
    // getText() drops a leading byte-order mark that the saved digest keeps.
    throw new Error(digest('\ufeff' + normalized) === candidate.sha256 ? BOM : CHANGED);
  }
  const lines = normalized.split('\n');
  const after = candidate.insert_after_line;
  const count = lines.length - (normalized.endsWith('\n') ? 1 : 0);
  if (after > count) throw new Error(INVALID);
  if (after < lines.length) return {line: after, character: 0, text: candidate.block + '\n'};
  // The directive ends the file without a newline: start the block on its own line.
  return {line: after - 1, character: lines[after - 1].length, text: '\n' + candidate.block};
}

/**
 * Insert a decision's checked block into the notebook and save it. This is the
 * only edit the extension makes to a notebook: the block comes from the backend,
 * never from the preview, and goes only into the saved text it was checked against.
 */
export async function acceptDecision(api: EditorApi, document: vscode.TextDocument,
  request: (path: string) => Promise<Reply>, key: unknown): Promise<void> {
  const name = decisionKey(key);
  if (document.isDirty) throw new Error(DIRTY);
  const candidate = servedCandidate(await request(`/api/decide/candidate?key=${name}`));
  // Read the text and edit it without yielding in between.
  const version = document.version;
  const at = insertion(document.getText(), document.isDirty, candidate);
  const edit = new api.WorkspaceEdit();
  edit.insert(document.uri, new api.Position(at.line, at.character), at.text);
  if (!await api.workspace.applyEdit(edit)) throw new Error('VS Code did not insert the decision; the notebook is unchanged.');
  // Exactly our edit happened since reading: anything else is not ours to save.
  if (document.version !== version + 1) throw new Error(RACED);
  if (!await document.save()) throw new Error('The decision was inserted but the notebook was not saved. Save it to accept the decision.');
}
