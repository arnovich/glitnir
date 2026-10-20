const assert = require('node:assert/strict');
const {test} = require('node:test');
const Module = require('node:module');

test('setup remembers validated choices, respects cancellation and rejects untrusted execution', async () => {
  const store = new Map(), updates = [], probes = [], dialogs = [], choices = [], opened = [];
  const file = fsPath => ({fsPath, scheme: 'file', toString: () => `file://${fsPath}`});
  const fake = {
    Uri: {joinPath: (uri, ...parts) => file(require('node:path').join(uri.fsPath, ...parts))},
    commands: {executeCommand: async (...args) => opened.push(args)},
    ConfigurationTarget: {WorkspaceFolder: 3},
    extensions: {getExtension: () => undefined},
    workspace: {
      isTrusted: true, workspaceFolders: [{uri: file('/work')}],
      getWorkspaceFolder: () => ({uri: file('/work')}),
      getConfiguration: namespace => (assert.equal(namespace, 'glitnir'), {get: () => '', update: async (...args) => updates.push(args)}),
    },
    window: {
      showOpenDialog: async () => dialogs.shift(),
      // Without a queued choice: More Options… → Choose Existing Environment.
      showQuickPick: async items => choices.length ? choices.shift() : items.find(i => i.action === 'more') ?? items.find(i => i.source === 'existing'),
      showErrorMessage: async () => 'Set Up Glitnir',
      showInformationMessage: async () => {},
    },
  };
  const original = Module._load;
  Module._load = function(name, ...args) { return name === 'vscode' ? fake : original.call(this, name, ...args); };
  let EnvironmentSetup;
  try { ({EnvironmentSetup} = require('../../dist/setup')); } finally { Module._load = original; }
  const setup = new EnvironmentSetup({extensionUri: file('/extension'), workspaceState: {get: key => store.get(key), update: async (key, value) => store.set(key, value)}}, {append() {}, appendLine() {}});
  setup.probe = async (...args) => {
    probes.push(args);
    assert.equal(args.length, 1, 'setup probes only Python');
    if (args[0] === '/wrong') throw new Error('backend is incompatible');
    return {python: args[0]};
  };
  const uri = file('/work/A.lean');
  fake.extensions.getExtension = () => ({activate: async () => ({environments: {
    getActiveEnvironmentPath: resource => { assert.equal(resource, uri); return {path: '/selected-env'}; },
    resolveEnvironment: async () => ({executable: {uri: file('/selected-env/bin/python')}}),
  }})});
  assert.deepEqual(await setup.resolve(uri), {python: '/selected-env/bin/python'});
  assert.equal(store.size, 0, 'automatic discovery does not pin a changing interpreter');
  fake.extensions.getExtension = () => undefined;
  dialogs.push(undefined);
  assert.equal(await setup.resolve(uri, true), undefined);
  assert.equal(store.size, 0, 'cancelling manual selection stores no environment');
  dialogs.push([file('/wrong')]);
  await assert.rejects(setup.resolve(uri, true), /incompatible/);
  assert.equal(store.size, 0);
  dialogs.push([file('/python')]);
  assert.deepEqual(await setup.resolve(uri, true), {python: '/python'});
  assert.equal(store.size, 1);
  assert.equal(dialogs.length, 0, 'setup asks for Python only, never a Lean project');
  probes.length = 0;
  assert.equal((await setup.resolve(uri)).python, '/python');
  assert.deepEqual(probes[0], ['/python'], 'remembered choices are revalidated');
  assert.equal(updates.length, 0, 'automatic choices do not write settings');
  store.clear();
  choices.push(undefined);
  assert.equal(await setup.resolve(uri, true), undefined);
  assert.equal(store.size, 0, 'cancelling the detected-environment choice stores nothing');
  choices.push({action: 'detected'});
  const detected = await setup.resolve(uri, true);
  assert.equal(detected.python, '/work/.venv/bin/python');
  assert.equal(store.size, 1, 'explicitly accepting detection remembers the verified choice');
  assert.equal(dialogs.length, 0);
  fake.workspace.getWorkspaceFolder = () => undefined;
  fake.workspace.getConfiguration = namespace => (assert.equal(namespace, 'glitnir'), {get: key => key === 'pythonPath' ? '/global/python' : '', update: async (...args) => updates.push(args)});
  choices.push({action: 'detected'});
  assert.equal((await setup.resolve(uri, true)).python, '/global/python');
  assert.equal(updates.length, 0, 'accepting an existing global override needs no workspace or settings rewrite');
  dialogs.push([file('/replacement/python')]);
  await assert.rejects(setup.resolve(uri, true), /workspace folder/);
  fake.workspace.getWorkspaceFolder = () => ({uri: file('/work')});
  dialogs.push([file('/replacement/python')]);
  await setup.resolve(uri, true);
  assert.deepEqual(updates, [['pythonPath', '/replacement/python', 3]]);
  fake.workspace.getConfiguration = namespace => (assert.equal(namespace, 'glitnir'), {get: () => '', update: async (...args) => updates.push(args)});
  setup.probe = async () => { throw new Error('Glitnir is missing'); };
  fake.window.showInformationMessage = async (message, options, ...actions) => {
    assert.equal(options.modal, true);
    assert.match(options.detail, /installed Python backend/);
    assert.deepEqual(actions, ['Choose Existing Environment', 'More Options…'], 'the guide moved into More Options…');
    return 'More Options…';
  };
  choices.push({source: 'guide'});
  const storedBeforeGuide = store.size;
  assert.equal(await setup.resolve(uri), undefined);
  assert.equal(opened[0][0], 'markdown.showPreview');
  assert.equal(opened[0][1].fsPath, '/extension/dist/media/setup.md');
  assert.equal(store.size, storedBeforeGuide);
  fake.window.showInformationMessage = async () => undefined;
  assert.equal(await setup.resolve(uri), undefined);
  assert.equal(dialogs.length, 0, 'dismissing setup opens no file picker');
  assert.equal(store.size, storedBeforeGuide);
  fake.window.showInformationMessage = async () => 'Choose Existing Environment';
  setup.probe = async python => {
    if (python !== '/manual/python') throw new Error('missing');
    return {python};
  };
  dialogs.push([file('/manual/python')]);
  assert.deepEqual(await setup.resolve(uri), {python: '/manual/python'});
  assert.equal(store.get('environment:file:///work').python, '/manual/python');
  assert.equal(dialogs.length, 0);
  fake.workspace.isTrusted = false;
  await assert.rejects(setup.resolve(uri, true), /Trust/);
  fake.workspace.isTrusted = true;
  setup.dispose();
  await assert.rejects(setup.resolve(uri), /cancelled/);
});

test('the title button has themed icons and a setup command', () => {
  const manifest = require('../../package.json');
  assert.equal(`${manifest.publisher}.${manifest.name}`, 'gimle.glitnir');
  assert.ok(Object.keys(manifest.contributes.configuration.properties).every(key => key.startsWith('glitnir.')));
  const command = manifest.contributes.commands.find(c => c.command === 'glitnir.openPreview');
  assert.equal(command.title, 'Open in Glitnir');
  const fs = require('node:fs'), path = require('node:path');
  for (const icon of Object.values(command.icon)) assert.ok(fs.existsSync(path.join(__dirname, '../..', icon)));
  assert.ok(manifest.contributes.menus['editor/title'][0].group.startsWith('navigation'));
  assert.equal(manifest.contributes.menus['editor/title'][0].command, 'glitnir.revealCell');
  const revealKey = manifest.contributes.keybindings.find(k => k.command === 'glitnir.revealCell');
  assert.equal(revealKey.mac, 'cmd+k g');
  assert.equal(revealKey.key, 'ctrl+k g');
  assert.match(revealKey.when, /editorTextFocus/);
  assert.ok(manifest.contributes.commands.some(c => c.command === 'glitnir.setup'));
  assert.equal(manifest.contributes.configuration.properties['glitnir.pythonPath'].default, '');
  const memory = manifest.contributes.configuration.properties['glitnir.memoryLimitMb'];
  assert.deepEqual([memory.type, memory.default, memory.minimum, memory.maximum, memory.scope], ['integer', 16384, 1024, 32768, 'resource']);
  for (const removed of ['glitnir.leanProject']) assert.ok(!(removed in manifest.contributes.configuration.properties), `${removed} is not a setting`);
});

test('setup offers extension packages and quiet discovery never prompts', async () => {
  const commands = [], prompts = [];
  const file = fsPath => ({fsPath, scheme: 'file', toString: () => `file://${fsPath}`});
  const fake = {
    Uri: {joinPath: (uri, ...parts) => file(require('node:path').join(uri.fsPath, ...parts))},
    commands: {executeCommand: async (...args) => { commands.push(args); }},
    extensions: {getExtension: () => undefined},
    workspace: {isTrusted: true, workspaceFolders: [{uri: file('/work')}], getWorkspaceFolder: () => ({uri: file('/work')}), getConfiguration: () => ({get: () => ''})},
    window: {
      showQuickPick: async items => {
        const item = items.find(i => i.action === 'package');
        assert.equal(item.label, 'Install Extension Package…');
        return item;
      },
      showInformationMessage: async (...args) => { prompts.push(args); },
    },
  };
  const original = Module._load;
  Module._load = function(name, ...args) { return name === 'vscode' ? fake : original.call(this, name, ...args); };
  let EnvironmentSetup;
  try {
    delete require.cache[require.resolve('../../dist/setup')];
    ({EnvironmentSetup} = require('../../dist/setup'));
  } finally { Module._load = original; }
  const store = new Map();
  const setup = new EnvironmentSetup({extensionUri: file('/extension'), workspaceState: {get: key => store.get(key), update: async (k, v) => store.set(k, v)}}, {append() {}, appendLine() {}});
  const uri = file('/work/A.lean');
  setup.probe = async python => { if (python !== '/work/.venv/bin/python') throw new Error('missing'); return {python}; };
  assert.equal(await setup.resolve(uri, true), undefined);
  assert.deepEqual(commands, [['glitnir.installExtensionPackage', uri]]);
  assert.equal(store.size, 0, 'choosing the package installer remembers nothing');
  assert.deepEqual(await setup.discovered(uri), {python: '/work/.venv/bin/python'});
  setup.probe = async () => { throw new Error('missing'); };
  assert.equal(await setup.discovered(uri), undefined);
  assert.deepEqual(prompts, [], 'quiet discovery never offers setup itself');
  fake.workspace.isTrusted = false;
  await assert.rejects(setup.discovered(uri), /Trust/);
});
