const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {test} = require('node:test');
const {sourceCell, sourceTactic, cellAtLine} = require('../../dist/navigation');
const source = 'import Init\n\ntheorem example : True := by\n  trivial\n';
const sha256 = createHash('sha256').update(source).digest('hex');
const snapshot = {sha256, cells: [{index: 0, start: 1, end: 2}, {index: 1, start: 3, end: 4}]};

test('tactic editing binds host-fetched UTF-8 spans to UTF-16 editor positions', () => {
  const text = 'theorem t : True := by\n  -- λ🙂\n  trivial\n';
  const digest = createHash('sha256').update(text).digest('hex');
  const start = Buffer.from(text).indexOf('trivial');
  const snap = {sha256: digest, cells: [{index: 0, start: 1, end: 3}]};
  const trace = {id: 'record', status: 'ready', source_sha256: digest, nodes: [{id: 7, start, end: start + 7}]};
  const request = {id: 'record', node: 7};
  assert.deepEqual(sourceTactic(snap, text.replaceAll('\n', '\r\n'), 0, digest, trace, request), {start: {line: 3, character: 2}, end: {line: 3, character: 9}});
  assert.throws(() => sourceTactic(snap, text, 0, digest, trace, {...request, id: 'old'}), /changed/);
  assert.throws(() => sourceTactic(snap, text + ' ', 0, digest, trace, request), /changed/);
  const invalid = {...trace, nodes: [{id: 7, start: Buffer.from(text).indexOf('λ') + 1, end: start}]};
  assert.throws(() => sourceTactic(snap, text, 0, digest, invalid, request), /UTF-8/);
});

test('navigation binds a cell to the exact saved source, including CRLF buffers', () => {
  assert.deepEqual(sourceCell(snapshot, source, 1, sha256), {start: 3, end: 4});
  assert.deepEqual(sourceCell(snapshot, source.replaceAll('\n', '\r\n'), 1, sha256), {start: 3, end: 4});
  assert.equal(cellAtLine(snapshot, source, 4), 1);
  assert.equal(cellAtLine(snapshot, source, 5), undefined);
});

test('stale clicks and changed source never select unrelated text', () => {
  assert.throws(() => sourceCell(snapshot, source, 1, 'old'), /changed/);
  assert.throws(() => sourceCell(snapshot, '\n' + source, 1, sha256), /changed/);
  assert.throws(() => cellAtLine(snapshot, '\n' + source, 3), /changed/);
});

test('invalid cells and line ranges are rejected', () => {
  for (const index of [-1, 20, '1', 0.5]) assert.throws(() => sourceCell(snapshot, source, index, sha256), /cell/);
  for (const bounds of [{start: 0, end: 1}, {start: 4, end: 2}, {start: 3, end: 900}, {start: 1.5, end: 2}]) {
    assert.throws(() => sourceCell({...snapshot, cells: [{index: 1, ...bounds}]}, source, 1, sha256), /range/);
  }
});

test('inline card navigation accepts only a saved subrange inside its host cell', () => {
  assert.deepEqual(sourceCell(snapshot, source, 1, sha256, {start: 4, end: 4}), {start: 4, end: 4});
  for (const range of [null, {}, {start: 2, end: 4}, {start: 3, end: 5}, {start: 4, end: 3}, {start: 3.5, end: 4}, {start: '3', end: 4}]) {
    assert.throws(() => sourceCell(snapshot, source, 1, sha256, range), /range/);
  }
  assert.throws(() => sourceCell(snapshot, source, 1, 'stale', {start: 3, end: 3}), /changed/);
});
