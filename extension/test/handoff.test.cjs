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
    const restarted = new HandoffJournal(root);
    assert.deepEqual(await restarted.submitted(), [{id: one, value: {request: 'one'}, receipt: {url: 'https://tasks.example/#task=lab/one'}}]);
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
    vscode.workspace.textDocuments = [vscode.window.activeTextEditor.document];
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

test('notebook-only preview hands off saved source without opening a text editor', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-preview-'));
  try {
    const uri = {scheme: 'file', path: '/Preview.lean', fsPath: '/Preview.lean'};
    const source = 'theorem preview : True := by trivial\n';
    const documents = [];
    const values = {taskvaletServer: 'https://tasks.example', taskvaletProject: 'lab'};
    const vscode = {workspace: {isTrusted: true, textDocuments: documents,
      fs: {readFile: async () => Buffer.from(source)},
      getConfiguration: () => ({get: key => values[key]})},
      window: {showQuickPick: async items => items[0]}};
    const {NotebookHandoff, panels} = load(vscode);
    const instance = new NotebookHandoff({globalStorageUri: {fsPath: root}},
      {resolve: async target => {assert.equal(target, uri); return {python: 'python'};}},
      async requestPython => [{name: 'taskvalet', label: 'Taskvalet'}]);
    await instance.start(uri);
    assert.equal(panels[0].state.source, source);
    assert.equal(panels[0].state.notebook, 'Preview.lean');
    await assert.rejects(instance.start({...uri, fsPath: '/Other.lean', path: '/Other.lean'}), /Close the existing handoff panel/);
    assert.equal(panels.length, 1);
    documents.push({uri, isDirty: true, getText: () => source + '-- edit'});
    await panels[0].action('submit', 'Investigate');
    assert.match(panels[0].state.error, /Source or destination changed/);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('menu discovery uses only the local provider list and fails closed', async () => {
  const {NotebookHandoff} = load({workspace: {isTrusted: true}});
  const context = {globalStorageUri: {fsPath: '/unused'}, secrets: {get: () => assert.fail('secret read')}};
  let providers = [];
  const instance = new NotebookHandoff(context, {}, async (python, request) => {
    assert.equal(python, 'python'); assert.deepEqual(JSON.parse(JSON.stringify(request)), {action: 'list'});
    if (providers === null) throw Error('unavailable');
    return providers;
  });
  assert.equal(await instance.available('python', 'taskvalet'), false);
  providers = [{name: 'taskvalet'}];
  assert.equal(await instance.available('python', 'taskvalet'), true);
  providers = null;
  assert.equal(await instance.available('python', 'taskvalet'), false);
});

test('Jobs retains multiple submitted requests across reloads and opens only a saved identity', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-jobs-'));
  try {
    const journal = new HandoffJournal(path.join(root, 'notebook-handoffs'));
    const request = name => ({provider: {name: 'taskvalet', label: 'Taskvalet'}, python: 'python',
      payload: {server: 'https://tasks.example', project: 'lab'},
      review: {notebook: name, source: 'source', objective: 'objective', server: 'https://tasks.example', project: 'lab'}});
    const one = await journal.persist(request('One.lean'));
    await journal.finish(one, {task: '001-one', stage: 'review'});
    const two = await journal.persist(request('Two.lean'));
    await journal.finish(two, {task: '002-two', stage: 'new'});
    const {NotebookHandoff, panels} = load({workspace: {isTrusted: true}});
    const instance = new NotebookHandoff({globalStorageUri: {fsPath: root}, secrets: {get: () => assert.fail('secret read')}}, {}, () => assert.fail('network'));
    const jobs = await instance.jobs();
    assert.equal(jobs.length, 2);
    assert.ok(jobs.every(job => job.canReview && !job.canStop));
    assert.equal(jobs.find(job => job.id === 'handoff:' + one).state, 'Ready for review');
    await assert.rejects(instance.openRequest('https://untrusted.example'), /Invalid/);
    await instance.openRequest('handoff:' + two);
    assert.equal(panels[0].state.notebook, 'Two.lean');
    assert.equal(panels[0].state.task, '002-two');
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('tracking polls read-only, persists findings, warns on stale source and notifies once across reload', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'handoff-monitor-'));
  try {
    const journal = new HandoffJournal(path.join(root, 'notebook-handoffs'));
    const pending = {notebookPath: '/Demo.lean', provider: {name: 'taskvalet', label: 'Taskvalet', supportsStatus: true},
      python: 'python', scope: 'scope', payload: {}, review: {notebook: 'Demo.lean', source: 'original', objective: 'Explore', server: 'https://tasks.example', project: 'lab'}};
    const id = await journal.persist(pending); await journal.finish(id, {task: '001-request', stage: 'new'});
    const notices=[];const calls=[];
    const vscode={Uri:{file:path=>({fsPath:path})},workspace:{isTrusted:true,textDocuments:[],fs:{readFile:async()=>Buffer.from('changed')}},
      window:{showInformationMessage:async message=>{notices.push(message);},showInputBox:()=>assert.fail('background prompt')}};
    const {NotebookHandoff}=load(vscode);
    const context={globalStorageUri:{fsPath:root},secrets:{get:async()=> 'secret'}};
    const runner=async (python,request)=>{calls.push(request);return {task:'001-request',stage:'review',results:{availability:'available',digest:'finding-v1',hasFindings:true,sections:[],message:'Review findings'}};};
    await new NotebookHandoff(context,{},runner).refreshTracked();
    const restarted=new NotebookHandoff(context,{},runner);await restarted.refreshTracked();
    assert.equal(notices.length,1);assert.ok(calls.every(call=>call.action==='status'&&call.payload.results===true));
    assert.equal((await restarted.jobs())[0].state,'Results need review');
    assert.match((await journal.submitted())[0].receipt.freshness,/changed/);
    vscode.workspace.isTrusted=false;await restarted.refreshTracked();assert.equal(calls.length,2);
  } finally {await rm(root,{recursive:true,force:true});}
});

test('reviewing a recovered result survives an in-flight poll and an unanswered notification', async () => {
  const root=await mkdtemp(path.join(os.tmpdir(),'handoff-review-race-'));
  try {
    const journal=new HandoffJournal(path.join(root,'notebook-handoffs'));
    const pending={provider:{name:'taskvalet',label:'Taskvalet',supportsStatus:true},python:'python',scope:'scope',payload:{},review:{notebook:'A.lean',source:'source',objective:'Explore'}};
    const id=await journal.persist(pending);const results={availability:'available',digest:'v1',hasFindings:true,sections:[]};
    await journal.finish(id,{task:'001-request',stage:'review',results});
    let resolveStatus;let entered;const started=new Promise(resolve=>{entered=resolve;});let notifications=0;
    const {NotebookHandoff,panels}=load({workspace:{isTrusted:true},window:{showInformationMessage:()=>{notifications++;return new Promise(()=>{});}}});
    const context={globalStorageUri:{fsPath:root},secrets:{get:async()=> 'secret'}};
    const instance=new NotebookHandoff(context,{},async()=>{entered();return new Promise(resolve=>{resolveStatus=resolve;});});
    const refresh=instance.refreshTracked();await started;
    await instance.recover();await panels[0].action('markReviewed','');
    resolveStatus({task:'001-request',stage:'review',results});await refresh;
    assert.equal((await journal.submitted())[0].receipt.reviewedDigest,'v1');assert.equal(notifications,0);
    let reads=0;const newer=new NotebookHandoff(context,{},async()=>{reads++;return {task:'001-request',stage:'review',results:{...results,digest:'v2'}};});
    await newer.refreshTracked();await newer.refreshTracked();assert.equal(reads,2);assert.equal(notifications,1);
  } finally {await rm(root,{recursive:true,force:true});}
});
