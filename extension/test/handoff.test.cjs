const assert = require('node:assert/strict');
const {test} = require('node:test');
const {mkdtemp, rm, readFile} = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const fs = require('node:fs');
const {HandoffJournal} = require('../../dist/handoff-journal');

function load(vscode = {}) {
  const exports = {};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../dist/handoff'), 'utf8'), {
    exports, URL, Buffer, setTimeout, clearTimeout, require: name => name === 'vscode' ? vscode : name === './setup' ? {} : name === './handoff-journal' ? {HandoffJournal} : require(name),
  });
  return exports;
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
    const {NotebookHandoff} = load(vscode);
    const instance = new NotebookHandoff({globalStorageUri: {fsPath: root}, secrets: {get: async () => 'secret'}}, {resolve: async () => ({python: 'python'})}, runner);
    await assert.rejects(instance.start(), /Source, destination or credential changed/);
    assert.equal(submits, 0);
    assert.deepEqual(await new HandoffJournal(path.join(root, 'notebook-handoffs')).pending(), []);
  } finally { await rm(root, {recursive: true, force: true}); }
});
