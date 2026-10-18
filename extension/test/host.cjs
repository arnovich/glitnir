const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');

// Save a checked candidate as a decision run would. The host has no Lean, so
// the checker's verdict is written here; everything after it is real.
const SAVE_CANDIDATE = `
import sys
from datetime import UTC, datetime
from pathlib import Path
from glitnir.lean.fingerprint import environment_fingerprint
from glitnir.notebook.check_records import (
    decision_prefix_sha256, load_records, save_records, with_decision)
from glitnir.notebook.decision_cards import checked_decision
path, key, end, block = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
text = path.read_text()
outcome = checked_decision({"status": "proven", "block": block, "theorem": "four"})
entry = {
    "prefix_sha256": decision_prefix_sha256(text, end),
    "fingerprint": environment_fingerprint(path, text)["sha256"],
    "provider": "demo",
    "created": datetime.now(UTC).isoformat(timespec="seconds"),
    "outcome": outcome,
    "verification": {"status": "checked", "message": "demo", "axioms": [], "type": "Eq (instHAdd.hAdd 2 2) 4"},
}
save_records(path, with_decision(load_records(path), key, entry, keep=[key], notebook=path))
`;

/** Accept a saved demo candidate through the editor: the block is inserted and saved. */
async function acceptCandidate(extension, root) {
  const notebook = vscode.Uri.joinPath(root, 'Decide.lean');
  const text = 'def c : Nat := 1\n-- @decide four : sum\n';
  const block = 'theorem four : 2 + 2 = 4 := by decide';
  await vscode.workspace.fs.writeFile(notebook, Buffer.from(text));
  const python = process.env.GLITNIR_HOST_PYTHON;
  const dist = path.join(extension.extensionPath, 'dist');
  const {Backend} = require(path.join(dist, 'backend.js'));
  const {acceptDecision} = require(path.join(dist, 'accept-decision.js'));
  const backend = new Backend({python, cwd: root.fsPath, env: process.env,
    args: ['-I', '-B', '-u', '-m', 'glitnir.notebook', 'serve', notebook.fsPath, '--port', '0']});
  try {
    await backend.ready;
    const card = async () => (await backend.request('/api/notebook')).json.decisions[0];
    const {key, end_line: end} = await card();
    const saved = spawnSync(python, ['-I', '-c', SAVE_CANDIDATE, notebook.fsPath, key, String(end), block], {encoding: 'utf8', timeout: 60000});
    assert.equal(saved.status, 0, saved.stdout + saved.stderr);
    assert.equal((await card()).status, 'candidate');
    const document = await vscode.workspace.openTextDocument(notebook);
    await acceptDecision(vscode, document, url => backend.request(url), key);
    assert.equal(document.isDirty, false, 'accepting saves the notebook');
    assert.equal(readFileSync(notebook.fsPath, 'utf8'), text + block + '\n');
    assert.equal((await card()).status, 'accepted');
    await assert.rejects(acceptDecision(vscode, document, url => backend.request(url), key), /already in the notebook/);
  } finally { await backend.close(); }
}

/** Read another notebook of the project through the real host transport, and nothing outside it. */
async function readLinkedNotebook(extension, root) {
  const python = process.env.GLITNIR_HOST_PYTHON;
  const {Backend} = require(path.join(extension.extensionPath, 'dist', 'backend.js'));
  const backend = new Backend({python, cwd: root.fsPath, env: process.env,
    args: ['-I', '-B', '-u', '-m', 'glitnir.notebook', 'serve', vscode.Uri.joinPath(root, 'First.lean').fsPath, '--port', '0']});
  try {
    await backend.ready;
    const linked = await backend.request('/api/linked?path=Second.lean');
    assert.equal(linked.status, 200);
    assert.equal(linked.json.file, 'Second.lean');
    assert.deepEqual(linked.json.preview.linked, {path: 'Second.lean', file: 'Second.lean', compared: true});
    for (const self of ['First.lean', 'FIRST.lean', 'second.lean']) assert.equal((await backend.request(`/api/linked?path=${self}`)).status, 404, self);
    assert.equal(linked.json.preview.check, null, 'nothing is checked for a linked notebook');
    assert.equal((await backend.request('/api/linked?path=..%2FFirst.lean')).status, 400);
    assert.equal((await backend.request('/api/linked?path=Missing.lean')).status, 404);
    await assert.rejects(backend.request('/api/linked?path=Second.lean&notebook=First'), /Invalid preview query/);
    await assert.rejects(backend.request('/api/linked?path=Second.lean', 'POST', '{}'), /unavailable/);
  } finally { await backend.close(); }
}

/**
 * Ask the real backend for a check through one line. The digest names no saved file, so the check stops before any
 * Lean runs, and reports the request back; the host transport refuses any other body.
 */
async function checkThroughLine(extension, root) {
  const python = process.env.GLITNIR_HOST_PYTHON;
  const {Backend} = require(path.join(extension.extensionPath, 'dist', 'backend.js'));
  const backend = new Backend({python, cwd: root.fsPath, env: process.env,
    args: ['-I', '-B', '-u', '-m', 'glitnir.notebook', 'serve', vscode.Uri.joinPath(root, 'First.lean').fsPath, '--port', '0']});
  try {
    await backend.ready;
    const sha256 = '0'.repeat(64);
    const queued = await backend.request('/api/project/check', 'POST', JSON.stringify({sha256, through_line: 1}));
    assert.equal(queued.status, 202);
    let check;
    for (let i = 0; i < 100; i++) {
      check = (await backend.request('/api/notebook')).json.preview.check;
      if (check.status !== 'running') break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(check.status, 'unavailable');
    assert.equal(check.through_line, 1);
    assert.match(check.message, /Saved source changed/);
    for (const body of [{sha256, through_line: 0}, {sha256, through_line: '1'}, {sha256, through_line: 1, file: 'Second.lean'}]) {
      await assert.rejects(backend.request('/api/project/check', 'POST', JSON.stringify(body)), /Invalid request/, JSON.stringify(body));
    }
  } finally { await backend.close(); }
}

/**
 * Install uv's terminal gets exactly the environment Glitnir builds: a
 * workspace `terminal.integrated.env.*` setting and the editor's own
 * environment (its PATH, HOMEBREW_* variables) never reach it.
 */
async function installerEnvironment(extension, root) {
  const {uvInstaller, uvInstallerTerminal, INSTALLER_PATH} = require(path.join(extension.extensionPath, 'dist', 'backend-install.js'));
  const platform = process.platform === 'darwin' ? 'osx' : 'linux';
  const settings = vscode.workspace.getConfiguration('terminal.integrated.env');
  await settings.update(platform, {GLITNIR_INJECTED: 'from settings', PATH: '/injected'}, vscode.ConfigurationTarget.Workspace);
  const out = path.join(root.fsPath, 'installer-env.txt');
  const installer = uvInstaller();
  const terminal = vscode.window.createTerminal(uvInstallerTerminal({...installer, command: `/usr/bin/env > '${out}.tmp' && /bin/mv '${out}.tmp' '${out}'`}));
  try {
    const deadline = Date.now() + 30000;
    while (!require('node:fs').existsSync(out) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
    const env = Object.fromEntries(readFileSync(out, 'utf8').trim().split('\n').map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]));
    assert.equal(env.GLITNIR_INJECTED, undefined, 'terminal.integrated.env does not apply under strictEnv');
    assert.equal(env.PATH, INSTALLER_PATH, 'only the minimal PATH');
    assert.equal(env.UV_NO_MODIFY_PATH, '1');
    assert.equal(env.HOME, installer.env.HOME);
    assert.equal(process.env.HOMEBREW_GLITNIR_PARENT, 'inherited', 'the editor itself has a HOMEBREW_ variable');
    assert.ok(!Object.keys(env).some(name => name.startsWith('HOMEBREW_') || name.startsWith('GLITNIR_') || name === 'ELECTRON_RUN_AS_NODE'), Object.keys(env).join(' '));
    return Object.keys(env).sort();
  } finally {
    terminal.dispose();
    await settings.update(platform, undefined, vscode.ConfigurationTarget.Workspace);
  }
}

async function run() {
  const extension = vscode.extensions.getExtension('gimle.glitnir');
  assert.ok(extension);
  await extension.activate();
  assert.equal(vscode.workspace.getConfiguration('glitnir', vscode.workspace.workspaceFolders[0].uri).get('pythonPath'), '', 'host test uses automatic discovery');
  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes('glitnir.setup'));
  assert.ok(commands.includes('glitnir.setupAgent'));
  assert.ok(commands.includes('glitnir.installExtensionPackage'));
  assert.ok(commands.includes('glitnir.setupBackendForThisVersion'));
  assert.equal(vscode.workspace.getConfiguration('glitnir').get('offerBackendUpdate'), true, 'the update offer is on by default');
  assert.ok(!commands.includes('glitnir.removeExtensionPackage'), 'removal is manual');
  assert.ok(commands.includes('glitnir.revealCell'));
  const root = vscode.workspace.workspaceFolders[0].uri;
  const first = vscode.Uri.joinPath(root, 'First.lean');
  const second = vscode.Uri.joinPath(root, 'Second.lean');
  await vscode.commands.executeCommand('glitnir.openPreview', first);
  assert.ok(vscode.window.visibleTextEditors.some(editor => editor.document.uri.toString() === first.toString()), 'opening from Explorer shows the first source');
  await vscode.commands.executeCommand('glitnir.openPreview', second);
  assert.ok(vscode.window.visibleTextEditors.some(editor => editor.document.uri.toString() === second.toString()), 'opening another notebook shows its matching source');
  await vscode.commands.executeCommand('glitnir.openPreview', first);
  const previews = () => vscode.window.tabGroups.all.flatMap(group => group.tabs).filter(tab => tab.label.startsWith('Glitnir:'));
  assert.equal(previews().length, 2, 'two notebooks, no duplicate first preview');
  const document = await vscode.workspace.openTextDocument(first);
  const editor = await vscode.window.showTextDocument(document, vscode.ViewColumn.One);
  editor.selection = new vscode.Selection(2, 0, 2, 0);
  await vscode.commands.executeCommand('glitnir.revealCell');
  assert.equal(previews().length, 2, 'reverse navigation reuses the matching preview');
  const edit = new vscode.WorkspaceEdit();
  edit.insert(first, new vscode.Position(0, 0), '/- unsaved note -/\n');
  await vscode.workspace.applyEdit(edit);
  assert.ok(document.isDirty);
  await document.save();
  assert.equal(document.isDirty, false);
  await vscode.window.tabGroups.close(previews()[0]);
  assert.equal(previews().length, 1);
  await vscode.window.tabGroups.close(previews()[0]);
  assert.equal(previews().length, 0);
  await vscode.commands.executeCommand('glitnir.openPreview', first);
  const metadata = vscode.Uri.joinPath(root, 'First.forseti.json');
  await vscode.workspace.fs.writeFile(metadata, Buffer.from('{}'));
  await new Promise(resolve => setTimeout(resolve, 1000));
  assert.equal(previews().length, 1, 'a legacy sidecar does not change or close the preview');
  await vscode.workspace.fs.delete(metadata);
  const config = vscode.workspace.getConfiguration('glitnir', first);
  const python = config.get('pythonPath');
  await config.update('pythonPath', '/changed/python', vscode.ConfigurationTarget.WorkspaceFolder);
  const deadline = Date.now() + 5000;
  while (previews().length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(previews().length, 0, 'Python changes close the old preview');
  await config.update('pythonPath', python || undefined, vscode.ConfigurationTarget.WorkspaceFolder);
  await acceptCandidate(extension, root);
  await readLinkedNotebook(extension, root);
  await checkThroughLine(extension, root);
  console.log(`Install uv terminal environment: ${(await installerEnvironment(extension, root)).join(' ')}`);
  console.log('VS Code host: two previews, reverse navigation, edit/save, disposal, accepting a decision, reading a linked notebook and a check through one line passed.');
  if (process.env.GLITNIR_HOST_SUCCESS) require('node:fs').writeFileSync(process.env.GLITNIR_HOST_SUCCESS, 'passed');
}

exports.run = async function() {
  try { await run(); }
  catch (error) {
    if (process.env.GLITNIR_HOST_RESULT) require('node:fs').writeFileSync(process.env.GLITNIR_HOST_RESULT, String(error.stack || error));
    throw error;
  }
};
