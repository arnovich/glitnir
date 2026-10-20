const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');

test('the shared renderer loads through the bridge and keeps unsaved checks off the wire', async () => {
  const media = path.resolve(__dirname, '../../dist/media');
  const html = readFileSync(path.join(media, 'index.html'), 'utf8').replace('<body>', '<body><div id="editor-state"></div><button id="open-source"></button><button id="next-unresolved" hidden></button>');
  const dom = new JSDOM(html, {runScripts: 'outside-only', url: 'https://preview.invalid/'});
  const {window} = dom;
  const requests = [];
  let poll;
  const schedule = window.setTimeout.bind(window);
  // Capture the notebook poll and drive it by hand. It reschedules itself at
  // 4000ms when idle and 1000ms while a run is going.
  window.setTimeout = (fn, delay) => delay === 4000 || delay === 1000 ? (poll = fn, 0) : schedule(fn, delay);
  const claim = {name: 'simple', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}};
  const snapshot = {
    file: 'Example.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    cells: [{index: 0, kind: 'prose', start: 1, end: 5, text: '/-! # Example -/', markdown: '# Example\n\n## Read me\n\nA saved notebook.\n\n![Figure](assets/a.svg)'}],
    claims: [claim], links: [], revisions: [], assets: [{path: 'assets/a.svg', sha256: 'b'.repeat(64), status: 'ok'}],
  };
  window.Response = Response;
  window.matchMedia = () => ({matches: false});
  window.acquireVsCodeApi = () => ({postMessage: message => {
    requests.push(message);
    if (!['request', 'export', 'clipboard'].includes(message.type)) return;
    const reply = message.path?.startsWith('/asset?')
      ? {data: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>').toString('base64'), contentType: 'image/svg+xml'}
      : {json: message.path === '/api/notebook' ? snapshot : {queued: []}};
    queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, ...reply}})));
  }});
  try {
    window.eval(readFileSync(path.join(media, 'bridge.js'), 'utf8'));
    window.eval(readFileSync(path.join(media, 'review.js'), 'utf8'));
    window.eval(readFileSync(path.join(media, 'circuits.js'), 'utf8'));
  window.eval(readFileSync(path.join(media, 'cards.js'), 'utf8'));
    window.eval(readFileSync(path.join(media, 'app.js'), 'utf8'));
    await new Promise(resolve => setTimeout(resolve, 60));
    assert.equal(window.document.getElementById('title').textContent, 'Example');
    assert.match(window.document.getElementById('doc').textContent, /A saved notebook/);
    assert.match(window.document.querySelector('#doc .figure img').src, /^data:image\/svg\+xml;base64,/);
    assert.deepEqual(requests.filter(item => item.path?.startsWith('/asset')).map(item => item.path), [`/asset?path=assets%2Fa.svg&sha256=${'b'.repeat(64)}`]);
    await window.glitnirHost.saveHTML('<!doctype html><p>export</p>');
    assert.equal(requests.at(-1).type, 'export');
    assert.equal(requests.at(-1).body, '<!doctype html><p>export</p>');
    await window.glitnirHost.copyText('python -m replay --goal goal.json');
    assert.equal(requests.at(-1).type, 'clipboard');
    assert.equal(requests.at(-1).body, 'python -m replay --goal goal.json');
    assert.equal(window.document.querySelector('#insp .pill').textContent, 'Checked');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    assert.match(window.document.getElementById('editor-state').textContent, /Unsaved Lean/);
    assert.equal(window.document.getElementById('editor-state').hidden, false);
    const blocked = await window.glitnirHost.fetch('/api/project/check', {method: 'POST', body: '{}'});
    assert.equal(blocked.status, 409);
    assert.equal(requests.filter(item => item.path === '/api/project/check').length, 0);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: false}}));
    assert.equal(window.document.getElementById('editor-state').hidden, true);
    assert.equal(window.document.getElementById('editor-state').textContent, '');
    snapshot.sha256 = 'd'.repeat(64);
    claim.status = 'stale';
    claim.reason = 'context changed';
    await poll();
    assert.equal(window.document.querySelector('#insp .pill').textContent, 'Out of date');
    assert.match(window.document.getElementById('banner-text').textContent, /no longer match/);
    assert.equal((await window.glitnirHost.fetch('/api/project/check', {method: 'POST', body: '{}'})).status, 200);
    assert.equal(window.document.querySelectorAll('[data-act="replay"], [data-act="goal"], #replay-all, #replay-stale').length, 0, 'no pinned replay or goal actions');
    window.document.getElementById('open-source').click();
    assert.equal(requests.at(-1).type, 'source');
  } finally { window.close(); }
});

// Loads the packaged renderer over a fixed snapshot and waits for it to draw.
async function renderSnapshot(snapshot, {katex = false, onPoll, onMessage, onView, onProof, onGit, onNotebook, onLinked, onAsset, browser = false} = {}) {
  const media = path.resolve(__dirname, '../../dist/media');
  const html = readFileSync(path.join(media, 'index.html'), 'utf8').replace('<body>', '<body><div id="editor-state"></div><button id="open-source"></button><button id="next-unresolved" hidden></button>');
  const dom = new JSDOM(html, {runScripts: 'outside-only', url: 'https://preview.invalid/'});
  const {window} = dom;
  const errors = [];
  window.addEventListener('error', event => errors.push(event.error));
  const close = window.close.bind(window);
  window.close = () => { close(); assert.deepEqual(errors, [], 'renderer must not emit uncaught errors'); };
  const schedule = window.setTimeout.bind(window);
  window.setTimeout = (fn, delay) => delay === 4000 || delay === 1000 ? (window.glitnirPoll = fn, onPoll?.(fn, delay), 0) : schedule(fn, delay);
  window.Response = Response;
  window.matchMedia = () => ({matches: false});
  window.acquireVsCodeApi = () => ({postMessage: message => {
    onMessage?.(message);
    if (['gitHistory', 'gitDiff'].includes(message.type)) {
      Promise.resolve(onGit?.(message)).then(json => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, json}})));
      return;
    }
    if (message.type !== 'request') return;
    if (message.path.startsWith('/asset?')) {
      Promise.resolve(onAsset?.(message.path) ?? {status: 404, json: {error: 'not found'}}).then(reply => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, ...reply}})));
      return;
    }
    if (message.path.startsWith('/api/linked?')) {
      Promise.resolve(onLinked?.(message.path) ?? {status: 404, json: {error: 'There is no such notebook.'}}).then(reply => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: reply.status ?? 200, json: structuredClone(reply.json)}})));
      return;
    }
    if (message.path.startsWith('/api/proof/') && onProof) {
      // A reply may name its HTTP status: {httpStatus, json}.
      const reply = onProof(message) || {};
      const [status, json] = reply.httpStatus ? [reply.httpStatus, reply.json] : [200, reply];
      queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status, json: structuredClone(json)}})));
      return;
    }
    if (message.path === '/api/notebook' && onNotebook) {
      Promise.resolve(onNotebook()).then(reply => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: reply.status ?? 200, json: structuredClone(reply.json)}})));
      return;
    }
    const json = message.path === '/api/notebook' ? snapshot : message.path.startsWith('/api/proof/') ? onProof?.(message) || {} : message.path === '/api/export/styles' ? {css: '.static-notebook{color:black}'} : message.path.startsWith('/api/view?') || message.path.startsWith('/api/view/job?') ? onView?.(message.path) || {} : {queued: []};
    queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, json: structuredClone(json)}})));
  }});
  if (katex) window.eval(readFileSync(path.join(media, 'katex/katex.min.js'), 'utf8'));
  window.eval(readFileSync(path.join(media, 'export.js'), 'utf8'));
  if (browser) window.fetch = async (url, options) => typeof browser === 'function' ? browser(url, options, window) : {ok: true, json: async () => snapshot};
  else { window.eval(readFileSync(path.join(media, 'bridge.js'), 'utf8')); window.eval(readFileSync(path.join(media, 'review.js'), 'utf8')); }
  window.eval(readFileSync(path.join(media, 'circuits.js'), 'utf8'));
  window.eval(readFileSync(path.join(media, 'cards.js'), 'utf8'));
  window.eval(readFileSync(path.join(media, 'app.js'), 'utf8'));
  await new Promise(resolve => setTimeout(resolve, 60));
  return window;
}

test('history separates source identity, Git commits and metadata activity with safe diffs', async () => {
  const sha = 'c'.repeat(40), messages = [];
  const snapshot = {file: 'Example.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, cells: [], claims: [], links: [], revisions: [{rev: 1, time: 1759100000, label: 'Edited theorem', made_stale: ['first']}], unrecorded_change: true};
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onGit: m => m.type === 'gitDiff' ? {diff: '+<script>unsafe()</script>\n-theorem old'} : {state: 'repository', branch: '<main>', head: sha, fileState: 'staged_and_modified', commits: [{sha, author: '<author>', date: '2026-10-11T12:00:00Z', subject: '<img src=x onerror=unsafe()> Fix theorem'}], more: false}});
  const wait = () => new Promise(resolve => setTimeout(resolve, 30));
  try {
    const doc = window.document;
    let scrolled;
    window.HTMLElement.prototype.scrollIntoView = function() { scrolled = this; };
    assert.match(doc.getElementById('file-state').textContent, /Source hash: aaaaaaaa/);
    assert.match(doc.getElementById('file-state').title, /not a Git commit/);
    assert.equal(messages.filter(m => m.type === 'gitHistory').length, 0, 'no Git polling on notebook load');
    doc.getElementById('history').open = true;
    await wait();
    assert.equal(messages.filter(m => m.type === 'gitHistory').length, 1);
    const panel = doc.getElementById('git-history');
    assert.match(panel.textContent, /Branch <main>/);
    assert.match(panel.textContent, /Staged and unstaged changes to this file/);
    assert.match(panel.textContent, /earlier names are not followed/);
    assert.equal(panel.querySelectorAll('img,script').length, 0);
    doc.querySelector('[data-act="git-diff"]').click();
    await wait();
    const diff = doc.querySelector('.git-diff-text');
    assert.equal(scrolled, doc.getElementById('git-diff'));
    assert.match(diff.textContent, /\+<script>unsafe/);
    assert.equal(diff.querySelectorAll('script').length, 0);
    await window.glitnirPoll();
    assert.equal(doc.querySelector('.git-diff-text'), diff, 'notebook polling preserves the diff and its focus');
    assert.equal(messages.filter(m => m.type === 'gitHistory').length, 1);
    assert.match(doc.getElementById('notebook-activity').textContent, /Made stale: first/);
    assert.match(doc.getElementById('notebook-activity').textContent, /Saves in VS Code are not added/);
    doc.querySelector('[data-act="git-refresh"]').click();
    await wait();
    assert.equal(messages.filter(m => m.type === 'gitHistory').length, 2);
    assert.equal(doc.activeElement, doc.querySelector('[data-act="git-refresh"]'));
  } finally { window.close(); }
});

test('outside Git the history panel keeps notebook activity available and makes no promises about saved versions', async () => {
  const window = await renderSnapshot({file: 'Loose.lean', sha256: 'a'.repeat(64), checking: [], claims: [], cells: [], revisions: []}, {onGit: () => ({state: 'no_repository', reason: 'Not in a Git repository.'})});
  try {
    window.document.getElementById('history').open = true;
    await new Promise(resolve => setTimeout(resolve, 30));
    const panel = window.document.getElementById('history-panel');
    assert.match(panel.textContent, /Not in a Git repository/);
    assert.match(panel.textContent, /Source hashes and notebook activity work without Git/);
    assert.match(panel.textContent, /No recorded notebook activity/);
    assert.doesNotMatch(panel.textContent, /Every save|No revisions yet/);
  } finally { window.close(); }
});

test('imports and setup stay in Source while definitions read as mathematical cards', async () => {
  const header = 'import Mathlib.Tactic.NormNum\nimport Gimle.Forseti.LinearCircuitHoare';
  const body = [
    'namespace Examples.Jordan',
    '',
    'open Gimle.Asgard Gimle.Forseti',
    '',
    '/-- Two coordinates with update `(I + S)/2`,',
    'reading the last coordinate. -/',
    'def jordanLeft2 : Program 2 where',
    '  readout := .var 1',
    '',
    '  matrix := ![![1 / 2, 0], ![1 / 2, 1 / 2]]',
    '',
    '-- A comment before an undocumented definition.',
    '@[simp]',
    'noncomputable def jordanRight2 : Program 2 := jordanLeft2',
    '',
    '#check jordanLeft2',
  ].join('\n');
  const snapshot = {
    file: 'Jordan.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    cells: [
      {index: 0, kind: 'lean', start: 1, end: 2, text: header},
      {index: 1, kind: 'prose', start: 3, end: 5, text: '/-! # Jordan -/', markdown: '# Jordan'},
      {index: 2, kind: 'lean', start: 6, end: 21, text: body},
      {index: 3, kind: 'lean', start: 22, end: 22, text: 'end Examples.Jordan'},
    ],
    claims: [], links: [], revisions: [], assets: [],
  };
  const window = await renderSnapshot(snapshot);
  try {
    const doc = window.document.getElementById('doc');
    assert.equal(doc.querySelectorAll('.setup-cell,.lean-cell').length, 0);
    assert.ok([...doc.querySelectorAll('.definition')].every(card => card.querySelector('.def-namespace').textContent === 'Namespace Examples.Jordan'));

    const cards = [...doc.querySelectorAll('.definition')];
    assert.equal(cards.length, 2);
    const [left, right] = cards;
    assert.match(left.querySelector('.def-head').textContent, /Definition\s+jordanLeft2\s+: Program 2\s+L10–15/);
    assert.match(left.querySelector('.def-body').textContent, /Two coordinates with update \(I \+ S\)\/2, reading the last coordinate\./);
    assert.equal(left.querySelector('.def-body code').textContent, '(I + S)/2');
    assert.equal(left.querySelector('.card-signature').textContent, ': Program 2');
    const lean = left.querySelector('details.def-lean');
    assert.ok(!lean.open);
    assert.match(lean.querySelector('pre').textContent, /^\/-- Two coordinates[\s\S]*def jordanLeft2 : Program 2 where[\s\S]*matrix := /);
    assert.match(lean.querySelector('summary').textContent, /Lean source · 6 lines/);
    assert.match(right.querySelector('.def-head').textContent, /Definition\s+jordanRight2\s+: Program 2\s+L17–19/);
    assert.match(right.querySelector('.def-body').textContent, /A comment before an undocumented definition/);
    assert.equal(right.querySelector('.card-body .formal-expression').textContent, 'jordanLeft2');
    assert.match(right.querySelector('details.def-lean pre').textContent, /-- A comment before[\s\S]*@\[simp\]\nnoncomputable def/);

    assert.doesNotMatch(doc.textContent, /#check jordanLeft2/);
  } finally { window.close(); }
});

test('the textbook view gives uncertain syntax and unfinished declarations explicit cards', async () => {
  const cells = [
    'open Nat\n  axiom cheat : 1 = 2',
    '/-- Looks finished. -/\ndef bad : Nat := sorry',
    '/-- The answer. -/ def answer : Nat := 42',
    '/- outer\n/- inner -/\ndef fake : Nat := 0\n-/',
    'def fib : Nat → Nat\n| 0 => 0\n| n+1 => n',
    'def step (x : Nat := 3) : Nat := x',
    'def later : Nat := by\n  have h := 1\n-- then\n  exact h\n\ndef after : Nat := 2',
    'open Classical in\ndef pick : Nat := 0',
    'set_option autoImplicit true',
  ];
  let start = 1;
  const snapshot = {
    file: 'Hostile.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    cells: cells.map((text, index) => {
      const cell = {index, kind: 'lean', start, end: start + text.split('\n').length - 1, text};
      start = cell.end + 1;
      return cell;
    }),
    claims: [], links: [], revisions: [], assets: [],
  };
  const window = await renderSnapshot(snapshot);
  try {
    const doc = window.document.getElementById('doc');
    assert.equal(doc.querySelectorAll('details.setup').length, 0);
    const names = [...doc.querySelectorAll('.definition dfn')].map(d => d.textContent);
    assert.deepEqual(names, ['bad', 'fib', 'step', 'later', 'after', 'pick']);
    const plain = [...doc.querySelectorAll('.def-lean pre')].map(pre => pre.textContent);
    for (const text of ['axiom cheat', 'def bad : Nat := sorry', 'def answer', 'def fake', 'set_option autoImplicit']) {
      assert.ok(plain.some(p => p.includes(text)), text);
    }
    const card = name => [...doc.querySelectorAll('.definition')].find(c => c.querySelector('dfn').textContent === name);
    assert.equal(card('fib').querySelector('.card-signature').textContent, ': Nat → Nat');
    assert.match(card('fib').querySelector('summary').textContent, /3 lines/);
    assert.equal(card('step').querySelector('.card-signature').textContent, '(x : Nat := 3) : Nat');
    assert.match(card('later').querySelector('summary').textContent, /4 lines/);
    assert.equal(card('pick').querySelector('.card-body .formal-expression').textContent, '0');
  } finally { window.close(); }
});

test('TeX in prose, docstrings and claims is typeset, and stays readable without KaTeX', async () => {
  const prose = 'Compare $A = \\tfrac12(I + S)$ with `$literal$`.\n\n$$ x^+ = A x $$\n\nBroken: $\\frac{1$.';
  const snapshot = {
    file: 'Math.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    cells: [
      {index: 0, kind: 'prose', start: 1, end: 7, text: '/-! -/', markdown: prose},
      {index: 1, kind: 'lean', start: 8, end: 9, text: '/-- The update $B = I/2 + S$. -/\ndef right : Nat := 1'},
      {index: 2, kind: 'claim', id: 'agree', start: 10, end: 12, text: ''},
    ],
    claims: [{name: 'agree', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), doc: 'Outputs agree: $o_A = o_B$.', record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}}],
    links: [], revisions: [], assets: [],
  };
  let window = await renderSnapshot(snapshot, {katex: true});
  try {
    const doc = window.document.getElementById('doc');
    const maths = [...doc.querySelectorAll('.prose .math,.def-body .math,.doc-line .math')];
    assert.deepEqual(maths.map(m => m.dataset.tex), ['A = \\tfrac12(I + S)', 'x^+ = A x', '\\frac{1', 'B = I/2 + S', 'o_A = o_B']);
    const [inline, display, broken, docstring, claim] = maths;
    assert.ok(inline.classList.contains('typeset') && inline.querySelector('.katex'));
    assert.ok(display.querySelector('.katex-display'));
    assert.ok(docstring.closest('.def-body') && docstring.querySelector('.katex'));
    assert.ok(claim.closest('.doc-line') && claim.querySelector('.katex'));
    assert.ok(!broken.classList.contains('typeset'));
    assert.equal(broken.textContent, '\\frac{1');
    assert.match(broken.title, /could not typeset/);
    assert.equal(doc.querySelector('.prose code').textContent, '$literal$');
    assert.ok(window.document.querySelector('#insp .math .katex'));
  } finally { window.close(); }
  snapshot.cells[0].markdown = `Huge: $${'a+'.repeat(1500)}a$ and $\\rule{1e5em}{1e5em}$.`;
  window = await renderSnapshot(snapshot, {katex: true});
  try {
    const [huge, rule] = window.document.querySelectorAll('#doc .prose .math');
    assert.ok(!huge.classList.contains('typeset'));
    assert.match(huge.title, /longer than 2000 characters/);
    const box = rule.querySelector('.rule');
    assert.ok(box && parseFloat(box.style.borderRightWidth) <= 10, box && box.style.borderRightWidth);
  } finally { window.close(); }
  snapshot.cells[0].markdown = prose;
  window = await renderSnapshot(snapshot);
  try {
    const inline = window.document.querySelector('#doc .math');
    assert.equal(inline.textContent, 'A = \\tfrac12(I + S)');
    assert.ok(!inline.classList.contains('typeset'));
  } finally { window.close(); }
});

test('cell editing selects source and reverse navigation reveals the rendered cell', async () => {
  const claim = {name: 'simple', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}};
  const snapshot = {
    file: 'Example.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    cells: [{id: 'simple', index: 0, kind: 'claim', start: 1, end: 2, text: 'theorem simple : True := by\n  trivial'}],
    claims: [claim], links: [], revisions: [], assets: [],
  };
  const window = await renderSnapshot(snapshot);
  try {
    const sent = [];
    window.glitnirHost.openSource = cell => sent.push(cell);
    window.document.querySelector('[data-act="source"]').click();
    assert.equal(sent[0].index, 0);
    assert.equal(sent[0].sha256, snapshot.sha256);
    window.document.querySelector('.app').dispatchEvent(new window.KeyboardEvent('keydown', {key: 'e', bubbles: true}));
    assert.equal(sent.length, 2);
    let revealed;
    window.HTMLElement.prototype.scrollIntoView = function() { revealed = this; };
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 0, sha256: snapshot.sha256}}));
    assert.ok(revealed.classList.contains('source-selected'));
    assert.equal(revealed.dataset.index, '0');
  } finally { window.close(); }
});

test('reverse navigation reveals setup-only cells in Source', async () => {
  const snapshot = {
    file: 'Example.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    cells: [{index: 0, kind: 'lean', start: 1, end: 2, text: 'import Init\nopen Nat'}],
    claims: [], links: [], revisions: [], assets: [],
  };
  const window = await renderSnapshot(snapshot);
  try {
    assert.equal(window.document.querySelector('.cellwrap'), null);
    let revealed;
    window.HTMLElement.prototype.scrollIntoView = function() { revealed = this; };
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 0, sha256: snapshot.sha256}}));
    assert.equal(window.document.getElementById('source').hidden, false);
    assert.equal(revealed.id, 'source-L1');
    assert.equal(window.document.activeElement, revealed);
    assert.equal(window.document.querySelectorAll('[data-line-target="true"]').length, 2);
  } finally { window.close(); }
});

test('source handoff follows outline selection and does not reuse indices after insertion', async () => {
  const claim = name => ({name, keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: null});
  const cell = (id, index) => ({id, index, kind: 'claim', start: 1 + index * 2, end: 2 + index * 2, text: `theorem ${id} : True := by\n  trivial`});
  const snapshot = {file: 'Example.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, cells: [cell('first', 0), cell('second', 1)], claims: [claim('first'), claim('second')], links: [], revisions: [], assets: []};
  const window = await renderSnapshot(snapshot);
  try {
    window.HTMLElement.prototype.scrollIntoView = function() {};
    const sent = [];
    window.glitnirHost.openSource = cell => sent.push(cell);
    window.document.querySelector('[data-act="source"][data-index="0"]').click();
    window.document.querySelector('[data-act="goto"][data-id="second"]').click();
    window.document.getElementById('open-source').click();
    assert.equal(sent.at(-1).index, 1);
    window.document.querySelector('[data-act="source"][data-index="1"]').click();
    snapshot.sha256 = 'b'.repeat(64);
    snapshot.cells = [cell('new', 0), cell('first', 1), cell('second', 2)];
    snapshot.claims.unshift(claim('new'));
    await window.glitnirPoll();
    window.document.getElementById('open-source').click();
    assert.equal(sent.at(-1).index, 2, 'the old index now belongs to first, not second');
    assert.equal(sent.at(-1).sha256, snapshot.sha256);
  } finally { window.close(); }
});

// A project-mode snapshot listing evidence directives as the server does.
function evidenceSnapshot(text, evidence, extra = {}) {
  return {file: 'E.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: text.split('\n').length, text}], views: [], evidence,
    directive_providers: {evidence: ['simulate'], decide: []}, project_claims: [], references: [], revisions: [], assets: [],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}}, ...extra};
}
function evidenceEntry(fields = {}) {
  return {cell: 0, line: 2, verb: 'simulate', target: 'M', key: 'k'.repeat(64), provider: 'demo', error: '',
    unavailable: null, status: 'never', run_error: null, snapshot: null, ...fields};
}
// A decision directive as the server lists it; without a provider by default.
function decisionEntry(fields = {}) {
  return {cell: 0, line: 2, end_line: 2, name: 'band', kind: 'entailment', goal: 'entailment x : x > 0 |- x >= 0', key: 'd'.repeat(64),
    provider: null, error: 'No installed provider decides `entailment` goals.', unavailable: null, missing_imports: [], status: 'never',
    run_error: null, outcome: null, verification: null, ...fields};
}
function evidenceBody(fields = {}) {
  const times = [0, 0.5, 1];
  return {format: 'glitnir.evidence/v1', series: {times, values: times.map(t => [t, -t]), names: ['x', 'y']},
    provenance: [['solver', 'rk4'], ['step', '1/2']], finite: true, ...fields};
}
const EVIDENCE_TEXT = 'def M := 1\n-- @evidence simulate M (h := 1/10)';

test('a phase plane draws two series against each other beside the time series', async () => {
  const n = 500;
  const times = Array.from({length: n}, (_, i) => i / 10);
  const values = times.map(t => [Math.cos(t), Math.sin(t), t]);
  const body = plane => evidenceBody({series: {times, values, names: ['x', 'y', 'z']}, ...(plane ? {plane} : {})});
  const snapshot = plane => evidenceSnapshot(EVIDENCE_TEXT, [evidenceEntry({status: 'current', snapshot: body(plane)})]);
  let window = await renderSnapshot(snapshot([0, 1]));
  try {
    const card = window.document.querySelector('#doc .evidence');
    const phase = card.querySelector('.pane.phase svg');
    assert.ok(phase, 'the phase panel is drawn');
    assert.match(phase.getAttribute('aria-label'), /x against y/);
    const orbit = phase.querySelector('path.orbit').getAttribute('d');
    assert.equal((orbit.match(/L/g) || []).length, n - 1);
    assert.ok(phase.querySelector('circle'), 'the start is marked');
    assert.equal(card.querySelectorAll('.evidence-charts .pane').length, 2, 'the time series stays');
    assert.match(phase.textContent, /1\.0 x/);
  } finally { window.close(); }
  window = await renderSnapshot(snapshot(null));
  try {
    const card = window.document.querySelector('#doc .evidence');
    assert.equal(card.querySelector('.pane.phase'), null);
    assert.equal(card.querySelectorAll('.evidence-charts .pane').length, 1);
  } finally { window.close(); }
});

test('missing values break the drawn series and non-finite runs say so', async () => {
  const series = {times: [0, 1, 2, 3, 4, 5], values: [[0, null], [1, null], [null, null], [3, null], [4, null], [null, null]], names: ['x', 'y']};
  let window = await renderSnapshot(evidenceSnapshot(EVIDENCE_TEXT, [evidenceEntry({status: 'current', snapshot: evidenceBody({series, plane: [0, 1], finite: false})})]));
  try {
    const card = window.document.querySelector('#doc .evidence');
    const path = card.querySelector('.evidence-charts path[stroke-width="2"]').getAttribute('d');
    assert.equal((path.match(/M/g) || []).length, 2, 'the missing value starts a new segment');
    assert.doesNotMatch(path, /NaN/);
    assert.match(card.textContent, /Some values were not finite/);
    assert.equal(card.querySelector('.pane.phase'), null);
    assert.match(card.textContent, /The phase plane of x against y has no finite values to draw/);
  } finally { window.close(); }
  const single = {times: [0, 1, 2], values: [[1], [null], [2]], names: ['x']};
  window = await renderSnapshot(evidenceSnapshot(EVIDENCE_TEXT, [evidenceEntry({status: 'current', snapshot: evidenceBody({series: single})})]));
  try {
    const card = window.document.querySelector('#doc .evidence');
    assert.equal(card.querySelector('.evidence-charts path[stroke-width="2"]').getAttribute('d'), '');
    assert.equal(card.querySelectorAll('.evidence-charts circle.dot-sample').length, 2, 'each isolated sample is a dot');
  } finally { window.close(); }
});

test('the largest evidence the server accepts is drawn', async () => {
  const times = Array.from({length: 4000}, (_, i) => i);
  const names = Array.from({length: 16}, (_, k) => `s${k}`);
  const series = {times, values: times.map(t => names.map((_, k) => t * k)), names};
  const window = await renderSnapshot(evidenceSnapshot(EVIDENCE_TEXT, [evidenceEntry({status: 'current', snapshot: evidenceBody({series, plane: [1, 15]})})]));
  try {
    const card = window.document.querySelector('#doc .evidence');
    assert.equal(card.querySelectorAll('.evidence-charts path[stroke-width="2"]').length, 16);
    assert.ok(card.querySelector('.pane.phase path.orbit'));
  } finally { window.close(); }
});

test('evidence cards show each status, provenance and the run action', async () => {
  const cases = [
    ['never', null, 'Never run', 'Run', /Not run yet/],
    ['current', evidenceBody(), 'Current', 'Run again', null],
    ['stale', evidenceBody(), 'Out of date', 'Run again', /Out of date: the source or its environment changed/],
    ['running', evidenceBody(), 'Running', null, /Running in Lean and the provider/],
  ];
  for (const [status, snapshot, label, button, note] of cases) {
    const window = await renderSnapshot(evidenceSnapshot(EVIDENCE_TEXT, [evidenceEntry({status, snapshot})]));
    try {
      const card = window.document.querySelector('#doc .evidence');
      assert.equal(card.querySelector('.box-head .pill').textContent, label, status);
      assert.match(card.querySelector('.box-head').textContent, /simulate M · demo/);
      const run = card.querySelector('[data-act="evidence-run"]');
      assert.equal(run?.textContent ?? null, button, status);
      if (note) assert.match(card.textContent, note);
      if (status === 'running') assert.equal(card.querySelector('.evidence-charts'), null, 'a running card waits for its result');
      else if (snapshot) {
        assert.ok(card.querySelector('.evidence-charts svg'));
        assert.equal(card.querySelector('.evidence-charts .pane').classList.contains('dim'), status === 'stale');
        assert.deepEqual([...card.querySelectorAll('.evidence-provenance tr')].map(row => [row.querySelector('th').textContent, row.querySelector('td').textContent]), [['solver', 'rk4'], ['step', '1/2']]);
      }
      assert.equal(window.document.querySelector('#doc pre').closest('section').nextElementSibling, card, 'the card follows its source cell');
    } finally { window.close(); }
  }
});

test('the run action posts the saved source digest and the directive line', async () => {
  const requests = [];
  let reread;
  const refreshed = new Promise(resolve => { reread = resolve; });
  const onMessage = message => {
    requests.push(message);
    if (message.path === '/api/notebook' && requests.some(r => r.path === '/api/evidence/run')) reread();
  };
  const window = await renderSnapshot(evidenceSnapshot(EVIDENCE_TEXT, [evidenceEntry()]), {onMessage});
  try {
    const button = window.document.querySelector('[data-act="evidence-run"]');
    assert.equal(button.dataset.sha, 'a'.repeat(64));
    // The digest the card was drawn for is posted, never whatever the renderer
    // holds when the click arrives: make the two differ.
    button.dataset.sha = 'b'.repeat(64);
    button.click();
    await refreshed;
    const run = requests.filter(r => r.path === '/api/evidence/run');
    assert.equal(run.length, 1);
    assert.equal(run[0].method, 'POST');
    assert.deepEqual(JSON.parse(run[0].body), {sha256: 'b'.repeat(64), line: 2});
  } finally { window.close(); }
});

test('polling speeds up while an evidence run is in progress', async () => {
  let interval;
  const entry = evidenceEntry({status: 'running'});
  const snapshot = evidenceSnapshot(EVIDENCE_TEXT, [entry]);
  const window = await renderSnapshot(snapshot, {onPoll: (_, delay) => { interval = delay; }});
  try {
    assert.equal(interval, 1000);
    entry.status = 'current'; entry.snapshot = evidenceBody();
    await window.glitnirPoll();
    assert.equal(interval, 4000);
    assert.equal(window.document.querySelector('#doc .evidence .pill').textContent, 'Current');
  } finally { window.close(); }
});

test('evidence errors, failed runs and unavailable providers stay on their own card', async () => {
  const text = 'def M := 1\n-- @evidence simulate M (h := oops)\n-- @evidence simulate M\n-- @evidence simulate M (h := 1)\n-- @evidence simulate M (h := 2)';
  const hostile = '<img src=x onerror=alert(1)>';
  const window = await renderSnapshot(evidenceSnapshot(text, [
    evidenceEntry({line: 2, error: `Bad option ${hostile}`}),
    evidenceEntry({line: 3, status: 'current', run_error: `crashed ${hostile}`, snapshot: evidenceBody({
      series: {times: [0], values: [[1]], names: [hostile]}, provenance: [[hostile, hostile]]})}),
    evidenceEntry({line: 4, status: 'current', unavailable: 'Demo support is not installed.', snapshot: evidenceBody()}),
    evidenceEntry({line: 5, verb: hostile, target: hostile, provider: hostile, unavailable: `Missing ${hostile}`}),
  ]));
  try {
    const cards = [...window.document.querySelectorAll('#doc .evidence')];
    assert.equal(cards.length, 4);
    assert.equal((cards[3].querySelector('.box-head').textContent.match(/<img/g) || []).length, 3, 'verb, target and provider are text');
    assert.match(cards[3].textContent, /Missing <img/);
    assert.match(cards[0].textContent, /Bad option <img/);
    assert.equal(cards[0].querySelector('[data-act="evidence-run"]'), null);
    assert.equal(cards[0].querySelector('.pill'), null);
    assert.match(cards[1].textContent, /The last run failed: crashed <img/);
    assert.ok(cards[1].querySelector('[data-act="evidence-run"]'), 'a failed run may be tried again');
    assert.equal(window.document.querySelectorAll('#doc img').length, 0, 'provider text is never markup');
    assert.match(cards[1].querySelector('.evidence-provenance th').textContent, /<img/);
    assert.match(cards[2].textContent, /Demo support is not installed\. Saved evidence remains viewable/);
    assert.ok(cards[2].querySelector('.evidence-charts svg'));
    assert.equal(cards[2].querySelector('[data-act="evidence-run"]'), null);
  } finally { window.close(); }
});

test('saved evidence without its provider still says when it is out of date', async () => {
  const text = 'def M := 1\n-- @evidence orbit M (n := 2)';
  const reason = 'No installed provider runs `@evidence orbit`.';
  const window = await renderSnapshot(evidenceSnapshot(text, [
    evidenceEntry({verb: 'orbit', provider: null, error: reason, status: 'stale', snapshot: evidenceBody()}),
  ]), {katex: true});
  const shows = (card, where) => {
    const pill = card.querySelector('.pill');
    assert.deepEqual([pill.textContent, pill.dataset.tone], ['Out of date', 'amber'], where);
    const notes = [...card.querySelectorAll('.note-line')].map(n => n.textContent);
    assert.equal(notes[0], `${reason} Saved evidence remains viewable.`, where);
    assert.match(notes[1], /^Out of date: the source or its environment changed since this run\.$/, where);
    assert.ok(card.querySelector('.evidence-charts .pane.dim'), where);
    assert.equal(card.querySelector('[data-act="evidence-run"]'), null, where);
  };
  try {
    shows(window.document.querySelector('#doc .evidence'), 'preview');
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try { shows(page.window.document.querySelector('.evidence'), 'export'); } finally { page.window.close(); }
  } finally { window.close(); }
});

test('an errored evidence card shows its error, then at most the failed run', async () => {
  const text = 'def M := 1\n-- @evidence simulate M (h := oops)';
  const window = await renderSnapshot(evidenceSnapshot(text, [evidenceEntry({error: 'Bad option.', status: 'stale',
    unavailable: 'Demo support is not installed.', run_error: 'crashed', snapshot: evidenceBody()})]));
  try {
    const notes = [...window.document.querySelectorAll('#doc .evidence .note-line')].map(n => n.textContent);
    assert.deepEqual(notes, ['Bad option. Saved evidence remains viewable.', 'The last run failed: crashed']);
  } finally { window.close(); }
});

test('a directive without its provider shows why and keeps saved evidence', async () => {
  const text = 'def M := 1\n-- @evidence orbit M\n-- @evidence orbit M (n := 2)';
  const reason = 'No installed provider runs `@evidence orbit`. (Provider demo failed to load: <b>boom</b>)';
  const window = await renderSnapshot(evidenceSnapshot(text, [
    evidenceEntry({line: 2, verb: 'orbit', provider: null, error: reason}),
    evidenceEntry({line: 3, verb: 'orbit', provider: null, error: reason, status: 'current', snapshot: evidenceBody()}),
  ]), {katex: true});
  try {
    const doc = window.document.getElementById('doc');
    const note = doc.querySelector('.note-line[data-document-start="2"]');
    assert.match(note.textContent, /Provider demo failed to load: <b>boom<\/b>/);
    assert.equal(doc.querySelectorAll('b').length, 0, 'the reason is text');
    const cards = [...doc.querySelectorAll('.evidence')];
    assert.deepEqual(cards.map(card => card.dataset.documentStart), ['3'], 'saved evidence keeps its card');
    assert.ok(cards[0].querySelector('.evidence-charts svg'));
    assert.match(cards[0].textContent, /failed to load: <b>boom<\/b>\)? Saved evidence remains viewable/);
    const pill = cards[0].querySelector('.pill');
    assert.deepEqual([pill.textContent, pill.dataset.tone], ['Saved', 'muted'], 'nothing can say it is current');
    assert.equal(cards[0].querySelector('[data-act="evidence-run"]'), null, 'nothing can run it');
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const d = page.window.document;
      assert.equal(d.querySelectorAll('.evidence .evidence-charts svg').length, 1);
      assert.match(d.body.textContent, /Provider demo failed to load/);
      assert.equal(d.querySelectorAll('button,b').length, 0);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('export shows saved evidence with its status and no run action', async () => {
  const text = 'def M := 1\n-- @evidence simulate M\n-- @evidence simulate M (h := 1)';
  const window = await renderSnapshot(evidenceSnapshot(text, [
    evidenceEntry({line: 2, status: 'stale', snapshot: evidenceBody()}),
    evidenceEntry({line: 3, status: 'running', snapshot: evidenceBody()}),
  ]), {katex: true});
  try {
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const d = page.window.document;
      const cards = [...d.querySelectorAll('.evidence')];
      assert.equal(cards.length, 2);
      assert.equal(cards[0].querySelector('.pill').textContent, 'Out of date');
      assert.equal(cards[1].querySelector('.pill').textContent, 'Running at export');
      assert.match(cards[1].textContent, /A run was in progress at export time/);
      assert.equal(d.querySelectorAll('.evidence-charts svg').length, 2, 'each card keeps its saved chart');
      assert.match(d.body.textContent, /Evidence · not proof/);
      assert.match(d.body.textContent, /solver/);
      assert.equal(d.querySelectorAll('button,script,.claim-actions').length, 0);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

const DECISION_TEXT = 'def M := 1\n-- @decide band : entailment x : x > 0 |- x >= 0';
function decisionSnapshot(text, decisions, extra = {}) {
  return evidenceSnapshot(text, [], {decisions, directive_providers: {evidence: [], decide: ['entailment']}, ...extra});
}
function servedDecision(fields = {}) {
  return decisionEntry({provider: 'demo', error: '', ...fields});
}
function decisionOutcome(fields = {}) {
  return {status: 'proven', reason: 'linear arithmetic', stages: [['search', 'found', 'linarith'], ['replay', 'ok', '']],
    block: 'theorem band : ∀ x : ℝ, x > 0 → x ≥ 0 := by\n  intro x h\n  linarith', theorem: 'band', ...fields};
}
function checkedVerification(fields = {}) {
  return {status: 'checked', message: '', axioms: ['propext', 'Classical.choice'], type: '∀ (x : Real), GT.gt x 0 → GE.ge x 0', ...fields};
}
const decisionButtons = card => [...card.querySelectorAll('.claim-actions button')].map(button => button.textContent);

test('decision cards show each status, the saved outcome and only the allowed actions', async () => {
  const unknown = decisionOutcome({status: 'unknown', reason: 'nonlinear goal', block: null, theorem: null});
  const cases = [
    ['never', null, null, 'Never run', ['Run'], /Not run yet/],
    ['running', decisionOutcome(), checkedVerification(), 'Running', ['Stop'], /Running the provider/],
    ['unknown', unknown, null, 'Unknown', ['Run again', 'Discard'], /Not decided: nonlinear goal\. This is not a refutation/],
    ['rejected', decisionOutcome(), checkedVerification({status: 'rejected', message: 'type mismatch', type: null, axioms: []}), 'Rejected', ['Run again', 'Discard'], /The kernel check rejected it: type mismatch/],
    ['candidate', decisionOutcome(), checkedVerification(), 'Candidate', ['Accept', 'Run again', 'Discard'], /Nothing is written until it is accepted/],
    ['accepted', decisionOutcome(), checkedVerification(), 'Accepted', ['Discard'], /Accepted: this block follows the directive/],
    ['stale', decisionOutcome(), checkedVerification(), 'Out of date', ['Run again', 'Discard'], /Out of date: the source through this directive or its environment changed/],
  ];
  for (const [status, outcome, verification, label, buttons, note] of cases) {
    const window = await renderSnapshot(decisionSnapshot(DECISION_TEXT, [servedDecision({status, outcome, verification})]));
    try {
      const card = window.document.querySelector('#doc .decision');
      assert.equal(card.querySelector('.box-head .pill').textContent, label, status);
      assert.match(card.querySelector('.box-head').textContent, /band · entailment · demo/);
      assert.equal(card.querySelector('.decision-goal').textContent, 'entailment x : x > 0 |- x >= 0');
      assert.deepEqual(decisionButtons(card), buttons, status);
      assert.match(card.textContent, note, status);
      assert.equal(window.document.querySelector('#doc pre').closest('section').nextElementSibling, card, 'the card follows its source cell');
      const block = card.querySelector('.decision-block');
      if (status === 'running') assert.equal(block, null, 'a running card waits for its result');
      else if (outcome?.block) {
        assert.equal(block.textContent, outcome.block, 'the whole block is readable');
        assert.deepEqual([...card.querySelectorAll('.decision-stages tbody tr')].map(row => [...row.cells].map(c => c.textContent)), [['search', 'found', 'linarith'], ['replay', 'ok', '']]);
      }
      const kernel = [...card.querySelectorAll('.decision-field')].find(field => /Kernel statement/.test(field.textContent));
      if (['candidate', 'accepted', 'stale'].includes(status)) {
        assert.equal(kernel.querySelector('pre').textContent, '∀ (x : Real), GT.gt x 0 → GE.ge x 0');
        assert.match(card.querySelector('.decision-axioms').textContent, /Assumptions: propext, Classical\.choice/);
        assert.match(card.querySelector('.decision-claim').textContent, /proposed a proof, theorem band: linear arithmetic/);
      } else assert.equal(kernel, undefined, status);
      if (status === 'candidate') assert.equal(card.querySelector('[data-act="accept"]').dataset.key, 'd'.repeat(64));
    } finally { window.close(); }
  }
});

test('decision actions post the saved digest and line, and accept goes only to the editor', async () => {
  const requests = [];
  const window = await renderSnapshot(decisionSnapshot(DECISION_TEXT, [servedDecision({status: 'candidate', outcome: decisionOutcome(), verification: checkedVerification()})]), {onMessage: message => requests.push(message)});
  const settle = () => new Promise(resolve => setTimeout(resolve, 30));
  try {
    const card = () => window.document.querySelector('#doc .decision');
    assert.equal(card().querySelector('[data-act="decision-run"]').dataset.sha, 'a'.repeat(64));
    card().querySelector('[data-act="decision-run"]').click();
    await settle();
    card().querySelector('[data-act="decision-discard"]').click();
    await settle();
    card().querySelector('[data-act="accept"]').click();
    await settle();
    const posts = requests.filter(r => r.method === 'POST');
    assert.deepEqual(posts.map(r => [r.path, JSON.parse(r.body)]), [
      ['/api/decide/run', {sha256: 'a'.repeat(64), line: 2}],
      ['/api/decide/discard', {sha256: 'a'.repeat(64), line: 2}],
    ]);
    assert.deepEqual(requests.filter(r => r.type === 'acceptDecision').map(r => ({...r})), [{type: 'acceptDecision', key: 'd'.repeat(64)}]);
    assert.equal(requests.filter(r => /accept/.test(r.path || '')).length, 0, 'accepting is never a server call');
  } finally { window.close(); }
});

test('a running decision can be stopped, even with unsaved edits, and polling speeds up', async () => {
  let interval;
  const requests = [];
  const entry = servedDecision({status: 'running'});
  const window = await renderSnapshot(decisionSnapshot(DECISION_TEXT, [entry]), {onPoll: (_, delay) => { interval = delay; }, onMessage: message => requests.push(message)});
  try {
    assert.equal(interval, 1000);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    window.document.querySelector('#doc .decision [data-act="decision-stop"]').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(requests.filter(r => r.method === 'POST').map(r => [r.path, JSON.parse(r.body)]), [['/api/decide/stop', {}]]);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: false}}));
    Object.assign(entry, {status: 'unknown', outcome: decisionOutcome({status: 'unknown', reason: 'gave up', block: null, theorem: null})});
    await window.glitnirPoll();
    assert.equal(interval, 4000);
    assert.equal(window.document.querySelector('#doc .decision .pill').textContent, 'Unknown');
  } finally { window.close(); }
});

test('decision text from providers and the server is never markup', async () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const text = 'def M := 1\n-- @decide band : entailment x\n-- @decide other : entailment y\n-- @decide third : entailment z';
  const window = await renderSnapshot(decisionSnapshot(text, [
    servedDecision({line: 2, end_line: 2, name: hostile, kind: hostile, provider: hostile, goal: hostile, key: `"${hostile}`, status: 'candidate', run_error: `crashed ${hostile}`,
      outcome: decisionOutcome({reason: hostile, stages: [[hostile, hostile, hostile]], block: hostile, theorem: hostile}),
      verification: checkedVerification({type: hostile, axioms: [hostile]})}),
    servedDecision({line: 3, end_line: 3, status: 'rejected', outcome: decisionOutcome(), verification: checkedVerification({status: 'rejected', message: hostile}), unavailable: `Missing ${hostile}`}),
    servedDecision({line: 4, end_line: 4, error: `Bad goal ${hostile}`}),
  ]));
  try {
    const cards = [...window.document.querySelectorAll('#doc .decision')];
    assert.equal(cards.length, 3);
    assert.equal(window.document.querySelectorAll('#doc img').length, 0, 'provider text is never markup');
    assert.equal((cards[0].querySelector('.box-head').textContent.match(/<img/g) || []).length, 3, 'name, kind and provider are text');
    assert.equal(cards[0].querySelector('.decision-goal').textContent, hostile);
    assert.equal(cards[0].querySelector('.decision-block').textContent, hostile);
    assert.equal(cards[0].querySelector('[data-act="accept"]').dataset.key, `"${hostile}`, 'the key is an attribute value, never markup');
    assert.match(cards[0].textContent, /The last run failed: crashed <img/);
    assert.equal(cards[0].querySelectorAll('.decision-stages td').length, 3);
    assert.ok([...cards[0].querySelectorAll('.decision-stages td')].every(td => td.textContent === hostile));
    assert.ok([...cards[0].querySelectorAll('.decision-field pre')].some(pre => pre.textContent === hostile), 'the kernel statement is text');
    assert.match(cards[1].textContent, /Missing <img[^]*The saved outcome remains viewable/);
    assert.match(cards[1].textContent, /The kernel check rejected it: <img/);
    assert.deepEqual(decisionButtons(cards[1]), ['Discard'], 'an unavailable provider cannot run');
    assert.match(cards[2].textContent, /Bad goal <img/);
    assert.equal(cards[2].querySelector('.pill').textContent, 'Error');
    assert.deepEqual(decisionButtons(cards[2]), [], 'a directive in error cannot run');
  } finally { window.close(); }
});

test('a decision without its provider keeps its saved outcome but cannot run or be accepted', async () => {
  const text = 'def M := 1\n-- @decide band : entailment x : x > 0 |- x >= 0\n-- @decide other : entailment x : x > 1 |- x >= 0';
  const reason = 'No installed provider decides `entailment` goals.';
  const window = await renderSnapshot(decisionSnapshot(text, [
    decisionEntry({line: 2, end_line: 2, error: reason}),
    decisionEntry({line: 3, end_line: 3, name: 'other', error: reason, status: 'candidate', outcome: decisionOutcome({theorem: 'other'}), verification: checkedVerification()}),
  ], {directive_providers: {evidence: [], decide: []}}));
  try {
    const doc = window.document.getElementById('doc');
    assert.match(doc.querySelector('.note-line[data-document-start="2"]').textContent, /No installed provider decides/);
    const cards = [...doc.querySelectorAll('.decision')];
    assert.deepEqual(cards.map(card => card.dataset.documentStart), ['3'], 'a saved outcome keeps its card');
    assert.equal(cards[0].querySelector('.pill').textContent, 'Candidate');
    assert.match(cards[0].textContent, /No installed provider decides `entailment` goals\. The saved outcome remains viewable/);
    assert.ok(cards[0].querySelector('.decision-block'));
    assert.deepEqual(decisionButtons(cards[0]), ['Discard']);
  } finally { window.close(); }
});

test('the editor preview styles never hide decision or evidence actions', async () => {
  const css = readFileSync(path.resolve(__dirname, '../media/preview.css'), 'utf8');
  for (const [, selector, body] of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (/display\s*:\s*none|visibility\s*:\s*hidden/.test(body)) assert.doesNotMatch(selector, /data-act|claim-actions/, selector.trim());
  }
  const window = await renderSnapshot(decisionSnapshot(DECISION_TEXT, [servedDecision({status: 'candidate', outcome: decisionOutcome(), verification: checkedVerification()})]));
  try {
    const style = window.document.createElement('style');
    style.textContent = css;
    window.document.head.append(style);
    window.document.body.classList.add('glitnir-preview');
    for (const act of ['accept', 'decision-run', 'decision-discard']) {
      const button = window.document.querySelector(`#doc .decision [data-act="${act}"]`);
      assert.notEqual(window.getComputedStyle(button).display, 'none', act);
    }
  } finally { window.close(); }
});

test('a decision record missing its stages or axioms still renders', async () => {
  const outcome = decisionOutcome(); delete outcome.stages;
  const verification = checkedVerification(); delete verification.axioms;
  const window = await renderSnapshot(decisionSnapshot(DECISION_TEXT, [servedDecision({status: 'candidate', outcome, verification})]));
  try {
    const card = window.document.querySelector('#doc .decision');
    assert.equal(card.querySelector('.decision-stages'), null);
    assert.match(card.querySelector('.decision-axioms').textContent, /Assumptions: none/);
    assert.equal(card.querySelector('.decision-block').textContent, outcome.block);
  } finally { window.close(); }
});

test('without the editor a candidate says where to accept it', async () => {
  const snapshot = decisionSnapshot(DECISION_TEXT, [servedDecision({status: 'candidate', outcome: decisionOutcome(), verification: checkedVerification()})]);
  const window = await renderSnapshot(snapshot, {browser: true});
  try {
    const card = window.document.querySelector('#doc .decision');
    assert.equal(card.querySelector('[data-act="accept"]'), null);
    assert.deepEqual(decisionButtons(card), ['Run again', 'Discard']);
    assert.match(card.textContent, /Accept it from the editor's preview/);
  } finally { window.close(); }
});

test('export shows decision status, outcome, kernel statement and block without actions', async () => {
  const text = 'def M := 1\n-- @decide band : entailment x : x > 0 |- x >= 0\n-- @decide other : entailment x : x > 1 |- x >= 0';
  const window = await renderSnapshot(decisionSnapshot(text, [
    servedDecision({line: 2, end_line: 2, status: 'candidate', outcome: decisionOutcome(), verification: checkedVerification()}),
    servedDecision({line: 3, end_line: 3, name: 'other', status: 'running', outcome: decisionOutcome({status: 'unknown', reason: 'gave up', block: null, theorem: null})}),
  ]), {katex: true});
  try {
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const d = page.window.document;
      const cards = [...d.querySelectorAll('.decision')];
      assert.equal(cards.length, 2);
      assert.equal(cards[0].querySelector('.pill').textContent, 'Candidate');
      assert.match(cards[0].textContent, /Kernel statement/);
      assert.match(cards[0].textContent, /GT\.gt x 0/);
      assert.equal(cards[0].querySelector('.decision-block').textContent, decisionOutcome().block);
      assert.equal(cards[0].querySelectorAll('.decision-stages tbody tr').length, 2);
      assert.equal(cards[1].querySelector('.pill').textContent, 'Running at export');
      assert.match(cards[1].textContent, /A run was in progress at export time[^]*Not decided: gave up/);
      assert.equal(d.querySelectorAll('button,script,.claim-actions,[data-key]').length, 0);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('Export HTML uses the whole saved notebook with statements and proofs even from source mode', async () => {
  const snapshot = {
    file: 'Math.lean', sha256: 'a'.repeat(64), checking: ['agree'], check_errors: {},
    cells: [
      {index: 0, kind: 'prose', start: 1, end: 7, text: '/-! -/', markdown: '# Offline\n\nA formula $x^2$.'},
      {index: 1, kind: 'claim', id: 'agree', start: 8, end: 10, text: 'theorem agree : True := by\n  trivial'},
    ],
    claims: [{name: 'agree', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'stale', reason: 'context changed', uses: [], context: 'c'.repeat(64), doc: 'Outputs agree: $o_A = o_B$.', record: null, refutation: {context: 'd'.repeat(64), verdict: 'holds', script: ['exact saved_negation']}}],
    links: [], revisions: [], assets: [],
  };
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const saved = [];
    window.glitnirHost.saveHTML = async html => saved.push(html);
    window.document.querySelector('[data-mode="source"]').click();
    await window.document.getElementById('export-html').onclick();
    assert.equal(saved.length, 1);
    const page = new JSDOM(saved[0]);
    try {
      const d = page.window.document;
      assert.match(d.body.textContent, /A formula/);
      assert.match(d.body.textContent, /theorem agree : True := by\s+trivial/);
      assert.match(d.body.textContent, /Out of date/);
      assert.doesNotMatch(d.body.textContent, /Checking in Lean/);
      assert.equal(d.querySelectorAll('.checking-badge').length, 0);
      assert.match(d.body.textContent, /Refutation · old context/);
      assert.match(d.body.textContent, /exact saved_negation/);
      assert.equal(d.querySelectorAll('.katex').length, 3);
      assert.equal(d.querySelector('.claim .def-lean').open, false);
      assert.equal(d.querySelectorAll('button,script').length, 0);
    } finally { page.window.close(); }
    assert.equal(window.document.getElementById('export-html').disabled, false);
  } finally { window.close(); }
});

test('export refuses unreadable notebook or metadata snapshots', async () => {
  for (const error of ['error', 'metadata_error']) {
    const window = await renderSnapshot({file: 'Broken.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, cells: [], claims: [], links: [], revisions: [], assets: [], [error]: 'Unreadable saved notebook'}, {katex: true});
    try {
      let saved = false;
      window.glitnirHost.saveHTML = async () => { saved = true; };
      await window.document.getElementById('export-html').onclick();
      assert.equal(saved, false);
      assert.match(window.document.body.textContent, /Unreadable saved notebook/);
      assert.equal(window.document.getElementById('export-html').disabled, false);
    } finally { window.close(); }
  }
});

test('reading folds keep both open and closed states through polling and view changes', async () => {
  const snapshot = {file: 'Folds.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 5, text: 'def value : Nat := 3\n\ndef second : Nat := 4'}],
  };
  let poll;
  const window = await renderSnapshot(snapshot, {onPoll: fn => { poll = fn; }});
  try {
    const d = window.document;
    d.querySelector('details.def-lean summary').click();
    d.querySelector('details.def-lean summary').focus();
    await poll();
    assert.equal(d.querySelector('details.def-lean').open, true);
    assert.equal(d.querySelectorAll('details.def-lean')[1].open, false);
    assert.equal(d.activeElement, d.querySelector('details.def-lean summary'));
    snapshot.cells[0].start += 10;
    snapshot.cells[0].end += 10;
    snapshot.sha256 = 'b'.repeat(64);
    await poll();
    assert.equal(d.querySelector('details.def-lean').open, true);
    snapshot.cells.push({index: 1, kind: 'lean', start: 30, end: 34, text: 'namespace Other\n\ndef value : Nat := 3\n\nend Other'});
    await poll();
    assert.equal(d.querySelectorAll('details.def-lean')[0].open, true);
    assert.equal(d.querySelectorAll('details.def-lean')[2].open, false, 'identical definitions have separate fold states');
    d.querySelector('details.def-lean summary').click();
    await poll();
    assert.equal(d.querySelector('details.def-lean').open, false);
    d.querySelector('[data-mode="source"]').click();
    await poll();
    d.querySelector('[data-mode="doc"]').click();
    assert.equal(d.querySelector('details.def-lean').open, false);
    d.getElementById('panel-hide').click();
  } finally { window.close(); }
});

test('definition namespace labels follow nested namespaces and sections across cells', async () => {
  const texts = [
    'namespace Outer',
    'namespace Inner\nsection local_section\ndef first : Nat := 1',
    'end local_section\nend Inner\ndef second : Nat := 2',
    'namespace «Other space»\ndef third : Nat := 3\nend «Other space»',
    'end Outer\ndef outside : Nat := 4',
  ];
  const snapshot = {file: 'Scopes.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: texts.flatMap((text, i) => [
      {index: i * 2, kind: 'lean', start: i * 20 + 1, end: i * 20 + text.split('\n').length, text},
      {index: i * 2 + 1, kind: 'prose', start: i * 20 + 10, end: i * 20 + 12, markdown: 'Between declarations'},
    ]),
  };
  const window = await renderSnapshot(snapshot);
  try {
    const doc = window.document.getElementById('doc');
    const cards = [...doc.querySelectorAll('.definition')];
    assert.deepEqual(cards.map(card => card.querySelector('.def-namespace')?.textContent || ''), [
      'Namespace Outer.Inner', 'Namespace Outer', 'Namespace Outer.«Other space»', '',
    ]);
    assert.ok([...doc.querySelectorAll('.setup-cell')].every(cell => !/namespace |end (?:Inner|Outer|«Other)/.test(cell.textContent)));
    assert.doesNotMatch(doc.textContent, /section local_section|end local_section/);
  } finally { window.close(); }
});


test('uncertain scope syntax stays in Lean and namespace labels escape quoted names', async () => {
  const snapshot = {file: 'Scopes.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 3, text: 'namespace «<img src=x onerror=alert(1)>»\ndef value : Nat := 1\nend «<img src=x onerror=alert(1)>»'}],
  };
  let poll;
  const window = await renderSnapshot(snapshot, {onPoll: fn => { poll = fn; }});
  try {
    const d = window.document;
    assert.equal(d.querySelector('.definition .def-namespace').textContent, 'Namespace «<img src=x onerror=alert(1)>»');
    assert.equal(d.querySelector('#doc img'), null);
    for (const text of [
      'namespace Outer\ndef value : Nat := 1\nend Wrong',
      'namespace Outer -- extra syntax\ndef value : Nat := 1\nend Outer',
      'open Nat in\nnamespace Outer\ndef value : Nat := 1\nend Outer',
      'namespace Outer\ndef text := "line\nnamespace Fake\nline"\nend Outer',
      'namespace Outer\n/- nested /- comment -/ -/\ndef value : Nat := 1\nend Outer',
    ]) {
      snapshot.cells[0].text = text;
      await poll();
      assert.equal(d.querySelector('#doc .def-namespace'), null);
      assert.match(d.getElementById('doc').textContent, /namespace Outer/);
    }
  } finally { window.close(); }
});


test('claims retain namespace context when their surrounding setup boxes disappear', async () => {
  const snapshot = {file: 'ClaimScope.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, links: [], revisions: [], assets: [],
    cells: [
      {index: 0, kind: 'lean', start: 1, end: 1, text: 'namespace Outer'},
      {index: 1, kind: 'claim', start: 2, end: 2, id: 'holds', text: 'theorem holds : True := by trivial'},
      {index: 2, kind: 'lean', start: 3, end: 3, text: 'end Outer'},
    ],
    claims: [{name: 'holds', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}}],
  };
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const d = window.document;
    assert.equal(d.querySelector('#doc .claim .def-namespace').textContent, 'Namespace Outer');
    assert.equal(d.querySelectorAll('#doc .setup-cell').length, 0);
    assert.equal(d.querySelectorAll('#doc .cellwrap').length, 1, 'scope-only cells leave no empty space or toolbars');
    let html;
    window.glitnirHost.saveHTML = async result => { html = result; };
    await d.getElementById('export-html').onclick();
    const exported = new JSDOM(html);
    try {
      assert.equal(exported.window.document.querySelector('.claim .def-namespace').textContent, 'Namespace Outer');
    } finally { exported.window.close(); }
  } finally { window.close(); }
});


test('reading view omits scaffolding and presents mathematical content without raw Lean blocks', async () => {
  const text = [
    'import Mathlib', 'namespace Notebook.Demo', 'open Nat (zero succ)', 'section',
    '#print axioms claim', '#check Nat', '#eval 1 + 2',
    '/-- A constant. -/', 'def value : Nat := 3',
    'structure Pair where', '  left : Nat', '  right : Nat',
    'inductive Color', '| red', '| blue',
    'example : value = 3 := rfl',
    'axiom assumed : True', 'def unfinished : Nat := sorry',
    'variable (n : Nat)', 'set_option autoImplicit false',
    'end', 'end Notebook.Demo',
  ].join('\n');
  const snapshot = {file: 'Cards.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: text.split('\n').length, text}],
  };
  const window = await renderSnapshot(snapshot, {katex: true});
  window.HTMLElement.prototype.scrollIntoView = function() {};
  try {
    const d = window.document;
    const doc = d.getElementById('doc');
    assert.equal(doc.querySelectorAll('.lean-cell,.setup-cell').length, 0);
    assert.ok([...doc.querySelectorAll('pre')].every(pre => pre.closest('details.def-lean')));
    assert.ok([...doc.querySelectorAll('details.def-lean')].every(fold => !fold.open));
    assert.doesNotMatch(doc.textContent, /import Mathlib|namespace Notebook|#print|#check|#eval|open Nat/);
    assert.ok(doc.querySelectorAll('.formal-expression .katex').length >= 6);
    const definitions = [...doc.querySelectorAll('.definition')];
    assert.equal(definitions.length, 6);
    assert.ok(definitions.every(card => card.querySelector('.def-namespace').textContent === 'Namespace Notebook.Demo'));
    assert.match(definitions[0].querySelector('.card-body').textContent, /3/);
    assert.match(doc.textContent, /Fields[\s\S]*left : Nat[\s\S]*right : Nat/);
    assert.match(doc.textContent, /Constructors[\s\S]*red[\s\S]*blue/);
    assert.match(doc.textContent, /Example[\s\S]*value = 3/);
    assert.match(doc.textContent, /Assumption[\s\S]*assumed/);
    assert.match(doc.textContent, /Unfinished/);
    assert.match(doc.textContent, /Parameters[\s\S]*n : Nat/);
    assert.match(doc.textContent, /Option[\s\S]*autoImplicit false/);
    d.querySelector('[data-mode="source"]').click();
    assert.equal(d.getElementById('source').hidden, false);
    assert.match(d.getElementById('source').textContent, /import Mathlib/);
    assert.match(d.getElementById('source').textContent, /def unfinished : Nat := sorry/);
  } finally { window.close(); }
});


test('declaration presentation preserves quoted text, whitespace, comments and constructor meaning', async () => {
  const text = [
    'def        spaced : Nat := 3',
    'def bracket (s : String := "(") : Nat := 3',
    'def spaces (s : String := "a  b") : String := s',
    'def commented : Nat -- := not a value',
    ' := 3',
    'inductive Color where', '| red', '| blue',
    'example : 1 = 1 := rfl',
    "def character (c : Char := '(') : Nat := 3",
    "def primed' : Nat := 4",
  ].join('\n');
  const snapshot = {file: 'Lexical.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 9, text}],
  };
  const window = await renderSnapshot(snapshot);
  try {
    const cards = [...window.document.querySelectorAll('#doc .definition')];
    const expressions = card => [...card.querySelectorAll('.card-signature,.formal-expression')].map(el => el.textContent);
    assert.deepEqual(expressions(cards[0]), [': Nat', '3']);
    assert.deepEqual(expressions(cards[1]), ['(s : String := "(") : Nat', '3']);
    assert.deepEqual(expressions(cards[2]), ['(s : String := "a  b") : String', 's']);
    assert.deepEqual(expressions(cards[3]), [': Nat', '3']);
    assert.match(cards[4].textContent, /Constructors/);
    assert.equal(cards[5].querySelectorAll('.formal-expression').length, 1, 'a proof term belongs only in source');
    assert.deepEqual(expressions(cards[6]), ["(c : Char := '(') : Nat", '3']);
    assert.deepEqual(expressions(cards[7]), [': Nat', '4']);
  } finally { window.close(); }
});

test('common arithmetic uses mathematical notation by default', async () => {
  const snapshot = {file: 'Values.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 1, text: 'def energy (x : ℝ) : ℝ := x ^ 2 / 2'}],
  };
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const card = window.document.querySelector('#doc .definition');
    assert.ok(card.querySelector('.formal-expression .mfrac'));
    assert.ok(card.querySelector('.formal-expression .msupsub'));
  } finally { window.close(); }
});

test('line indicators switch to Source, focus and highlight the range without losing reading folds', async () => {
  const snapshot = {file: 'Locations.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [
      {index: 0, kind: 'lean', start: 260, end: 261, text: 'def value : Nat :=\n  3'},
      {index: 1, kind: 'lean', start: 263, end: 263, text: 'variable (n : Nat)'},
      {index: 2, kind: 'claim', start: 265, end: 266, id: 'holds', text: 'theorem holds : True := by\n  trivial'},
    ],
    claims: [{name: 'holds', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}}],
  };
  let poll;
  const window = await renderSnapshot(snapshot, {katex: true, onPoll: fn => { poll = fn; }});
  const scrolled = [];
  window.HTMLElement.prototype.scrollIntoView = function(options) { scrolled.push({id: this.id, options}); };
  try {
    const d = window.document;
    d.querySelector('.definition .def-lean summary').click();
    let link = d.querySelector('.definition .lines');
    link.focus();
    await poll();
    link = d.querySelector('.definition .lines');
    assert.equal(d.activeElement, link, 'a line link keeps keyboard focus through polling before activation');
    assert.equal(link.tagName, 'A');
    assert.equal(link.textContent, 'L260–261');
    assert.match(link.getAttribute('aria-label'), /260.*261.*Source/);
    link.click();
    assert.equal(d.getElementById('source').hidden, false);
    assert.equal(d.getElementById('doc').hidden, true);
    assert.equal(d.querySelector('[data-mode="source"]').getAttribute('aria-pressed'), 'true');
    assert.equal(d.activeElement.id, 'source-L260');
    assert.equal(scrolled.at(-1).id, 'source-L260');
    const highlighted = () => [...d.querySelectorAll('#source [data-line-target="true"]')].map(row => row.id);
    assert.deepEqual(highlighted(), ['source-L260', 'source-L261']);
    await poll();
    assert.deepEqual(highlighted(), ['source-L260', 'source-L261']);
    assert.equal(d.activeElement.id, 'source-L260');
    d.querySelector('[data-mode="doc"]').click();
    assert.equal(d.querySelector('.definition .def-lean').open, true);
    d.querySelector('.context-card .lines').click();
    assert.deepEqual(highlighted(), ['source-L263']);
    d.querySelector('[data-mode="doc"]').click();
    d.querySelector('.claim-head .lines').click();
    assert.deepEqual(highlighted(), ['source-L265', 'source-L266']);
    d.querySelector('[data-mode="doc"]').click();
    d.querySelector('.definition .lines').click();
    assert.deepEqual(highlighted(), ['source-L260', 'source-L261']);
    snapshot.sha256 = 'b'.repeat(64);
    await poll();
    assert.deepEqual(highlighted(), [], 'a source edit invalidates the old range');
    let html;
    window.glitnirHost.saveHTML = async result => { html = result; };
    await d.getElementById('export-html').onclick();
    const exported = new JSDOM(html);
    try {
      assert.equal(exported.window.document.querySelector('.definition .lines').textContent, 'L260–261');
      assert.equal(exported.window.document.querySelectorAll('[data-act="source-lines"],a[href^="#source-L"]').length, 0);
    } finally { exported.window.close(); }
  } finally { window.close(); }
});


test('prose sibling references open a notebook tab and export as readable text', async () => {
  const prose = '[Neighbor](Neighbor.lean), [again](./Neighbor.lean), `Neighbor.lean`, `Neighbor`. '
    + '[missing](Missing.lean), `Missing.lean`, `neighbor x`, $Neighbor$, [outside](../Neighbor.lean), '
    + '[<img src=x onerror=alert(1)>](Neighbor.lean).';
  const messages = [];
  let poll;
  const snapshot = {file: 'Root.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    claims: [], links: [], references: [{module: 'Neighbor', file: 'Neighbor.lean'}], revisions: [], assets: [],
    cells: [{id: 'cell-0', index: 0, kind: 'prose', start: 1, end: 3, text: prose, markdown: prose}]};
  const window = await renderSnapshot(snapshot, {katex: true, onMessage: message => messages.push(message), onPoll: fn => { poll = fn; }});
  try {
    const d = window.document;
    const links = [...d.querySelectorAll('.notebook-reference')];
    assert.equal(links.length, 5);
    assert.ok(links.every(a => a.getAttribute('href') === '/?reference=Neighbor'
      && a.target === '_blank' && a.rel.includes('noopener')));
    assert.equal(links.at(-1).textContent, '<img src=x onerror=alert(1)>');
    assert.equal(d.querySelectorAll('.prose img').length, 0);
    links[0].focus();
    await poll();
    assert.equal(d.activeElement.getAttribute('href'), '/?reference=Neighbor');
    d.activeElement.click();
    assert.equal(messages.at(-1).type, 'reference');
    assert.equal(messages.at(-1).module, 'Neighbor');
    assert.match(d.querySelector('.prose').textContent, /Missing.lean/);
    let html;
    window.glitnirHost.saveHTML = async body => { html = body; };
    await d.getElementById('export-html').onclick();
    const exported = new JSDOM(html);
    try {
      assert.equal(exported.window.document.querySelectorAll('a[href*="reference="]').length, 0);
      assert.match(exported.window.document.body.textContent, /Neighbor/);
    } finally { exported.window.close(); }
  } finally { window.close(); }
});

for (const browser of [false, true]) test(`Source returns to the exact document card (${browser ? 'browser' : 'VS Code'})`, async () => {
  const snapshot = {file: 'Return.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    claims: [{name: 'done', keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'open', reason: '', uses: [], context: 'c'.repeat(64)}],
    links: [], revisions: [], assets: [], cells: [
      {index: 0, kind: 'lean', start: 1, end: 1, text: 'import Init'},
      {index: 1, kind: 'prose', start: 2, end: 4, text: '/-!\nA paragraph.\n-/', markdown: 'A paragraph.'},
      {index: 2, kind: 'lean', start: 5, end: 7, text: 'def first : Nat := 1\n\ndef second : Nat := 2'},
      {index: 3, id: 'done', kind: 'claim', start: 8, end: 9, text: 'theorem done : True := by\n  trivial'},
    ]};
  const window = await renderSnapshot(snapshot, {browser});
  const d = window.document;
  let scrolled;
  window.HTMLElement.prototype.scrollIntoView = function() { scrolled = this; };
  try {
    const second = () => [...d.querySelectorAll('.definition')].find(el => el.textContent.includes('second'));
    second().querySelector('.lines').click();
    d.querySelector('[data-act="document-return"]').focus();
    await window.glitnirPoll();
    assert.equal(d.activeElement.dataset.act, 'document-return');
    d.querySelector('[data-act="document-return"]').click();
    assert.equal(d.getElementById('doc').hidden, false);
    assert.equal(scrolled, second());
    assert.equal(d.activeElement, second());
    await window.glitnirPoll();
    assert.equal(d.activeElement, second(), 'polling preserves the return focus');
    d.querySelector('[data-mode="source"]').click();
    d.querySelector('#source-L3 [data-act="document-line"]').focus();
    await window.glitnirPoll();
    assert.equal(d.activeElement.dataset.line, '3');
    d.activeElement.click();
    assert.ok(scrolled.classList.contains('prose'));
    d.querySelector('[data-mode="source"]').click();
    d.querySelector('#source-L9 [data-act="document-line"]').click();
    assert.equal(scrolled.id, 'cell-done');
    assert.equal(d.activeElement, scrolled);
    second().querySelector('.lines').click();
    d.querySelector('[data-mode="doc"]').click();
    assert.equal(scrolled, second(), 'Document tab also returns to the matching card');
    d.querySelector('[data-mode="source"]').click();
    d.querySelector('#source-L1 [data-act="document-line"]').click();
    assert.equal(d.getElementById('doc').hidden, false);
    assert.ok(scrolled.classList.contains('prose'), 'setup-only source returns to nearby visible content');
    second().querySelector('.lines').click();
    snapshot.sha256 = 'b'.repeat(64);
    await window.glitnirPoll();
    scrolled = null;
    d.querySelector('[data-act="document-return"]').click();
    assert.equal(d.getElementById('doc').hidden, false);
    assert.equal(scrolled, null, 'a stale range cannot target different content');
  } finally { window.close(); }
});


test('source lines reveal directive cards and decision continuations within a mixed cell', async () => {
  const snapshot = {file: 'Directives.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {},
    claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 6, text: 'def first : Nat := 1\n-- @decide one : invalid\n--   continuation\n-- @decide two : invalid\n-- @view model missing\n-- @evidence simulate missing'}]};
  const window = await renderSnapshot(snapshot);
  const d = window.document;
  let target;
  window.HTMLElement.prototype.scrollIntoView = function() { target = this; };
  try {
    for (const [line, text] of [[2, 'one : invalid'], [3, 'one : invalid'], [4, 'two : invalid'], [5, 'view directive'], [6, '@evidence needs a provider']]) {
      d.querySelector('[data-mode="source"]').click();
      d.querySelector(`#source-L${line} [data-act="document-line"]`).click();
      assert.ok(target.textContent.includes(text));
      assert.ok(!target.classList.contains('definition'));
      assert.equal(d.activeElement, target);
    }
  } finally { window.close(); }
});

test('reading card kinds preserve exact source and warnings in the document and static export', async () => {
  const text = '/-- A reusable constant. -/\ndef value : Nat := 3\n\nexample : value = 3 := by\n  rfl\n\naxiom assumed : False\n\ndef pending : Nat := sorry';
  const snapshot = {file: 'Styles.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: text.split('\n').length, text}]};
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const d = window.document;
    assert.equal(d.querySelectorAll('.reading-card.card-definition').length, 2);
    const example = d.querySelector('.reading-card.card-example');
    assert.match(example.querySelector('.card-header').textContent, /Example/);
    assert.match(example.querySelector('.card-body').textContent, /value\s*=\s*3/);
    assert.equal(example.querySelector('.field-label'), null);
    assert.ok(example.querySelector('.formal-expression .katex'));
    assert.equal(example.querySelector('.def-lean pre').textContent, 'example : value = 3 := by\n  rfl');
    assert.match(d.querySelector('.card-assumption .card-notice').textContent, /Assumed without a proof/);
    assert.match(d.querySelectorAll('.card-definition')[1].textContent, /Unfinished/);
    assert.equal(d.querySelectorAll('.reading-card[data-tone]').length, 0, 'kind accents do not claim proof status');
    example.querySelector('summary').click();
    await window.glitnirPoll();
    assert.equal(d.querySelector('.card-example .def-lean').open, true);
    let html;
    window.glitnirHost.saveHTML = async result => { html = result; };
    await d.getElementById('export-html').onclick();
    const output = new JSDOM(html);
    try {
      const page = output.window.document;
      assert.equal(page.querySelectorAll('.reading-card').length, 4);
      assert.equal(page.querySelectorAll('.card-example').length, 1);
      assert.equal(page.querySelector('.card-example .field-label'), null);
      assert.equal(page.querySelectorAll('script,button').length, 0);
      assert.match(page.querySelector('.card-assumption').textContent, /Assumed without a proof/);
      assert.ok([...page.querySelectorAll('.def-lean')].every(source => !source.open));
    } finally { output.window.close(); }
  } finally { window.close(); }
});

test('shared theorem headers retain status and inline edit selects each declaration', async () => {
  const claim = {name: 'result', keyword: 'lemma', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}};
  const snapshot = {file: 'Headers.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [claim], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 3, text: 'def first : Nat := 1\n\ndef second : Nat := 2'}, {index: 1, kind: 'claim', id: 'result', start: 4, end: 5, text: 'lemma result : True := by\n  trivial'}]};
  const messages = [];
  const window = await renderSnapshot(snapshot, {katex: true, onMessage: message => messages.push(message)});
  try {
    const d = window.document;
    const head = d.querySelector('.claim .card-header');
    assert.equal(head.querySelector('.def-kind').textContent, 'Lemma');
    assert.equal(head.querySelector('code').textContent, 'result');
    assert.equal(head.querySelector('.pill').textContent, 'Checked');
    assert.equal(head.querySelector('.pill').dataset.tone, 'green');
    assert.equal(d.querySelectorAll('#doc .cell-tools').length, 0);
    const controls = [...d.querySelectorAll('#doc .source-controls')];
    assert.equal(controls.length, 3);
    for (const [i, bounds] of [[0, [1, 1]], [1, [3, 3]], [2, [4, 5]]]) {
      const button = controls[i].querySelector('button');
      assert.equal(button.title, 'Edit in editor');
      assert.equal(button.getAttribute('aria-label'), 'Edit in editor');
      button.click();
      assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), {type: 'source', index: i === 2 ? 1 : 0, sha256: snapshot.sha256, range: {start: bounds[0], end: bounds[1]}});
    }
    controls[1].querySelector('button').focus();
    await window.glitnirPoll();
    assert.equal(d.activeElement, d.querySelectorAll('#doc .source-edit')[1], 'unchanged polling keeps the edit icon focused');
    claim.keyword = 'theorem'; claim.status = 'stale';
    await window.glitnirPoll();
    assert.equal(d.querySelector('.claim .def-kind').textContent, 'Theorem');
    assert.equal(d.querySelector('.claim .pill').textContent, 'Out of date');
    let html;
    window.glitnirHost.saveHTML = async result => { html = result; };
    await d.getElementById('export-html').onclick();
    const output = new JSDOM(html);
    try {
      assert.equal(output.window.document.querySelector('.claim .card-header .pill').textContent, 'Out of date');
      assert.equal(output.window.document.querySelectorAll('button').length, 0);
    } finally { output.window.close(); }
  } finally { window.close(); }
  const browser = await renderSnapshot(snapshot, {browser: true});
  try {
    assert.equal(browser.document.querySelectorAll('.source-controls button').length, 0);
    assert.equal(browser.document.querySelector('[data-act="edit"],[data-act="insert"],[data-act="remove"],textarea'), null, 'the preview has no cell editor');
  } finally { browser.close(); }
});


test('prose mentioning source-controls retains its source toolbar', async () => {
  const window = await renderSnapshot({file: 'Prose.lean', sha256: 'a'.repeat(64), checking: [], claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'prose', start: 1, end: 1, text: '/-! source-controls -/', markdown: 'Describe the `source-controls` UI.'}]});
  try { assert.ok(window.document.querySelector('#doc .cell-tools .source-edit')); }
  finally { window.close(); }
});

test('reading cards show mathematics once and fold exact statement with proof', async () => {
  const text = 'def energy (x : ℝ) : ℝ := x * x\n\nexample : energy 0 = 0 := by\n  sorry';
  const claim = {name: 'zero', keyword: 'theorem', statement: ': energy 0 = 0', proof: 'rfl', proof_block: '  rfl', status: 'checked', reason: '', uses: [], context: 'c'.repeat(64), record: {verdict: 'holds', axioms: [], context: 'c'.repeat(64)}};
  const theorem = 'theorem zero : energy 0 = 0 := by\n  rfl';
  const snapshot = {file: 'Reading.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [claim], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 4, text}, {index: 1, kind: 'claim', id: 'zero', start: 5, end: 6, text: theorem}]};
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const d = window.document;
    const definition = d.querySelector('.card-definition');
    assert.equal(definition.querySelectorAll('.card-body .formal-expression').length, 1);
    assert.equal(definition.querySelector('.card-signature').textContent, '(x : ℝ) : ℝ');
    assert.doesNotMatch(definition.querySelector('.card-body').textContent, /energy/);
    const example = d.querySelector('.card-example');
    assert.match(example.querySelector('.card-notice').textContent, /Unfinished/);
    assert.match(example.querySelector('summary').textContent, /Lean source & proof/);
    const source = d.querySelector('.claim details.def-lean');
    assert.ok(source && !source.open);
    assert.equal(source.querySelector('pre').textContent, theorem);
    assert.equal(d.querySelectorAll('.claim .lean-tabs,.claim .lean-pane').length, 0);
    claim.proof = 'sorry';
    await window.glitnirPoll();
    assert.match(d.querySelector('.claim-notice').textContent, /Unfinished/);
    claim.proof = 'rfl';
    await window.glitnirPoll();
    d.querySelector('.claim details.def-lean summary').click();
    await window.glitnirPoll();
    assert.equal(d.querySelector('.claim details.def-lean').open, true);
    let html;
    window.glitnirHost.saveHTML = async result => { html = result; };
    await d.getElementById('export-html').onclick();
    const exported = new JSDOM(html);
    try {
      assert.equal(exported.window.document.querySelectorAll('.claim pre').length, 1);
      assert.equal(exported.window.document.querySelector('.claim details').open, false);
    } finally { exported.window.close(); }
  } finally { window.close(); }
});

test('compact definitions foreground values and preserve signatures, documentation and warnings', async () => {
  const longType = 'Very.Long.Namespace.' + 'ComplicatedType'.repeat(9);
  const longDoc = 'This is a detailed explanation of the definition. '.repeat(5);
  const text = `namespace Demo\n/-- Energy per coordinate. -/\ndef energy (x : ℝ) : ℝ := x ^ 2 / 2\n\n/-- ${longDoc}-/\ndef detailed : ${longType} := value\n\ndef unfinished : Nat := by\n  sorry\n\nend Demo`;
  const snapshot = {file: 'Compact.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 11, text}]};
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const d = window.document;
    const [energy, detailed, unfinished] = d.querySelectorAll('.card-definition');
    assert.equal(energy.querySelector('.card-signature').textContent, '(x : ℝ) : ℝ');
    assert.equal(energy.querySelector('.card-description').textContent, 'Energy per coordinate.');
    assert.ok(energy.querySelector('.card-body .mfrac'));
    assert.doesNotMatch(energy.querySelector('.card-body').textContent, /Type and parameters|Energy per coordinate|energy/);
    assert.equal(energy.querySelector('.card-body > .def-namespace').textContent, 'Namespace Demo');
    assert.equal(energy.querySelector('.definition-details'), null, 'a namespace alone needs no Details fold');
    assert.ok([...d.querySelectorAll('#doc .def-namespace')].every(label => !label.closest('details')));
    assert.equal(detailed.querySelector('.card-signature'), null);
    assert.equal(detailed.querySelector('.definition-details').open, false);
    assert.match(detailed.querySelector('.definition-details').textContent, /detailed explanation/);
    assert.match(detailed.querySelector('.definition-details').textContent, /ComplicatedType/);
    assert.match(unfinished.querySelector('.card-body .card-notice').textContent, /Unfinished/);
    const details = detailed.querySelector('.definition-details'); details.querySelector('summary').click();
    await window.glitnirPoll();
    assert.equal(d.querySelector('.definition-details').open, true);
    let html;
    window.glitnirHost.saveHTML = async result => { html = result; };
    await d.getElementById('export-html').onclick();
    const output = new JSDOM(html);
    try {
      assert.ok([...output.window.document.querySelectorAll('.definition-details')].every(fold => !fold.open));
      assert.match(output.window.document.body.textContent, /ComplicatedType/);
      assert.match(output.window.document.body.textContent, /Unfinished/);
      assert.equal(output.window.document.querySelectorAll('button,script').length, 0);
    } finally { output.window.close(); }
  } finally { window.close(); }
});

const circuitFixture = () => ({graph: {
  nodes: [{kind:'scalar',label:'× (2)',inputs:1,outputs:1,x:0,y:0}, {kind:'scalar',label:'× (3)',inputs:1,outputs:1,x:232,y:0}],
  edges:[{source:[0,0],target:[1,0],feedback:false,lane:185}],inputs:[[0,0]],outputs:[[1,0]],
}, contracts: [
  {name:'first',pre:'P',post:'M',preKey:'P',postKey:'M',epsilonKey:null,epsilon:null,nodeStart:0,nodeCount:1,inputs:[[0,0]],outputs:[[0,0]]},
  {name:'second',pre:'M',post:'Q',preKey:'M',postKey:'Q',epsilonKey:null,epsilon:null,nodeStart:1,nodeCount:1,inputs:[[1,0]],outputs:[[1,0]]},
]});

test('circuit diagrams label edges and preserve exploration across refresh and export', async () => {
  const data = circuitFixture();
  const snapshot={file:'Circuit.lean',sha256:'a'.repeat(64),checking:[],claims:[],links:[],revisions:[],assets:[],
    views:[{cell:0,line:1,kind:'circuit',name:'pipeline'}], cells:[{index:0,kind:'lean',start:1,end:1,text:'-- @view circuit pipeline using first, second'}]};
  const window=await renderSnapshot(snapshot,{katex:true,onView:()=>data});
  try {
    const d=window.document;
    window.HTMLElement.prototype.scrollIntoView=function() {};
    window.HTMLDialogElement.prototype.showModal=function() {this.setAttribute('open','');};
    window.HTMLDialogElement.prototype.close=function() {if(this.open){this.removeAttribute('open');this.dispatchEvent(new window.Event('close'));}};
    assert.deepEqual([...d.querySelectorAll('.circuit-condition text')].map(t=>t.textContent),['P','M','Q']);
    assert.equal(d.querySelectorAll('.circuit-operator').length,2);
    assert.equal(d.querySelectorAll('.circuit-boundary').length,0,'one-wire conditions need no bracket');
    d.querySelector('.circuit-condition').dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true}));
    assert.equal(d.querySelector('.diagram-proof').open,true);
    d.querySelector('.diagram-proof summary').focus();
    await window.glitnirPoll();
    assert.equal(d.querySelector('.diagram-proof').open,true);
    assert.equal(d.activeElement,d.querySelector('.diagram-proof summary'));
    d.querySelector('.diagram-viewport').focus();
    await window.glitnirPoll();
    assert.equal(d.activeElement,d.querySelector('.diagram-viewport'));
    const button=d.querySelector('[data-command=in]');button.focus();button.click();
    const width=d.querySelector('.circuit-scene').getAttribute('width');
    assert.notEqual(width,'100%');
    d.querySelector('.diagram-viewport').scrollLeft=82;
    await window.glitnirPoll();
    assert.equal(d.querySelector('.circuit-scene').getAttribute('width'),width);
    assert.equal(d.querySelector('.diagram-viewport').scrollLeft,82);
    assert.equal(d.activeElement.dataset.command,'in');
    d.querySelector('[data-command=expand]').click();
    assert.ok(d.querySelector('dialog').open);
    assert.ok(d.querySelector('dialog .circuit-scene'));
    const summary=d.querySelector('dialog .diagram-proof summary');summary.click();summary.focus();
    await window.glitnirPoll();
    assert.equal(d.querySelector('dialog .diagram-proof').open,true);
    assert.equal(d.activeElement,d.querySelector('dialog .diagram-proof summary'));
    d.querySelector('dialog .diagram-viewport').focus();
    await window.glitnirPoll();
    assert.equal(d.activeElement,d.querySelector('dialog .diagram-viewport'));
    d.querySelector('[data-act=diagram-close]').click();
    assert.equal(d.querySelector('dialog').open,false);
    assert.equal(d.activeElement.dataset.command,'expand');
    d.querySelector('[data-command=labels]').click();
    assert.equal(d.querySelectorAll('#doc .circuit-condition').length,0);
    d.querySelector('[data-command=labels]').click();
    let html;window.glitnirHost.saveHTML=async value=>{html=value;};
    await d.getElementById('export-html').onclick();
    const exported=new JSDOM(html);
    try {
      const page=exported.window.document;
      assert.equal(page.querySelectorAll('.circuit-condition').length,3);
      assert.ok(page.querySelector('.circuit-scene').classList.contains('diagram-fit'));
      assert.equal(page.querySelector('.diagram-toolbar'),null);
      assert.ok(page.querySelector(page.querySelector('.circuit-condition').getAttribute('href')));
    } finally {exported.window.close();}
    snapshot.sha256='b'.repeat(64);await window.glitnirPoll();
    await new Promise(resolve=>setTimeout(resolve,0));
    assert.ok(d.querySelector('#doc .circuit-scene').classList.contains('diagram-fit'),'changed source resets view state');
  } finally {window.close();}
});

test('circuit renderer keeps bundles, structural wiring and hostile labels explicit', async () => {
  const window=await renderSnapshot({file:'Empty.lean',sha256:'a'.repeat(64),checking:[],claims:[],links:[],revisions:[],assets:[],cells:[]});
  try {
    const renderer=window.glitnirCircuits;
    const graph={nodes:[{kind:'swap',label:'swap',inputs:2,outputs:2,x:0,y:0}],edges:[],inputs:[[0,0],[0,1]],outputs:[[0,0],[0,1]]};
    const contract={name:'bound',pre:'x² + y² ≤ 1',post:'<img src=x onerror=alert(1)>',epsilon:'1/3',nodeStart:0,nodeCount:1,inputs:graph.inputs,outputs:graph.outputs};
    const host=window.document.createElement('div');host.innerHTML=renderer.render({graph,contracts:[contract]},{key:'safe'});
    assert.equal(host.querySelector('.circuit-operator'),null);
    assert.ok(host.querySelector('.circuit-crossing'));
    assert.equal(host.querySelectorAll('.circuit-boundary').length,6);
    assert.match(host.textContent,/x² \+ y² ≤ 1/);
    assert.match(host.textContent,/ε = 1\/3/);
    assert.equal(host.querySelector('img'),null);
    graph.edges=[{source:[0,2],target:[0,0],feedback:true,lane:1}];
    assert.equal(renderer.validGraph(graph),false);
    assert.match(renderer.render({graph},{key:'bad'}),/unavailable/);
  } finally {window.close();}
});

test('model overviews and underlying circuits contain invalid data and zoom below 25 percent', async () => {
  const data={model:{axis:'t',start:'0',states:[{name:'x',id:'state-x',equation:'dx/dt = (-x)',initial:'1'}],observations:[['x','state-x']]},detail:circuitFixture().graph,contracts:[]};
  const snapshot={file:'Model.lean',sha256:'a'.repeat(64),checking:[],claims:[],links:[],revisions:[],assets:[],views:[{cell:0,line:1,kind:'model',name:'model'}],cells:[{index:0,kind:'lean',start:1,end:1,text:'-- @view model model'}]};
  const window=await renderSnapshot(snapshot,{onView:()=>data});
  try {
    const d=window.document;
    assert.equal(d.querySelector('.diagram-error'),null);
    assert.match(d.querySelector('.circuit-scene').textContent,/Vector field/);
    d.querySelector('[data-command=circuit]').click();
    assert.equal(d.querySelectorAll('.circuit-operator').length,2);
    const svg=d.querySelector('.circuit-scene');
    svg.setAttribute('viewBox','0 0 24000 500');
    Object.defineProperty(d.querySelector('.diagram-viewport'),'clientWidth',{value:800});
    d.querySelector('[data-command=out]').click();
    // The simulated wide view establishes the scale; the actual fixture has
    // natural width 522, whose new width must be below 522 * (800 / 24000).
    assert.ok(Number(d.querySelector('.circuit-scene').getAttribute('width'))<20);
    d.querySelector('[data-command=overview]').click();
    assert.match(d.querySelector('.circuit-scene').textContent,/Vector field/);
    data.model.observations=[null];
    const invalid=window.glitnirCircuits.render(data,{key:'invalid'});
    assert.match(invalid,/Invalid model diagram data/);
    data.model.observations=[];
    assert.doesNotMatch(window.glitnirCircuits.render(data,{key:'empty'}),/diagram-error/);
  } finally {window.close();}
});

test('identical printed predicate names do not merge distinct conditions', async () => {
  const window=await renderSnapshot({file:'Empty.lean',sha256:'a'.repeat(64),checking:[],claims:[],links:[],revisions:[],assets:[],cells:[]});
  try {
    const data=circuitFixture();data.contracts[1].preKey='different implicit argument';
    const host=window.document.createElement('div');host.innerHTML=window.glitnirCircuits.render(data,{key:'distinct'});
    assert.equal(host.querySelectorAll('.circuit-condition').length,4);
  } finally {window.close();}
});


test('project source preview reads Lean as cards, with source navigation and an honest export', async () => {
  const media = path.resolve(__dirname, '../../dist/media');
  const dom = new JSDOM(readFileSync(path.join(media, 'index.html'), 'utf8').replace('<body>', '<body><div id="editor-state"></div><button id="open-source"></button><button id="next-unresolved" hidden></button>'), {runScripts: 'outside-only', url: 'https://preview.invalid/'});
  const {window} = dom;
  const requests = [];
  const text = 'theorem t : True := True.intro\n-- <script>never</script>\n';
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells: [{index: 0, kind: 'source', start: 1, end: 2, text}, {index: 1, kind: 'source', start: 3, end: 3, text: '\n'}], preview: {mode: 'project', notice: 'Saved-source preview. Check this file explicitly.', project: {kind: 'lake', root: '/project', toolchain: 'test'}}};
  window.Response = Response;
  window.matchMedia = () => ({matches: false});
  window.katex = {};
  window.acquireVsCodeApi = () => ({postMessage: message => {
    requests.push(message);
    if (!['request', 'export'].includes(message.type)) return;
    queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, json: message.path === '/api/notebook' ? snapshot : {css: ''}}})));
  }});
  try {
    for (const file of ['bridge.js', 'review.js', 'circuits.js', 'cards.js', 'export.js', 'app.js']) window.eval(readFileSync(path.join(media, file), 'utf8'));
    await new Promise(resolve => setTimeout(resolve, 80));
    const doc = window.document.getElementById('doc');
    assert.equal(doc.querySelector('.definition dfn').textContent, 't');
    assert.ok([...doc.querySelectorAll('.def-lean pre')].some(pre => pre.textContent === '-- <script>never</script>'), 'the exact comment stays in its source fold');
    assert.match(doc.querySelector('.context-card .def-body').textContent, /^<script>never<\/script>$/, 'a comment reads as escaped prose');
    assert.equal(window.document.querySelector('#doc .cellwrap[data-index="1"]'), null, 'blank lines get no cell');
    assert.equal(window.document.querySelectorAll('#doc script,.pill').length, 0);
    assert.equal(window.document.getElementById('replay-all'), null);
    assert.match(window.document.getElementById('banner-text').textContent, /Check this file explicitly/);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: false, mode: 'project'}}));
    assert.equal(window.document.getElementById('editor-state').hidden, true);
    assert.equal(window.document.getElementById('editor-state').textContent, '');
    window.document.getElementById('export-html').click();
    await new Promise(resolve => setTimeout(resolve, 80));
    const exported = requests.find(r => r.type === 'export');
    assert.ok(exported);
    assert.match(exported.body, /static page does not check/);
    assert.doesNotMatch(exported.body, /Statuses describe recorded Lean observations/);
    window.document.querySelector('[data-mode="source"]').click();
    assert.deepEqual([...window.document.querySelectorAll('[data-source-line]')].map(row => row.id), ['source-L1', 'source-L2', 'source-L3']);
    assert.equal(window.document.querySelector('#source-L2 td:last-child').textContent, '-- <script>never</script>');
    assert.doesNotMatch(window.document.querySelector('.source-note').textContent, /gutter shows each claim|checking unavailable/);
    assert.match(window.document.querySelector('.source-note').textContent, /use Check saved file/);
    assert.equal(requests.filter(r => /api\/(goal|view|replay)/.test(r.path)).length, 0);
  } finally { dom.window.close(); }
});

test('project mode keeps decide and evidence directives visible as source with a provider note', async () => {
  const text = 'def x : Nat := 1\n-- @decide band : trajectory lorenz from (1) at 0\n--   observe V always : V <= 1600\n-- @evidence simulate M (h := 1/10) (steps := 1)\n';
  const snapshot = {file: 'P.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: 4, text}], views: [],
    evidence: [evidenceEntry({line: 4, provider: null, error: 'No installed provider runs `@evidence simulate`.'})],
    decisions: [decisionEntry({line: 2, end_line: 3, kind: 'trajectory', error: 'No installed provider decides `trajectory` goals. <b>x</b>'})],
    directive_providers: {evidence: [], decide: []}, project_claims: [], references: [], revisions: [], assets: [],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}}};
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const doc = window.document.getElementById('doc');
    assert.equal(doc.querySelector('.evidence'), null, 'an unserved verb gets a note, not a card');
    assert.match(doc.textContent, /-- @decide band : trajectory lorenz/);
    assert.match(doc.textContent, /observe V always/);
    assert.match(doc.textContent, /-- @evidence simulate M/);
    assert.match(doc.textContent, /No installed provider decides `trajectory` goals\. <b>x<\/b>/, 'the server error is shown escaped');
    assert.equal(doc.querySelector('.note-line b'), null);
    assert.equal(doc.querySelector('.note-line[data-document-start="2"]').dataset.documentEnd, '3');
    assert.match(doc.textContent, /No installed provider runs `@evidence simulate`\./);
    assert.doesNotMatch(doc.textContent, /directive is incomplete/);
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const body = page.window.document.body.textContent;
      assert.match(body, /-- @decide band : trajectory lorenz/);
      assert.match(body, /-- @evidence simulate M/);
      assert.match(body, /No installed provider decides `trajectory` goals\./);
      assert.match(body, /No installed provider runs `@evidence simulate`\./);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('project mode notes only the listed directives no installed provider serves', async () => {
  const text = 'def M : Nat := 1\n-- @evidence simulate M (h := 1/10)\n-- @evidence iterate M\n-- @decide band : entailment x : x > 0 |- x >= 0\n/- -- @evidence iterate M -/\n  -- @evidence iterate M\n-- @decide served : stream s\n/- -- @decide hidden : entailment x -/\n';
  const snapshot = {file: 'P.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: 8, text}], views: [],
    evidence: [evidenceEntry({line: 2}), evidenceEntry({line: 3, verb: 'iterate', provider: null, error: 'No installed provider runs `@evidence iterate`.'}),
      evidenceEntry({line: 6, verb: 'iterate', provider: null, error: ''})],
    decisions: [decisionEntry({line: 4, error: ''}), decisionEntry({line: 7, name: 'served', kind: 'stream', provider: 'demo', error: ''})],
    directive_providers: {evidence: ['simulate'], decide: ['stream']}, project_claims: [], references: [], revisions: [], assets: [],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}}};
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const notes = [...window.document.querySelectorAll('#doc .note-line[data-document-start]')]
      .map(note => [note.dataset.documentStart, note.textContent.replace(/^L\d+–\d+\s*/, '')]);
    assert.deepEqual(notes, [
      ['3', 'No installed provider runs `@evidence iterate`.'],
      ['4', '@decide needs a provider and is not available yet.'],
      ['6', '@evidence needs a provider and is not available yet.'],
    ], 'notes follow the listed directives, not every line that looks like one');
    assert.deepEqual([...window.document.querySelectorAll('#doc .evidence')].map(card => card.dataset.documentStart), ['2']);
    assert.deepEqual([...window.document.querySelectorAll('#doc .decision')].map(card => card.dataset.documentStart), ['7'], 'a served kind gets a card');
  } finally { window.close(); }
});

test('lean cells without decision or evidence data keep directive source and say a provider is needed', async () => {
  const text = 'def x : Nat := 1\n-- @decide band : entailment x : x > 0 |- x >= 0\n--   continued goal\n-- @evidence simulate M (h := 1/10) (steps := 1)';
  const snapshot = {file: 'L.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [{index: 0, kind: 'lean', start: 1, end: 4, text}]};
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const doc = window.document.getElementById('doc');
    assert.match(doc.textContent, /-- @decide band : entailment/);
    assert.match(doc.textContent, /continued goal/);
    assert.match(doc.textContent, /-- @evidence simulate M/);
    assert.match(doc.textContent, /@decide needs a provider and is not available yet\./);
    assert.match(doc.textContent, /@evidence needs a provider and is not available yet\./);
    assert.doesNotMatch(doc.textContent, /directive is incomplete/);
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const body = page.window.document.body.textContent;
      assert.match(body, /-- @decide band : entailment/);
      assert.match(body, /continued goal/);
      assert.match(body, /@evidence needs a provider and is not available yet\./);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('project prose links open sibling notebooks only', async () => {
  const media = path.resolve(__dirname, '../../dist/media');
  const dom = new JSDOM(readFileSync(path.join(media, 'index.html'), 'utf8').replace('<body>', '<body><div id="editor-state"></div><button id="open-source"></button><button id="next-unresolved" hidden></button>'), {runScripts: 'outside-only', url: 'https://preview.invalid/'});
  const {window} = dom;
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], references: [{module: 'Basics', file: 'Basics.lean'}], cells: [{index: 0, kind: 'prose', start: 1, end: 1, text: '/-! [B](Basics.lean) [O](../Other.lean) -/\n', markdown: '[B](Basics.lean) [O](../Other.lean)'}], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}}};
  window.Response = Response;
  window.matchMedia = () => ({matches: false});
  window.katex = {};
  window.acquireVsCodeApi = () => ({postMessage: message => {
    if (!['request', 'export'].includes(message.type)) return;
    queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, json: message.path === '/api/notebook' ? snapshot : {css: ''}}})));
  }});
  try {
    for (const file of ['bridge.js', 'review.js', 'circuits.js', 'cards.js', 'export.js', 'app.js']) window.eval(readFileSync(path.join(media, file), 'utf8'));
    await new Promise(resolve => setTimeout(resolve, 80));
    const links = [...window.document.querySelectorAll('#doc a.notebook-reference')];
    assert.equal(links.length, 1);
    assert.equal(links[0].textContent, 'B');
    assert.match(links[0].getAttribute('href'), /reference=Basics/);
    assert.doesNotMatch(window.document.getElementById('doc').innerHTML, /reference=Other/);
    assert.match(window.document.getElementById('doc').textContent, /\[O\]\(\.\.\/Other\.lean\)/);
  } finally { dom.window.close(); }
});

test('project claim cards show saved status and out-of-date results', async () => {
  const media = path.resolve(__dirname, '../../dist/media');
  const dom = new JSDOM(readFileSync(path.join(media, 'index.html'), 'utf8').replace('<body>', '<body><div id="editor-state"></div><button id="open-source"></button><button id="next-unresolved" hidden></button>'), {runScripts: 'outside-only', url: 'https://preview.invalid/'});
  const {window} = dom;
  const requests = [];
  const text = 'theorem a : True := by\n  trivial\n';
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [
      {index: 0, kind: 'source', start: 1, end: 2, text, claim: 'Demo.a'},
      {index: 1, kind: 'source', start: 3, end: 3, text: 'theorem b : True := sorry\n', claim: 'Demo.b'},
      {index: 2, kind: 'source', start: 4, end: 4, text: 'theorem c : True := trivial\n', claim: 'Demo.c'},
      {index: 3, kind: 'source', start: 5, end: 5, text: 'example : True := trivial\n', claim: '_private.A.0._glitnir_example_90'},
      {index: 4, kind: 'source', start: 6, end: 6, text: 'example : Nat := 3\n'},
      {index: 5, kind: 'source', start: 7, end: 7, text: 'example : 1 = 1 := rfl\n', claim: '_private.A.0._glitnir_example_120'},
    ],
    project_claims: [
      {name: 'Demo.a', start_line: 1, end_line: 2, status: 'checked', axioms: [], current: true},
      {name: 'Demo.b', start_line: 3, end_line: 3, status: 'admitted', axioms: [{name: 'sorryAx', module: 'Init'}], current: false},
      {name: 'Demo.c', start_line: 4, end_line: 4, status: 'constructor', axioms: [], current: true},
      {name: '_private.A.0._glitnir_example_90', example: true, statement: 'True ∧ <b>', kernel_statement: 'And True Demo.<b>', start_line: 5, end_line: 5, status: 'checked', axioms: [], current: true},
      {name: '_private.A.0._glitnir_example_120', example: true, kernel_statement: 'Eq 1 …', kernel_statement_shortened: true, start_line: 7, end_line: 7, status: 'checked', axioms: [], current: true},
    ],
    project_non_claims: [{start_line: 6, end_line: 6}],
    project_withheld: {from_line: 8, message: 'type mismatch'},
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}}};
  window.Response = Response;
  window.matchMedia = () => ({matches: false});
  window.katex = {};
  window.acquireVsCodeApi = () => ({postMessage: message => {
    requests.push(message);
    if (!['request', 'export'].includes(message.type)) return;
    queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, json: message.path === '/api/notebook' ? snapshot : {css: ''}}})));
  }});
  try {
    for (const file of ['bridge.js', 'review.js', 'circuits.js', 'cards.js', 'export.js', 'app.js']) window.eval(readFileSync(path.join(media, file), 'utf8'));
    await new Promise(resolve => setTimeout(resolve, 80));
    const cards = [...window.document.querySelectorAll('#doc .project-claim')];
    assert.equal(cards.length, 5);
    assert.equal(cards[3].querySelector('dfn').textContent, 'example (line 5)');
    assert.equal(cards[3].querySelector('.def-kind').textContent, 'Example');
    assert.equal(cards[0].querySelector('dfn').textContent, 'a', 'the source names the claim');
    assert.equal(cards[0].dataset.claim, 'Demo.a');
    assert.match(cards[3].textContent, /example : True := trivial/);
    assert.match(cards[3].textContent, /Kernel checked/);
    assert.doesNotMatch(cards[3].textContent, /_glitnir_example/);
    // As Lean prints it in the example's scope, typeset like a Statement
    // field; the kernel form is one fold away.
    assert.equal(cards[3].querySelector('.claim-statement .field-label').textContent, 'Checked statement, as this project’s notation prints it');
    assert.equal(cards[3].querySelector('.claim-statement .formal-line').textContent, 'True ∧ <b>');
    assert.equal(cards[3].querySelector('.checked-kernel summary').textContent, 'Kernel form, without notation');
    assert.equal(cards[3].querySelector('.checked-kernel .checked-statement').textContent, 'And True Demo.<b>');
    assert.doesNotMatch(cards[3].textContent, /shortened/);
    // A record of an older check holds only the kernel form, and says so.
    assert.match(cards[4].textContent, /Checked statement, kernel form \(shortened\)/);
    assert.equal(cards[4].querySelector('.checked-statement').textContent, 'Eq 1 …');
    assert.equal(cards[4].querySelector('.checked-kernel'), null);
    assert.equal(cards[0].querySelector('.checked-statement'), null);
    cards[3].click();
    await new Promise(resolve => setTimeout(resolve, 20));
    const inspector = window.document.getElementById('insp');
    assert.equal(inspector.querySelector('h5').textContent, 'Checked statement, as this project’s notation prints it');
    assert.equal(inspector.querySelector('.checked-statement').textContent, 'True ∧ <b>');
    assert.equal(inspector.querySelector('.checked-kernel .checked-statement').textContent, 'And True Demo.<b>');
    const plain = [...window.document.querySelectorAll('#doc .project-non-claim')];
    assert.deepEqual(plain.map(p => p.textContent), ['Line 6: example without a proposition; not a claim.']);
    assert.match(cards[0].textContent, /Kernel checked/);
    assert.doesNotMatch(cards[0].textContent, /Out of date/);
    assert.match(cards[1].textContent, /Contains sorry/);
    assert.match(cards[1].textContent, /Out of date/);
    assert.match(cards[1].textContent, /sorryAx/);
    assert.match(window.document.getElementById('doc').textContent, /Checking stopped at line 8: type mismatch/);
    assert.equal(cards[0].querySelector('.pill').dataset.tone, 'green');
    assert.equal(cards[1].querySelector('.pill').dataset.tone, 'amber');
    assert.equal(cards[1].querySelectorAll('.pill')[1].dataset.tone, 'amber');
    assert.equal(cards[2].querySelector('.pill').dataset.tone, 'blue');
    assert.match(cards[2].textContent, /constructor/);
    const before = requests.filter(r => r.path === '/api/notebook').length;
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'environmentChanged'}}));
    await new Promise(resolve => setTimeout(resolve, 700));
    assert.ok(requests.filter(r => r.path === '/api/notebook').length > before);
    window.document.getElementById('export-html').click();
    await new Promise(resolve => setTimeout(resolve, 80));
    const exported = requests.find(r => r.type === 'export');
    assert.ok(exported);
    assert.match(exported.body, /Checking stopped at line/);
    assert.match(exported.body, /Checked statement/);
    assert.match(exported.body, /Checked statement, kernel form \(shortened\)/);
    assert.match(exported.body, /not a claim/);
  } finally { dom.window.close(); }
});

test('project source cells show provider views under the cell', async () => {
  const media = path.resolve(__dirname, '../../dist/media');
  const text = 'def c := 1\n-- note\n-- @view circuit c\n';
  const run = async (error) => {
    const dom = new JSDOM(readFileSync(path.join(media, 'index.html'), 'utf8').replace('<body>', '<body><div id="editor-state"></div><button id="open-source"></button><button id="next-unresolved" hidden></button>'), {runScripts: 'outside-only', url: 'https://preview.invalid/'});
    const {window} = dom;
    const snapshot = {file: 'A.lean', sha256: 'b'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
      cells: [{index: 0, kind: 'source', start: 1, end: 3, text}],
      views: [{cell: 0, line: 3, kind: 'circuit', name: 'c', using: [], error}],
      preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}}};
    const fetched = [];
    window.Response = Response;
    window.matchMedia = () => ({matches: false});
    window.katex = {};
    window.fetch = async (url) => {
      fetched.push(String(url));
      const body = String(url).startsWith('/api/view') ? {svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'} : snapshot;
      return {ok: true, status: 200, json: async () => body};
    };
    window.acquireVsCodeApi = () => ({postMessage: message => {
      if (!['request', 'export'].includes(message.type)) return;
      const json = message.path.startsWith('/api/view') ? {svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'} : message.path === '/api/notebook' ? snapshot : {css: ''};
      queueMicrotask(() => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'response', id: message.id, status: 200, json}})));
    }});
    try {
      for (const file of ['bridge.js', 'review.js', 'circuits.js', 'cards.js', 'export.js', 'app.js']) window.eval(readFileSync(path.join(media, file), 'utf8'));
      await new Promise(resolve => setTimeout(resolve, 120));
      const doc = window.document.getElementById('doc');
      const view = doc.querySelector('.view-cell');
      assert.equal(view.dataset.documentStart, '3', 'the card stands at its directive');
      assert.ok(view.previousElementSibling.matches('.context-card'), 'after the note above it');
      assert.equal(view.nextElementSibling, null);
      assert.doesNotMatch(doc.textContent, /-- @view circuit c/, 'a served directive is its card');
      return view.cloneNode(true);
    } finally { dom.window.close(); }
  };
  const ok = await run('');
  assert.match(ok.querySelector('img').getAttribute('src'), /^data:image\/svg\+xml/);
  const bad = await run('No installed provider draws `x` views.');
  assert.match(bad.textContent, /No installed provider draws `x` views\./);
});

test('transient view errors are asked again; lasting ones only after a check changes', async () => {
  const text = 'def c := 1\n-- @view circuit c\n';
  const snapshot = {file: 'A.lean', sha256: 'b'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: 2, text}],
    views: [{cell: 0, line: 2, kind: 'circuit', name: 'c', using: [], error: ''}],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}, check: {status: 'running', message: 'Checking', theorems: []}}};
  const answers = [
    {error: 'Another Lean run is in progress; retry when it finishes.', transient: true},
    {error: 'unknown identifier c'},
    {svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'},
  ];
  const asked = [];
  const window = await renderSnapshot(snapshot, {onView: path => { asked.push(path); return answers[asked.length - 1]; }});
  const settle = () => new Promise(resolve => setTimeout(resolve, 30));
  const body = () => window.document.querySelector('.view-cell').textContent;
  try {
    await settle();
    assert.equal(asked.length, 1);
    assert.match(body(), /Another Lean run is in progress/);
    await window.glitnirPoll(); await settle();
    assert.equal(asked.length, 2, 'a transient error is asked again on the next snapshot');
    assert.match(body(), /unknown identifier c/);
    await window.glitnirPoll(); await settle();
    assert.equal(asked.length, 2, 'a lasting error is kept while the check status is unchanged');
    snapshot.preview.check = {status: 'checked_snapshot', source_sha256: 'b'.repeat(64), message: 'Done', theorems: []};
    await window.glitnirPoll(); await settle();
    assert.equal(asked.length, 3, 'a finished check asks again');
    assert.ok(window.document.querySelector('.view-cell img'));
  } finally { window.close(); }
});

test('a view inside a saved claim cell is drawn under the claim card', async () => {
  const text = 'theorem t : True := by\n  -- @view circuit c\n  trivial\n';
  const snapshot = {file: 'A.lean', sha256: 'b'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: 3, text, claim: 't'}],
    project_claims: [{name: 't', status: 'checked', current: true, axioms: [], start_line: 1, end_line: 3}],
    views: [{cell: 0, line: 2, kind: 'circuit', name: 'c', using: [], error: ''}],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}}};
  const asked = [];
  const window = await renderSnapshot(snapshot, {onView: path => { asked.push(path); return {svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'}; }});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    const card = window.document.querySelector('#doc .project-claim');
    assert.ok(card);
    const view = card.nextElementSibling;
    assert.ok(view.classList.contains('view-cell'));
    assert.match(view.querySelector('img').getAttribute('src'), /^data:image\/svg\+xml/);
    assert.deepEqual(asked, ['/api/view?cell=0&line=2']);
    assert.equal(card.querySelector('.pill').textContent, 'Kernel checked', 'a view never changes the claim status');
  } finally { window.close(); }
});

test('a view drawn with the notebook\'s proofs skipped says so', async () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"/>';
  for (const [drawing, shown] of [[{svg, proofs_skipped: true}, true], [{svg}, false]]) {
    const snapshot = {file: 'A.lean', sha256: 'd'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
      cells: [{index: 0, kind: 'source', start: 1, end: 1, text: '-- @view circuit c'}], views: [{cell: 0, line: 1, kind: 'circuit', name: 'c', using: [], error: ''}],
      preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}}};
    const window = await renderSnapshot(snapshot, {onView: () => drawing});
    try {
      await new Promise(resolve => setTimeout(resolve, 30));
      const view = window.document.querySelector('#doc .view-cell');
      assert.match(view.querySelector('img').getAttribute('src'), /^data:image\/svg\+xml/);
      const note = [...view.querySelectorAll('.note-line')].find(line => /proofs/.test(line.textContent));
      assert.equal(Boolean(note), shown);
      if (shown) {
        assert.equal(note.textContent, 'Drawn without re-running the notebook\'s proofs.');
        assert.equal(note.dataset.tone, 'muted');
      }
    } finally { window.close(); }
  }
});

test('a failed view keeps the lines of its Lean error, as text', async () => {
  const snapshot = {file: 'A.lean', sha256: 'c'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: 1, text: '-- @view model m'}], views: [{cell: 0, line: 1, kind: 'model', name: 'm', using: [], error: ''}],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}}};
  const error = 'Line 1: Ambiguous term\n  Circuit\nPossible interpretations:\n  <b>Left.Circuit</b> : Type\n  Right.Circuit : Type';
  const window = await renderSnapshot(snapshot, {onView: () => ({error})});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    const message = window.document.querySelector('#doc .view-cell .lean-message');
    assert.ok(message, 'the error is shown in the view card');
    assert.equal(message.textContent, `No drawing: ${error}`, 'every line is kept');
    assert.equal(message.querySelector('b'), null, 'the error is escaped');
  } finally { window.close(); }
});

test('project inspection runs only on click and unsaved files stay off the wire', async () => {
  const requests = [];
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells: [], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/project', toolchain: 'leanprover/lean4:v4.32.1'}}};
  const window = await renderSnapshot(snapshot, {onMessage: message => requests.push(message)});
  try {
    const button = window.document.getElementById('inspect-project');
    assert.equal(button.hidden, false);
    assert.equal(requests.filter(r => r.path === '/api/project/inspect').length, 0);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true, mode: 'project'}}));
    button.click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(requests.filter(r => r.path === '/api/project/inspect').length, 0);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: false, mode: 'project'}}));
    button.click();
    await new Promise(resolve => setTimeout(resolve, 30));
    const sent = requests.filter(r => r.path === '/api/project/inspect');
    assert.equal(sent.length, 1);
    assert.equal(sent[0].method, 'POST');
    assert.deepEqual(JSON.parse(sent[0].body), {sha256: snapshot.sha256});
    assert.equal(window.document.getElementById('replay-all'), null);
    assert.match(window.document.getElementById('preview-footer').textContent, /Checks are explicit/);
  } finally { window.close(); }
});

test('project inspection is reported from snapshots for the saved source it read', async () => {
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells: [], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/project', toolchain: 't'},
    inspection: {status: 'running', source_sha256: 'a'.repeat(64), message: 'Inspecting the saved project setup'}}};
  const requests = [];
  const window = await renderSnapshot(snapshot, {onMessage: message => requests.push(message)});
  const $ = id => window.document.getElementById(id);
  try {
    assert.equal($('inspect-project').disabled, true, 'one inspection or check at a time');
    assert.equal($('inspect-project').textContent, 'Inspecting…');
    assert.equal($('check-project').disabled, true);
    assert.match($('project-inspection').textContent, /Inspecting the saved project setup…/);
    $('project-inspection').querySelector('[data-act="inspect-cancel"]').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(requests.filter(r => r.path === '/api/project/inspect/cancel').map(r => [r.method, r.body]), [['POST', '{}']], 'Stop cancels the inspection');
    snapshot.preview.inspection = {status: 'resolved', source_sha256: 'a'.repeat(64), message: 'Module setup resolved for <A>.', resolution: {kind: 'prepared_local', packages: [{name: 'dep', path: 'p', entry: {}}]}};
    await window.glitnirPoll();
    assert.equal($('inspect-project').disabled, false);
    assert.match($('project-inspection').textContent, /^Last inspection: Module setup resolved for <A>\./);
    assert.match($('project-inspection').innerHTML, /&lt;A&gt;/, 'the message is escaped');
    snapshot.preview.inspection.source_sha256 = 'b'.repeat(64);
    await window.glitnirPoll();
    assert.equal($('project-inspection').hidden, true, 'an inspection of another saved source is not shown');
  } finally { window.close(); }
});

test('a running check, also one waiting for another Lean run, can be stopped', async () => {
  const requests = [];
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells: [], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'},
    check: {status: 'running', source_sha256: 'a'.repeat(64), message: 'Waiting for another Lean run to finish.', theorems: []}}};
  const window = await renderSnapshot(snapshot, {onMessage: message => requests.push(message)});
  try {
    const panel = window.document.getElementById('project-check');
    assert.match(panel.textContent, /Waiting for another Lean run to finish/);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    panel.querySelector('[data-act="check-cancel"]').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(requests.filter(r => r.path === '/api/project/check/cancel').map(r => [r.method, r.body]), [['POST', '{}']], 'sent even with unsaved Lean files');
  } finally { window.close(); }
});

test('views are drawn in the background: progress, Stop and Draw again', async () => {
  const requests = [];
  const job = 'a1'.repeat(16), other = 'b2'.repeat(16), next = 'c3'.repeat(16);
  const answers = {
    view: [
      {status: 'drawing', message: 'Preparing the view.', job},
      {status: 'queued', message: 'Waiting for another view to finish drawing.', job: other},
      {status: 'drawing', message: 'Lean is drawing it.', job: next},
    ],
    [job]: [{status: 'drawing', message: 'Waiting for another Lean run to finish.', job}, {error: 'View drawing cancelled.'}],
    [other]: [{status: 'drawing', message: 'Lean is drawing it.', job: other}, {svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'}],
    [next]: [{svg: '<svg xmlns="http://www.w3.org/2000/svg"/>', uncertain: true}],
  };
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [],
    cells: [{index: 0, kind: 'source', start: 1, end: 1, text: '-- @view circuit c'}], views: [{cell: 0, line: 1, kind: 'circuit', name: 'c', using: [], error: ''}],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}}};
  const onView = path => {
    const id = /^\/api\/view\/job\?id=([0-9a-f]{32})$/.exec(path)?.[1];
    return (id ? answers[id] : answers.view).shift();
  };
  const window = await renderSnapshot(snapshot, {onMessage: message => requests.push(message), onView});
  const view = () => window.document.querySelector('#doc .view-cell');
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const asked = () => requests.filter(r => r.path?.startsWith('/api/view')).map(r => r.path.replace(/[0-9a-f]{32}/, id => ({[job]: 'job', [other]: 'other', [next]: 'next'})[id]));
  try {
    assert.equal(view().querySelector('.view-wait').textContent, 'Preparing the view.');
    await wait(2100);
    assert.equal(view().querySelector('.view-wait').textContent, 'Waiting for another Lean run to finish.');
    const stop = view().querySelector('[data-act="view-cancel"]');
    assert.ok(stop, 'a drawing in progress can be stopped');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    stop.click();
    await wait(30);
    const cancel = requests.filter(r => r.path === '/api/view/cancel');
    assert.equal(cancel.length, 1, 'stopping is sent even with unsaved Lean files');
    assert.deepEqual([cancel[0].method, JSON.parse(cancel[0].body)], ['POST', {sha256: snapshot.sha256, line: 1}], 'Stop names this card only');
    await wait(2100);
    assert.match(view().textContent, /No drawing: View drawing cancelled\./);
    assert.equal(view().querySelector('[data-act="view-cancel"]'), null);
    await window.glitnirPoll();
    assert.match(view().textContent, /cancelled/, 'a cancelled drawing is not asked again unasked');
    view().querySelector('[data-act="view-retry"]').click();
    await wait(30);
    assert.equal(view().querySelector('.view-wait').textContent, 'Waiting for another view to finish drawing.');
    assert.equal(view().querySelector('[data-act="view-cancel"]'), null, 'a queued view has nothing to stop');
    await wait(2100);
    assert.equal(view().querySelector('.view-wait').textContent, 'Waiting for another view to finish drawing.', 'the card keeps its own state');
    await wait(6300);
    assert.match(view().querySelector('img').getAttribute('src'), /^data:image\/svg\+xml/);
    assert.match(view().textContent, /May be out of date: the state of a dependency could not be read/);
    // Polls go to the job's own route; the notebook route is asked only to start.
    assert.deepEqual(asked(), ['/api/view?cell=0&line=1', '/api/view/job?id=job', '/api/view/cancel', '/api/view/job?id=job',
      '/api/view?cell=0&line=1', '/api/view/job?id=other', '/api/view/job?id=other', '/api/view?cell=0&line=1', '/api/view/job?id=next']);
  } finally { window.close(); }
});

test('project checks show historical compiled propositions separately from source', async () => {
  const requests = [];
  const snapshot = {file: 'A.lean', sha256: 'b'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells: [], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}, check: {status: 'checked_snapshot', source_sha256: 'a'.repeat(64), environment_sha256: 'c'.repeat(64), message: 'Last checked snapshot', resolution: {kind: 'prepared_local', packages: [{name: '<dep>', path: 'vendor/dep', entry: {rev: 'abc'}}]}, theorems: [{name: 'N.result', type: '<script>x</script>', status: 'assumptions', axioms: [{name: 'custom', module: 'A'}]}, {name: '_private.A.0._glitnir_example_40', example: true, start_line: 3, end_line: 3, type: '1 = 1', status: 'checked', axioms: []}]}}};
  const window = await renderSnapshot(snapshot, {katex: true, onMessage: message => requests.push(message)});
  try {
    const panel = window.document.getElementById('project-check');
    assert.match(panel.textContent, /example \(line 3\) ·\s*Kernel checked/);
    assert.doesNotMatch(panel.textContent, /_glitnir_example/);
    assert.equal(panel.hidden, false);
    assert.match(panel.textContent, /Last checked snapshot/);
    assert.match(panel.textContent, /Git revision and worktree cleanliness were not verified/);
    assert.match(panel.textContent, /<dep>: vendor\/dep/);
    assert.match(panel.textContent, /saved source has changed/);
    assert.match(panel.textContent, /N.result/);
    assert.match(panel.textContent, /Additional assumptions/);
    assert.match(panel.textContent, /custom/);
    assert.equal(panel.querySelector('script'), null);
    window.document.getElementById('export-html').click();
    await new Promise(resolve => setTimeout(resolve, 50));
    const exported = requests.find(r => r.type === 'export');
    assert.ok(exported);
    assert.match(exported.body, /Last checked snapshot/);
    assert.match(exported.body, /declared revision abc \(not verified\)/);
    assert.match(exported.body, /Additional assumptions/);
    assert.match(exported.body, /saved source has changed/);
    assert.equal(window.document.querySelectorAll('.claim').length, 0);
    assert.equal(requests.filter(r => r.path === '/api/project/check').length, 0);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true, mode: 'project'}}));
    window.document.getElementById('check-project').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(requests.filter(r => r.path === '/api/project/check').length, 0);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: false, mode: 'project'}}));
    window.document.getElementById('check-project').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    const sent = requests.filter(r => r.path === '/api/project/check');
    assert.equal(sent.length, 1);
    assert.deepEqual(JSON.parse(sent[0].body), {sha256: snapshot.sha256});
  } finally { window.close(); }
});


test('review navigation visits unresolved claims without checking and resets after saved source changes', async () => {
  const statuses = ['checked', 'open', 'refuted', 'failed', 'conditional'];
  const cells = statuses.map((status, index) => ({id: `claim_${index}`, index, kind: 'claim', start: index * 2 + 1, end: index * 2 + 2, text: `theorem claim_${index} : True := by\n  sorry`}));
  const claims = statuses.map((status, index) => ({name: `claim_${index}`, keyword: 'theorem', statement: ': True', proof: 'sorry', proof_block: '  sorry', status, reason: '', uses: [], context: 'c'.repeat(64), record: null}));
  const snapshot = {file: 'Example.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, cells, claims, links: [], revisions: [], assets: []};
  const requests = [];
  const window = await renderSnapshot(snapshot, {onMessage: message => requests.push(message)});
  try {
    window.HTMLElement.prototype.scrollIntoView = function() {};
    const button = window.document.getElementById('next-unresolved');
    const selected = () => window.document.querySelector('.source-selected')?.dataset.index;
    assert.equal(button.hidden, false);
    assert.match(button.title, /3 matching claims/);
    button.click(); assert.equal(selected(), '1');
    button.click(); assert.equal(selected(), '3');
    button.click(); assert.equal(selected(), '4');
    button.click(); assert.equal(selected(), '4', 'end does not wrap');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    assert.equal(button.disabled, true);
    button.click(); assert.equal(selected(), '4', 'end does not wrap');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: false}}));
    snapshot.sha256 = 'b'.repeat(64);
    await window.glitnirPoll();
    button.click(); assert.equal(selected(), '1', 'new saved text resets navigation instead of reusing an old index');
    const dependency = window.document.createElement('button');
    dependency.dataset.act = 'goto'; dependency.dataset.id = 'claim_1';
    window.document.querySelector('.cellwrap[data-index="4"]').append(dependency);
    dependency.click();
    button.click(); assert.equal(selected(), '3', 'dependency navigation continues after the destination, not the containing claim');
    window.document.querySelector('.app').dispatchEvent(new window.KeyboardEvent('keydown', {key: 'k', bubbles: true}));
    await Promise.resolve();
    button.click(); assert.equal(selected(), '3', 'keyboard navigation updates the review position');
    snapshot.sha256 = 'c'.repeat(64);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 3, sha256: snapshot.sha256}}));
    await new Promise(resolve => setTimeout(resolve, 30));
    button.click(); assert.equal(selected(), '4', 'a host reveal retains its position through a source refresh');

    claims.forEach(c => { c.status = 'checked'; });
    await window.glitnirPoll();
    assert.equal(button.disabled, true);
    assert.equal(button.title, 'No matching claims');
    snapshot.check_errors.claim_3 = 'Replay failed to run';
    await window.glitnirPoll();
    window.document.getElementById('review-previous').click(); assert.equal(selected(), '3', 'a failed last check remains reviewable');
    assert.equal(requests.filter(r => r.method === 'POST').length, 0, 'stepping never starts a check or changes a proof');
    snapshot.preview = {mode: 'project', project: {kind: 'loose'}};
    await window.glitnirPoll();
    assert.equal(button.hidden, false, 'project snapshots review their saved results');
    assert.equal(button.disabled, true);
    assert.equal(button.title, 'Nothing needs attention', 'a project without saved results has nothing to step through');
  } finally { window.close(); }
});

test('proof review uses scoped observations, stays read-only while stepping, and scopes keyboard help', async () => {
  const claims = [{name: 'pair', keyword: 'theorem', statement: ': True ∧ True', proof: 'constructor\n· trivial\n· trivial', proof_block: '  constructor\n  · trivial\n  · trivial', status: 'stale', reason: 'changed', uses: [], context: 'c'.repeat(64), record: null}];
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), claims, checking: [], check_errors: {}, links: [], revisions: [], assets: [], cells: [{id: 'pair', index: 0, kind: 'claim', start: 1, end: 4, text: 'theorem pair : True ∧ True := by\n  constructor\n  · trivial\n  · trivial'}]};
  const nodes = [
    {id: 0, parent: null, text: 'constructor', range: {start: {line: 2, character: 2}, end: {line: 2, character: 13}}, before: ['⊢ True ∧ True'], after: ['left ⊢ True', 'right ⊢ True']},
    {id: 1, parent: null, text: '· trivial', range: {start: {line: 3, character: 2}, end: {line: 3, character: 11}}, before: ['left ⊢ True'], after: []},
  ];
  const trace = {id: 'record', status: 'ready', source_sha256: snapshot.sha256, environment: {kind: 'read_only_live_dependencies', toolchain: 'Lean 4.32.1'}, claim: 'pair', nodes, warnings: ['illustrative warning'], errors: [], message: 'Recorded goals'};
  const messages = [];
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: () => trace});
  const $ = id => window.document.getElementById(id);
  const app = window.document.querySelector('.app');
  const key = (key, target = app) => target.dispatchEvent(new window.KeyboardEvent('keydown', {key, bubbles: true}));
  try {
    key('t', window.document.body);
    assert.equal($('proof-review').hidden, true);
    key('t');
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal($('proof-review').hidden, false);
    assert.equal($('review-position').textContent, 'Initial goals');
    const requested = messages.filter(m => m.path === '/api/proof/start');
    assert.equal(requested.length, 1);
    assert.deepEqual(JSON.parse(requested[0].body), {sha256: snapshot.sha256, claim: 'pair'});
    key('n'); assert.match($('proof-after').textContent, /right/);
    key('n'); assert.match($('proof-after').textContent, /No goals in this tactic scope/);
    assert.match($('proof-before-text').textContent, /left/);
    assert.match($('proof-status').textContent, /Out of date/);
    assert.equal($('next-unresolved').disabled, true);
    key('n'); assert.equal($('review-position').textContent, 'Step 2 of 2');
    key('e'); assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1).trace)), {id: 'record', node: 1});
    key('Escape'); key('t'); assert.equal($('review-position').textContent, 'Step 2 of 2');
    assert.equal(messages.filter(m => m.path === '/api/proof/start').length, 1);
    key('?'); key('p'); assert.equal($('review-position').textContent, 'Step 2 of 2');
    assert.match($('keys').textContent, /Ctrl\+K then G/, 'the reveal chord, outside macOS');
    assert.match($('keys').textContent, /Switch between document and Lean source/);
    key('Escape', $('keys-close'));
    $('review-letter-keys').checked = false; key('p'); assert.equal($('review-position').textContent, 'Step 2 of 2');
    $('review-letter-keys').checked = true;
    const edit = window.document.createElement('div'); edit.contentEditable = 'true'; edit.setAttribute('contenteditable', 'true'); app.append(edit);
    key('p', edit); assert.equal($('review-position').textContent, 'Step 2 of 2');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'environmentChanged'}}));
    assert.equal($('proof-tactics').children.length, 0);
    assert.match($('proof-message').textContent, /changed/);
    assert.equal(messages.filter(m => m.method === 'POST' && m.path !== '/api/proof/start').length, 0);
  } finally { window.close(); }
});

test('review follows source selection, exact editor returns and every help close path', async () => {
  const claims = ['one', 'two'].map(name => ({name, keyword: 'theorem', statement: ': True', proof: 'trivial', proof_block: '  trivial', status: 'stale', uses: [], context: 'c'.repeat(64)}));
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), claims, checking: [], check_errors: {}, links: [], revisions: [], assets: [], cells: claims.map((c, index) => ({index, id: c.name, kind: 'claim', start: index * 2 + 1, end: index * 2 + 2, text: `theorem ${c.name} : True := by\n  trivial`}))};
  const messages = [];
  let trace;
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: message => {
    if (message.path === '/api/proof/start') {
      const claim = JSON.parse(message.body).claim;
      const line = claim === 'one' ? 2 : 4;
      trace = {id: claim, claim, status: 'ready', source_sha256: snapshot.sha256, nodes: [
        {id: 0, parent: null, start: 10, end: 30, text: 'parent', range: {start: {line, character: 0}, end: {line, character: 20}}, before: ['parent goal'], after: []},
        {id: 1, parent: 0, start: 15, end: 20, text: 'child', range: {start: {line, character: 5}, end: {line, character: 10}}, before: ['child goal'], after: []},
      ]};
    }
    return trace;
  }});
  window.HTMLElement.prototype.scrollIntoView = function () {};
  const $ = id => window.document.getElementById(id), app = window.document.querySelector('.app');
  const key = k => app.dispatchEvent(new window.KeyboardEvent('keydown', {key: k, bubbles: true}));
  const settle = () => new Promise(resolve => setTimeout(resolve, 30));
  try {
    key('s'); key('j'); await settle();
    const sourceRow = window.document.querySelector('#source tr[data-selected="true"]');
    assert.equal(sourceRow.dataset.claim, 'two');
    sourceRow.focus(); key('t'); await settle();
    assert.equal($('proof-title').textContent, 'two');
    assert.equal(window.document.activeElement.id, 'review-proof');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 1, sha256: snapshot.sha256, line: 4, character: 7}}));
    assert.equal($('review-position').textContent, 'Step 2 of 2');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 0, sha256: snapshot.sha256, line: 2, character: 7}}));
    await settle();
    assert.equal($('proof-title').textContent, 'one');
    assert.equal($('review-position').textContent, 'Step 2 of 2');
    const startsBeforeChangedSource = messages.filter(m => m.path === '/api/proof/start').length;
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 2, sha256: 'b'.repeat(64), line: 4, character: 7}}));
    await settle();
    assert.equal(messages.filter(m => m.path === '/api/proof/start').length, startsBeforeChangedSource, 'wait for the matching snapshot before resolving changed indices');
    snapshot.sha256 = 'b'.repeat(64); snapshot.cells[1].index = 2;
    await window.glitnirPoll(); await settle();
    assert.equal($('proof-title').textContent, 'two');
    $('review-proof').focus(); key('?'); $('keys-close').click(); await settle();
    assert.equal(window.document.activeElement.id, 'review-proof');
    $('keys-open').focus(); $('keys-open').click(); $('keys').click(); await settle();
    assert.equal(window.document.activeElement.id, 'keys-open');
    window.document.querySelector('[data-mode="source"]').click();
    assert.equal($('proof-review').hidden, true);
  } finally { window.close(); }
});

test('project proof steps edit the exact recorded tactic without claim metadata', async () => {
  const snapshot = {file: 'Loose.lean', sha256: 'a'.repeat(64), preview: {mode: 'project', project: {kind: 'loose'}}, claims: [], checking: [], check_errors: {}, links: [], revisions: [], assets: [], cells: [{index: 0, kind: 'source', start: 1, end: 2, text: 'theorem t : True := by\n  trivial'}]};
  const trace = {id: 'project-record', status: 'ready', source_sha256: snapshot.sha256, nodes: [{id: 0, parent: null, text: 'trivial', range: {start: {line: 2, character: 2}, end: {line: 2, character: 9}}, before: ['⊢ True'], after: []}]};
  const messages = [];
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: () => trace});
  const key = key => window.document.querySelector('.app').dispatchEvent(new window.KeyboardEvent('keydown', {key, bubbles: true}));
  try {
    key('t'); await new Promise(resolve => setTimeout(resolve, 30)); key('n'); key('e');
    assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), {type: 'source', index: 0, sha256: snapshot.sha256, trace: {id: 'project-record', node: 0}});
  } finally { window.close(); }
});

test('switching a running proof queues once and explicit cancel cancels that intent', async () => {
  const claims = ['one', 'two'].map(name => ({name, keyword: 'theorem', statement: ': True', proof: 'trivial', status: 'stale', uses: [], context: 'c'.repeat(64)}));
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), claims, checking: [], check_errors: {}, links: [], revisions: [], assets: [], cells: claims.map((c, index) => ({index, id: c.name, kind: 'claim', start: index + 1, end: index + 1, text: `theorem ${c.name} : True := by trivial`}))};
  let trace, starts = 0;
  const window = await renderSnapshot(snapshot, {onProof: message => {
    if (message.path === '/api/proof/start') trace = {id: String(++starts), claim: JSON.parse(message.body).claim, status: 'running', source_sha256: snapshot.sha256, nodes: []};
    if (message.path === '/api/proof/cancel') trace.status = 'cancelled';
    return trace;
  }});
  window.HTMLElement.prototype.scrollIntoView = function () {};
  const $ = id => window.document.getElementById(id);
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  try {
    $('review-proof').click(); await settle(30);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 1, sha256: snapshot.sha256, line: 2, character: 0}}));
    $('proof-cancel').click(); await settle(850);
    assert.equal(starts, 1, 'cancel also cancels the queued replacement');
    $('proof-refresh').click(); await settle(30);
    trace.status = 'ready'; await settle(850);
    assert.equal(starts, 2, 'a completed replacement does not restart itself');
  } finally { window.close(); }
});

function liveSnapshot(text = 'First text', sha = 'a') {
  return {file: 'Live.lean', sha256: sha.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [], cells: [
    {index: 0, kind: 'prose', start: 1, end: 3, text: `/-! ${text} -/`, markdown: text},
    {index: 1, kind: 'prose', start: 4, end: 6, text: '/-! Keep reading -/', markdown: 'Keep reading'},
  ]};
}

test('live saved edits coalesce, highlight changes, and follow only by choice', async () => {
  let snapshot = liveSnapshot(), reads = 0;
  const messages = [];
  const window = await renderSnapshot(snapshot, {onNotebook: () => { reads++; return {json: snapshot}; }, onMessage: m => messages.push(m)});
  const $ = id => window.document.getElementById(id);
  const changed = () => window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'sourceChanged'}}));
  const pause = () => new Promise(resolve => setTimeout(resolve, 360));
  const scrolled = [];
  window.HTMLElement.prototype.scrollIntoView = function() { scrolled.push(this); };
  try {
    assert.equal($('live-follow').checked, false);
    snapshot = liveSnapshot('Agent wrote this', 'b');
    changed(); changed(); changed();
    await pause();
    assert.equal(reads, 2, 'one refresh for a burst of writes');
    assert.match($('doc').textContent, /Agent wrote this/);
    assert.equal($('doc').querySelectorAll('.live-changed').length, 1);
    assert.equal(scrolled.length, 0);
    $('live-follow').checked = true;
    $('live-follow').dispatchEvent(new window.Event('change'));
    $('live-follow').focus();
    snapshot = liveSnapshot('Another write', 'c'); changed(); await pause();
    assert.equal(scrolled.length, 1);
    assert.equal(window.document.activeElement, $('live-follow'), 'follow does not steal keyboard focus');
    $('keys-open').click();
    snapshot = liveSnapshot('While help is open', 'd'); changed(); await pause();
    assert.equal(scrolled.length, 1, 'no scrolling behind a dialog');
    assert.equal($('keys').hidden, false);
    assert.equal(messages.filter(m => m.method === 'POST').length, 0, 'live rendering never starts checks');
    await new Promise(resolve => setTimeout(resolve, 2500));
    assert.equal($('doc').querySelectorAll('.live-changed').length, 0, 'highlights fade without another update');
  } finally { window.close(); }
});

test('live updates retain an inactive readable view through parse errors and deletion', async () => {
  let reply = {json: liveSnapshot()};
  const window = await renderSnapshot(reply.json, {onNotebook: () => reply});
  const $ = id => window.document.getElementById(id);
  try {
    reply = {json: {...liveSnapshot('', 'b'), cells: [], error: 'unfinished comment'}};
    await window.glitnirPoll();
    assert.match($('doc').textContent, /First text/);
    assert.equal($('doc').inert, true);
    assert.match($('live-status').textContent, /previous|last readable/i);
    reply = {status: 409, json: {error: 'file missing'}};
    await window.glitnirPoll();
    assert.match($('doc').textContent, /First text/);
    assert.equal($('doc').inert, true);
    reply = {json: liveSnapshot('Recovered source', 'c')};
    await window.glitnirPoll();
    assert.match($('doc').textContent, /Recovered source/);
    assert.equal($('doc').inert, false);
  } finally { window.close(); }
});

test('live update requests serialize and ignore a response superseded by a saved write', async () => {
  let snapshot = liveSnapshot(), resolveRead, defer = false, reads = 0;
  const window = await renderSnapshot(snapshot, {onNotebook: () => {
    reads++;
    return defer ? new Promise(resolve => { resolveRead = resolve; }) : {json: snapshot};
  }});
  const $ = id => window.document.getElementById(id);
  try {
    defer = true;
    const oldRead = window.glitnirPoll();
    await new Promise(resolve => setTimeout(resolve, 5));
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'sourceChanged'}}));
    await new Promise(resolve => setTimeout(resolve, 350));
    assert.equal(reads, 2, 'at most one request in flight');
    defer = false;
    snapshot = liveSnapshot('Newest source', 'c');
    resolveRead({json: liveSnapshot('Obsolete response', 'b')});
    await oldRead;
    assert.doesNotMatch($('doc').textContent, /Obsolete response/);
    await new Promise(resolve => setTimeout(resolve, 350));
    assert.match($('doc').textContent, /Newest source/);
    assert.equal(reads, 3);
  } finally { window.close(); }
});

test('live unchanged polls preserve repeated prose anchors and settle duplicate save events', async () => {
  const snapshot = liveSnapshot();
  snapshot.cells.push({...snapshot.cells[0], index: 2, start: 7, end: 9});
  const window = await renderSnapshot(snapshot);
  const $ = id => window.document.getElementById(id);
  const shifts = [];
  window.scrollBy = (x, y) => shifts.push(y);
  const native = window.HTMLElement.prototype.getBoundingClientRect;
  window.HTMLElement.prototype.getBoundingClientRect = function() {
    if (this.classList.contains('cellwrap')) {
      const top = Number(this.dataset.index) === 2 ? -20 : -1000;
      return {top, bottom: top + 200};
    }
    return native.call(this);
  };
  try {
    await window.glitnirPoll();
    assert.deepEqual(shifts, [], 'the later identical prose section remains the anchor');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'sourceChanged'}}));
    await new Promise(resolve => setTimeout(resolve, 350));
    assert.equal($('live-status').textContent, 'Saved file is up to date');
  } finally { window.close(); }
});

test('reverse reveal waits for a fresh live snapshot after an obsolete poll', async () => {
  let snapshot = liveSnapshot(), resolveRead, defer = false;
  const window = await renderSnapshot(snapshot, {onNotebook: () => defer ? new Promise(resolve => { resolveRead = resolve; }) : {json: snapshot}});
  const scrolled = [];
  window.HTMLElement.prototype.scrollIntoView = function() { scrolled.push(this); };
  try {
    defer = true;
    const oldRead = window.glitnirPoll();
    await new Promise(resolve => setTimeout(resolve, 5));
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'sourceChanged'}}));
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 1, sha256: 'c'.repeat(64)}}));
    defer = false; snapshot = liveSnapshot('New source', 'c');
    resolveRead({json: liveSnapshot('Old source', 'b')});
    await oldRead;
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(scrolled.some(el => el.dataset.index === '1'));
    assert.equal(window.document.querySelector('.source-selected')?.dataset.index, '1');
  } finally { window.close(); }
});

test('proof review becomes unavailable cleanly if its saved file cannot be read', async () => {
  let reply = {json: {...liveSnapshot(), preview: {mode: 'project', project: {}, notice: ''}}};
  const window = await renderSnapshot(reply.json, {onNotebook: () => reply, onProof: () => ({status: 'ready', id: 'trace', source_sha256: 'a'.repeat(64), nodes: [], environment: {toolchain: 'Lean'}})});
  const $ = id => window.document.getElementById(id);
  try {
    $('review-proof').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    reply = {status: 409, json: {error: 'File temporarily missing'}};
    await window.glitnirPoll();
    assert.equal($('proof-title').textContent, 'Saved source unavailable');
    assert.equal($('proof-refresh').disabled, true);
    assert.equal($('proof-tactics').children.length, 0);
  } finally { window.close(); }
});

function outlineSnapshot() {
  const statuses = ['checked', 'open', 'stale', 'conditional', 'refuted', 'failed', 'unknown'];
  const claims = statuses.map((status, i) => ({name: `step${i}`, keyword: 'theorem', statement: ': True', proof: status === 'open' ? 'sorry' : 'trivial', status, reason: '', uses: [], context: 'c'.repeat(64)}));
  let line = 1;
  const cells = [];
  const prose = markdown => { const text = `/-!\n${markdown}\n-/`; const start = line; line += text.split('\n').length; cells.push({index: cells.length, kind: 'prose', start, end: line - 1, text, markdown}); };
  const addClaim = c => { const text = `theorem ${c.name} : True := by\n  ${c.proof}`; cells.push({index: cells.length, kind: 'claim', id: c.name, start: line, end: line + 1, text}); line += 2; };
  prose('# Example\n\nOverview before any fold.\n\n## Main argument {fold}\n\nA short strategy with $x^2$.\n\nExtra explanation.\n\n### Local step {fold}\n\nIts assumptions and conclusion.');
  claims.forEach(addClaim);
  prose('### Local step {fold}\n\nA different step with the same title.');
  prose('## Limitations\n\nScope stays visible outside the proof.');
  return {file: 'Outline.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, cells, claims, links: [], revisions: [], assets: []};
}

test('proof outline folds nested sections, keeps summaries and every claim status visible, and respects boundaries', async () => {
  const snapshot = outlineSnapshot();
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const d = window.document, folds = [...d.querySelectorAll('#doc details.proof-section')];
    assert.equal(folds.length, 3);
    assert.ok(folds.every(fold => !fold.open));
    assert.equal(folds[1].closest('.proof-section-body').parentElement, folds[0]);
    assert.match(folds[0].querySelector(':scope > summary').textContent, /Main argument.*A short strategy/s);
    assert.ok(folds[0].querySelector(':scope > summary .katex'));
    for (const label of ['Checked', 'Open goal', 'Out of date', 'Conditional', 'Refuted', 'Failed', 'Unknown']) {
      assert.match(folds[0].querySelector(':scope > summary .section-statuses').textContent, new RegExp(`1 ${label}`));
    }
    assert.equal(folds[0].querySelectorAll('.claim').length, 7);
    assert.match(d.getElementById('doc').firstElementChild.textContent, /Overview before any fold/);
    assert.match(d.getElementById('doc').lastElementChild.textContent, /Scope stays visible/);
    assert.equal(d.getElementById('doc').lastElementChild.closest('.proof-section'), null);
    assert.doesNotMatch(d.getElementById('doc').textContent, /\{fold\}/);
    assert.equal(d.querySelectorAll('#outline [data-act="scroll"]').length, 4);
    assert.equal(new Set([...d.querySelectorAll('#outline [data-act="scroll"]')].map(b => b.dataset.id)).size, 4);
  } finally { window.close(); }
});

test('proof outline navigation opens ancestors and retains independent folds and focus after refresh', async () => {
  const snapshot = outlineSnapshot();
  const window = await renderSnapshot(snapshot);
  try {
    const d = window.document;
    let scrolled;
    window.HTMLElement.prototype.scrollIntoView = function() { scrolled = this; };
    d.querySelector('#outline [data-act="goto"][data-id="step1"]').click();
    let folds = [...d.querySelectorAll('#doc details.proof-section')];
    assert.equal(folds[0].open, true);
    assert.equal(folds[1].open, true);
    assert.equal(folds[2].open, false);
    assert.equal(scrolled.id, 'cell-step1');
    folds[1].querySelector('summary').click();
    folds[1].querySelector('summary').focus();
    snapshot.claims[1].status = 'checked';
    snapshot.claims[1].proof = 'trivial';
    await window.glitnirPoll();
    folds = [...d.querySelectorAll('#doc details.proof-section')];
    assert.equal(folds[0].open, true);
    assert.equal(folds[1].open, false);
    assert.equal(d.activeElement, folds[1].querySelector('summary'));
    assert.match(folds[0].querySelector(':scope > summary .section-statuses').textContent, /2 Checked/);
    d.querySelectorAll('#outline [data-act="scroll"]')[2].click();
    folds = [...d.querySelectorAll('#doc details.proof-section')];
    assert.equal(folds[2].open, true, 'outline titles distinguish duplicate section names');
    assert.ok(folds[2].contains(scrolled));
    folds[0].open = false;
    const cell = snapshot.cells.find(c => c.id === 'step0');
    d.dispatchEvent(new window.CustomEvent('glitnir-reveal-cell', {detail: {index: cell.index, sha256: snapshot.sha256}}));
    folds = [...d.querySelectorAll('#doc details.proof-section')];
    assert.equal(folds[0].open, true);
    assert.equal(folds[1].open, true);
  } finally { window.close(); }
});

test('proof outlines export as safe script-free expandable sections with source and all statuses retained', async () => {
  const snapshot = outlineSnapshot();
  snapshot.cells[0].markdown = snapshot.cells[0].markdown.replace('Main argument', '<img src=x onerror=bad()>');
  const window = await renderSnapshot(snapshot, {katex: true});
  try {
    const d = window.document;
    assert.equal(d.querySelector('#doc img'), null);
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await d.getElementById('export-html').onclick();
    const exported = new JSDOM(html);
    try {
      const page = exported.window.document;
      const folds = [...page.querySelectorAll('details.proof-section')];
      assert.equal(folds.length, 3);
      assert.ok(folds.every(fold => fold.open), 'export starts open for fragment links and printing');
      assert.match(folds[0].querySelector('summary').textContent, /<img src=x onerror=bad\(\)>/);
      assert.match(folds[0].querySelector('summary').textContent, /Open goal/);
      assert.equal(page.querySelectorAll('.claim pre').length, 7);
      assert.equal(page.querySelectorAll('script,button,img,[data-act]').length, 0);
      assert.ok([...page.querySelectorAll('details.def-lean')].every(fold => !fold.open));
    } finally { exported.window.close(); }
  } finally { window.close(); }
});

test('proof section summaries retain source controls and document return targets', async () => {
  const snapshot = outlineSnapshot();
  snapshot.cells = [{index: 0, kind: 'prose', start: 1, end: 5, text: '/-!\n## Only heading {fold}\n\nOnly summary.\n-/', markdown: '## Only heading {fold}\n\nOnly summary.'}];
  snapshot.claims = [];
  const window = await renderSnapshot(snapshot, {browser: true});
  try {
    const d = window.document;
    window.HTMLElement.prototype.scrollIntoView = function() {};
    const summary = d.querySelector('#doc .proof-section > summary');
    assert.ok(summary.querySelector('.section-tools [data-act="source-lines"]'));
    summary.querySelector('[data-act="source-lines"]').click();
    d.querySelector('[data-act="document-return"]').click();
    assert.equal(d.activeElement, d.querySelector('#doc .proof-section > summary'));
    assert.equal(d.querySelector('.proof-section').open, true);
  } finally { window.close(); }
});

test('unchanged polls keep the visible fragment of a prose cell as the reading anchor', async () => {
  const window = await renderSnapshot(outlineSnapshot());
  try {
    const d = window.document, scrolls = [];
    window.scrollBy = (...args) => scrolls.push(args);
    const geometry = function() {
      const top = this.matches('#doc .cellwrap[data-index="0"]') ? (Number(this.dataset.fragment) < 2 ? -500 : 100) : 0;
      return {top, bottom: top + 50, left: 0, right: 0, width: 0, height: 50};
    };
    window.HTMLElement.prototype.getBoundingClientRect = geometry;
    await window.glitnirPoll();
    assert.deepEqual(scrolls, [], 'poll must not jump to the first fragment sharing a source cell');
  } finally { window.close(); }
});

test('a withheld project check says where checking stopped', async () => {
  const snapshot = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells: [], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}, check: {status: 'checked_snapshot', source_sha256: 'a'.repeat(64), environment_sha256: 'c'.repeat(64), message: 'Last checked snapshot: independently replayed.', withheld: {from_line: 7, message: 'unknown identifier'}, theorems: []}}};
  const window = await renderSnapshot(snapshot);
  try {
    const panel = window.document.getElementById('project-check');
    assert.match(panel.textContent, /Checking stopped at line 7: unknown identifier\. Claims from there on were not checked\./);
  } finally { window.close(); }
});

test('a withheld banner from an older version of the file says so', async () => {
  const cells = [{index: 0, kind: 'source', start: 1, end: 1, text: 'def x := 1\n'}];
  const base = {file: 'A.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells, project_claims: [], preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'loose'}}};
  const stale = await renderSnapshot({...base, project_withheld: {from_line: 3, message: 'type mismatch', stale: true}});
  try {
    assert.match(stale.document.getElementById('doc').textContent, /In the last check, checking stopped at line 3: type mismatch\. Claims from there on were not checked\./);
  } finally { stale.close(); }
  const current = await renderSnapshot({...base, project_withheld: {from_line: 3, message: 'type mismatch'}});
  try {
    const text = current.document.getElementById('doc').textContent;
    assert.match(text, /Checking stopped at line 3: type mismatch/);
    assert.doesNotMatch(text, /In the last check/);
  } finally { current.close(); }
});

const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>';
const svgReply = {status: 200, data: Buffer.from(SVG).toString('base64'), contentType: 'image/svg+xml'};
function illustrated(markdown, assets) {
  return {file: 'Pictured.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets,
    cells: [{index: 0, kind: 'prose', start: 1, end: 9, text: '/-! -/', markdown}]};
}

test('prose images load through the host at their hash and failures are named', async () => {
  const ok = {path: 'assets/a.svg', status: 'ok', sha256: 'b'.repeat(64)};
  const moved = {path: 'assets/moved.png', status: 'ok', sha256: 'c'.repeat(64)};
  const snapshot = illustrated([
    '# Pictured', '![Sketch <b>](assets/a.svg)', '![Gone](assets/gone.png)', '![Moved](assets/moved.png)',
    '![Web](https://example.invalid/x.png)', '![Unlisted](assets/unlisted.png)', '![Bad](assets/../x.png)',
  ].join('\n\n'), [ok, {path: 'assets/gone.png', status: 'missing', message: 'assets/gone.png is missing'}, moved,
    {path: 'assets/../x.png', status: 'invalid', message: "'assets/../x.png' is not a file in the assets/ folder"}]);
  const messages = [];
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onAsset: path => path.includes('c'.repeat(64)) ? {status: 409, json: {error: 'assets/moved.png changed since the preview read it; reload'}} : svgReply});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    const doc = window.document.getElementById('doc');
    const images = [...doc.querySelectorAll('img')];
    assert.equal(images.length, 1);
    assert.equal(images[0].getAttribute('src'), `data:image/svg+xml;base64,${Buffer.from(SVG).toString('base64')}`);
    assert.equal(images[0].alt, 'Sketch <b>');
    assert.equal(images[0].closest('figure').querySelector('figcaption').textContent, 'Sketch <b>');
    assert.equal(doc.querySelectorAll('figure b, script').length, 0);
    const asked = () => messages.filter(m => m.path?.startsWith('/asset')).map(m => m.path);
    assert.deepEqual(asked(), [`/asset?path=assets%2Fa.svg&sha256=${'b'.repeat(64)}`, `/asset?path=assets%2Fmoved.png&sha256=${'c'.repeat(64)}`]);
    assert.ok(asked().every(p => !/token|glitnir/i.test(p)), 'the capability never travels in the URL');
    const text = doc.textContent;
    assert.match(text, /assets\/gone\.png is missing/);
    assert.match(text, /assets\/moved\.png could not be shown: assets\/moved\.png changed since the preview read it/);
    assert.match(text, /External images are not loaded/);
    assert.match(text, /No image information for assets\/unlisted\.png/);
    assert.match(text, /is not a file in the assets\/ folder/);
    assert.doesNotMatch(text, /not shown in the preview yet/);
    // A loaded image is not asked for again; a changed one is asked at its new hash.
    window.glitnirPoll();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(asked().length, 2);
    moved.sha256 = 'd'.repeat(64);
    window.glitnirPoll();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(asked().at(-1), `/asset?path=assets%2Fmoved.png&sha256=${'d'.repeat(64)}`);
    assert.equal(window.document.querySelectorAll('#doc img').length, 2);
  } finally { window.close(); }
});

test('export embeds prose images as data URIs and keeps failures visible', async () => {
  const snapshot = illustrated('![Sketch](assets/a.svg)\n\n![Gone](assets/gone.png)',
    [{path: 'assets/a.svg', status: 'ok', sha256: 'b'.repeat(64)}, {path: 'assets/gone.png', status: 'missing', message: 'assets/gone.png is missing'}]);
  let answer;
  const pending = new Promise(resolve => { answer = resolve; });
  const window = await renderSnapshot(snapshot, {katex: true, onAsset: () => pending});
  try {
    assert.match(window.document.getElementById('doc').textContent, /Loading image/);
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    const exporting = window.document.getElementById('export-html').onclick();
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(html, undefined, 'export waits for images to load');
    answer(svgReply);
    await exporting;
    const page = new JSDOM(html);
    try {
      const d = page.window.document;
      const [img] = d.querySelectorAll('img');
      assert.equal(d.querySelectorAll('img').length, 1);
      assert.match(img.getAttribute('src'), /^data:image\/svg\+xml;base64,/);
      assert.equal(img.getAttribute('data-asset'), null);
      assert.match(d.body.textContent, /assets\/gone\.png is missing/);
      assert.match(d.querySelector('meta[http-equiv="Content-Security-Policy"]').content, /img-src data:/);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('the bridge refuses non-image replies for images', async () => {
  const snapshot = illustrated('![Page](assets/a.svg)', [{path: 'assets/a.svg', status: 'ok', sha256: 'b'.repeat(64)}]);
  const window = await renderSnapshot(snapshot, {onAsset: () => ({status: 200, data: Buffer.from('<p>hi</p>').toString('base64'), contentType: 'text/html'})});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(window.document.querySelectorAll('#doc img').length, 0);
    assert.match(window.document.getElementById('doc').textContent, /assets\/a\.svg could not be shown/);
  } finally { window.close(); }
});

test('a page without the editor host fetches images with the API header', async () => {
  const snapshot = illustrated('![Plot](assets/p.png)', [{path: 'assets/p.png', status: 'ok', sha256: 'b'.repeat(64)}]);
  const calls = [];
  const window = await renderSnapshot(snapshot, {browser: async (url, options = {}, page) => {
    calls.push({url, headers: options.headers});
    if (url.startsWith('/asset?')) return {ok: true, status: 200, blob: async () => new page.Blob([Buffer.from('PNG')], {type: 'image/png'})};
    return {ok: true, json: async () => snapshot};
  }});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    const [call] = calls.filter(c => c.url.startsWith('/asset?'));
    assert.equal(call.url, `/asset?path=assets%2Fp.png&sha256=${'b'.repeat(64)}`);
    assert.ok(call.headers['X-Glitnir']);
    assert.equal(window.document.querySelector('#doc img').getAttribute('src'), `data:image/png;base64,${Buffer.from('PNG').toString('base64')}`);
  } finally { window.close(); }
});

test('export renders the snapshot whose images it waited for, even after a poll', async () => {
  const asset = {path: 'assets/a.svg', status: 'ok', sha256: 'b'.repeat(64)};
  const snapshot = illustrated('![Sketch](assets/a.svg)', [asset]);
  const answers = [];
  const window = await renderSnapshot(snapshot, {katex: true, onAsset: () => new Promise(resolve => answers.push(resolve))});
  try {
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    const exporting = window.document.getElementById('export-html').onclick();
    await new Promise(resolve => setTimeout(resolve, 20));
    asset.sha256 = 'c'.repeat(64);
    window.glitnirPoll();
    await new Promise(resolve => setTimeout(resolve, 20));
    answers.shift()(svgReply);
    await new Promise(resolve => setTimeout(resolve, 20));
    answers.shift()(svgReply);
    await exporting;
    assert.doesNotMatch(window.document.body.textContent, /Export failed/);
    const page = new JSDOM(html);
    try {
      assert.equal(page.window.document.querySelectorAll('img').length, 1);
      assert.doesNotMatch(page.window.document.body.textContent, /Loading image/);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

// A project notebook as the server sends it: source cells split at the
// saved claims, which carry the project records' fields only.
function projectNotebook({current = true} = {}) {
  const cells = [
    {index: 0, kind: 'source', start: 1, end: 1, text: 'import Mathlib\n'},
    {index: 1, kind: 'prose', start: 2, end: 6, text: '/-!\n# Demo notebook\n## Facts {fold}\nSummary of the facts.\n-/\n', markdown: '# Demo notebook\n\n## Facts {fold}\n\nSummary of the facts.'},
    {index: 2, kind: 'source', start: 7, end: 10, text: 'namespace Demo\n\n/-- The rate. -/\nnoncomputable def k : ℝ := 1 / 2\n'},
    {index: 3, kind: 'source', start: 11, end: 14, text: '/-- The rate is $k > 0$, so it is damped. -/\ntheorem k_pos : 0 < k := by\n  unfold k\n  norm_num\n', claim: 'Demo.k_pos'},
    {index: 4, kind: 'source', start: 15, end: 17, text: '/-- Unproved. -/\ntheorem floor (x : ℝ) : 0 ≤ x ^ 2 := by\n  sorry\n', claim: 'Demo.floor'},
    {index: 5, kind: 'source', start: 18, end: 18, text: 'end Demo\n'},
  ];
  return {file: 'Demo.lean', sha256: 'a'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], cells, views: [], evidence: [], decisions: [],
    project_claims: [
      {name: 'Demo.k_pos', start_line: 11, end_line: 14, status: 'checked', current: true, axioms: [{name: 'propext', module: 'Init.Core'}, {name: 'Classical.choice', module: 'Init.Prelude'}]},
      {name: 'Demo.floor', start_line: 15, end_line: 17, status: 'admitted', current, axioms: [{name: 'sorryAx', module: 'Init.Prelude'}]},
    ],
    project_non_claims: [], references: [], revisions: [], assets: [],
    preview: {mode: 'project', notice: 'Saved-source preview.', project: {kind: 'lake', root: '/p', toolchain: 't'}}};
}

test('project claim cards read like the reading view: prose docstring, statement, highlighted source', async () => {
  const window = await renderSnapshot(projectNotebook({current: false}), {katex: true});
  try {
    const doc = window.document.getElementById('doc');
    const [pos, floor] = doc.querySelectorAll('.project-claim');
    assert.equal(pos.querySelector('dfn').textContent, 'k_pos');
    assert.equal(pos.querySelector('.def-kind').textContent, 'Theorem');
    assert.equal(pos.querySelector('.def-namespace').textContent, 'Namespace Demo');
    assert.equal(pos.querySelector('.doc-line').textContent.replace(/\s+/g, ' ').includes('The rate is'), true);
    assert.equal(pos.querySelector('.doc-line .math').dataset.tex, 'k > 0', 'the docstring is prose with TeX');
    for (const card of [pos, floor]) {
      const shown = [...card.querySelectorAll('.claim > :not(.card-source)')].map(el => el.textContent).join('\n');
      assert.doesNotMatch(shown, /\/--|-\//, 'no docstring markers outside the exact-source fold');
    }
    assert.equal(pos.querySelector('.claim-statement .field-label').textContent, 'Statement');
    assert.equal(pos.querySelector('.claim-statement .formal-line').dataset.tex, '0\\;<\\;\\text{k}');
    assert.ok(pos.querySelector('.claim-statement .formal-line .katex'), 'the statement is typeset');
    const fold = pos.querySelector('details.def-lean');
    assert.ok(!fold.open);
    assert.equal(fold.querySelector('pre').textContent, '/-- The rate is $k > 0$, so it is damped. -/\ntheorem k_pos : 0 < k := by\n  unfold k\n  norm_num', 'the exact source');
    assert.deepEqual([...fold.querySelectorAll('pre .kw')].map(k => k.textContent), ['theorem', 'by'], 'highlighted, and never inside the comment');
    assert.equal(fold.querySelector('pre .cm').textContent, '/-- The rate is $k > 0$, so it is damped. -/');
    assert.deepEqual([...pos.querySelectorAll('.pill')].map(p => [p.textContent, p.dataset.tone]), [['Kernel checked', 'green']]);
    assert.match(pos.querySelector('.claim-axioms').textContent, /^Assumptions: propext, Classical\.choice$/);
    assert.ok(pos.querySelector('.source-controls a.source-link'), 'the line link stays');
    assert.ok(pos.querySelector('.source-edit'), 'the edit button stays in the editor');
    assert.deepEqual([...floor.querySelectorAll('.pill')].map(p => p.textContent), ['Contains sorry', 'Out of date']);
    assert.equal(floor.querySelector('.claim').dataset.tone, 'amber', 'an out-of-date result is not shown green');
    assert.match(floor.querySelector('.claim-notice').textContent, /placeholder/);
    assert.match(floor.querySelector('.note-line[data-tone="amber"]').textContent, /Out of date/);
    // Definitions read as before; scaffolding is quiet, exact and never hidden.
    assert.equal(doc.querySelector('.definition dfn').textContent, 'k');
    assert.deepEqual([...doc.querySelectorAll('.source-context pre')].map(pre => pre.textContent), ['import Mathlib', 'namespace Demo', 'end Demo']);
    assert.equal(doc.querySelector('.source-context .kw').textContent, 'import');
  } finally { window.close(); }
});

test('a project claim the line reader cannot read keeps its exact source in view', async () => {
  const snapshot = projectNotebook();
  snapshot.cells[3].text = '/-- Inline -/ theorem k_pos : 0 < k := by\n  unfold k\n  norm_num\n  -- spare\n';
  const window = await renderSnapshot(snapshot);
  try {
    const card = window.document.querySelector('.project-claim');
    assert.equal(card.querySelector('.doc-line'), null);
    assert.equal(card.querySelector('details.def-lean'), null);
    assert.equal(card.querySelector('.lean-code').textContent, snapshot.cells[3].text.slice(0, -1));
    assert.equal(card.querySelector('.pill').textContent, 'Kernel checked');
  } finally { window.close(); }
});

test('project directives become their cards at their lines; unserved ones keep their source and note', async () => {
  const text = 'def M := 1\n-- @evidence simulate M (h := 1/10)\n-- @decide band : entailment x : x > 0 |- x >= 0\n--   continued\ndef N := 2\n-- @evidence iterate M\ndef P := 3\n';
  const snapshot = evidenceSnapshot(text, [evidenceEntry({line: 2}), evidenceEntry({line: 6, verb: 'iterate', provider: null, error: 'No installed provider runs `@evidence iterate`.'})],
    {decisions: [decisionEntry({line: 3, end_line: 4, provider: 'demo', error: ''})], directive_providers: {evidence: ['simulate'], decide: ['entailment']}});
  const window = await renderSnapshot(snapshot);
  try {
    const wrap = window.document.querySelector('#doc .cellwrap');
    const order = [...wrap.children].filter(el => !el.matches('.cell-tools')).map(el => el.matches('.definition') ? `def ${el.querySelector('dfn').textContent}`
      : el.matches('.evidence') ? `evidence ${el.dataset.documentStart}` : el.matches('.decision') ? `decision ${el.dataset.documentStart}-${el.dataset.documentEnd}`
      : el.matches('.directive-unavailable') ? `unavailable ${el.dataset.documentStart}` : el.className);
    assert.deepEqual(order, ['def M', 'evidence 2', 'decision 3-4', 'def N', 'unavailable 6', 'def P']);
    assert.doesNotMatch(wrap.textContent, /-- @evidence simulate|-- @decide band/, 'a served directive is shown as its card');
    const unavailable = wrap.querySelector('.directive-unavailable');
    assert.equal(unavailable.querySelector('.source-context pre').textContent, '-- @evidence iterate M');
    assert.match(unavailable.querySelector('.note-line').textContent, /No installed provider runs `@evidence iterate`\./);
    assert.equal(window.document.querySelectorAll('#doc .note-line[data-document-start="6"]').length, 1, 'its note is not repeated after the cell');
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    window.katex = {render() {}};
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const body = page.window.document.querySelector('.doc-inner');
      assert.equal(body.querySelectorAll('.evidence').length, 1);
      assert.equal(body.querySelectorAll('.decision').length, 1);
      assert.match(body.textContent, /-- @evidence iterate M/);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('export renders project notebooks as the preview does', async () => {
  const window = await renderSnapshot(projectNotebook(), {katex: true});
  try {
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await window.document.getElementById('export-html').onclick();
    const page = new JSDOM(html);
    try {
      const body = page.window.document.querySelector('.doc-inner');
      const preview = window.document.getElementById('doc');
      assert.equal(body.querySelectorAll('.project-claim .claim').length, 2);
      assert.deepEqual([...body.querySelectorAll('.claim dfn')].map(d => d.textContent), ['k_pos', 'floor']);
      assert.equal(body.querySelector('.doc-line').textContent, preview.querySelector('.doc-line').textContent);
      assert.ok(body.querySelector('.doc-line .katex'), 'docstring TeX is typeset in the export');
      assert.deepEqual([...body.querySelectorAll('.source-context pre')].map(pre => pre.textContent), ['import Mathlib', 'namespace Demo', 'end Demo']);
      assert.ok(body.querySelector('details.def-lean pre .kw'), 'exact, highlighted source stays in its fold');
      assert.equal(body.querySelectorAll('button, [data-claim], .source-edit').length, 0);
      assert.match(body.textContent, /Assumptions: propext, Classical\.choice/);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('project claims drive the inspector, summary, contents, chips, keys, sections and gutter', async () => {
  const window = await renderSnapshot(projectNotebook({current: false}), {browser: true});
  try {
    const d = window.document;
    window.HTMLElement.prototype.scrollIntoView = () => {};
    assert.equal(d.getElementById('insp-title').textContent, 'k_pos');
    assert.match(d.getElementById('insp-sub').textContent, /theorem · kernel checked/);
    assert.deepEqual([...d.querySelectorAll('#insp .axiom-module')].map(m => m.textContent), ['Init.Core', 'Init.Prelude']);
    assert.match(d.getElementById('insp').textContent, /The rate is/);
    assert.deepEqual([...d.querySelectorAll('#subline .stat')].map(s => s.textContent), ['1 kernel checked', '1 contains sorry', '1 out of date']);
    assert.deepEqual([...d.querySelectorAll('#outline .o-claim')].map(o => o.textContent), ['k_pos', 'floor']);
    assert.deepEqual([...d.querySelectorAll('#outline .o-claim + .dot')].map(o => o.dataset.tone), ['green', 'amber']);
    assert.deepEqual([...d.querySelectorAll('#chips-row button')].map(b => b.textContent.trim()), ['k_pos', 'floor']);
    for (const id of ['graph', 'graph-label', 'graph-note']) assert.equal(d.getElementById(id).hidden, true, 'no uses: no claim graph');
    assert.deepEqual([...d.querySelectorAll('#key-hints dt')].map(k => k.textContent), ['j k', 's', 'i', 'r', '?'], 'no editor-only keys outside the editor');
    assert.deepEqual([...d.querySelectorAll('.proof-section .section-statuses .pill')].map(p => p.textContent), ['1 Kernel checked', '1 Contains sorry', '1 Out of date']);
    assert.equal(d.querySelector('[data-claim="Demo.k_pos"] .claim').dataset.selected, 'true');
    d.body.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'j', bubbles: true}));
    assert.equal(d.getElementById('insp-title').textContent, 'floor');
    assert.equal(d.querySelector('[data-claim="Demo.floor"] .claim').dataset.selected, 'true');
    d.querySelector('[data-claim="Demo.k_pos"] .doc-line').click();
    assert.equal(d.getElementById('insp-title').textContent, 'k_pos', 'clicking a card selects it');
    d.querySelector('[data-mode="source"]').click();
    assert.equal(d.getElementById('source-L12').dataset.tone, 'green');
    assert.equal(d.getElementById('source-L12').dataset.claim, 'Demo.k_pos');
    assert.equal(d.getElementById('source-L16').dataset.tone, 'amber');
    assert.equal(d.getElementById('source-L18').dataset.tone, undefined);
  } finally { window.close(); }
  const host = await renderSnapshot(projectNotebook());
  try { assert.deepEqual([...host.document.querySelectorAll('#key-hints dt')].map(k => k.textContent), ['j k', 'n p', 'c t', 's', 'i', 'r', 'e', '?'], 'the editor preview handles e, n/p and c/t'); } finally { host.close(); }
});

test('the project banner is amber only when a result needs attention', async () => {
  let window = await renderSnapshot(projectNotebook());
  try {
    const banner = window.document.getElementById('banner');
    assert.equal(banner.hidden, false, 'it holds the check actions');
    assert.ok(banner.classList.contains('quiet'));
    assert.match(banner.textContent, /Project: \/p/);
    assert.equal(window.document.getElementById('check-project').hidden, false);
  } finally { window.close(); }
  for (const change of [s => { s.project_claims[1].current = false; }, s => { s.project_withheld = {from_line: 18, message: 'x'}; },
    s => { s.preview.check = {status: 'unavailable', message: 'no lake'}; }]) {
    const snapshot = projectNotebook();
    change(snapshot);
    window = await renderSnapshot(snapshot);
    try {
      const banner = window.document.getElementById('banner');
      assert.ok(!banner.classList.contains('quiet'));
      assert.match(banner.textContent, /out of date|stopped|could not run/);
    } finally { window.close(); }
  }
});

test('the empty project inspector does not claim the notebook has no theorems', async () => {
  const snapshot = projectNotebook();
  snapshot.project_claims = [];
  for (const cell of snapshot.cells) delete cell.claim;
  const window = await renderSnapshot(snapshot);
  try {
    assert.equal(window.document.getElementById('insp-title').textContent, 'No saved results');
    assert.match(window.document.getElementById('insp').textContent, /Check the saved file/);
    assert.equal(window.document.querySelectorAll('#doc .project-claim').length, 0);
    assert.ok(window.document.querySelectorAll('#doc .definition').length >= 2, 'unchecked theorems still read as cards');
  } finally { window.close(); }
});

// One recorded observation of the whole saved file, as /api/proof/trace serves it.
function goalTrace(sha256, fields = {}) {
  const node = (id, line, text, before, after, parent = null) => ({id, parent, text, start: id * 10, end: id * 10 + 5, declaration: 'Demo.k_pos', kind: 'tactic',
    range: {start: {line, character: 2}, end: {line, character: 2 + text.length}}, before, after});
  return {id: 'observation', status: 'ready', source_sha256: sha256, message: 'Recorded goals; the claim check status is unchanged.',
    environment: {kind: 'read_only_live_dependencies', toolchain: 'leanprover/lean4:v4.32.1'}, errors: [], warnings: ['unused variable'],
    nodes: [
      node(0, 13, 'unfold k', ['⊢ 0 < k'], ['⊢ 0 < 1 / 2']),
      node(1, 14, 'norm_num', ['⊢ 0 < 1 / 2'], []),
      node(2, 14, 'positivity', ['⊢ 0 < 1 / 2'], [], 1),
    ], ...fields};
}

test('the inspector shows a claim’s proof goals at each tactic step as a historical observation', async () => {
  const snapshot = projectNotebook();
  const messages = [];
  let trace = {status: 'idle', nodes: []};
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: message => {
    if (message.path === '/api/proof/start') trace = {id: 'observation', status: 'running', source_sha256: snapshot.sha256, message: 'Recording saved tactic goals', nodes: []};
    return trace;
  }});
  const d = window.document, insp = d.getElementById('insp');
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  try {
    const button = insp.querySelector('.goal-section [data-act="goal"]');
    assert.equal(button.textContent, 'Inspect proof goals');
    assert.match(insp.querySelector('.goal-section').textContent, /historical observation: it never changes a status/);
    assert.equal(messages.filter(m => m.path?.startsWith('/api/proof/')).length, 0, 'nothing runs until asked');
    button.focus();
    button.click(); await settle(20);
    assert.deepEqual(messages.filter(m => m.path?.startsWith('/api/proof/')).map(m => [m.method, m.path]), [['GET', '/api/proof/trace'], ['POST', '/api/proof/start']]);
    assert.deepEqual(JSON.parse(messages.find(m => m.path === '/api/proof/start').body), {sha256: snapshot.sha256});
    assert.match(insp.querySelector('.goal-state').textContent, /Asking Lean… Recording saved tactic goals/);
    assert.equal(d.activeElement.dataset.act, 'goal-cancel', 'focus stays in the goal controls');
    insp.querySelector('[data-act="goal"]')?.click();
    trace = goalTrace(snapshot.sha256);
    await settle(820);
    const steps = [...insp.querySelectorAll('.goal-step')];
    assert.deepEqual(steps.map(step => step.querySelector('code').textContent), ['unfold k', 'norm_num', 'positivity']);
    assert.deepEqual(steps.map(step => step.dataset.depth), ['0', '0', '1'], 'nested tactics are indented under their parent');
    assert.equal(insp.querySelector('.goal-initial pre.goal').textContent, '⊢ 0 < k');
    assert.equal(steps[0].querySelector('pre.goal-block').textContent, '⊢ 0 < 1 / 2');
    assert.equal(steps[1].querySelector('pre.goal').dataset.tone, 'green');
    assert.match(steps[1].textContent, /No goals remain/);
    assert.match(insp.querySelector('.goal-messages').textContent, /unused variable/);
    assert.match(insp.querySelector('.goal-section').textContent, /Recorded from source aaaaaaaa with leanprover\/lean4:v4\.32\.1.*never changes a status/s);
    assert.equal(insp.querySelector('.goal-section [data-act="goal"]').textContent, 'Refresh proof goals');
    assert.equal(insp.querySelector('.focus .pill').textContent, 'Kernel checked', 'the status is the saved record’s');
    assert.equal(messages.filter(m => m.path === '/api/proof/start').length, 1, 'one observation at a time');
    // The same observation serves the next claim: here a term proof records no steps.
    d.querySelector('.app').dispatchEvent(new window.KeyboardEvent('keydown', {key: 'j', bubbles: true}));
    assert.equal(d.getElementById('insp-title').textContent, 'floor');
    assert.equal(insp.querySelectorAll('.goal-step').length, 0);
    assert.match(insp.querySelector('.goal-section').textContent, /No tactic steps were recorded for this claim\. It may be a term proof/);
    assert.equal(messages.filter(m => m.path === '/api/proof/start').length, 1);
    // A saved edit or a dependency change makes the goals stale.
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'environmentChanged'}}));
    assert.match(insp.querySelector('.goal-section').textContent, /dependencies changed since these goals were recorded/);
    assert.equal(insp.querySelectorAll('.goal-step').length, 0);
    snapshot.sha256 = 'b'.repeat(64);
    await window.glitnirPoll();
    assert.match(insp.querySelector('.goal-section').textContent, /saved source changed since these goals were recorded/);
  } finally { window.close(); }
});

test('proof goals say when they are cancelled or failed, join a running observation, and keep unsaved files off the wire', async () => {
  const snapshot = projectNotebook();
  const messages = [];
  let trace = {status: 'idle', nodes: []}, starts = 0, start = () => ({id: String(++starts), status: 'running', source_sha256: snapshot.sha256, message: 'Preparing proof steps', nodes: []});
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: message => {
    if (message.path === '/api/proof/start') trace = start();
    if (message.path === '/api/proof/cancel') trace = {...trace, status: 'cancelled', message: 'Proof observation cancelled.'};
    return trace;
  }});
  const d = window.document, insp = d.getElementById('insp'), $ = id => d.getElementById(id);
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  const section = () => insp.querySelector('.goal-section').textContent;
  try {
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    insp.querySelector('[data-act="goal-cancel"]').click(); await settle(820);
    assert.ok(messages.some(m => m.path === '/api/proof/cancel'));
    assert.match(section(), /cancelled\. No goals were recorded/);
    assert.equal(insp.querySelector('[data-act="goal"]').textContent, 'Inspect proof goals');
    // A failed observation says why.
    start = () => ({id: String(++starts), status: 'unavailable', source_sha256: snapshot.sha256, message: 'This project uses Lean 4.31.0. Glitnir checks Lean 4.32.1, so checking, proof steps, views, evidence and decisions are off; reading works as usual.', nodes: []});
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    assert.match(section(), /No proof goals: This project uses Lean 4\.31\.0\./);
    // Proof steps' running observation of this saved file is joined, not restarted.
    trace = {id: 'steps', status: 'running', source_sha256: snapshot.sha256, message: 'Resolving project environment', nodes: []};
    const before = starts;
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    assert.equal(starts, before);
    assert.match(section(), /Resolving project environment/);
    trace = goalTrace(snapshot.sha256, {id: 'steps'});
    await settle(820);
    assert.equal(insp.querySelectorAll('.goal-step').length, 3);
    // Unsaved Lean files hold the start back in the bridge.
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    trace = {status: 'idle', nodes: []};
    const sent = messages.filter(m => m.path === '/api/proof/start').length;
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    assert.equal(messages.filter(m => m.path === '/api/proof/start').length, sent);
    assert.match(section(), /No proof goals: Save or revert unsaved Lean files first/);
    assert.equal($('insp').querySelector('.focus .pill').textContent, 'Kernel checked');
  } finally { window.close(); }
});

test('Explore Proof Steps joins the observation the inspector started', async () => {
  const snapshot = projectNotebook();
  let trace = {status: 'idle', nodes: []}, starts = 0;
  const window = await renderSnapshot(snapshot, {onProof: message => {
    if (message.path === '/api/proof/start') trace = {id: String(++starts), status: 'running', source_sha256: snapshot.sha256, message: 'Recording saved tactic goals', nodes: []};
    return trace;
  }});
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  try {
    window.document.querySelector('#insp [data-act="goal"]').click(); await settle(20);
    window.document.querySelector('#doc .cellwrap[data-index="3"]').click();
    window.document.getElementById('review-proof').click(); await settle(20);
    assert.equal(starts, 1, 'the running observation is joined');
    assert.match(window.document.getElementById('proof-message').textContent, /Recording saved tactic goals/);
    trace = goalTrace(snapshot.sha256, {id: '1'});
    await settle(820);
    assert.equal(window.document.getElementById('review-position').textContent, 'Initial goals');
    assert.ok(window.document.querySelectorAll('#proof-tactics button').length > 0);
  } finally { window.close(); }
});

// A project notebook linking to a sibling, and the sibling linking back.
function linkedNotebooks() {
  const main = projectNotebook();
  main.cells[1] = {...main.cells[1], markdown: '# Demo notebook\n\nSee [the three states](ThreeState.lean) and [a gone one](Gone.lean).\n\n## Facts {fold}\n\nSummary of the facts.'};
  main.references = ['Demo', 'Gone', 'ThreeState'].map(module => ({module, file: `${module}.lean`}));
  const text = '/-!\n# Three states\nBack to [the demo](Demo.lean).\n-/\ntheorem settled : True := by\n  trivial\n-- @view circuit settled\n';
  const linked = {file: 'ThreeState.lean', sha256: 'c'.repeat(64), checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [],
    cells: [
      {id: 'cell-0', index: 0, kind: 'prose', start: 1, end: 4, text: '/-!\n# Three states\nBack to [the demo](Demo.lean).\n-/\n', markdown: '# Three states\nBack to [the demo](Demo.lean).'},
      {id: 'cell-1', index: 1, kind: 'source', start: 5, end: 6, text: 'theorem settled : True := by\n  trivial\n', claim: 'settled'},
      {id: 'cell-2', index: 2, kind: 'source', start: 7, end: 7, text: '-- @view circuit settled\n'},
    ],
    views: [{cell: 2, line: 7, kind: 'circuit', name: 'settled', error: ''}], evidence: [], decisions: [],
    project_claims: [{name: 'settled', start_line: 5, end_line: 6, status: 'checked', current: true, axioms: []}], project_non_claims: [],
    references: main.references,
    preview: {mode: 'project', notice: 'Saved-source preview.', check: null, project: {kind: 'lake', root: '/p', toolchain: 't'}, linked: {path: 'ThreeState.lean', file: 'ThreeState.lean'}}};
  void text;
  return {main, linked};
}

// A notebook whose claims use each other and a claim of a sibling notebook.
function notebooksWithUses() {
  const {main, linked} = linkedNotebooks();
  main.project_claims[0].uses = [];
  main.project_claims[1].uses = [
    {module: 'Demo', name: 'Demo.k_pos', in_notebook: true},
    {module: 'ThreeState', name: 'settled', in_notebook: false, path: 'ThreeState.lean'},
    {module: 'ThreeState', name: 'missing', in_notebook: false, path: 'ThreeState.lean'},
    {module: 'Gone', name: 'Gone.lemma', in_notebook: false},
    {module: 'Demo', name: 'Demo.not_a_result', in_notebook: true},
  ];
  linked.cells = [
    ...linked.cells.slice(0, 1),
    {id: 'cell-1', index: 1, kind: 'source', start: 5, end: 5, text: 'theorem first : True := trivial\n', claim: 'first'},
    {id: 'cell-2', index: 2, kind: 'source', start: 6, end: 7, text: 'theorem settled : True := by\n  trivial\n', claim: 'settled'},
  ];
  linked.views = [];
  linked.project_claims = [
    {name: 'first', start_line: 5, end_line: 5, status: 'checked', current: true, axioms: [], uses: []},
    {name: 'settled', start_line: 6, end_line: 7, status: 'checked', current: true, axioms: [], uses: [{module: 'ThreeState', name: 'first', in_notebook: true}]},
  ];
  return {main, linked};
}

test('claim cards, the inspector and the claim graph show the claims each claim uses', async () => {
  const {main, linked} = notebooksWithUses();
  const messages = [];
  const window = await renderSnapshot(main, {katex: true, onMessage: m => messages.push(m), onLinked: path => (path === '/api/linked?path=ThreeState.lean' ? {json: linked} : undefined)});
  const d = window.document, $ = id => d.getElementById(id);
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  try {
    const floor = d.querySelector('[data-claim="Demo.floor"]');
    const chips = [...floor.querySelectorAll('.uses > *')];
    assert.deepEqual(chips.map(c => [c.tagName, c.textContent.trim()]), [
      ['BUTTON', 'k_pos'], ['A', 'ThreeState:settled'], ['A', 'ThreeState:missing'], ['SPAN', 'Gone:lemma'], ['SPAN', 'not_a_result'],
    ]);
    assert.equal(chips[0].dataset.act, 'goto');
    assert.equal(chips[0].querySelector('.dot').dataset.tone, 'green', 'a claim of this notebook shows its status');
    assert.equal(chips[1].dataset.act, 'open-notebook');
    assert.equal(chips[1].dataset.link, 'ThreeState.lean');
    assert.equal(chips[1].dataset.claim, 'settled');
    assert.match(chips[3].title, /cannot be read here/);
    assert.equal(d.querySelector('[data-claim="Demo.k_pos"] .uses'), null, 'a claim using nothing shows no chips');
    // The graph is back: one node per claim, an arc for each use in this notebook.
    for (const id of ['graph', 'graph-label', 'graph-note']) assert.equal($(id).hidden, false, id);
    assert.deepEqual([...d.querySelectorAll('#graph .node text')].map(t => t.textContent), ['k_pos', 'floor']);
    assert.equal(d.querySelectorAll('#graph path[marker-end]').length, 1);
    assert.match($('graph-note').textContent, /names directly/);
    // The inspector lists the same uses.
    assert.match($('insp').textContent, /uses\s*—/, 'k_pos uses nothing');
    floor.querySelector('.doc-line').click();
    assert.equal($('insp-title').textContent, 'floor');
    assert.deepEqual([...d.querySelectorAll('#insp .insp-uses .uses > *')].map(c => c.textContent.trim()), ['k_pos', 'ThreeState:settled', 'ThreeState:missing', 'Gone:lemma', 'not_a_result']);
    d.querySelector('#insp .insp-uses button[data-act="goto"]').click();
    assert.equal($('insp-title').textContent, 'k_pos', 'a chip of this notebook selects its claim');
    // Another notebook's chip opens it in this panel at that claim.
    d.querySelector('[data-claim="Demo.floor"] .uses a[data-claim="settled"]').click(); await settle(30);
    assert.equal(messages.filter(m => m.type === 'reference').length, 0, 'no separate preview');
    assert.deepEqual(messages.filter(m => m.path?.startsWith('/api/linked')).map(m => m.path), ['/api/linked?path=ThreeState.lean']);
    assert.ok(d.body.classList.contains('glitnir-linked-view'));
    assert.equal($('insp-title').textContent, 'settled', 'the used claim is selected, not the first');
    assert.equal(d.querySelector('[data-claim="settled"] .claim').dataset.selected, 'true');
    assert.deepEqual([...d.querySelectorAll('#graph .node text')].map(t => t.textContent), ['first', 'settled']);
    d.querySelector('[data-act="notebook-back"]').click(); await settle(30);
    // A claim the other notebook has no saved result for: it opens, and says so.
    d.querySelector('[data-claim="Demo.floor"] .uses a[data-claim="missing"]').click(); await settle(30);
    assert.equal($('insp-title').textContent, 'first');
    assert.match($('load-error').textContent, /missing has no saved result in ThreeState\.lean/);
    d.querySelector('[data-act="notebook-back"]').click(); await settle(30);
    // Export keeps the uses as text and anchors, never as links out.
    $('export-html').click(); await settle(80);
    const exported = messages.find(m => m.type === 'export');
    assert.ok(exported);
    const page = new JSDOM(exported.body).window.document;
    const kept = [...page.querySelectorAll('[aria-label="Theorem floor"] .uses > *')];
    assert.deepEqual(kept.map(c => [c.tagName, c.textContent.trim()]), [['A', 'k_pos'], ['SPAN', 'ThreeState:settled'], ['SPAN', 'ThreeState:missing'], ['SPAN', 'Gone:lemma'], ['SPAN', 'not_a_result']]);
    assert.equal(kept[0].getAttribute('href'), '#cell-Demo.k_pos');
  } finally { window.close(); }
});

test('a claim card, the inspector and r check the saved file through that claim', async () => {
  const messages = [];
  const snapshot = projectNotebook();
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m)});
  const d = window.document, $ = id => d.getElementById(id);
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  const checks = () => messages.filter(m => m.path === '/api/project/check').map(m => JSON.parse(m.body));
  try {
    const sha256 = snapshot.sha256;
    const button = d.querySelector('[data-claim="Demo.floor"] .claim-actions [data-act="check-through"]');
    assert.equal(button.textContent.trim(), 'Check through here');
    assert.equal(button.dataset.line, '17');
    assert.match(d.querySelector('[data-claim="Demo.floor"] .claim-actions').textContent, /Lines 1–17 only; later claims are not rechecked/);
    button.click(); await settle(30);
    assert.deepEqual(checks(), [{sha256, through_line: 17}]);
    assert.equal($('insp-title').textContent, 'floor', 'the claim checked through is selected');
    const inspector = d.querySelector('#insp .insp-check [data-act="check-through"]');
    assert.equal(inspector.textContent.trim(), 'Check through this claim');
    inspector.click(); await settle(30);
    assert.deepEqual(checks().at(-1), {sha256, through_line: 17});
    d.querySelector('[data-claim="Demo.k_pos"] .doc-line').click();
    $('doc').dispatchEvent(new window.KeyboardEvent('keydown', {key: 'r', bubbles: true})); await settle(30);
    assert.deepEqual(checks().at(-1), {sha256, through_line: 14}, 'r checks through the selected claim');
    $('check-project').click(); await settle(30);
    assert.deepEqual(checks().at(-1), {sha256}, 'Check saved file checks all of it');
    // A button of an earlier saved file never checks other lines.
    const count = checks().length;
    const stale = d.querySelector('[data-claim="Demo.floor"] [data-act="check-through"]');
    stale.dataset.sha = 'b'.repeat(64);
    stale.click(); await settle(30);
    assert.equal(checks().length, count);
    // While a check runs, nothing else can be started.
    snapshot.preview.check = {status: 'running', source_sha256: sha256, through_line: 17, message: 'Checking saved file', theorems: []};
    await window.glitnirPoll(); await settle(20);
    assert.ok([...d.querySelectorAll('[data-act="check-through"]')].every(b => b.disabled));
    assert.match($('project-check').textContent, /Checking lines 1–17 · Checking saved file/);
  } finally { window.close(); }
});

test('after a check through a claim, later claims say they were not rechecked', async () => {
  const messages = [];
  const snapshot = projectNotebook();
  snapshot.project_claims[1].not_rechecked = true;
  snapshot.project_withheld = {from_line: 15, message: 'Checked through line 14 on request', through_line: 14};
  snapshot.preview.check = {status: 'checked_snapshot', source_sha256: snapshot.sha256, environment_sha256: 'e'.repeat(64), through_line: 14, message: 'Last checked snapshot.',
    withheld: snapshot.project_withheld, theorems: [{name: 'Demo.k_pos', status: 'checked', axioms: [], type: 'LT.lt 0 Demo.k', statement: '0 < k', start_line: 11, end_line: 14}]};
  const window = await renderSnapshot(snapshot, {katex: true, onMessage: m => messages.push(m)});
  const d = window.document, $ = id => d.getElementById(id);
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  try {
    const [pos, floor] = d.querySelectorAll('#doc .project-claim');
    assert.deepEqual([...floor.querySelectorAll('.pill')].map(p => [p.textContent, p.dataset.tone]), [['Contains sorry', 'amber'], ['Not rechecked', 'muted']]);
    assert.match(floor.textContent, /Not rechecked: the last check covered lines 1–14 only, so this is the result of an earlier check\./);
    assert.doesNotMatch(floor.textContent, /Out of date/, 'still current: same source and environment');
    assert.doesNotMatch(pos.textContent, /Not rechecked/);
    assert.match(d.querySelector('#doc .project-covered').textContent, /^The last check covered lines 1–14 only, on request\. Claims after line 14 were not rechecked/);
    assert.ok(d.querySelector('#doc .project-covered').classList.contains('quiet'), 'a requested cut is no warning');
    assert.deepEqual([...d.querySelectorAll('#subline .stat')].map(s => s.textContent), ['1 kernel checked', '1 contains sorry', '1 not rechecked', 'last check: lines 1–14']);
    assert.ok($('banner').classList.contains('quiet'));
    assert.match($('banner-text').textContent, /The last check covered lines 1–14 only\./);
    assert.doesNotMatch($('banner-text').textContent, /Checking stopped/);
    assert.match($('project-check').textContent, /Last checked snapshot · lines 1–14/);
    assert.match($('project-check').textContent, /Checked lines 1–14 on request; nothing after line 14 was checked\./);
    assert.match($('project-check').textContent, /0 < k/, 'the printed statement');
    // The panel says whose notation printed it, with the kernel form one fold away.
    assert.match($('project-check').textContent, /Checked statement, as this project’s notation prints it/);
    assert.equal($('project-check').querySelector('.checked-kernel summary').textContent, 'Kernel form, without notation');
    assert.equal($('project-check').querySelector('.checked-kernel pre').textContent, 'LT.lt 0 Demo.k');
    assert.deepEqual([...d.querySelectorAll('.proof-section .section-statuses .pill')].map(p => p.textContent), ['1 Kernel checked', '1 Contains sorry', '1 Not rechecked']);
    floor.querySelector('.doc-line').click();
    assert.match($('insp').textContent, /current · not rechecked by the last check/);
    $('export-html').click(); await settle(80);
    const exported = messages.find(m => m.type === 'export');
    assert.match(exported.body, /The last check covered lines 1–14 only, on request/);
    assert.match(exported.body, /Not rechecked/);
    assert.doesNotMatch(exported.body, /Check through/);
  } finally { window.close(); }
  // A check through a claim that stopped earlier, at a failing command, warns.
  const failed = projectNotebook();
  failed.project_withheld = {from_line: 11, message: 'unknown identifier', through_line: 17};
  const other = await renderSnapshot(failed);
  try {
    const banner = other.document.querySelector('#doc .banner');
    assert.equal(banner.classList.contains('quiet'), false);
    assert.match(banner.textContent, /Checking stopped at line 11: unknown identifier\. Claims from there on were not checked\. It was asked to check lines 1–17\./);
    assert.match(other.document.getElementById('banner-text').textContent, /Checking stopped before the end of the file/);
  } finally { other.close(); }
});

test('a linked notebook offers no check through its claims', async () => {
  const {main, linked} = linkedNotebooks();
  const window = await renderSnapshot(main, {onLinked: path => (path === '/api/linked?path=ThreeState.lean' ? {json: linked} : undefined)});
  const d = window.document;
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  try {
    assert.ok(d.querySelectorAll('#doc [data-act="check-through"]').length > 0);
    [...d.querySelectorAll('#doc a.notebook-reference')].find(a => a.textContent === 'the three states').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.ok(d.body.classList.contains('glitnir-linked-view'));
    assert.equal(d.querySelectorAll('[data-act="check-through"]').length, 0);
  } finally { window.close(); }
});

test('a statement shortened in one form says so for that form only, and truncated uses say there are more', async () => {
  const snapshot = projectNotebook();
  snapshot.cells[3] = {...snapshot.cells[3], text: 'example : 0 < k := by\n  unfold k\n  norm_num\n  done\n'};
  Object.assign(snapshot.project_claims[0], {example: true, statement: '0 < k', kernel_statement: 'LT.lt 0 …', kernel_statement_shortened: true,
    uses: [{module: 'Demo', name: 'Demo.floor', in_notebook: true}], uses_truncated: true});
  const window = await renderSnapshot(snapshot);
  try {
    const card = window.document.querySelector('[data-claim="Demo.k_pos"]');
    assert.equal(card.querySelector('.field-label').textContent, 'Checked statement, as this project’s notation prints it');
    assert.equal(card.querySelector('.checked-kernel summary').textContent, 'Kernel form, without notation (shortened)');
    assert.deepEqual([...card.querySelectorAll('.uses > *')].map(c => c.textContent.trim()), ['floor', 'and more']);
  } finally { window.close(); }
});

test('a check through the last line asks for the whole file', async () => {
  const messages = [];
  const snapshot = projectNotebook();
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m)});
  try {
    window.document.querySelector('[data-claim="Demo.floor"] [data-act="check-through"]').dataset.line = '18';
    window.document.querySelector('[data-claim="Demo.floor"] [data-act="check-through"]').click();
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(messages.filter(m => m.path === '/api/project/check').map(m => JSON.parse(m.body)), [{sha256: snapshot.sha256}]);
  } finally { window.close(); }
});

test('a claim whose check recorded no uses says so in the inspector', async () => {
  const window = await renderSnapshot(projectNotebook());
  try {
    assert.match(window.document.querySelector('#insp .insp-uses').textContent, /not recorded by this check/);
    assert.equal(window.document.querySelector('#doc .uses'), null);
  } finally { window.close(); }
});

test('a linked notebook is read in the same panel, read-only, with a way back and in-panel history', async () => {
  const {main, linked} = linkedNotebooks();
  const messages = [];
  const window = await renderSnapshot(main, {katex: true, onMessage: m => messages.push(m), onLinked: path => (path === '/api/linked?path=ThreeState.lean' ? {json: linked} : undefined)});
  const d = window.document, $ = id => d.getElementById(id);
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  try {
    assert.equal($('notebook-trail').hidden, true, 'no trail before a link is followed');
    const link = [...d.querySelectorAll('#doc a.notebook-reference')].find(a => a.textContent === 'the three states');
    assert.equal(link.dataset.act, 'open-notebook');
    link.click(); await settle(20);
    assert.equal(messages.filter(m => m.type === 'reference').length, 0, 'no separate preview');
    assert.deepEqual(messages.filter(m => m.path?.startsWith('/api/linked')).map(m => m.path), ['/api/linked?path=ThreeState.lean']);
    assert.equal($('title').textContent, 'Three states');
    assert.equal($('location').textContent, 'Linked from Demo.lean · read-only');
    assert.equal($('file-name').textContent, 'ThreeState.lean');
    assert.ok(d.body.classList.contains('glitnir-linked-view'));
    const trail = $('notebook-trail');
    assert.equal(trail.hidden, false);
    assert.deepEqual([...trail.querySelectorAll('.trail-crumbs li')].map(li => li.textContent), ['Demo.lean', 'ThreeState.lean']);
    assert.equal(trail.querySelector('[aria-current="page"]').textContent, 'ThreeState.lean');
    assert.equal(trail.querySelector('[data-act="notebook-back"]').disabled, false);
    assert.equal(trail.querySelector('[data-act="notebook-back"]').title, 'Back to Demo.lean');
    assert.equal(trail.querySelector('[data-act="notebook-forward"]').disabled, true);
    assert.match(trail.textContent, /Read-only · its own saved results · nothing is checked here/);
    // Its own saved results, and nothing that would act on it from here.
    assert.equal(d.querySelector('[data-claim="settled"] .pill').textContent, 'Kernel checked');
    assert.equal($('insp-title').textContent, 'settled');
    assert.match($('insp').textContent, /Open this notebook in its own preview to inspect its proof goals/);
    assert.equal(d.querySelectorAll('#insp [data-act="goal"], #doc .source-edit').length, 0);
    assert.equal($('check-project').hidden, true);
    assert.equal($('inspect-project').hidden, true);
    assert.match($('banner-text').textContent, /Read-only view of ThreeState\.lean/);
    assert.equal($('export-html').disabled, true);
    assert.match(d.querySelector('.view-cell').textContent, /own preview/);
    assert.equal(messages.filter(m => m.path?.startsWith('/api/view')).length, 0, 'nothing is drawn for it');
    // Live updates of the previewed notebook do not replace the view; polls read the linked notebook.
    const polled = messages.length;
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'sourceChanged'}}));
    await settle(300);
    await window.glitnirPoll();
    assert.equal($('title').textContent, 'Three states');
    assert.match($('live-status').textContent, /Demo\.lean changed on disk/);
    assert.ok(messages.slice(polled).some(m => m.path === '/api/linked?path=ThreeState.lean'));
    assert.equal(messages.slice(polled).filter(m => m.path === '/api/notebook').length, 0);
    // Editing here is refused; it belongs to the notebook's own preview.
    $('open-source').click();
    assert.equal(messages.filter(m => m.type === 'source').length, 0);
    assert.match($('load-error').textContent, /read-only here/);
    const own = trail.querySelector('a.notebook-own');
    assert.equal(own.textContent, 'Open in its own preview');
    own.click();
    assert.deepEqual(JSON.parse(JSON.stringify(messages.filter(m => m.type === 'reference'))), [{type: 'reference', module: 'ThreeState'}]);
    // Back, forward, and a link back to the previewed notebook.
    $('notebook-trail').querySelector('[data-act="notebook-back"]').click(); await settle(20);
    assert.equal($('title').textContent, 'Demo notebook');
    assert.equal($('location').textContent, 'Notebook');
    assert.ok(!d.body.classList.contains('glitnir-linked-view'));
    assert.equal($('notebook-trail').hidden, false, 'forward history stays');
    assert.equal($('notebook-trail').querySelector('[data-act="notebook-forward"]').title, 'Forward to ThreeState.lean');
    assert.equal($('export-html').disabled, false);
    $('notebook-trail').querySelector('[data-act="notebook-forward"]').click(); await settle(20);
    assert.equal($('title').textContent, 'Three states');
    d.querySelector('#doc a.notebook-reference[data-link=""]').click(); await settle(20);
    assert.equal($('title').textContent, 'Demo notebook');
    assert.deepEqual([...$('notebook-trail').querySelectorAll('.trail-crumbs li')].map(li => li.textContent), ['Demo.lean']);
    // A link that cannot be read returns here and says why.
    [...d.querySelectorAll('#doc a.notebook-reference')].find(a => a.textContent === 'a gone one').click(); await settle(30);
    assert.equal($('title').textContent, 'Demo notebook');
    assert.match($('load-error').textContent, /Gone\.lean could not be opened here: There is no such notebook\./);
    assert.equal($('notebook-trail').hidden, true, 'the unreadable notebook leaves no history entry');
    // Export of the previewed notebook is unchanged: the links are text.
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await $('export-html').onclick();
    const page = new JSDOM(html);
    try {
      assert.equal(page.window.document.querySelectorAll('a[href*="reference="], [data-act]').length, 0);
      assert.match(page.window.document.body.textContent, /the three states/);
    } finally { page.window.close(); }
  } finally { window.close(); }
});

test('a reveal from the editor returns from a linked notebook to the previewed one', async () => {
  const {main, linked} = linkedNotebooks();
  const window = await renderSnapshot(main, {onLinked: () => ({json: linked})});
  const d = window.document;
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  try {
    d.querySelector('#doc a.notebook-reference').click(); await settle(20);
    assert.equal(d.getElementById('title').textContent, 'Three states');
    // Proof steps belong to the previewed notebook.
    d.querySelector('.app').dispatchEvent(new window.KeyboardEvent('keydown', {key: 't', bubbles: true}));
    assert.equal(d.getElementById('proof-review').hidden, true);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'revealCell', index: 3, sha256: main.sha256, line: 12, character: 0}}));
    await settle(30);
    assert.equal(d.getElementById('title').textContent, 'Demo notebook');
    assert.ok(!d.body.classList.contains('glitnir-linked-view'));
    assert.equal(d.getElementById('insp-title').textContent, 'k_pos');
  } finally { window.close(); }
});

test('proof mode never carries into a linked notebook, and review keys stay off there', async () => {
  const {main, linked} = linkedNotebooks();
  const messages = [];
  let trace = {status: 'idle', nodes: []};
  const window = await renderSnapshot(main, {onMessage: m => messages.push(m), onLinked: () => ({json: linked}), onProof: message => {
    if (message.path === '/api/proof/start') trace = goalTrace(main.sha256, {id: 'steps'});
    return trace;
  }});
  const d = window.document, $ = id => d.getElementById(id);
  const app = d.querySelector('.app');
  const key = k => app.dispatchEvent(new window.KeyboardEvent('keydown', {key: k, bubbles: true}));
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  try {
    d.querySelector('#doc a.notebook-reference').click(); await settle(20);
    $('notebook-trail').querySelector('[data-act="notebook-back"]').click(); await settle(20);
    d.querySelector('#doc .cellwrap[data-index="3"]').click();
    key('t'); await settle(30);
    assert.equal($('proof-review').hidden, false, 'proof mode on the previewed notebook');
    assert.ok(d.body.classList.contains('glitnir-proof-mode'));
    $('notebook-trail').querySelector('[data-act="notebook-forward"]').click(); await settle(30);
    assert.equal($('title').textContent, 'Three states');
    assert.equal($('proof-review').hidden, true, 'the switch left proof mode');
    assert.ok(!d.body.classList.contains('glitnir-proof-mode'));
    const sent = messages.length;
    for (const k of ['t', 'e', 'n', 'p', 'c', 'r']) key(k);
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'reviewCommand', action: 'next'}}));
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'reviewCommand', action: 'proof'}}));
    await settle(30);
    assert.equal($('title').textContent, 'Three states', 'n and p do not reveal the previewed notebook');
    assert.equal($('proof-review').hidden, true);
    assert.deepEqual(messages.slice(sent).filter(m => m.type !== 'request' || !m.path.startsWith('/api/linked')), [], 'no edit, source, check or proof request');
    key('j');
    assert.equal($('insp-title').textContent, 'settled', 'the renderer’s own keys still work');
    key('?');
    assert.equal($('keys').hidden, false);
  } finally { window.close(); }
});

test('a linked notebook of another Lake project shows saved results as not compared', async () => {
  const {main, linked} = linkedNotebooks();
  linked.preview.linked.compared = false;
  linked.project_claims[0].current = null;
  const window = await renderSnapshot(main, {onLinked: () => ({json: linked})});
  const d = window.document, $ = id => d.getElementById(id);
  window.scrollTo = () => {};
  try {
    d.querySelector('#doc a.notebook-reference').click();
    await new Promise(resolve => setTimeout(resolve, 20));
    const card = d.querySelector('[data-claim="settled"]');
    assert.deepEqual([...card.querySelectorAll('.pill')].map(p => [p.textContent, p.dataset.tone]), [['Kernel checked', 'green'], ['Not compared', 'muted']]);
    assert.match(card.querySelector('.note-line[data-tone="muted"]').textContent, /not compared with this project’s environment/);
    assert.equal(card.querySelectorAll('.note-line[data-tone="amber"]').length, 0, 'not called out of date');
    assert.deepEqual([...d.querySelectorAll('#subline .stat')].map(s => s.textContent), ['1 kernel checked', '1 not compared']);
    assert.match($('banner-text').textContent, /not compared with this project’s environment because it belongs to another Lake project/);
    assert.match($('insp-sub').textContent, /not compared/);
  } finally { window.close(); }
});

test('proof goals reuse a finished observation of the saved file and only cancel what they started', async () => {
  const snapshot = projectNotebook();
  const messages = [];
  let trace = goalTrace(snapshot.sha256, {id: 'earlier'});
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: message => {
    if (message.path === '/api/proof/start') trace = {id: 'fresh', status: 'running', source_sha256: snapshot.sha256, message: 'Recording saved tactic goals', nodes: []};
    if (message.path === '/api/proof/cancel') trace = {...trace, status: 'cancelled'};
    return trace;
  }});
  const insp = window.document.getElementById('insp');
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  const starts = () => messages.filter(m => m.path === '/api/proof/start').length;
  try {
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    assert.equal(starts(), 0, 'a finished observation of this saved file is reused');
    assert.equal(insp.querySelectorAll('.goal-step').length, 3);
    // Refresh asks for a new recording.
    insp.querySelector('[data-act="goal"][data-refresh="true"]').click(); await settle(20);
    assert.equal(starts(), 1);
    insp.querySelector('[data-act="goal-cancel"]').click(); await settle(820);
    assert.equal(messages.filter(m => m.path === '/api/proof/cancel').length, 1, 'the inspector cancels its own run');
    assert.match(insp.textContent, /cancelled/);
    // After a dependency change an older finished observation is not reused.
    trace = goalTrace(snapshot.sha256, {id: 'before-change'});
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'environmentChanged'}}));
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    assert.equal(starts(), 2);
    // A run Proof steps started is joined, and Cancel only stops following it.
    trace = {id: 'steps', status: 'running', source_sha256: snapshot.sha256, message: 'Resolving project environment', nodes: []};
    await settle(820);
    assert.match(insp.textContent, /Another proof observation replaced this one/);
    insp.querySelector('[data-act="goal"]').click(); await settle(20);
    assert.match(insp.textContent, /Resolving project environment/, 'joined');
    insp.querySelector('[data-act="goal-cancel"]').click(); await settle(20);
    assert.equal(messages.filter(m => m.path === '/api/proof/cancel').length, 1, 'a joined run is not cancelled');
    assert.match(insp.querySelector('.goal-section').textContent, /Stopped following the proof observation Proof steps started/);
    assert.equal(trace.status, 'running');
  } finally { window.close(); }
});

// The shortcut rows of a help dialog: the chips of each key column and its description.
const shortcutRows = d => [...d.querySelectorAll('#keys-groups dt')].map(dt => ({keys: [...dt.querySelectorAll('kbd')].map(k => k.textContent), text: dt.nextElementSibling.textContent}));
// The keys a page acts on: each handled key is default-prevented.
function handledKeys(window) {
  const app = window.document.querySelector('.app');
  window.HTMLElement.prototype.scrollIntoView = () => {};
  return [...'abcdefghijklmnopqrstuvwxyz?'].filter(key => {
    const event = new window.KeyboardEvent('keydown', {key, bubbles: true, cancelable: true});
    app.dispatchEvent(event);
    if (!window.document.getElementById('keys').hidden) window.document.getElementById('keys-close').click();
    return event.defaultPrevented;
  });
}

test('every shortcut listing comes from one list that matches the keys that work', async () => {
  const media = path.resolve(__dirname, '../../dist/media');
  assert.doesNotMatch(readFileSync(path.join(media, 'index.html'), 'utf8'), /<kbd>[a-z]<\/kbd>/, 'no hand-written key list in the page');
  assert.doesNotMatch(readFileSync(path.join(media, 'review.js'), 'utf8'), /shortcuts\s*=\s*\[/, 'the review layer keeps no list of its own');
  const guide = readFileSync(path.resolve(__dirname, '../../docs/guide.md'), 'utf8');
  const table = guide.slice(guide.indexOf('<!-- shortcuts:start'), guide.indexOf('<!-- shortcuts:end -->'));
  const documented = table.split('\n').filter(line => line.startsWith('| `')).map(line => {
    const [keys, text] = line.slice(2, -2).split(' | ');
    return {keys: [...keys.matchAll(/`([^`]+)`/g)].map(m => m[1]), text};
  });
  const host = await renderSnapshot(projectNotebook());
  try {
    const d = host.document;
    // Unsaved source keeps the keys from checking or recording anything.
    host.dispatchEvent(new host.MessageEvent('message', {data: {type: 'editorState', dirty: true}}));
    const rows = shortcutRows(d);
    assert.deepEqual(rows.map(r => r.text), documented.map(r => r.text), 'the guide table says what the preview says');
    rows.forEach((row, i) => assert.ok(documented[i].keys.join(' ').includes(row.keys.join(' ')), `guide keys for ${row.keys}`));
    const listed = rows.flatMap(r => r.keys).filter(k => /^[a-z?]$/.test(k)).sort();
    assert.deepEqual(listed, ['?', 'c', 'e', 'i', 'j', 'k', 'n', 'p', 'r', 's', 't']);
    assert.deepEqual(handledKeys(host).sort(), listed, 'the editor preview acts on exactly the listed keys');
    const hinted = [...d.querySelectorAll('#key-hints kbd')].map(k => k.textContent);
    assert.ok(hinted.length && hinted.every(k => listed.includes(k)), 'the contents hint is drawn from the same list');
  } finally { host.close(); }
  const browser = await renderSnapshot(projectNotebook(), {browser: (url, options) => options?.method === 'POST' ? {ok: false, status: 409, json: async () => ({error: 'test'})} : {ok: true, json: async () => projectNotebook()}});
  try {
    const rows = shortcutRows(browser.document);
    const listed = rows.flatMap(r => r.keys).filter(k => /^[a-z?]$/.test(k)).sort();
    assert.deepEqual(listed, ['?', 'i', 'j', 'k', 'r', 's'], 'outside the editor only the renderer’s own keys are listed');
    assert.deepEqual(handledKeys(browser).sort(), listed);
    assert.equal(rows.find(r => r.keys[0] === 'r').text, 'Check the saved file through the selected claim');
  } finally { browser.close(); }
});

test('the shortcuts dialog is a two-column list of key chips and descriptions, in groups', async () => {
  const window = await renderSnapshot(projectNotebook());
  const d = window.document;
  try {
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'reviewCommand', action: 'help'}}));
    assert.equal(d.getElementById('keys').hidden, false);
    assert.deepEqual([...d.querySelectorAll('.keys-group .label')].map(h => h.textContent), ['Navigate', 'View', 'Check and edit', 'General']);
    assert.ok(d.getElementById('keys-intro').textContent.length < 120, 'a short introduction');
    for (const dt of d.querySelectorAll('.keys-group dt')) {
      assert.ok(dt.querySelector('kbd'));
      for (const kbd of dt.querySelectorAll('kbd')) assert.doesNotMatch(kbd.textContent, /\s|\//, 'one key per chip');
      assert.equal(dt.textContent.replace(/\s+/g, ' ').trim(), [...dt.querySelectorAll('kbd')].map(k => k.textContent).join(dt.textContent.includes('then') ? ' then ' : ' '), 'only chips in the key column');
    }
    assert.ok(d.querySelector('#keys-foot #review-letter-keys'), 'the editor’s setting sits in the footer');
    assert.ok(d.querySelector('#keys-foot #keys-close'));
  } finally { window.close(); }
  const css = readFileSync(path.resolve(__dirname, '../../dist/media/app.css'), 'utf8');
  assert.match(css, /\.keys-dialog\{width:min\(600px,100%\)/, 'a sensible width that shrinks on a phone');
  assert.match(css, /\.keys-group dl\{display:grid;grid-template-columns:minmax\(92px,max-content\) minmax\(0,1fr\)/, 'keys sized to content, descriptions take the rest');
  assert.match(css, /\.keys-group dt\{white-space:nowrap\}/);
  assert.match(css, /\.dialog\{[^}]*max-height:100%;overflow:auto\}/, 'a tall dialog scrolls instead of leaving the screen');
  const rules = css.match(/\.keys-[^{]*\{[^}]*\}/g).join('');
  assert.doesNotMatch(rules, /#[0-9a-f]{3,6}\b|rgb\(/i, 'dialog colours come from the theme tokens, light and dark');
});

test('the contents keep long claim names inside the rail and their status dots in one column', async () => {
  const snapshot = projectNotebook({current: false});
  snapshot.cells[4] = {...snapshot.cells[4], text: snapshot.cells[4].text.replace('theorem floor', 'theorem energy_rate_nonpositive'), claim: 'Demo.energy_rate_nonpositive'};
  snapshot.project_claims[1].name = 'Demo.energy_rate_nonpositive';
  const window = await renderSnapshot(snapshot);
  try {
    const entries = [...window.document.querySelectorAll('#outline button[data-act="goto"]')];
    assert.deepEqual(entries.map(b => b.title), ['Demo.k_pos', 'Demo.energy_rate_nonpositive'], 'the full name on hover');
    const name = entries[1].querySelector('.o-claim');
    assert.equal(name.textContent, 'energy_rate_nonpositive');
    assert.equal(name.innerHTML, 'energy_<wbr>rate_<wbr>nonpositive', 'a long name wraps at its parts');
    assert.ok(entries.every(b => b.lastElementChild.matches('.dot')), 'the dot ends each entry');
  } finally { window.close(); }
  const css = readFileSync(path.resolve(__dirname, '../../dist/media/app.css'), 'utf8');
  assert.match(css, /\.rail li button\{width:100%;display:grid;grid-template-columns:minmax\(0,1fr\) 8px;/, 'names shrink, dots keep a column');
  assert.match(css, /\.rail \.o-claim\{[^}]*min-width:0;overflow-wrap:anywhere\}/);
  assert.match(css, /\.rail\{[^}]*padding:18px 14px 18px 14px;/, 'the same space on both sides of the contents');
});

test('n and p step through the project claims that need attention, and steps in Proof steps', async () => {
  const sha = 'a'.repeat(64);
  const names = ['fine', 'stale', 'sorry', 'axioms', 'carried', 'later'];
  const cells = [{index: 0, kind: 'source', start: 1, end: 1, text: 'import Mathlib\n'}];
  names.forEach((name, i) => cells.push({index: i + 1, kind: 'source', start: i * 3 + 2, end: i * 3 + 4, text: `theorem ${name} : True := by\n  trivial\n\n`, claim: name}));
  cells.push({index: 7, kind: 'source', start: 20, end: 21, text: 'theorem broken : False := by\n  simp\n'});
  const saved = (name, i, fields) => ({name, start_line: i * 3 + 2, end_line: i * 3 + 3, status: 'checked', current: true, axioms: [], ...fields});
  const snapshot = {file: 'Review.lean', sha256: sha, checking: [], check_errors: {}, claims: [], links: [], revisions: [], assets: [], cells, views: [], evidence: [], decisions: [],
    project_claims: [saved('fine', 0), saved('stale', 1, {current: false}), saved('sorry', 2, {status: 'admitted'}), saved('axioms', 3, {status: 'assumptions'}), saved('carried', 4, {not_rechecked: true}), saved('later', 5)],
    project_non_claims: [], project_withheld: {from_line: 20, message: 'unsolved goals'}, references: [],
    preview: {mode: 'project', notice: 'Saved-source preview.', check: null, project: {kind: 'lake', root: '/p', toolchain: 't'}}};
  const node = (id, line, text) => ({id, parent: null, text, range: {start: {line, character: 2}, end: {line, character: 9}}, before: ['⊢ True'], after: []});
  const messages = [];
  const window = await renderSnapshot(snapshot, {onMessage: m => messages.push(m), onProof: message => ['/api/proof/start', '/api/proof/trace'].includes(message.path)
    ? {id: 'steps', status: 'ready', source_sha256: sha, nodes: [node(0, 6, 'trivial'), node(1, 9, 'trivial'), node(2, 10, 'simp')]} : {}});
  const d = window.document, $ = id => d.getElementById(id), app = d.querySelector('.app');
  const key = k => app.dispatchEvent(new window.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
  const revealed = () => d.querySelector('.source-selected')?.dataset.index;
  const settle = () => new Promise(resolve => setTimeout(resolve, 30));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  try {
    assert.equal($('next-unresolved').hidden, false, 'Next and Previous show in Claims');
    assert.equal($('review-position').textContent, '5 need attention');
    const visited = [];
    for (let i = 0; i < 6; i++) { key('n'); await settle(); visited.push(revealed()); }
    assert.deepEqual(visited, ['2', '3', '4', '5', '7', '7'], 'out of date, sorry, assumptions, not rechecked, then where checking stopped; no wrap');
    assert.equal($('review-position').textContent, '5 of 5 needing attention');
    key('p'); await settle();
    assert.equal(revealed(), '5');
    assert.equal($('insp-title').textContent, 'carried', 'the renderer selects the claim n and p reach');
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'reviewCommand', action: 'previous'}})); await settle();
    assert.equal(revealed(), '4', 'the Previous Review Item command steps too');
    $('review-previous').click(); await settle();
    assert.equal(revealed(), '3');
    // Proof steps: n and p walk the recorded steps of the selected claim's cell.
    key('t'); await settle();
    assert.equal($('proof-review').hidden, false);
    assert.equal($('review-position').textContent, 'Initial goals');
    key('n'); assert.equal($('review-position').textContent, 'Step 1 of 2');
    key('n'); assert.equal($('review-position').textContent, 'Step 2 of 2');
    key('p'); assert.equal($('review-position').textContent, 'Step 1 of 2');
    key('c'); await settle();
    key('n'); await settle();
    assert.equal(revealed(), '4', 'back in Claims, n continues from the claim');
    assert.equal(messages.filter(m => m.method === 'POST' && m.path !== '/api/proof/start').length, 0, 'stepping never starts a check');
    // A check stopped on request, or an older file's stop, is not a failure to review.
    snapshot.project_withheld = {from_line: 20, through_line: 19, message: 'Checked through line 19 on request'};
    await window.glitnirPoll(); await settle();
    assert.equal($('review-position').textContent, '3 of 4 needing attention');
    snapshot.project_withheld = {from_line: 20, message: 'unsolved goals', stale: true};
    snapshot.project_claims.forEach(c => { c.status = 'checked'; c.current = true; delete c.not_rechecked; });
    await window.glitnirPoll(); await settle();
    assert.equal($('review-position').textContent, 'Nothing needs attention');
    assert.equal($('next-unresolved').disabled, true);
  } finally { window.close(); }
});

test('n and p never act in a linked, read-only notebook', async () => {
  const {main, linked} = linkedNotebooks();
  const messages = [];
  const window = await renderSnapshot(main, {onMessage: m => messages.push(m), onLinked: () => ({json: linked})});
  const d = window.document, $ = id => d.getElementById(id), app = d.querySelector('.app');
  const key = k => app.dispatchEvent(new window.KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}));
  const settle = () => new Promise(resolve => setTimeout(resolve, 30));
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.scrollTo = () => {};
  try {
    key('n'); await settle();
    assert.equal(d.querySelector('.source-selected')?.dataset.index, '4', 'n reaches the claim containing sorry in the previewed notebook');
    d.querySelector('#doc a.notebook-reference').click(); await settle();
    assert.equal($('title').textContent, 'Three states');
    const sent = messages.length;
    key('n'); key('p');
    $('review-previous').click(); $('next-unresolved').click();
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'reviewCommand', action: 'next'}}));
    window.dispatchEvent(new window.MessageEvent('message', {data: {type: 'reviewCommand', action: 'previous'}}));
    await settle();
    assert.equal($('title').textContent, 'Three states', 'still the linked notebook');
    assert.ok(d.body.classList.contains('glitnir-linked-view'));
    assert.deepEqual(messages.slice(sent).filter(m => m.type !== 'request' || !m.path.startsWith('/api/linked')), []);
  } finally { window.close(); }
});

test('saved results shown as recorded get their own banner state and never read as current', async () => {
  const claimText = 'theorem t : True := by\n  trivial\n';
  const recordedSnapshot = recorded => evidenceSnapshot(claimText + EVIDENCE_TEXT, [evidenceEntry({cell: 1, line: 4, status: 'current', unavailable: null, snapshot: evidenceBody()})], {
    cells: [{index: 0, kind: 'source', start: 1, end: 2, text: claimText, claim: 't'}, {index: 1, kind: 'source', start: 3, end: 4, text: EVIDENCE_TEXT}],
    project_claims: [{name: 't', status: 'checked', current: true, axioms: [], start_line: 1, end_line: 2}],
    preview: {mode: 'project', project: {kind: 'lake', root: '/p', toolchain: 't'}, ...(recorded
      ? {recorded: true, notice: 'Saved results, shown as recorded. Each result is shown as its check last saved it, compared only with this saved source and not with the reader\'s Lean environment. Nothing was checked or run to show it.'}
      : {notice: 'Saved-source preview.'})}});
  let window = await renderSnapshot(recordedSnapshot(true), {katex: true});
  try {
    const $ = id => window.document.getElementById(id);
    assert.ok($('banner').classList.contains('recorded'));
    assert.equal($('banner').dataset.state, 'recorded');
    assert.match($('banner-text').textContent, /^Saved results, shown as recorded\. .*not with the reader's Lean environment/);
    assert.equal($('check-project').hidden, true, 'nothing checks saved results shown as recorded');
    assert.equal($('inspect-project').hidden, true);
    const card = window.document.querySelector('#doc .evidence');
    assert.deepEqual([card.querySelector('.pill').textContent, card.querySelector('.pill').dataset.tone], ['As recorded', 'muted']);
    assert.equal(window.document.querySelector('[data-act="evidence-run"], [data-act="check-through"]'), null);
    assert.doesNotMatch($('doc').textContent, /\bCurrent\b|Out of date/);
    assert.match($('insp').textContent, /as recorded/);
    assert.doesNotMatch($('insp').textContent, /\bcurrent\b/);
    // Each current-as-recorded claim says so: card, inspector and contents.
    const claimCard = window.document.querySelector('#doc .project-claim .claim');
    assert.equal(claimCard.dataset.tone, 'muted');
    assert.deepEqual([...claimCard.querySelectorAll('.claim-head .pill')].map(pill => [pill.textContent, pill.dataset.tone]).at(-1), ['As recorded', 'muted']);
    assert.match($('insp-sub').textContent, / · as recorded$/);
    const dot = $('outline').querySelector('.dot');
    assert.equal(dot.dataset.tone, 'muted');
    assert.match(dot.title, / · as recorded$/);
    let html;
    window.glitnirHost.saveHTML = async value => { html = value; };
    await $('export-html').onclick();
    assert.ok(html, $('load-error').textContent);
    const page = new JSDOM(html);
    try {
      const heading = page.window.document.querySelector('.snapshot');
      assert.ok(heading.classList.contains('recorded'));
      assert.match(heading.textContent, /Saved results, shown as recorded\. .* This static page does not check or update results\.$/);
      assert.equal(page.window.document.querySelector('.evidence .pill').textContent, 'As recorded');
    } finally { page.window.close(); }
  } finally { window.close(); }
  window = await renderSnapshot(recordedSnapshot(false));
  try {
    assert.equal(window.document.getElementById('banner').classList.contains('recorded'), false);
    assert.equal(window.document.getElementById('banner').dataset.state, '');
    assert.equal(window.document.querySelector('#doc .evidence .pill').textContent, 'Current');
    assert.ok(window.document.querySelector('[data-act="evidence-run"]'), 'the editor preview can run it');
    const claimCard = window.document.querySelector('#doc .project-claim .claim');
    assert.notEqual(claimCard.dataset.tone, 'muted');
    assert.doesNotMatch(claimCard.querySelector('.claim-head').textContent, /As recorded/);
    assert.doesNotMatch(window.document.getElementById('insp-sub').textContent, /recorded/);
  } finally { window.close(); }
});

// A project notebook with one saved claim, an evidence card, a decision candidate
// the kernel checked and a view: every place a Lean command could start.
const LEAN_MESSAGE = 'This project uses Lean 4.31.0. Glitnir checks Lean 4.32.1, so checking, proof steps, views, evidence and decisions are off; reading works as usual.';
function leanSupportNotebook({lean, recorded = false, toolchainError = null} = {}) {
  const claimText = 'theorem t : True := by\n  trivial\n';
  const rest = 'def M := 1\n-- @evidence simulate M (h := 1/10)\n-- @decide band : entailment x : x > 0 |- x >= 0\n-- @view circuit c\n';
  const files = toolchainError ? {'lean-toolchain': {error: toolchainError}} : {};
  return evidenceSnapshot(claimText + rest, [evidenceEntry({cell: 1, line: 4, status: 'current', unavailable: null, snapshot: evidenceBody()})], {
    cells: [{index: 0, kind: 'source', start: 1, end: 2, text: claimText, claim: 't'}, {index: 1, kind: 'source', start: 3, end: 6, text: rest}],
    project_claims: [{name: 't', status: 'checked', current: false, axioms: [], start_line: 1, end_line: 2}],
    decisions: [servedDecision({cell: 1, line: 5, end_line: 5, status: 'candidate', outcome: decisionOutcome(), verification: checkedVerification()})],
    views: [{cell: 1, line: 6, kind: 'circuit', name: 'c', using: [], error: ''}],
    directive_providers: {evidence: ['simulate'], decide: ['entailment']},
    preview: {mode: 'project', notice: recorded ? 'Saved results, shown as recorded.' : 'Saved-source preview.', ...(recorded ? {recorded: true} : {}),
      project: {kind: 'lake', root: '/p', toolchain: 'leanprover/lean4:v4.31.0', files}, ...(lean ? {lean} : {})}});
}
const UNSUPPORTED = {supported: false, declared: 'leanprover/lean4:v4.31.0', toolchain: 'leanprover/lean4:v4.32.1', message: LEAN_MESSAGE};
const SUPPORTED = {supported: true, declared: 'leanprover/lean4:v4.32.1', toolchain: 'leanprover/lean4:v4.32.1', message: null};
const RUN_CONTROLS = '[data-act="evidence-run"], [data-act="decision-run"], [data-act="accept"], [data-act="check-through"], [data-act="goal"], [data-act="view-retry"]';

test('a project on another Lean version shows one banner, no enabled Lean control and no view request', async () => {
  const messages = [];
  const window = await renderSnapshot(leanSupportNotebook({lean: UNSUPPORTED}), {onMessage: m => messages.push(m), onView: () => ({svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'})});
  try {
    const d = window.document, $ = id => d.getElementById(id);
    assert.equal($('lean-unsupported').hidden, false);
    assert.equal($('lean-unsupported').dataset.tone, 'amber');
    assert.equal($('lean-unsupported').textContent, LEAN_MESSAGE);
    assert.equal($('check-project').hidden, true);
    assert.equal($('inspect-project').hidden, true);
    assert.equal(d.querySelectorAll(`#doc ${RUN_CONTROLS.split(', ').join(', #doc ')}, #insp [data-act="goal"], #insp [data-act="check-through"]`).length, 0, 'no Lean control is offered');
    assert.match(d.querySelector('#doc .view-cell').textContent, /Views need Lean 4\.32\.1\./);
    assert.equal(messages.filter(m => m.path?.startsWith('/api/view')).length, 0, 'nothing is drawn');
    // The saved result still reads, out of date.
    assert.deepEqual([...d.querySelectorAll('#doc .project-claim .claim-head .pill')].map(p => p.textContent), ['Kernel checked', 'Out of date']);
    assert.ok(d.querySelector('#doc .evidence .evidence-series, #doc .evidence'), 'the saved evidence stays');
    assert.match(d.querySelector('#doc .decision').textContent, /band/);
  } finally { window.close(); }
});

test('the unsupported-version banner names a lean-toolchain read error and escapes everything', async () => {
  const hostile = {...UNSUPPORTED, message: 'This project declares no Lean version. <b>x</b> Glitnir checks Lean 4.32.1.'};
  const snapshot = leanSupportNotebook({lean: hostile, toolchainError: 'cannot read <i>it</i>'});
  const window = await renderSnapshot(snapshot);
  try {
    const banner = window.document.getElementById('lean-unsupported');
    assert.equal(banner.textContent, `${hostile.message} (lean-toolchain: cannot read <i>it</i>)`);
    assert.equal(banner.querySelector('b, i'), null);
  } finally { window.close(); }
});

test('a view card names the version from the snapshot, not a fixed one', async () => {
  const window = await renderSnapshot(leanSupportNotebook({lean: {...UNSUPPORTED, toolchain: 'leanprover/lean4:v4.40.2'}}));
  try {
    assert.match(window.document.querySelector('#doc .view-cell').textContent, /Views need Lean 4\.40\.2\./);
  } finally { window.close(); }
});

test('a supported project shows no Lean banner and keeps its controls', async () => {
  const messages = [];
  const window = await renderSnapshot(leanSupportNotebook({lean: SUPPORTED}), {onMessage: m => messages.push(m), onView: () => ({svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'})});
  try {
    const d = window.document, $ = id => d.getElementById(id);
    assert.equal($('lean-unsupported').hidden, true);
    assert.equal($('check-project').hidden, false);
    assert.equal($('inspect-project').hidden, false);
    for (const act of ['evidence-run', 'decision-run', 'accept']) assert.ok(d.querySelector(`#doc [data-act="${act}"]`), act);
    assert.ok(d.querySelector('#doc [data-act="check-through"]'));
    assert.equal(messages.filter(m => m.path?.startsWith('/api/view')).length, 1);
  } finally { window.close(); }
});

test('saved results shown as recorded never show the Lean banner, even when the snapshot carries lean.supported false', async () => {
  const window = await renderSnapshot(leanSupportNotebook({lean: UNSUPPORTED, recorded: true}));
  try {
    const d = window.document;
    assert.equal(d.getElementById('lean-unsupported').hidden, true);
    assert.doesNotMatch(d.body.textContent, /Glitnir checks Lean/);
    assert.equal(d.querySelector(`#doc ${RUN_CONTROLS.split(', ').join(', #doc ')}`), null);
  } finally { window.close(); }
});

test('a recorded static page of an unsupported project still asks for the saved drawing', async () => {
  const asked = [];
  const window = await renderSnapshot(leanSupportNotebook({lean: UNSUPPORTED, recorded: true}), {onView: path => { asked.push(path); return {svg: '<svg xmlns="http://www.w3.org/2000/svg"/>'}; }});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(asked.length, 1);
    assert.ok(window.document.querySelector('#doc .view-cell img'));
    assert.doesNotMatch(window.document.querySelector('#doc .view-cell').textContent, /Views need Lean/);
  } finally { window.close(); }
});

test('the toolchain read error is appended only when it is a real read error, not a missing file', async () => {
  const none = {...UNSUPPORTED, message: 'This project declares no Lean version. Glitnir checks Lean 4.32.1.'};
  for (const [error, suffix] of [['missing', ''], ['unreadable', ' (lean-toolchain: unreadable)']]) {
    const window = await renderSnapshot(leanSupportNotebook({lean: none, toolchainError: error}));
    try { assert.equal(window.document.getElementById('lean-unsupported').textContent, none.message + suffix, error); } finally { window.close(); }
  }
});

test('the evidence and decision run handlers are inert on an unsupported project', async () => {
  const messages = [];
  const window = await renderSnapshot(leanSupportNotebook({lean: UNSUPPORTED}), {onMessage: m => messages.push(m)});
  try {
    const d = window.document;
    for (const act of ['evidence-run', 'decision-run']) {
      const b = d.createElement('button');
      b.dataset.act = act; b.dataset.line = '4'; b.dataset.sha = 'a'.repeat(64);
      d.getElementById('doc').append(b); b.click();
    }
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(messages.filter(m => /^\/api\/(evidence|decide)\//.test(m.path || '')), []);
  } finally { window.close(); }
});
