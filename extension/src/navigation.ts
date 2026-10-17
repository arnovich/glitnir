import {createHash} from 'node:crypto';

interface Cell {index: number; start: number; end: number}
interface Snapshot {sha256: string; cells: Cell[]}

/** Bind navigation to saved text; the backend normalizes CRLF just as Lean does. */
function current(snapshot: Snapshot, source: string, expected = snapshot.sha256): string {
  const normalized = source.replace(/\r\n/g, '\n');
  if (expected !== snapshot.sha256 || createHash('sha256').update(normalized).digest('hex') !== snapshot.sha256) {
    throw new Error('The notebook changed. Save the Lean file and refresh the preview before navigating.');
  }
  return normalized;
}

/** Bind navigation to a host-fetched cell; optional card ranges must lie within it. */
export function sourceCell(snapshot: Snapshot, source: string, index: number, expected: string, range?: unknown): {start: number; end: number} {
  const text = current(snapshot, source, expected);
  const cell = Number.isSafeInteger(index) && snapshot.cells.find(c => c.index === index);
  if (!cell) throw new Error('This notebook cell is no longer available.');
  const lines = text.split('\n').length - (text.endsWith('\n') ? 1 : 0);
  if (!Number.isSafeInteger(cell.start) || !Number.isSafeInteger(cell.end) || cell.start < 1 || cell.end < cell.start || cell.end > lines) {
    throw new Error('Invalid notebook source range.');
  }
  if (range !== undefined) {
    if (!range || typeof range !== 'object' || !('start' in range) || !('end' in range)
      || typeof range.start !== 'number' || typeof range.end !== 'number'
      || !Number.isSafeInteger(range.start) || !Number.isSafeInteger(range.end)
      || range.start < cell.start || range.end > cell.end || range.end < range.start) {
      throw new Error('Invalid notebook card source range.');
    }
    return {start: range.start, end: range.end};
  }
  return {start: cell.start, end: cell.end};
}

/** Find the rendered cell containing a saved editor position (one-based). */
export function cellAtLine(snapshot: Snapshot, source: string, line: number): number | undefined {
  current(snapshot, source);
  return snapshot.cells.find(c => c.start <= line && line <= c.end)?.index;
}

/** Select only a host-fetched tactic range from the same saved source/cell. */
export function sourceTactic(snapshot: Snapshot, source: string, index: number, expected: string, trace: any, request: any): {start: {line: number; character: number}; end: {line: number; character: number}} {
  const bounds = sourceCell(snapshot, source, index, expected);
  const text = current(snapshot, source, expected);
  if (!request || typeof request.id !== 'string' || !Number.isSafeInteger(request.node)
    || !trace || !['ready', 'partial'].includes(trace.status) || trace.id !== request.id || trace.source_sha256 !== expected) throw new Error('The proof observation changed. Refresh its steps.');
  const node = trace.nodes?.find((n: any) => n.id === request.node);
  if (!node || !Number.isSafeInteger(node.start) || !Number.isSafeInteger(node.end)
    || node.start < 0 || node.end <= node.start || node.end > Buffer.byteLength(text)) throw new Error('Invalid tactic source range.');
  const bytes = Buffer.from(text);
  function position(offset: number): {line: number; character: number} {
    const prefix = bytes.subarray(0, offset).toString('utf8');
    if (Buffer.byteLength(prefix) !== offset || !Buffer.from(prefix).equals(bytes.subarray(0, offset))) throw new Error('Invalid UTF-8 tactic boundary.');
    const lines = prefix.split('\n');
    return {line: lines.length, character: lines.at(-1)!.length};
  }
  const start = position(node.start), end = position(node.end);
  if (start.line < bounds.start || start.line > bounds.end || end.line > bounds.end + 1
    || (end.line === bounds.end + 1 && end.character !== 0)) throw new Error('Tactic range escapes its notebook cell.');
  return {start, end};
}
