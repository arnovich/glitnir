import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {EnvironmentSetup} from './setup';
import {HandoffJournal} from './handoff-journal';

interface Provider {name: string; label: string}
interface Pending {provider: Provider; python: string; scope: string; payload: any}


/** Run installed plugin code in isolated Python, passing secrets only on stdin. */
export function invoke(python: string, request: object): Promise<any> {
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-I', '-B', '-m', 'glitnir.service.handoff'], {stdio: ['pipe', 'pipe', 'pipe']});
    let output = ''; let bytes = 0;
    const timer = setTimeout(() => child.kill(), 60000);
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > 200000) child.kill(); else output += chunk.toString();
    });
    child.stderr.resume(); // Provider diagnostics can contain secrets; never log them.
    child.on('error', () => {clearTimeout(timer); reject(new Error('Could not start the installed handoff provider.'));});
    child.stdin.on('error', () => {});
    child.on('close', code => {
      clearTimeout(timer);
      if (code !== 0 || bytes > 200000) return reject(new Error('Handoff failed. Check the destination and credential; retain any pending request for explicit recovery.'));
      try { resolve(JSON.parse(output)); } catch { reject(new Error('Invalid handoff response.')); }
    });
    child.stdin.end(JSON.stringify(request));
  });
}

/** Bind handoff secrets independently of computation credentials. */
export function handoffScope(provider: string, server: string, project: string): string {
  const url = new URL(server);
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash)
    throw new Error('Configure an HTTPS server origin without credentials or a path.');
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(project)) throw new Error('Configure the handoff project.');
  return 'glitnir.handoff:' + JSON.stringify([provider, url.origin, project]);
}

/** Optional installed-provider handoffs; opening the editor never sends a request. */
export class NotebookHandoff {
  private busy = false;
  private readonly journal: HandoffJournal<Pending>;
  constructor(private readonly context: vscode.ExtensionContext, private readonly setup: EnvironmentSetup, private readonly run: typeof invoke = invoke) {
    this.journal = new HandoffJournal(path.join(context.globalStorageUri.fsPath, 'notebook-handoffs'));
  }

  private trusted(): void {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before using a notebook handoff.');
  }

  private async token(scope: string, replace = false): Promise<string | undefined> {
    this.trusted();
    const existing = await this.context.secrets.get(scope);
    if (existing && !replace) return existing;
    const value = await vscode.window.showInputBox({title: 'Registered-agent credential for ' + scope.slice('glitnir.handoff:'.length), password: true, ignoreFocusOut: true});
    if (!value) return undefined;
    if (value.length > 256 || /\s/.test(value)) throw new Error('Invalid handoff credential.');
    this.trusted();
    await this.context.secrets.store(scope, value);
    return value;
  }

  private async send(id: string, pending: Pending, token: string, recovering = false): Promise<void> {
    this.trusted();
    const receipt = await this.run(pending.python, {action: 'submit', provider: pending.provider.name, payload: pending.payload, token});
    if (receipt.refused === true) {
      // A later rejection cannot disprove an earlier committed, lost response.
      if (recovering) throw new Error('Recovery was refused. The earlier outcome is still unknown; retain this request and reconcile it with the operator.');
      await this.journal.finish(id, receipt);
      await vscode.window.showWarningMessage('This submission was rejected without creating a task. Prepare a fresh notebook preview to try again.');
      return;
    }
    const url = new URL(receipt.url);
    if (url.origin !== new URL(pending.payload.server).origin || url.protocol !== 'https:' || url.username || url.password)
      throw new Error('The handoff receipt names another destination. Retain the saved request.');
    // Preserve the receipt before removing the pending envelope.
    await this.context.globalState.update('glitnir.lastNotebookHandoff', receipt);
    await this.journal.finish(id, receipt);
    const open = await vscode.window.showInformationMessage('Request submitted; awaiting owner setup and launch.', 'Open request');
    if (open) await vscode.env.openExternal(vscode.Uri.parse(url.href));
  }

  async start(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      this.trusted();
      if ((await this.journal.pending()).length) throw new Error('Recover the pending notebook handoff before creating another request.');
      const document = vscode.window.activeTextEditor?.document;
      if (!document || document.uri.scheme !== 'file' || !document.uri.path.endsWith('.lean') || document.isDirty)
        throw new Error('Open a saved, unchanged Lean notebook first.');
      if (vscode.workspace.textDocuments.some(d => d.isDirty && d.uri.path.endsWith('.lean')))
        throw new Error('Save changed Lean files before preparing the handoff.');
      const source = document.getText();
      const disk = Buffer.from(await vscode.workspace.fs.readFile(document.uri)).toString('utf8');
      if (source !== disk) throw new Error('The notebook changed on disk. Reopen it before submitting.');
      const environment = await this.setup.resolve(document.uri);
      if (!environment) return;
      const providers: Provider[] = await this.run(environment.python, {action: 'list'});
      if (!providers.length) throw new Error('Install a notebook handoff extension package first.');
      const selected = await vscode.window.showQuickPick(providers.map(p => ({label: p.label, provider: p})), {title: 'Send saved notebook'});
      if (!selected) return;
      const provider = selected.provider;
      if (!/^[a-z][a-z0-9_-]*$/.test(provider.name)) throw new Error('Invalid handoff provider name.');
      const config = vscode.workspace.getConfiguration('glitnir', document.uri);
      const server = config.get<string>(provider.name + 'Server', '');
      const project = config.get<string>(provider.name + 'Project', '');
      const scope = handoffScope(provider.name, server, project);
      const connect = await vscode.window.showWarningMessage(`Connect ${provider.label} to ${new URL(server).origin}, project ${project}? The plugin will verify your handoff credential before preparing a request.`, {modal: true}, 'Connect');
      if (connect !== 'Connect') return;
      const token = await this.token(scope);
      if (!token) return;
      const objective = await vscode.window.showInputBox({title: 'Research objective for this notebook', ignoreFocusOut: true});
      if (!objective?.trim()) return;
      const payload = await this.run(environment.python, {action: 'prepare', provider: provider.name, token,
        payload: {server: new URL(server).origin, project, name: path.basename(document.uri.fsPath), source, objective}});
      const review = await vscode.workspace.openTextDocument({language: 'json', content: JSON.stringify(payload, null, 2)});
      await vscode.window.showTextDocument(review, {preview: true, viewColumn: vscode.ViewColumn.Beside});
      const confirm = await vscode.window.showWarningMessage(`Send the displayed notebook request to ${server}, project ${project}? The saved source is included. Owner setup and launch are still required.`, {modal: true}, 'Submit request');
      if (confirm !== 'Submit request') return;
      this.trusted();
      const current = vscode.workspace.getConfiguration('glitnir', document.uri);
      if (handoffScope(provider.name, current.get<string>(provider.name + 'Server', ''), current.get<string>(provider.name + 'Project', '')) !== scope
          || await this.context.secrets.get(scope) !== token || document.isDirty
          || document.getText() !== source
          || Buffer.from(await vscode.workspace.fs.readFile(document.uri)).toString('utf8') !== source)
        throw new Error('Source, destination or credential changed; prepare a new preview.');
      if (payload.source_sha256 !== createHash('sha256').update(source).digest('hex')) throw new Error('Provider returned a different source binding.');
      const pending: Pending = {provider, python: environment.python, scope, payload};
      const id = await this.journal.persist(pending);
      await this.send(id, pending, token);
    } finally { this.busy = false; }
  }

  async credential(): Promise<void> {
    this.trusted();
    const uri = vscode.window.activeTextEditor?.document.uri;
    if (!uri) throw new Error('Open a notebook first.');
    const environment = await this.setup.resolve(uri);
    if (!environment) return;
    const providers: Provider[] = await this.run(environment.python, {action: 'list'});
    const selected = await vscode.window.showQuickPick(providers.map(provider => ({label: provider.label, provider})), {title: 'Replace handoff credential'});
    if (!selected) return;
    const config = vscode.workspace.getConfiguration('glitnir', uri);
    const name = selected.provider.name;
    const scope = handoffScope(name, config.get<string>(name + 'Server', ''), config.get<string>(name + 'Project', ''));
    await this.token(scope, true);
  }

  async recover(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      this.trusted();
      const requests = await this.journal.pending();
      if (!requests.length) {
        const last = this.context.globalState.get<{url: string}>('glitnir.lastNotebookHandoff');
        if (last && await vscode.window.showInformationMessage('No pending handoff. Open the last submitted request?', 'Open request'))
          await vscode.env.openExternal(vscode.Uri.parse(last.url));
        return;
      }
      const selected = await vscode.window.showQuickPick(requests.map(r => ({label: r.value.provider.label, description: r.value.payload.server + ' / ' + r.value.payload.project, request: r})), {title: 'Recover saved notebook request'});
      if (!selected) return;
      const {id, value: pending} = selected.request;
      const choice = await vscode.window.showWarningMessage(`Retry the exact saved request to ${pending.payload.server}, project ${pending.payload.project}, as ${pending.payload.actor}? It uses the originally reviewed source, not the current editor.`, {modal: true}, 'Retry saved request');
      if (choice !== 'Retry saved request') return;
      const token = await this.token(pending.scope);
      if (token) await this.send(id, pending, token, true);
    } finally { this.busy = false; }
  }
}
