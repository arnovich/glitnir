import * as path from 'node:path';
import {lstat, realpath} from 'node:fs/promises';
import * as vscode from 'vscode';
import {Backend, heldWhileDirty} from './backend';
import {previewHtml} from './webview';
import {EnvironmentSetup, openSetupGuide} from './setup';
import {ExtensionPackages} from './extension-packages';
import {BackendUpgrade} from './backend-upgrade';
import {setupAgent} from './agent-setup';
import {sourceCell, sourceTactic, cellAtLine} from './navigation';
import {validationCommand} from './validation-command';
import {GitHistory} from './git-history';
import {acceptDecision} from './accept-decision';

interface Preview {
  panel: vscode.WebviewPanel;
  backend: Backend;
  uri: vscode.Uri;
  /** The backend's Python, so changing its packages closes exactly its previews. */
  python: string;
  ready: boolean;
  accepting: boolean;
  pendingReveal?: {type: string; index: number; sha256: string; line?: number; character?: number};
}

const previews = new Map<string, Preview>();
const stopping = new Set<Promise<void>>();
let shuttingDown = false;
let setup: EnvironmentSetup | undefined;
let packages: ExtensionPackages | undefined;
let upgrade: BackendUpgrade | undefined;

/** Register a source-preserving preview; the Python checker owns every status. */
export function activate(context: vscode.ExtensionContext): void {
  shuttingDown = false;
  const output = vscode.window.createOutputChannel('Glitnir');
  setup = new EnvironmentSetup(context, output);
  context.subscriptions.push(output);
  let environmentEpoch = 0;
  /** Like a `glitnir.pythonPath` change: previews served by `python` close so reopening loads its providers again. */
  function closePreviewsUsing(python: string): boolean {
    environmentEpoch++;
    let closed = false;
    for (const preview of [...previews.values()]) {
      if (preview.python === python) { preview.panel.dispose(); closed = true; }
    }
    return closed;
  }
  packages = new ExtensionPackages(context, output, setup, closePreviewsUsing);
  upgrade = new BackendUpgrade(context, output, setup, packages);
  setup.upgrade = upgrade;
  // After an extension update, offer this build's backend once; nothing downloads without the click.
  void upgrade.offer().catch(error => output.appendLine(`Glitnir backend update check failed: ${error}`));
  const media = vscode.Uri.joinPath(context.extensionUri, 'dist', 'media');

  function dirty(): boolean {
    // Include imported files: the backend always reads their saved versions too.
    return vscode.workspace.textDocuments.some(d => d.isDirty && d.uri.path.endsWith('.lean'));
  }
  function updateState(): void {
    for (const preview of previews.values()) {
      void preview.panel.webview.postMessage({type: 'editorState', dirty: dirty()});
    }
  }
  function stop(preview: Preview): void {
    const promise = preview.backend.close();
    stopping.add(promise);
    void promise.finally(() => stopping.delete(promise));
  }

  async function openPreview(requested?: vscode.Uri): Promise<void> {
    if (shuttingDown) return;
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before starting Glitnir.');
    if (vscode.env.remoteName) throw new Error('This first preview supports local VS Code windows. Open the project locally.');
    if (process.platform === 'win32') throw new Error('The Glitnir checker currently supports Linux and macOS.');
    const selected = requested ?? vscode.window.activeTextEditor?.document.uri;
    if (!selected || selected.scheme !== 'file' || !selected.path.endsWith('.lean')) throw new Error('Open a saved .lean file first.');
    if ((await lstat(selected.fsPath)).isSymbolicLink()) throw new Error('Open the original Lean file instead of a symbolic link, so imports and metadata have one location.');
    const uri = selected;
    const key = await realpath(uri.fsPath);
    if (shuttingDown) return;
    const existing = previews.get(key);
    if (existing && !existing.backend.running) existing.panel.dispose();
    const document = await vscode.workspace.openTextDocument(uri);
    if (shuttingDown) return;
    await vscode.window.showTextDocument(document, {viewColumn: vscode.ViewColumn.One, preserveFocus: true});
    if (shuttingDown) return;
    // Opening the document yields; another command may already have opened it.
    if (previews.has(key)) { previews.get(key)!.panel.reveal(vscode.ViewColumn.Beside); return; }
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before starting Glitnir.');
    const folder = vscode.workspace.getWorkspaceFolder(uri);
    const cwd = folder?.uri.fsPath ?? path.dirname(uri.fsPath);
    const config = vscode.workspace.getConfiguration('glitnir', uri);
    const memorySetting = config.inspect<number>('memoryLimitMb');
    const memoryLimitChosen = memorySetting?.globalValue !== undefined
      || memorySetting?.workspaceValue !== undefined || memorySetting?.workspaceFolderValue !== undefined;
    const epoch = environmentEpoch;
    const environment = await setup!.resolve(uri);
    if (!environment || shuttingDown) return;
    if (epoch !== environmentEpoch) throw new Error('Glitnir settings changed during setup. Open the preview again.');
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before starting Glitnir.');
    if (previews.has(key)) { previews.get(key)!.panel.reveal(vscode.ViewColumn.Beside); return; }
    const backend = new Backend({
      python: environment.python, cwd, args: ['-I', '-B', '-u', '-m', 'glitnir.notebook', 'serve', uri.fsPath, '--port', '0'],
      env: {...(environment.processEnv ?? process.env), PYTHONUNBUFFERED: '1',
        GLITNIR_ELABORATION_TIMEOUT_MS: String(config.get<number>('elaborationTimeoutMs', 600000)),
        // Left unset unless the user chose a value: the backend then sizes the default to the machine.
        ...(memoryLimitChosen ? {GLITNIR_MEMORY_LIMIT_MB: String(config.get<number>('memoryLimitMb', 16384))} : {})},
      // Project cancellation stops Lean first, then removes its large snapshot.
      shutdownTimeoutMs: 60000,
      log: text => output.append(`[${path.basename(uri.fsPath)}] ${text}`),
    });
    let panel: vscode.WebviewPanel;
    try {
      panel = vscode.window.createWebviewPanel('glitnir.notebook', `Glitnir: ${path.basename(uri.fsPath)}`, vscode.ViewColumn.Beside, {
        enableScripts: true, localResourceRoots: [media], retainContextWhenHidden: true,
      });
    } catch (error) { await backend.close(); throw error; }
    const preview: Preview = {panel, backend, uri, python: environment.python, ready: false, accepting: false};
    previews.set(key, preview);
    const gitHistory = new GitHistory(key);
    // Watch the parent so atomic replacements and loose files outside the
    // workspace are included. Match the filename literally, not as a glob.
    const source = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(path.dirname(uri.fsPath), '*'));
    const sourceChanged = (changed: vscode.Uri) => {
      if (changed.fsPath !== uri.fsPath) return;
      void panel.webview.postMessage({type: 'environmentChanged'});
      void panel.webview.postMessage({type: 'sourceChanged'});
    };
    const sourceEvents = [source.onDidChange(sourceChanged), source.onDidCreate(sourceChanged), source.onDidDelete(sourceChanged)];
    panel.onDidDispose(() => {
      source.dispose(); sourceEvents.forEach(event => event.dispose());
      previews.delete(key); stop(preview);
    });
    panel.webview.onDidReceiveMessage(async message => {
      if (!message || typeof message !== 'object') return;
      if (message.type === 'ready') {
        preview.ready = true;
        updateState();
        if (preview.pendingReveal) {
          void panel.webview.postMessage(preview.pendingReveal);
          preview.pendingReveal = undefined;
        }
        return;
      }
      if (['gitHistory', 'gitDiff'].includes(message.type) && Number.isSafeInteger(message.id)) {
        try {
          if (!vscode.workspace.isTrusted) throw new Error('The workspace is no longer trusted.');
          const json = message.type === 'gitHistory' ? await gitHistory.read() : {diff: await gitHistory.diff(message.body)};
          await panel.webview.postMessage({type: 'response', id: message.id, status: 200, json});
        } catch (error) {
          await panel.webview.postMessage({type: 'response', id: message.id, status: 409, json: {error: String(error)}});
        }
        return;
      }
      if (message.type === 'clipboard' && Number.isSafeInteger(message.id)) {
        try {
          if (typeof message.body !== 'string' || message.body.length > 100000) throw new Error('Invalid clipboard text.');
          await vscode.env.clipboard.writeText(message.body);
          await panel.webview.postMessage({type: 'response', id: message.id, status: 200});
        } catch {
          await panel.webview.postMessage({type: 'response', id: message.id, status: 400});
        }
        return;
      }
      if (message.type === 'source') {
        try {
          const document = await vscode.workspace.openTextDocument(uri);
          if (message.index === undefined) {
            await vscode.window.showTextDocument(document, {viewColumn: vscode.ViewColumn.One});
          } else {
            if (document.isDirty) throw new Error('Save or revert the Lean file before selecting a saved notebook cell.');
            const snapshot = (await backend.request('/api/notebook')).json;
            if (typeof message.sha256 !== 'string') throw new Error('Missing saved notebook identity.');
            const bounds = sourceCell(snapshot, document.getText(), message.index, message.sha256, message.range);
            const observed = message.trace ? (await backend.request('/api/proof/trace')).json : undefined;
            const tactic = message.trace ? sourceTactic(snapshot, document.getText(), message.index, message.sha256, observed, message.trace) : undefined;
            const range = tactic ? new vscode.Range(tactic.start.line - 1, tactic.start.character, tactic.end.line - 1, tactic.end.character)
              : new vscode.Range(bounds.start - 1, 0, bounds.end - 1, document.lineAt(bounds.end - 1).text.length);
            const editor = await vscode.window.showTextDocument(document, {viewColumn: vscode.ViewColumn.One});
            if (document.isDirty) throw new Error('The Lean file changed while opening it. Save and try again.');
            sourceCell(snapshot, document.getText(), message.index, message.sha256, message.range);
            editor.selection = new vscode.Selection(range.start, range.start);
            editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
          }
        }
        catch (error) { void vscode.window.showErrorMessage(String(error)); }
        return;
      }
      if (message.type === 'acceptDecision') {
        // The preview names only a decision; its block comes from the backend.
        if (preview.accepting) { void vscode.window.showErrorMessage('A decision is already being accepted.'); return; }
        preview.accepting = true;
        try {
          if (!vscode.workspace.isTrusted) throw new Error('The workspace is no longer trusted.');
          const document = await vscode.workspace.openTextDocument(uri);
          await acceptDecision(vscode, document, path => backend.request(path), message.key);
          void vscode.window.showInformationMessage('Decision accepted: its checked block was inserted and the notebook saved.');
        } catch (error) { void vscode.window.showErrorMessage(String(error)); }
        finally { preview.accepting = false; }
        return;
      }
      if (message.type === 'export' && Number.isSafeInteger(message.id)) {
        try {
          if (!vscode.workspace.isTrusted) throw new Error('The workspace is no longer trusted.');
          if (typeof message.body !== 'string' || Buffer.byteLength(message.body) > 20_000_000) throw new Error('Invalid or oversized HTML export.');
          const target = await vscode.window.showSaveDialog({
            defaultUri: vscode.Uri.file(uri.fsPath.replace(/\.lean$/i, '') + '.html'),
            filters: {'HTML page': ['html']}, title: 'Export saved notebook as HTML',
          });
          if (target) {
            if (target.scheme !== 'file' || !target.fsPath.endsWith('.html')) throw new Error('Choose a local .html file for the export.');
            await vscode.workspace.fs.writeFile(target, Buffer.from(message.body, 'utf8'));
          }
          void panel.webview.postMessage({type: 'response', id: message.id, status: 200, json: {saved: !!target}});
        } catch (error) {
          void panel.webview.postMessage({type: 'response', id: message.id, status: 409, json: {error: String(error)}});
        }
        return;
      }
      if (message.type === 'reference' && typeof message.module === 'string') {
        try {
          if (!vscode.workspace.isTrusted) throw new Error('The workspace is no longer trusted.');
          const snapshot = (await backend.request('/api/notebook')).json;
          const link = snapshot.references?.find((item: any) => item.module === message.module);
          if (!link || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(link.module)) throw new Error('This notebook is no longer available.');
          await openPreview(vscode.Uri.file(path.join(path.dirname(uri.fsPath), `${link.module}.lean`)));
        } catch (error) { void vscode.window.showErrorMessage(String(error)); }
        return;
      }
      if (message.type !== 'request' || !Number.isSafeInteger(message.id) || typeof message.path !== 'string') return;
      const method = message.method ?? 'GET';
      try {
        if (!vscode.workspace.isTrusted) throw new Error('The workspace is no longer trusted.');
        if (heldWhileDirty(message.path, method, dirty())) throw new Error('Save or revert unsaved Lean files before running preview actions. The preview shows saved files.');
        if (message.body !== undefined && typeof message.body !== 'string') throw new Error('Invalid request body');
        const reply = await backend.request(message.path, method, message.body);
        void panel.webview.postMessage({type: 'response', id: message.id, ...reply});
      } catch (error) {
        void panel.webview.postMessage({type: 'response', id: message.id, status: 409, json: {error: String(error)}});
      }
    });
    try {
      panel.webview.html = previewHtml(panel.webview, media);
      await backend.ready;
      updateState();
    }
    catch (error) {
      if (previews.get(key) === preview) {
        panel.dispose();
        throw new Error(`${error} Configure glitnir.pythonPath; see Output → Glitnir.`);
      }
    }
  }
  const dependencyChanges = vscode.workspace.createFileSystemWatcher('**/{*.lean,*.olean,*.ir,*.ir.sig,*.so,*.dylib,lean-toolchain,lakefile.toml,lake-manifest.json}');
  const invalidateObservations = () => {
    for (const preview of previews.values()) void preview.panel.webview.postMessage({type: 'environmentChanged'});
  };
  context.subscriptions.push(dependencyChanges, dependencyChanges.onDidChange(invalidateObservations),
    dependencyChanges.onDidCreate(invalidateObservations), dependencyChanges.onDidDelete(invalidateObservations));
  for (const [command, action] of Object.entries({reviewNext: 'next', reviewPrevious: 'previous', reviewClaims: 'claims', reviewProof: 'proof', reviewHelp: 'help'})) {
    context.subscriptions.push(vscode.commands.registerCommand(`glitnir.${command}`, () => {
      const preview = [...previews.values()].find(p => p.panel.active);
      if (preview) void preview.panel.webview.postMessage({type: 'reviewCommand', action});
      else void vscode.window.showInformationMessage('Focus a Glitnir notebook to use its review commands.');
    }));
  }
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration('glitnir.pythonPath')) environmentEpoch++;
      let closed = false;
      for (const preview of previews.values()) {
        if (event.affectsConfiguration('glitnir.pythonPath', preview.uri)) {
          preview.panel.dispose(); closed = true;
        }
      }
      if (closed) void vscode.window.showInformationMessage('Glitnir environment settings changed. Open the preview again to use them.');
    }),
    vscode.commands.registerCommand('glitnir.setupAgent', async () => {
      try { await setupAgent(context); } catch (error) { void vscode.window.showErrorMessage(String(error)); }
    }),
    vscode.commands.registerCommand('glitnir.openPreview', async uri => {
      try { await openPreview(uri); } catch (error) { void vscode.window.showErrorMessage(String(error)); }
    }),
    vscode.commands.registerCommand('glitnir.revealCell', async () => {
      try {
        const editor = vscode.window.activeTextEditor;
        if (!editor || editor.document.uri.scheme !== 'file' || !editor.document.uri.path.endsWith('.lean')) throw new Error('Open a Lean source editor first.');
        if (editor.document.isDirty) throw new Error('Save or revert the Lean file before revealing its notebook cell.');
        const uri = editor.document.uri;
        const line = editor.selection.active.line + 1;
        const version = editor.document.version;
        await openPreview(uri);
        const preview = previews.get(await realpath(uri.fsPath));
        if (!preview) return;
        const snapshot = (await preview.backend.request('/api/notebook')).json;
        if (editor.document.isDirty || editor.document.version !== version) throw new Error('The Lean file changed. Save and try again.');
        const index = cellAtLine(snapshot, editor.document.getText(), line);
        if (index !== undefined) {
          const reveal = {type: 'revealCell', index, sha256: snapshot.sha256, line, character: editor.selection.active.character};
          if (preview.ready) await preview.panel.webview.postMessage(reveal);
          else preview.pendingReveal = reveal;
        }
      } catch (error) { void vscode.window.showErrorMessage(String(error)); }
    }),
    vscode.commands.registerCommand('glitnir.setup' , async requested => {
      try {
        if (vscode.env.remoteName || process.platform === 'win32') throw new Error('Glitnir setup supports local Linux and macOS windows.');
        const uri = requested ?? vscode.window.activeTextEditor?.document.uri ?? vscode.workspace.workspaceFolders?.[0]?.uri;
        if (!uri || uri.scheme !== 'file') throw new Error('Open a local workspace or Lean file first.');
        await setup!.resolve(uri, true);
      } catch (error) {
        if (shuttingDown) return;
        if (await vscode.window.showErrorMessage(String(error), 'Read Setup Guide') === 'Read Setup Guide') await openSetupGuide(context);
      }
    }),
    vscode.commands.registerCommand('glitnir.setupBackendForThisVersion', async () => {
      try { await upgrade!.run(); } catch (error) { if (!shuttingDown) void vscode.window.showErrorMessage(String(error)); }
    }),
    vscode.commands.registerCommand('glitnir.installExtensionPackage', async requested => {
      try { await packages!.install(requested); } catch (error) { if (!shuttingDown) void vscode.window.showErrorMessage(String(error)); }
    }),
    vscode.commands.registerCommand('glitnir.copyValidationCommand', async () => {
      try {
        const document = vscode.window.activeTextEditor?.document;
        if (vscode.env.remoteName || !document || document.uri.scheme !== 'file' || !document.uri.fsPath.endsWith('.lean') || document.isDirty) throw new Error('Open a saved local Lean file to copy its validation command.');
        const environment = await setup!.resolve(document.uri);
        if (!environment) return;
        await vscode.env.clipboard.writeText(validationCommand(environment, document.uri.fsPath));
        void vscode.window.showInformationMessage('Glitnir validation command copied. Give it to your agent or run it in a terminal; it checks without writing notebook metadata.');
      } catch (error) { if (!shuttingDown) void vscode.window.showErrorMessage(String(error)); }
    }),
    vscode.workspace.onDidChangeTextDocument(updateState),
    vscode.workspace.onDidSaveTextDocument(document => {
      updateState();
      if (document.uri.path.endsWith('.lean')) for (const preview of previews.values()) void preview.panel.webview.postMessage({type: 'environmentChanged'});
      for (const preview of previews.values()) if (document.uri.fsPath === preview.uri.fsPath) void preview.panel.webview.postMessage({type: 'sourceChanged'});
    }),
    vscode.workspace.onDidCloseTextDocument(updateState),
  );
}

/** Await all owned servers, including those whose tabs were already closed. */
export async function deactivate(): Promise<void> {
  shuttingDown = true;
  setup?.dispose();
  packages?.dispose();
  upgrade?.dispose();
  const active = [...previews.values()];
  for (const preview of active) preview.panel.dispose();
  await Promise.all([...active.map(p => p.backend.close()), ...stopping]);
  previews.clear();
}
