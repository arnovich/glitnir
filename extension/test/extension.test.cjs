const assert = require('node:assert/strict');
const {test} = require('node:test');
const {EventEmitter} = require('node:events');
const Module = require('node:module');
const path = require('node:path');
const {mkdtempSync, symlinkSync, rmSync, readFileSync, realpathSync, writeFileSync} = require('node:fs');
const {tmpdir} = require('node:os');

// Exercise command and message handlers through a small VS Code API double.
test('preview tabs deduplicate, restrict dirty-file checks and reject injected routes', async () => {
  const commands = new Map(), panels = [], servers = [], events = {}, writes = [];
  const pendingWrites = new Map(), temporaryWrites = [];
  const gitFiles = [], gitDiffs = [], sourceWatchers = [];
  let memoryInspect = {defaultValue: 16384, globalValue: 12288};
  let saveTarget, dialogs = 0, clipboard;
  const externalUris = [];
  let references = [];
  let candidateReply;
  let packageRestart;
  const packageCalls = [];
  const edits = [], saves = [], infos = [];
  const fixture = path.resolve(__dirname, 'fixtures/Dissipative.lean');
  const file = name => ({scheme: 'file', fsPath: name, path: name, toString: () => `file://${name}`});
  const source = readFileSync(fixture, 'utf8');
  const hash = require('node:crypto').createHash('sha256').update(source).digest('hex');
  const snapshot = {sha256: hash, cells: [{index: 0, start: 1, end: 2}], claims: [], links: []};
  const documents = [{uri: file(fixture), isDirty: false, version: 1, getText: () => source, lineAt: i => ({text: source.split('\n')[i]}),
    save: async () => { saves.push(fixture); return true; }}];
  const editor = {document: documents[0], selection: {active: {line: 1}}, revealRange(range) { this.revealed = range; }};
  class FakeBackend {
    constructor(options) { this.options = options; this.ready = Promise.resolve(); this.requests = []; this.running = true; servers.push(this); }
    async request(url, method = 'GET', body) {
      require('../../dist/backend').validateRequest(url, method, body);
      this.requests.push([url, method]);
      if (url.startsWith('/api/decide/candidate?')) return candidateReply;
      return {status: 200, json: {...snapshot, references}};
    }
    async close() { this.closed = true; this.running = false; }
  }
  const fake = {
    RelativePattern: class { constructor(base, pattern) { this.base = base; this.pattern = pattern; } },
    Uri: {file, joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts)), parse: value => {
      const parsed = new URL(value);
      return {scheme: parsed.protocol.slice(0, -1), toString: () => value};
    }},
    ViewColumn: {One: 1, Beside: 2}, env: {
      clipboard: {writeText: async text => { clipboard = text; }},
      openExternal: async uri => { externalUris.push(uri.toString()); return true; },
    },
    TextEditorRevealType: {InCenterIfOutsideViewport: 1},
    Position: class { constructor(line, character) { this.line = line; this.character = character; } },
    WorkspaceEdit: class { constructor() { this.inserts = []; } insert(uri, position, text) { this.inserts.push([uri.fsPath, position.line, position.character, text]); } },
    Range: class { constructor(a, b, c, d) { this.start = {line: a, character: b}; this.end = {line: c, character: d}; } },
    Selection: class { constructor(start, end) { this.start = start; this.end = end; this.active = end; } },
    workspace: {
      createFileSystemWatcher: pattern => {
        if (typeof pattern === 'string') return {dispose() {}, onDidCreate: fn => {events.dependency = fn; return {};}, onDidDelete: () => ({}), onDidChange: () => ({})};
        assert.equal(pattern.pattern, '*', 'previews watch only their source directory, never a sidecar');
        const watcher = {pattern, dispose() { this.disposed = true; }};
        for (const event of ['Change', 'Create', 'Delete']) watcher['onDid' + event] = fn => { watcher[event] = fn; return {dispose() {}}; };
        sourceWatchers.push(watcher);
        return watcher;
      },
      onDidChangeConfiguration: fn => { events.configuration = fn; return {}; },
      isTrusted: true, textDocuments: documents,
      applyEdit: async edit => { edits.push(...edit.inserts); documents[0].version++; return true; },
      fs: {
        writeFile: async (uri, bytes) => { pendingWrites.set(uri.fsPath, bytes.toString()); temporaryWrites.push(uri.fsPath); },
        rename: async (from, to, options) => {
          assert.deepEqual(options, {overwrite: true});
          writes.push([to, pendingWrites.get(from.fsPath)]);
          pendingWrites.delete(from.fsPath);
        },
        delete: async uri => { pendingWrites.delete(uri.fsPath); },
      },
      openTextDocument: async uri => documents.find(d => d.uri.fsPath === uri.fsPath) || {uri},
      getWorkspaceFolder: () => ({uri: file('/workspace')}),
      getConfiguration: namespace => (assert.equal(namespace, 'glitnir'), {get: (key, fallback) => ({pythonPath: '.venv/bin/python', memoryLimitMb: memoryInspect.globalValue ?? memoryInspect.defaultValue})[key] ?? fallback, inspect: key => (assert.equal(key, 'memoryLimitMb'), memoryInspect)}),
      onDidChangeTextDocument: fn => { events.change = fn; return {}; },
      onDidSaveTextDocument: fn => { events.save = fn; return {}; },
      onDidCloseTextDocument: () => ({}),
    },
    window: {
      showInformationMessage: async text => { infos.push(text); },
      showSaveDialog: async () => { dialogs++; return saveTarget; },
      showTextDocument: async document => { fake.window.shown = document; return editor; },
      createOutputChannel: () => ({append() {}}), showErrorMessage: text => { throw new Error(text); },
      createWebviewPanel: (kind, title) => {
        assert.equal(kind, 'glitnir.notebook');
        const panel = {
          title, messages: [],
          webview: {postMessage(message) { panel.messages.push(message); }, onDidReceiveMessage(fn) { panel.receive = fn; }},
          onDidDispose(fn) { panel.closed = fn; },
          dispose() { panel.closed(); }, reveal() { panel.revealed = true; },
        };
        panels.push(panel); return panel;
      },
    },
    commands: {registerCommand: (name, fn) => { commands.set(name, fn); return {}; }},
  };
  const load = Module._load;
  Module._load = function(name, parent, ...rest) {
    if (name === 'vscode') return fake;
    if (name === './backend' && parent.filename.endsWith('/dist/extension.js')) return {...load.call(this, name, parent, ...rest), Backend: FakeBackend};
    if (name === './webview' && parent.filename.endsWith('/dist/extension.js')) return {previewHtml: () => '<html></html>'};
    if (name === './git-history' && parent.filename.endsWith('/dist/extension.js')) return {GitHistory: class {
      constructor(file) { gitFiles.push(file); }
      async read() { return {state: 'no_repository', reason: 'Not in a Git repository.'}; }
      async diff(sha) { gitDiffs.push(sha); throw new Error('Select a commit from the current file history.'); }
    }};
    if (name === './extension-packages' && parent.filename.endsWith('/dist/extension.js')) return {ExtensionPackages: class {
      constructor(context, output, setup, restart) { packageRestart = restart; }
      async install(uri) { packageCalls.push(['install', uri]); }
      dispose() { packageCalls.push(['dispose']); }
    }};
    if (name === './setup' && parent.filename.endsWith('/dist/extension.js')) return {EnvironmentSetup: class {
      async resolve() { return {python: '/workspace/.venv/bin/python'}; }
      dispose() {}
    }};
    return load.call(this, name, parent, ...rest);
  };
  let extension;
  try { extension = require('../../dist/extension'); } finally { Module._load = load; }
  extension.activate({subscriptions: [], extensionUri: file(path.resolve(__dirname, '../..'))});
  const open = commands.get('glitnir.openPreview');
  const temporary = mkdtempSync(path.join(tmpdir(), 'glitnir-symlink-test-'));
  try {
    const alias = path.join(temporary, 'Alias.lean');
    symlinkSync(fixture, alias);
    await assert.rejects(open(file(alias)), /symbolic link/);
    assert.equal(servers.length, 0, 'a symlink cannot silently change the import context');
  } finally { rmSync(temporary, {recursive: true}); }
  await Promise.all([open(file(fixture)), open(file(fixture))]);
  assert.equal(panels.length, 1);
  assert.equal(gitFiles.length, 1);
  assert.equal(gitFiles[0], require('node:fs').realpathSync(fixture));
  await panels[0].receive({type: 'gitHistory', id: 401, path: '/tmp/injected.lean'});
  assert.equal(panels[0].messages.at(-1).json.state, 'no_repository');
  await panels[0].receive({type: 'gitDiff', id: 402, body: '--help', path: '/tmp/injected.lean'});
  assert.equal(panels[0].messages.at(-1).status, 409);
  assert.deepEqual(gitDiffs, ['--help']);
  fake.workspace.isTrusted = false;
  await panels[0].receive({type: 'gitHistory', id: 403});
  assert.equal(panels[0].messages.at(-1).status, 409);
  fake.workspace.isTrusted = true;
  assert.equal(fake.window.shown.uri.fsPath, fixture);
  assert.equal(servers[0].options.python, '/workspace/.venv/bin/python');
  assert.deepEqual(servers[0].options.args.slice(-4), ['serve', fixture, '--port', '0']);
  assert.ok(!('FORSETI_LEAN_PROJECT' in servers[0].options.env), 'no pinned Lean release is passed');
  assert.equal(servers[0].options.env.GLITNIR_MEMORY_LIMIT_MB, '12288', 'the memory setting reaches the backend');
  assert.equal(servers[0].options.env.GLITNIR_ELABORATION_TIMEOUT_MS, '600000');
  const panel = panels[0];
  await panel.receive({type: 'clipboard', id: 301, body: 'python -m replay --goal goal.json'});
  assert.equal(clipboard, 'python -m replay --goal goal.json');
  assert.equal(panel.messages.at(-1).status, 200);
  for (const body of [null, {}, 'x'.repeat(100001)]) {
    await panel.receive({type: 'clipboard', id: 302, body});
    assert.equal(panel.messages.at(-1).status, 400);
  }
  await panel.receive({type: 'external', href: 'https://example.com/a?x=1&y=2'});
  await panel.receive({type: 'external', href: 'mailto:help@example.com'});
  assert.deepEqual(externalUris, ['https://example.com/a?x=1&y=2', 'mailto:help@example.com']);
  for (const href of ['http://example.com', 'javascript:alert(1)', 'file:///tmp/a', 'https://example.com/space here', '', null, {}]) {
    await assert.rejects(panel.receive({type: 'external', href}), /Invalid external link/);
  }
  assert.equal(externalUris.length, 2, 'invalid schemes and malformed links never reach VS Code');
  assert.equal(servers[0].requests.length, 0, 'copying never executes or sends the command to the backend');
  documents[0].isDirty = true; events.change();
  assert.deepEqual(panel.messages.at(-1), {type: 'editorState', dirty: true});
  await panel.receive({type: 'request', id: 1, path: '/api/project/check', method: 'POST', body: '{"sha256":"' + hash + '"}'});
  assert.equal(panel.messages.at(-1).status, 409);
  assert.equal(servers[0].requests.length, 0);
  await panel.receive({type: 'request', id: 11, path: '/api/project/inspect', method: 'POST', body: '{}'});
  assert.equal(panel.messages.at(-1).status, 409);
  assert.equal(servers[0].requests.length, 0);
  for (const [id, route, body] of [[12, '/api/decide/run', '{"sha256":"' + hash + '","line":2}'], [13, '/api/decide/discard', '{"sha256":"' + hash + '","line":2}'], [14, '/api/evidence/run', '{"sha256":"' + hash + '","line":2}']]) {
    await panel.receive({type: 'request', id, path: route, method: 'POST', body});
    assert.equal(panel.messages.at(-1).status, 409, route);
  }
  assert.equal(servers[0].requests.length, 0, 'provider work waits for saved files');
  await panel.receive({type: 'request', id: 15, path: '/api/decide/stop', method: 'POST', body: '{}'});
  assert.equal(panel.messages.at(-1).status, 200, 'stopping a decision never waits for saved files');
  assert.deepEqual(servers[0].requests.splice(0), [['/api/decide/stop', 'POST']]);
  documents[0].isDirty = false; events.save(documents[0]);
  assert.equal(panel.messages.at(-1).type, 'sourceChanged');
  assert.equal(sourceWatchers[0].pattern.base, path.dirname(fixture), 'watch loose files outside the workspace');
  for (const event of ['Change', 'Create', 'Delete']) {
    const count = panel.messages.length;
    sourceWatchers[0][event](file(path.join(path.dirname(fixture), 'Unrelated.lean')));
    assert.equal(panel.messages.length, count);
    sourceWatchers[0][event](file(fixture));
    assert.equal(panel.messages.at(-1).type, 'sourceChanged', 'including atomic replacement and deletion');
  }
  await panel.receive({type: 'request', id: 2, path: '/api/project/check', method: 'POST', body: '{"sha256":"' + hash + '"}'});
  assert.equal(panel.messages.at(-1).status, 200);
  await panel.receive({type: 'request', id: 5, path: '/api/project/check', method: 'POST', body: '{"sha256":"' + hash + '","through_line":3}'});
  assert.equal(panel.messages.at(-1).status, 200, 'a check through one claim');
  for (const body of ['{}', '{"sha256":"' + hash + '","through_line":0}', '{"sha256":"' + hash + '","file":"Other.lean"}']) {
    await panel.receive({type: 'request', id: 6, path: '/api/project/check', method: 'POST', body});
    assert.equal(panel.messages.at(-1).status, 409, body);
  }
  await panel.receive({type: 'request', id: 3, path: '/api/cells/edit', method: 'POST', body: '{}'});
  assert.equal(panel.messages.at(-1).status, 409);
  fake.workspace.isTrusted = false;
  await panel.receive({type: 'request', id: 4, path: '/api/notebook'});
  assert.equal(panel.messages.at(-1).status, 409);
  fake.workspace.isTrusted = true;
  await panel.receive({type: 'export', id: 10, body: '<!doctype html><p>snapshot</p>'});
  assert.equal(dialogs, 1);
  assert.equal(writes.length, 0, 'cancelling never writes');
  assert.equal(panel.messages.at(-1).json.saved, false);
  saveTarget = file('/tmp/Notebook.html');
  await panel.receive({type: 'export', id: 11, body: '<!doctype html><p>snapshot</p>'});
  assert.equal(writes[0][0].fsPath, path.join(realpathSync(path.dirname(saveTarget.fsPath)), path.basename(saveTarget.fsPath)));
  assert.equal(writes[0][1], '<!doctype html><p>snapshot</p>');
  assert.equal(panel.messages.at(-1).json.saved, true);
  saveTarget = file(fixture);
  await panel.receive({type: 'export', id: 12, body: 'bad'});
  assert.equal(panel.messages.at(-1).status, 409);
  assert.equal(writes.length, 1, 'export cannot overwrite Lean source');
  await panel.receive({type: 'export', id: 13, body: {html: 'bad'}});
  assert.equal(panel.messages.at(-1).status, 409);
  fake.workspace.isTrusted = false;
  await panel.receive({type: 'export', id: 14, body: '<html></html>'});
  assert.equal(panel.messages.at(-1).status, 409);
  assert.equal(dialogs, 3, 'untrusted or malformed messages do not open a save dialog');
  fake.workspace.isTrusted = true;
  saveTarget = undefined;
  await panel.receive({type: 'exportTarget', id: 15, body: ['Neighbor']});
  assert.deepEqual(panel.messages.at(-1).json, {saved: false, siblings: []}, 'cancelling destination selection stops before serialization');
  const exportDirectory = mkdtempSync(path.join(tmpdir(), 'glitnir-export-test-'));
  try {
    writeFileSync(path.join(exportDirectory, 'Neighbor.html'), '<!doctype html><p>neighbor</p>');
    symlinkSync(path.join(exportDirectory, 'Neighbor.html'), path.join(exportDirectory, 'Alias.html'));
    saveTarget = file(path.join(exportDirectory, 'Notebook.html'));
    await panel.receive({type: 'exportTarget', id: 16, body: ['Neighbor', 'Missing', 'Alias']});
    const prepared = panel.messages.at(-1);
    assert.equal(prepared.status, 200);
    assert.equal(prepared.json.saved, true);
    assert.deepEqual(prepared.json.siblings, ['Neighbor'], 'only regular sibling exports are linked; symlinks are refused');
    assert.match(prepared.json.token, /^[a-f0-9-]+$/);
    await panel.receive({type: 'export', id: 17, body: '<!doctype html><p>prepared snapshot</p>', token: prepared.json.token});
    assert.equal(writes.at(-1)[0].fsPath, path.join(realpathSync(path.dirname(saveTarget.fsPath)), path.basename(saveTarget.fsPath)));
    assert.equal(writes.at(-1)[1], '<!doctype html><p>prepared snapshot</p>');
    assert.ok(temporaryWrites.at(-1).startsWith(path.join(realpathSync(exportDirectory), '.Notebook.html.')));
    assert.ok(temporaryWrites.at(-1).endsWith('.tmp'), 'exports are atomically renamed from an unpredictable sibling file');
    assert.equal(panel.messages.at(-1).json.saved, true);
    await panel.receive({type: 'export', id: 18, body: '<p>replay</p>', token: prepared.json.token});
    assert.equal(panel.messages.at(-1).status, 409, 'an export target capability is single-use');
  } finally { rmSync(exportDirectory, {recursive: true}); }
  await panel.receive({type: 'source', index: 0, sha256: hash, path: '/tmp/foreign.lean'});
  assert.equal(fake.window.shown.uri.fsPath, fixture, 'webview cannot choose another source file');
  assert.deepEqual(editor.selection.start, {line: 0, character: 0});
  assert.deepEqual(editor.selection.end, editor.selection.start, 'source navigation places the caret without leaving a whole theorem selected');
  assert.equal(editor.revealed.end.line, 1);
  assert.deepEqual(editor.revealed.start, editor.selection.start);
  await assert.rejects(panel.receive({type: 'source', index: 0, sha256: 'stale'}), /changed/);
  documents[0].isDirty = true;
  await assert.rejects(panel.receive({type: 'source', index: 0, sha256: hash}), /Save or revert/);
  documents[0].isDirty = false;
  // Accepting a decision: the preview names a key; only the served block is written.
  const key = 'd'.repeat(64);
  const served = {sha256: hash, insert_after_line: 1, block: 'theorem served : True := trivial'};
  const candidateRequests = () => servers[0].requests.filter(([url]) => url.startsWith('/api/decide/candidate')).length;
  candidateReply = {status: 200, json: served};
  for (const bad of [undefined, 'D'.repeat(64), 'd'.repeat(63), `${key}&key=${key}`, {key}]) {
    await assert.rejects(panel.receive({type: 'acceptDecision', key: bad}), /Invalid decision key/);
  }
  assert.equal(candidateRequests(), 0, 'a malformed key never reaches the backend');
  documents[0].isDirty = true;
  await assert.rejects(panel.receive({type: 'acceptDecision', key}), /Save the notebook before accepting/);
  documents[0].isDirty = false;
  assert.equal(candidateRequests(), 0, 'a dirty notebook is refused before asking the backend');
  candidateReply = {status: 409, json: {error: 'This decision is out of date; run it again.'}};
  await assert.rejects(panel.receive({type: 'acceptDecision', key}), /This decision is out of date; run it again\./);
  candidateReply = {status: 200, json: {...served, sha256: 'e'.repeat(64)}};
  await assert.rejects(panel.receive({type: 'acceptDecision', key}), /changed since this decision ran/);
  assert.deepEqual([edits, saves, infos.length], [[], [], 0], 'refusals insert and save nothing');
  candidateReply = {status: 200, json: served};
  await panel.receive({type: 'acceptDecision', key, block: 'theorem evil : False := sorry', path: '/tmp/foreign.lean', line: 0});
  assert.equal(servers[0].requests.at(-1)[0], `/api/decide/candidate?key=${key}`);
  assert.deepEqual(edits, [[fixture, 1, 0, 'theorem served : True := trivial\n']], 'only the served block, at the served line, in the preview\'s file');
  assert.deepEqual(saves, [fixture]);
  assert.match(infos.at(-1), /Decision accepted/);
  fake.workspace.applyEdit = async () => false;
  await assert.rejects(panel.receive({type: 'acceptDecision', key}), /did not insert/);
  fake.workspace.applyEdit = async edit => { edits.push(...edit.inserts); documents[0].version++; return true; };
  documents[0].save = async () => false;
  await assert.rejects(panel.receive({type: 'acceptDecision', key}), /not saved/);
  fake.window.activeTextEditor = editor;
  await commands.get('glitnir.revealCell')();
  assert.ok(!panel.messages.some(m => m.type === 'revealCell'), 'cold preview queues navigation until ready');
  await panel.receive({type: 'ready'});
  assert.deepEqual(panel.messages.at(-1), {type: 'revealCell', index: 0, sha256: hash, line: 1, character: 0});
  await commands.get('glitnir.revealCell')();
  assert.deepEqual(panel.messages.at(-1), {type: 'revealCell', index: 0, sha256: hash, line: 1, character: 0});
  servers[0].running = false;
  await open(file(fixture));
  assert.equal(panels.length, 2, 'a dead backend is replaced when reopened');
  await assert.rejects(panels[1].receive({type: 'reference', module: 'DissipativeOpen'}), /no longer available/);
  const beforeLinked = panels.length;
  await panels[1].receive({type: 'linked', module: 'DissipativeOpen'});
  assert.equal(panels.length, beforeLinked, 'pinned notebook links are not followed');
  references = [{module: 'DissipativeOpen', file: 'DissipativeOpen.lean'}, {module: '../Escape'}];
  await assert.rejects(panels[1].receive({type: 'reference', module: '../Escape'}), /no longer available/);
  await panels[1].receive({type: 'reference', module: 'DissipativeOpen'});
  await panels[1].receive({type: 'reference', module: 'DissipativeOpen'});
  assert.equal(panels[2].revealed, true, 'an existing reference preview is reused');
  assert.equal(panels.length, 3);
  panels[1].dispose();
  assert.equal(servers[0].closed, true);
  assert.equal(sourceWatchers[0].disposed, true);
  assert.equal(servers[2].closed, undefined);
  const affects = keys => ({affectsConfiguration: key => keys.includes(key)});
  events.configuration(affects(['glitnir.leanProject']));
  assert.equal(servers[2].closed, undefined, 'removed settings no longer close previews');
  events.configuration(affects(['glitnir.pythonPath']));
  assert.equal(servers[2].closed, true, 'changing the Python setting closes previews');
  await commands.get('glitnir.installExtensionPackage')();
  assert.equal(commands.get('glitnir.removeExtensionPackage'), undefined, 'Glitnir does not remove packages');
  assert.deepEqual(packageCalls.map(call => call[0]), ['install']);
  await open(file(fixture));
  assert.equal(servers.length, 4);
  assert.equal(packageRestart('/other/bin/python'), false, 'previews of another backend stay open');
  assert.equal(servers[3].closed, undefined);
  assert.equal(packageRestart('/workspace/.venv/bin/python'), true);
  assert.equal(servers[3].closed, true, 'changing extension packages closes the previews of that backend');
  memoryInspect = {defaultValue: 16384};
  await open(file(fixture));
  assert.equal(servers.length, 5);
  assert.ok(!('GLITNIR_MEMORY_LIMIT_MB' in servers[4].options.env), 'a defaulted memory setting leaves the backend to size its own');
  let resume;
  fake.workspace.openTextDocument = uri => new Promise(resolve => { resume = () => resolve({uri}); });
  const opening = open(file(path.join(path.dirname(fixture), 'DissipativeModel.lean')));
  const deadline = Date.now() + 3000;
  while (!resume) {
    assert.ok(Date.now() < deadline, 'pending open reached the editor');
    await new Promise(resolve => setImmediate(resolve));
  }
  await extension.deactivate();
  resume();
  await opening;
  assert.equal(servers[2].closed, true);
  assert.equal(servers.length, 5, 'deactivation prevents a pending open from spawning');
  assert.deepEqual(packageCalls.at(-1), ['dispose'], 'deactivation cancels package changes');
});
