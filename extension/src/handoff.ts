import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {EnvironmentSetup} from './setup';
import {HandoffJournal} from './handoff-journal';
import {HandoffAction, HandoffReview, HandoffReviewPanel} from './handoff-panel';

interface Provider {name: string; label: string; supportsStatus?: boolean}
interface Pending {provider: Provider; python: string; scope: string; payload: any; review?: HandoffReview}
interface Submitted {pending: Pending; receipt: any}


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
  private panel?: HandoffReviewPanel;
  private panelNotebook?: string;
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
    const [, server, project] = JSON.parse(scope.slice('glitnir.handoff:'.length)) as string[];
    const value = await vscode.window.showInputBox({title: `Handoff credential for ${server} / ${project}`, password: true, ignoreFocusOut: true});
    if (!value) return undefined;
    if (value.length > 256 || /\s/.test(value)) throw new Error('Invalid handoff credential.');
    this.trusted();
    await this.context.secrets.store(scope, value);
    return value;
  }

  private async send(id: string, pending: Pending, token: string, recovering = false): Promise<any> {
    this.trusted();
    const receipt = await this.run(pending.python, {action: 'submit', provider: pending.provider.name, payload: pending.payload, token});
    if (receipt.refused === true) {
      // A later rejection cannot disprove an earlier committed, lost response.
      if (recovering) throw new Error('Recovery was refused. The earlier outcome is still unknown; retain this request and reconcile it with the operator.');
      await this.journal.finish(id, receipt);
      return receipt;
    }
    const url = new URL(receipt.url);
    if (url.origin !== new URL(pending.payload.server).origin || url.protocol !== 'https:' || url.username || url.password)
      throw new Error('The handoff receipt names another destination. Retain the saved request.');
    receipt.checkedAt = new Date().toLocaleString();
    // Preserve the receipt before removing the pending envelope.
    await this.context.globalState.update('glitnir.lastNotebookHandoff', {pending, receipt});
    await this.journal.finish(id, receipt);
    return receipt;
  }

  private review(pending: Pending): HandoffReview {
    return pending.review ?? {notebook: 'Saved notebook request', source: 'This older request retains its original source in the saved envelope.', objective: 'Original research objective', server: pending.payload.server, project: pending.payload.project};
  }

  private showRequest(review: HandoffReview, saved?: {pending: Pending; id?: string; receipt?: any},
    providerLabel = saved?.pending.provider.label, canRefresh = saved?.pending.provider.supportsStatus === true, prepare?: (objective: string) => Promise<{pending: Pending; token: string} | undefined>): void {
    let pending = saved?.pending, id = saved?.id, receipt = saved?.receipt;
    const panel = new HandoffReviewPanel({...review, providerLabel, canRefresh, phase: receipt ? 'submitted' : id ? 'uncertain' : 'review', stage: receipt?.stage, task: receipt?.task, checkedAt: receipt?.checkedAt}, async (action: HandoffAction, objective: string) => {
      if (this.busy) return;
      this.busy = true;
      try {
        this.trusted();
        if (action === 'open' && pending && receipt) {
          const url = new URL(receipt.url);
          if (url.protocol !== 'https:' || url.origin !== new URL(pending.payload.server).origin || url.username || url.password) throw new Error('Invalid request destination.');
          await vscode.env.openExternal(vscode.Uri.parse(url.href));
          return;
        }
        if (action === 'refresh' && canRefresh && pending && receipt) {
          panel.update({refreshing: true, error: undefined});
          const token = await this.token(pending.scope);
          if (!token || panel.closed) {panel.update({refreshing: false}); return;}
          this.trusted();
          const status = await this.run(pending.python, {action: 'status', provider: pending.provider.name, payload: {request: pending.payload, task: receipt.task}, token});
          if (status.task !== receipt.task) throw new Error('Status belongs to another request.');
          receipt = {...receipt, stage: status.stage, checkedAt: new Date().toLocaleString()};
          await this.context.globalState.update('glitnir.lastNotebookHandoff', {pending, receipt});
          panel.update({stage: receipt.stage, refreshing: false, checkedAt: receipt.checkedAt});
          return;
        }
        let token: string | undefined;
        const recovering = action === 'retry';
        panel.update({phase: 'submitting', objective: recovering && pending ? this.review(pending).objective : objective, error: undefined});
        if (!recovering && prepare) {
          if ((await this.journal.pending()).length) throw new Error('Recover the existing pending request before submitting another.');
          const prepared = await prepare(objective);
          if (!prepared || panel.closed) {panel.update({phase: 'review'}); return;}
          pending = prepared.pending; token = prepared.token;
          id = await this.journal.persist(pending);
        } else if (recovering && pending && id) {
          token = await this.token(pending.scope);
          if (!token || panel.closed) {panel.update({phase: 'uncertain'}); return;}
        }
        if (!pending || !id || !token) throw new Error('No reviewed request is available.');
        receipt = await this.send(id, pending, token, recovering);
        if (receipt.refused) {panel.update({phase: 'rejected'}); return;}
        panel.update({phase: 'submitted', task: receipt.task, stage: receipt.stage, checkedAt: receipt.checkedAt});
      } catch (error) {
        panel.update({phase: receipt && !receipt.refused ? 'submitted' : id ? 'uncertain' : 'review', refreshing: false, ...(action === 'refresh' ? {stage: 'unknown'} : {}),
          error: error instanceof Error ? error.message : 'Request could not be completed.'});
      } finally { this.busy = false; }
    }, () => {if (this.panel === panel) {this.panel = undefined; this.panelNotebook = undefined;}});
    this.panel = panel;
  }

  /** Discover a local capability without reading credentials or contacting Taskvalet. */
  async available(python: string, provider: string): Promise<boolean> {
    this.trusted();
    try {
      const providers: Provider[] = await this.run(python, {action: 'list'});
      return providers.some(item => item.name === provider);
    } catch { return false; }
  }

  async start(target?: vscode.Uri, providerName?: string): Promise<void> {
    if (this.busy) return;
    if (this.panel) {
      const requested = target ?? vscode.window.activeTextEditor?.document.uri;
      if (requested && requested.fsPath !== this.panelNotebook)
        throw new Error('Close the existing handoff panel before reviewing another notebook. Pending requests remain available through Recover Notebook Handoff.');
      this.panel.reveal(); return;
    }
    this.busy = true;
    try {
      this.trusted();
      if ((await this.journal.pending()).length) throw new Error('Recover the pending notebook handoff before creating another request.');
      const uri = target ?? vscode.window.activeTextEditor?.document.uri;
      const openDocument = () => vscode.workspace.textDocuments.find(d => d.uri.fsPath === uri?.fsPath);
      const document = openDocument();
      if (!uri || uri.scheme !== 'file' || !uri.path.endsWith('.lean') || document?.isDirty)
        throw new Error('Open a saved, unchanged Lean notebook first.');
      if (vscode.workspace.textDocuments.some(d => d.isDirty && d.uri.path.endsWith('.lean')))
        throw new Error('Save changed Lean files before preparing the handoff.');
      const disk = Buffer.from(await vscode.workspace.fs.readFile(uri)).toString('utf8');
      const source = document?.getText() ?? disk;
      if (source !== disk) throw new Error('The notebook changed on disk. Reopen it before submitting.');
      const environment = await this.setup.resolve(uri);
      if (!environment) return;
      const providers: Provider[] = await this.run(environment.python, {action: 'list'});
      if (!providers.length) throw new Error('Install a notebook handoff extension package first.');
      const selected = providerName ? {provider: providers.find(p => p.name === providerName)}
        : await vscode.window.showQuickPick(providers.map(p => ({label: p.label, provider: p})), {title: 'Send saved notebook'});
      if (providerName && !selected?.provider) throw new Error('Install the Taskvalet handoff package first.');
      if (!selected?.provider) return;
      const provider = selected.provider;
      if (!/^[a-z][a-z0-9_-]*$/.test(provider.name)) throw new Error('Invalid handoff provider name.');
      const config = vscode.workspace.getConfiguration('glitnir', uri);
      const server = config.get<string>(provider.name + 'Server', '');
      const project = config.get<string>(provider.name + 'Project', '');
      const scope = handoffScope(provider.name, server, project);
      const review: HandoffReview = {server: new URL(server).origin, project, notebook: path.basename(uri.fsPath), source, objective: ''};
      this.panelNotebook = uri.fsPath;
      this.showRequest(review, undefined, provider.label, provider.supportsStatus === true, async objective => {
        const unchanged = async (): Promise<void> => {
          this.trusted();
          const current = vscode.workspace.getConfiguration('glitnir', uri);
          if (handoffScope(provider.name, current.get<string>(provider.name + 'Server', ''), current.get<string>(provider.name + 'Project', '')) !== scope
              || openDocument()?.isDirty || (openDocument()?.getText() ?? source) !== source
              || Buffer.from(await vscode.workspace.fs.readFile(uri)).toString('utf8') !== source)
            throw new Error('Source or destination changed; close this panel and prepare a fresh review.');
        };
        await unchanged();
        const token = await this.token(scope);
        if (!token) return;
        const payload = await this.run(environment.python, {action: 'prepare', provider: provider.name, token,
          payload: {server: review.server, project, name: review.notebook, source, objective}});
        await unchanged();
        if (await this.context.secrets.get(scope) !== token) throw new Error('Credential changed; prepare a fresh review.');
        if (payload.source_sha256 !== createHash('sha256').update(source).digest('hex')) throw new Error('Provider returned a different source binding.');
        return {pending: {provider, python: environment.python, scope, payload, review: {...review, objective}}, token};
      });
    } finally { this.busy = false; }
  }

  async credential(target?: vscode.Uri): Promise<void> {
    this.trusted();
    const uri = target ?? vscode.window.activeTextEditor?.document.uri;
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
    if (this.panel) {this.panel.reveal(); return;}
    this.busy = true;
    try {
      this.trusted();
      const requests = await this.journal.pending();
      if (!requests.length) {
        const last = this.context.globalState.get<Submitted>('glitnir.lastNotebookHandoff');
        if (last?.pending && last.receipt) this.showRequest(this.review(last.pending), last);
        else void vscode.window.showInformationMessage('No saved request is available in this version.');
        return;
      }
      const selected = await vscode.window.showQuickPick(requests.map(r => ({label: r.value.provider.label, description: r.value.payload.server + ' / ' + r.value.payload.project, request: r})), {title: 'Recover saved notebook request'});
      if (!selected) return;
      const {id, value: pending} = selected.request;
      this.showRequest(this.review(pending), {pending, id});
    } finally { this.busy = false; }
  }
}
