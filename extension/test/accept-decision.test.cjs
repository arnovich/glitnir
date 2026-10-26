const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {readFileSync, readdirSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const {acceptDecision, decisionKey, insertion, servedCandidate} = require('../../dist/accept-decision');

const sha = text => createHash('sha256').update(text.replace(/\r\n/g, '\n')).digest('hex');
const SOURCE = 'import Demo\n\ndef x := 1\n-- @decide band : entailment x : x > 0\n--   |- x >= 0\ndef after := 2\n';
const BLOCK = 'namespace Decided_band\ntheorem band : True := trivial\nend Decided_band';
const KEY = 'c'.repeat(64);

/** Apply an insertion as VS Code does: positions count lines split at LF. */
function apply(text, edit) {
  const lines = text.split('\n');
  const offset = lines.slice(0, edit.line).reduce((sum, line) => sum + line.length + 1, 0) + edit.character;
  return text.slice(0, offset) + edit.text + text.slice(offset);
}

/** The backend's `_block_follows`: the block's lines right after the one-based line. */
function follows(text, after, block) {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const wanted = block.split('\n');
  return JSON.stringify(lines.slice(after, after + wanted.length)) === JSON.stringify(wanted);
}

test('a decision key is exactly 64 lowercase hex digits', () => {
  assert.equal(decisionKey(KEY), KEY);
  for (const bad of [undefined, null, 7, {}, [KEY], '', 'C'.repeat(64), 'c'.repeat(63), 'c'.repeat(65), `${KEY}\n`, `${KEY}&x=1`]) {
    assert.throws(() => decisionKey(bad), /Invalid decision key/, String(bad));
  }
});

test('the backend reply must be exactly a candidate; refusals pass through', () => {
  const served = {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK};
  assert.deepEqual(servedCandidate({status: 200, json: served}), served);
  assert.throws(() => servedCandidate({status: 409, json: {error: 'This decision is out of date; run it again.'}}), /^Error: This decision is out of date; run it again\.$/);
  assert.throws(() => servedCandidate({status: 503, json: {}}), /cannot be accepted/);
  for (const json of [null, [], {...served, extra: 1}, {sha256: served.sha256, block: BLOCK}, {...served, sha256: 'X'.repeat(64)},
    {...served, insert_after_line: 0}, {...served, insert_after_line: 2.5}, {...served, insert_after_line: '5'},
    {...served, block: ''}, {...served, block: 'a\r\nb'}, {...served, block: 'a\0'}, {...served, block: 'x'.repeat(200_001)}, {...served, block: 3}]) {
    assert.throws(() => servedCandidate({status: 200, json}), /invalid decision/, JSON.stringify(json)?.slice(0, 80));
  }
});

test('the block lands on the lines right after the directive, byte-exact', () => {
  const candidate = {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK};
  const edit = insertion(SOURCE, false, candidate);
  assert.deepEqual(edit, {line: 5, character: 0, text: BLOCK + '\n'});
  const result = apply(SOURCE, edit);
  assert.equal(result, 'import Demo\n\ndef x := 1\n-- @decide band : entailment x : x > 0\n--   |- x >= 0\n' + BLOCK + '\ndef after := 2\n');
  assert.ok(follows(result, 5, BLOCK));
});

test('a directive on the last line, with or without a final newline', () => {
  const ending = 'def x := 1\n-- @decide band : entailment x : x > 0';
  for (const text of [ending + '\n', ending]) {
    const edit = insertion(text, false, {sha256: sha(text), insert_after_line: 2, block: BLOCK});
    const result = apply(text, edit);
    assert.equal(result, `${ending}\n${BLOCK}${text.endsWith('\n') ? '\n' : ''}`);
    assert.ok(follows(result, 2, BLOCK));
  }
  assert.throws(() => insertion(ending, false, {sha256: sha(ending), insert_after_line: 3, block: BLOCK}), /invalid decision/);
  assert.throws(() => insertion(ending + '\n', false, {sha256: sha(ending + '\n'), insert_after_line: 3, block: BLOCK}), /invalid decision/);
});

test('CRLF documents are compared as Lean reads them and keep the block exact', () => {
  const crlf = SOURCE.replaceAll('\n', '\r\n');
  const edit = insertion(crlf, false, {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK});
  assert.deepEqual(edit, {line: 5, character: 0, text: BLOCK + '\n'});
  assert.ok(follows(apply(crlf, edit), 5, BLOCK));
  const raw = createHash('sha256').update(crlf).digest('hex');
  assert.throws(() => insertion(crlf, false, {sha256: raw, insert_after_line: 5, block: BLOCK}), /changed since this decision ran/);
});

test('a notebook saved with a byte-order mark gets its own refusal', () => {
  // VS Code's text drops the mark that the backend's digest of the saved file keeps.
  const candidate = {sha256: sha('\ufeff' + SOURCE), insert_after_line: 5, block: BLOCK};
  assert.throws(() => insertion(SOURCE, false, candidate),
    /^Error: Notebooks starting with a byte-order mark cannot accept decisions yet; remove the BOM\.$/);
});

test('dirty or changed notebooks are refused', () => {
  const candidate = {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK};
  assert.throws(() => insertion(SOURCE, true, candidate), /^Error: Save the notebook before accepting a decision\.$/);
  assert.throws(() => insertion('-- note\n' + SOURCE, false, candidate), /^Error: The notebook changed since this decision ran; run it again\.$/);
});

function editor(text, {dirty = false, applied = true, saved = true, bumps = 1} = {}) {
  const calls = {requests: [], edits: [], saves: 0};
  const document = {uri: {fsPath: '/work/A.lean'}, version: 7, isDirty: dirty, getText: () => text, save: async () => { calls.saves++; return saved; }};
  const api = {
    Position: class { constructor(line, character) { Object.assign(this, {line, character}); } },
    WorkspaceEdit: class { insert(uri, position, value) { calls.edits.push([uri.fsPath, position.line, position.character, value]); } },
    // VS Code bumps the version once per applied edit; another edit landing in between bumps it again.
    workspace: {applyEdit: async () => { if (applied) document.version += bumps; return applied; }},
  };
  return {calls, document, api};
}

test('accepting fetches the candidate by key, inserts it and saves', async () => {
  const {calls, document, api} = editor(SOURCE);
  const reply = {status: 200, json: {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK}};
  await acceptDecision(api, document, async url => { calls.requests.push(url); return reply; }, KEY);
  assert.deepEqual(calls.requests, [`/api/decide/candidate?key=${KEY}`]);
  assert.deepEqual(calls.edits, [['/work/A.lean', 5, 0, BLOCK + '\n']]);
  assert.equal(calls.saves, 1);
});

test('accepting refuses before asking, and reports edits or saves that fail', async () => {
  const reply = {status: 200, json: {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK}};
  for (const [options, key, pattern] of [[{dirty: true}, KEY, /Save the notebook/], [{}, 'nope', /Invalid decision key/]]) {
    const {calls, document, api} = editor(SOURCE, options);
    await assert.rejects(acceptDecision(api, document, async url => { calls.requests.push(url); return reply; }, key), pattern);
    assert.deepEqual([calls.requests, calls.edits, calls.saves], [[], [], 0]);
  }
  const unapplied = editor(SOURCE, {applied: false});
  await assert.rejects(acceptDecision(unapplied.api, unapplied.document, async () => reply, KEY), /did not insert/);
  assert.equal(unapplied.calls.saves, 0);
  const unsaved = editor(SOURCE, {saved: false});
  await assert.rejects(acceptDecision(unsaved.api, unsaved.document, async () => reply, KEY), /not saved/);
});

test('accepting never saves a notebook that changed besides the inserted block', async () => {
  const reply = {status: 200, json: {sha256: sha(SOURCE), insert_after_line: 5, block: BLOCK}};
  for (const bumps of [0, 2]) {
    const raced = editor(SOURCE, {bumps});
    await assert.rejects(acceptDecision(raced.api, raced.document, async () => reply, KEY),
      /^Error: The notebook changed while accepting; review and save it yourself\.$/);
    assert.equal(raced.calls.edits.length, 1);
    assert.equal(raced.calls.saves, 0);
  }
});

test('only the accept path edits documents', () => {
  const sources = path.resolve(__dirname, '../src');
  for (const name of readdirSync(sources)) {
    const text = readFileSync(path.join(sources, name), 'utf8');
    if (name !== 'accept-decision.ts') assert.doesNotMatch(text, /applyEdit|WorkspaceEdit|\.save\(|TextEditorEdit|\.edit\(/, name);
  }
});

test('the bridge sends only the decision key when Accept is clicked', () => {
  const media = path.resolve(__dirname, '../../dist/media');
  const dom = new JSDOM(`<body><div id="editor-state"></div><button data-act="accept" data-key="${KEY}" data-block="theorem evil : False := sorry">Accept</button></body>`, {runScripts: 'outside-only'});
  const {window} = dom;
  const posted = [];
  window.acquireVsCodeApi = () => ({postMessage: message => posted.push(message)});
  window.eval(readFileSync(path.join(media, 'bridge.js'), 'utf8'));
  let reached = false;
  window.document.addEventListener('click', () => { reached = true; });
  window.document.querySelector('button').click();
  assert.deepEqual(JSON.parse(JSON.stringify(posted.slice(1))), [{type: 'acceptDecision', key: KEY}]);
  assert.equal(reached, false, 'the renderer never handles Accept itself');
});
