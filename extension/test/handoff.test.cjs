const assert = require('node:assert/strict');
const {test} = require('node:test');
const {mkdtemp, rm, readFile} = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const fs = require('node:fs');
const {HandoffJournal} = require('../../dist/handoff-journal');

function load(vscode = {}) {
  const exports = {}; const panels = [];
  class HandoffReviewPanel {
    constructor(state, action, onClose) {this.state=state;this.action=action;this.onClose=onClose;this.closed=false;panels.push(this);}
    update(value) {this.state={...this.state,...value};}
    reveal() {}
  }
  vm.runInNewContext(fs.readFileSync(require.resolve('../../dist/handoff'), 'utf8'), {
    exports, URL, Buffer, setTimeout, clearTimeout, require: name => name === 'vscode' ? vscode : name === './setup' ? {} : name === './handoff-journal' ? {HandoffJournal} : name === './handoff-panel' ? {HandoffReviewPanel} : require(name),
  });
  return {...exports, panels};
}

test('handoff credentials are separate and destination-bound', () => {
  const {handoffScope} = load();
  const first = handoffScope('taskvalet', 'https://TASKS.example:443/', 'lab');
  assert.equal(first, handoffScope('taskvalet', 'https://tasks.example', 'lab'));
  assert.notEqual(first, handoffScope('taskvalet', 'https://tasks.example', 'other'));
  assert.ok(first.startsWith('glitnir.handoff:'));
  for (const server of ['http://tasks.example', 'https://u:p@tasks.example', 'https://tasks.example/path'])
    assert.throws(() => handoffScope('taskvalet', server, 'lab'));
});

test('separate editor windows retain independent lost-response envelopes', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-'));
  try {
    const a = new HandoffJournal(root), b = new HandoffJournal(root);
    const [one, two] = await Promise.all([a.persist({request: 'one'}), b.persist({request: 'two'})]);
    assert.notEqual(one, two);
    assert.equal((await a.pending()).length, 2);
    await a.finish(one, {url: 'https://tasks.example/#task=lab/one'});
    assert.deepEqual(await b.pending(), [{id: two, value: {request: 'two'}}]);
    // Simultaneous exact replay completion cannot clear another request.
    await a.finish(one, {url: 'https://tasks.example/#task=lab/one'});
    assert.equal((await b.pending())[0].id, two);
    assert.ok(await readFile(path.join(root, one + '.completed'), 'utf8'));
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('untrusted or dirty editor refuses before invoking provider or reading a secret', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-'));
  try {
    const vscode = {workspace: {isTrusted: false}, window: {}};
    const {NotebookHandoff} = load(vscode);
    const context = {globalStorageUri: {fsPath: root}, secrets: {get: () => assert.fail('secret read')}};
    const instance = new NotebookHandoff(context, {resolve: () => assert.fail('provider invoked')});
    await assert.rejects(instance.start(), /Trust/);
    vscode.workspace.isTrusted = true;
    vscode.window.activeTextEditor = {document: {uri: {scheme: 'file', path: '/A.lean'}, isDirty: true}};
    await assert.rejects(instance.start(), /saved/);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('pending recovery requires explicit confirmation and keeps the envelope on cancel', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-'));
  try {
    const journal = new HandoffJournal(path.join(root, 'notebook-handoffs'));
    await journal.persist({provider: {name: 'taskvalet', label: 'Taskvalet'}, payload: {server: 'https://tasks.example', project: 'lab', actor: 'agent/a'}});
    const vscode = {workspace: {isTrusted: true}, window: {showQuickPick: async items => items[0], showWarningMessage: async () => undefined}};
    const {NotebookHandoff} = load(vscode);
    await new NotebookHandoff({globalStorageUri: {fsPath: root}, secrets: {get: () => assert.fail('secret read on cancel')}}, {}).recover();
    assert.equal((await journal.pending()).length, 1);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('a refused recovery cannot retire an earlier committed but lost response', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-'));
  try {
    const journal = new HandoffJournal(path.join(root, 'notebook-handoffs'));
    const pending = {provider: {name: 'taskvalet', label: 'Taskvalet'}, python: 'python', payload: {server: 'https://tasks.example', project: 'lab', actor: 'agent/a'}};
    const id = await journal.persist(pending);
    const {NotebookHandoff} = load({workspace: {isTrusted: true}, window: {showWarningMessage: async () => {}}});
    const instance = new NotebookHandoff({globalStorageUri: {fsPath: root}}, {}, async () => ({refused: true}));
    await assert.rejects(instance.send(id, pending, 'token', true), /earlier outcome is still unknown/);
    assert.equal((await journal.pending())[0].id, id);
    // A direct, first-attempt refusal is safe to archive, without retrying.
    await instance.send(id, pending, 'token', false);
    assert.equal((await journal.pending()).length, 0);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('a reloaded host recovers the exact lost-response request only after an explicit retry', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-reload-'));
  try {
    const saved = new Map();
    const vscode = {workspace: {isTrusted: true}, window: {showQuickPick: async items => items[0]}};
    const context = {globalStorageUri: {fsPath: root}, secrets: {get: async () => 'secret'},
      globalState: {get: key => saved.get(key), update: async (key, value) => saved.set(key, value)}};
    const {NotebookHandoff, handoffScope} = load(vscode);
    const pending = {provider: {name: 'taskvalet', label: 'Taskvalet'}, python: 'original-python',
      scope: handoffScope('taskvalet', 'https://tasks.example', 'lab'),
      payload: {server: 'https://tasks.example', project: 'lab', request_id: require('node:crypto').randomUUID(),
        actor: 'agent/a', source_sha256: 'a'.repeat(64), body: {objective: 'Original objective'}}};
    const journal = new HandoffJournal(path.join(root, 'notebook-handoffs'));
    const id = await journal.persist(pending);
    const first = new NotebookHandoff(context, {}, async () => {throw Error('Response lost after commit');});
    await assert.rejects(first.send(id, pending, 'secret'), /Response lost/);

    const reloaded = load(vscode);
    const calls = [];
    const next = new reloaded.NotebookHandoff(context, {resolve: () => assert.fail('must not prepare a replacement')}, async (python, request) => {
      calls.push(request.action);
      assert.equal(python, pending.python);
      assert.equal(request.action, 'submit');
      assert.deepEqual(request.payload, pending.payload);
      return {url: 'https://tasks.example/#task=lab/001-original', task: '001-original', stage: 'new'};
    });
    assert.deepEqual(calls, [], 'constructing the replacement host must not retry');
    await next.recover();
    assert.deepEqual(calls, [], 'opening recovery must not submit');
    assert.equal(reloaded.panels[0].state.phase, 'uncertain');
    assert.deepEqual(await journal.pending(), [{id, value: pending}]);
    await reloaded.panels[0].action('retry', 'Ignore this replacement objective');
    assert.deepEqual(calls, ['submit']);
    assert.equal(reloaded.panels[0].state.phase, 'submitted');
    assert.deepEqual(await journal.pending(), []);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('reviewed handoff refuses source changes before persisting or submitting', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-'));
  try {
    let source = 'theorem demo : True := by trivial\n';
    let submits = 0;
    const document = {uri: {scheme: 'file', path: '/Demo.lean', fsPath: '/Demo.lean'}, isDirty: false, getText: () => source};
    const values = {taskvaletServer: 'https://tasks.example', taskvaletProject: 'lab'};
    const vscode = {ViewColumn: {Beside: 2}, workspace: {isTrusted: true, textDocuments: [document],
      fs: {readFile: async () => Buffer.from(source)}, getConfiguration: () => ({get: (key, fallback) => values[key] ?? fallback}),
      openTextDocument: async value => value}, window: {activeTextEditor: {document},
      showQuickPick: async items => items[0], showInputBox: async () => 'Explore this theorem',
      showTextDocument: async () => {}, showWarningMessage: async (message, options, choice) => {
        if (choice === 'Submit request') source += '-- changed during preview\n';
        return choice;
      }}};
    const runner = async (python, request) => {
      if (request.action === 'list') return [{name: 'taskvalet', label: 'Taskvalet'}];
      if (request.action === 'prepare') return {source_sha256: require('node:crypto').createHash('sha256').update(source).digest('hex')};
      submits++; throw Error('unexpected submit');
    };
    const {NotebookHandoff, panels} = load(vscode);
    const instance = new NotebookHandoff({globalStorageUri: {fsPath: root}, secrets: {get: async () => 'secret'}}, {resolve: async () => ({python: 'python'})}, runner);
    await instance.start();
    source += '-- changed during review\n';
    await panels[0].action('submit', 'Explore this theorem');
    assert.match(panels[0].state.error, /Source or destination changed/);
    assert.equal(submits, 0);
    assert.deepEqual(await new HandoffJournal(path.join(root, 'notebook-handoffs')).pending(), []);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('panel submits once, keeps secrets in the host and refreshes the returned task', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-'));
  try {
    const source = 'theorem demo : True := by trivial\n';
    const document = {uri: {scheme: 'file', path: '/Demo.lean', fsPath: '/Demo.lean'}, isDirty: false, getText: () => source};
    const values = {taskvaletServer: 'https://tasks.example', taskvaletProject: 'lab'};
    const saved = new Map(); const calls = []; let lostResponse = true;
    const vscode = {workspace: {isTrusted: true, textDocuments: [document], fs: {readFile: async () => Buffer.from(source)},
      getConfiguration: () => ({get: (key, fallback) => values[key] ?? fallback})},
      window: {activeTextEditor: {document}, showQuickPick: async items => items[0]}};
    const journal = new HandoffJournal(path.join(root, 'notebook-handoffs'));
    const runner = async (python, request) => {
      calls.push(request.action);
      if (request.action === 'list') return [{name: 'taskvalet', label: 'Send to Taskvalet', supportsStatus: true}];
      if (request.action === 'prepare') return {server: values.taskvaletServer, project: 'lab', source_sha256: require('node:crypto').createHash('sha256').update(source).digest('hex')};
      if (request.action === 'submit') {
        assert.equal((await journal.pending()).length, 1, 'journal precedes POST');
        if (lostResponse) {lostResponse=false;throw Error('Response lost');}
        return {url: 'https://tasks.example/#task=lab/001-request', task: '001-request', stage: 'new'};
      }
      assert.equal(request.action, 'status');assert.equal(request.payload.task, '001-request');
      return {task: '001-request', stage: 'needs_input'};
    };
    const {NotebookHandoff, panels} = load(vscode);
    const instance = new NotebookHandoff({globalStorageUri: {fsPath: root}, secrets: {get: async () => 'private-secret'},
      globalState: {get: key => saved.get(key), update: async (key, value) => saved.set(key,value)}}, {resolve: async () => ({python: 'python'})}, runner);
    await instance.start();assert.deepEqual(calls, ['list']);
    await Promise.all([panels[0].action('submit', 'Explore a simpler proof'),panels[0].action('submit','Duplicate')]);
    assert.equal(calls.filter(a => a === 'submit').length,1);assert.equal(panels[0].state.phase,'uncertain');
    await panels[0].action('retry','');
    assert.equal(panels[0].state.phase,'submitted');
    assert.equal(panels[0].state.objective,'Explore a simpler proof');assert.ok(!JSON.stringify(panels[0].state).includes('private-secret'));
    await panels[0].action('refresh','');assert.equal(panels[0].state.stage,'needs_input');
    assert.equal((await journal.pending()).length,0);
    panels[0].onClose();await instance.recover();assert.equal(panels[1].state.task,'001-request');
    assert.equal(panels[1].state.source,source);
  } finally { await rm(root, {recursive: true, force: true}); }
});
