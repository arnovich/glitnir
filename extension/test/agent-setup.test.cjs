const assert = require('node:assert/strict');
const {test} = require('node:test');
const Module = require('node:module');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');

test('agent setup previews destinations, respects trust and cancellation, and installs the selected workspace', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-agent-ui-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const file = fsPath => ({fsPath, scheme: 'file'});
  const extension = path.resolve(__dirname, '../..');
  const choices = [], prompts = [], opened = [], messages = [];
  const first = path.join(root, 'first'), second = path.join(root, 'second');
  await fs.mkdir(first); await fs.mkdir(second);
  const fake = {
    Uri: {file, joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts))},
    env: {},
    workspace: {isTrusted: true, workspaceFolders: [{name: 'First', uri: file(first)}]},
    window: {
      showQuickPick: async (items, options) => { prompts.push([items, options]); return choices.shift()?.(items); },
      showOpenDialog: async () => undefined,
      showInformationMessage: async text => { messages.push(text); return 'Open Instructions'; },
      showTextDocument: async uri => opened.push(uri.fsPath),
    },
  };
  const original = Module._load;
  Module._load = function(name, ...args) { return name === 'vscode' ? fake : original.call(this, name, ...args); };
  let setupAgent;
  try { ({setupAgent} = require('../../dist/agent-setup')); } finally { Module._load = original; }
  const context = {extensionUri: file(extension), extension: {packageJSON: {version: '0.8.3'}}};
  fake.workspace.isTrusted = false;
  await assert.rejects(setupAgent(context), /trusted local/);
  assert.equal(prompts.length, 0);
  fake.workspace.isTrusted = true;
  fake.env.remoteName = 'ssh';
  await assert.rejects(setupAgent(context), /trusted local/);
  fake.env.remoteName = undefined;
  await setupAgent(context);
  assert.deepEqual(await fs.readdir(first), [], 'cancelling agent selection creates nothing');
  assert.equal(prompts[0][0][0].description, path.join(first, '.agents/skills/glitnir-notebooks'));
  fake.workspace.workspaceFolders.push({name: 'Second', uri: file(second)});
  choices.push(items => items[1], items => items.find(item => item.label === 'Claude Code'));
  await setupAgent(context);
  const destination = path.join(second, '.claude/skills/glitnir-notebooks/SKILL.md');
  assert.match(await fs.readFile(destination, 'utf8'), /name: glitnir-notebooks/);
  assert.deepEqual(opened, [destination]);
  assert.deepEqual(await fs.readdir(first), []);
  assert.match(messages[0], /new agent session/);
  choices.push(items => items[0], items => { fake.workspace.isTrusted = false; return items[0]; });
  await assert.rejects(setupAgent(context), /cancelled/);
  assert.deepEqual(await fs.readdir(first), [], 'revoked trust prevents installation');
  fake.workspace.isTrusted = true;
  fake.window.showOpenDialog = async () => [file(first)];
  choices.push(items => items[0], items => items.find(item => item.parent === ''));
  await setupAgent(context);
  assert.match(messages.at(-1), /Give .*SKILL.md to your agent/);
  assert.equal(opened.at(-1), path.join(first, 'glitnir-notebooks/SKILL.md'));
});
