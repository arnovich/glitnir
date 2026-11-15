import {randomBytes} from 'node:crypto';
import * as vscode from 'vscode';

export interface HandoffReview {notebook: string; source: string; objective: string; server: string; project: string}
export interface ResearchResults {availability: string; message: string; digest?: string; hasFindings?: boolean; truncated?: boolean; sections?: {investigation?: string; kind: string; author: string; version: number; problem: string; text: string; truncated?: boolean}[]}
export interface LaunchReview {team: string; team_version: number; environment: string; max_seconds: number; max_workers: number; compute_available: number | null; ready: boolean; status: string; blockers: string[]; checkedAt?: string; scope_id?: string; preview_id?: string; preview_expires_at?: number; budget_micro_usd?: number; campaign_id?: string; cleanup?: string; can_stop?: boolean; exposure_micro_usd?: number | null; controlSupported?: boolean; intent?: {scope_id: string; preview_id: string}}
export interface HandoffPanelState extends HandoffReview {
  results?: ResearchResults;
  canLaunchReview?: boolean;
  canLaunchControl?: boolean;
  launch?: LaunchReview;
  freshness?: string;
  reviewed?: boolean;
  phase: 'review' | 'submitting' | 'submitted' | 'uncertain' | 'rejected';
  providerLabel?: string;
  canRefresh?: boolean;
  stage?: string;
  task?: string;
  error?: string;
  checkedAt?: string;
  refreshing?: boolean;
}
export type HandoffAction = 'submit' | 'retry' | 'refresh' | 'open' | 'markReviewed' | 'launchReview' | 'launchCredential' | 'launchStart' | 'launchRetry' | 'launchStatus' | 'launchStop';

/** A self-contained themed view; data arrives as messages and is rendered as text. */
export function handoffHtml(): string {
  const nonce = randomBytes(24).toString('hex');
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<title>Notebook request</title><style nonce="${nonce}">
body{font-family:var(--vscode-font-family,system-ui);color:var(--vscode-foreground,#ddd);background:var(--vscode-editor-background,#202124);padding:28px;line-height:1.5}main{max-width:780px;margin:auto}h1{font-size:24px;font-weight:600;margin:4px 0 8px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--vscode-descriptionForeground,#aaa)}.intro,.hint{color:var(--vscode-descriptionForeground,#aaa)}.card{border:1px solid var(--vscode-panel-border,#444);border-radius:8px;padding:18px;margin:20px 0}.row{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}h2,label{font-size:14px;font-weight:600;margin:0 0 10px;display:block}dl{margin:0;display:grid;grid-template-columns:100px minmax(0,1fr);gap:6px 12px}dt{color:var(--vscode-descriptionForeground,#aaa)}dd{margin:0;overflow-wrap:anywhere}textarea{box-sizing:border-box;width:100%;min-height:110px;padding:12px;resize:vertical;border:1px solid var(--vscode-input-border,#666);border-radius:4px;color:var(--vscode-input-foreground,#eee);background:var(--vscode-input-background,#303134);font:inherit}textarea:focus,button:focus-visible,summary:focus-visible{outline:2px solid var(--vscode-focusBorder,#55aaff);outline-offset:2px}summary{cursor:pointer;font-weight:500}pre{font-family:var(--vscode-editor-font-family,monospace);font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:350px;overflow:auto;padding:12px;background:var(--vscode-textCodeBlock-background,#292a2d);border-radius:4px}button{font:inherit;padding:8px 14px;border:0;border-radius:4px;cursor:pointer;background:var(--vscode-button-background,#0767ae);color:var(--vscode-button-foreground,#fff)}button.secondary{background:var(--vscode-button-secondaryBackground,#414246);color:var(--vscode-button-secondaryForeground,#eee)}button:disabled{opacity:.55;cursor:default}.actions{display:flex;gap:10px;flex-wrap:wrap}.badge{padding:3px 9px;border-radius:20px;background:var(--vscode-badge-background,#3c5269);color:var(--vscode-badge-foreground,#fff);font-size:12px}.hint{font-size:12px;margin:8px 0 0}.error{color:var(--vscode-errorForeground,#ff9090);white-space:pre-wrap}.status-text{margin:10px 0}#task{overflow-wrap:anywhere}[hidden]{display:none!important}
</style></head><body><main>
<div class="eyebrow">Glitnir · notebook request</div><h1 id="title">Send notebook</h1>
<p class="intro">Review your notebook and research objective before sending a request.</p>
<section class="card" aria-labelledby="destination-title"><h2 id="destination-title">Destination</h2><dl><dt>Server</dt><dd id="server"></dd><dt>Project</dt><dd id="project"></dd></dl></section>
<section class="card" aria-labelledby="notebook-title"><h2 id="notebook-title">Notebook</h2><p id="notebook"></p><details open><summary>Review saved source</summary><pre id="source" tabindex="0"></pre></details><p class="hint">Only this saved notebook is included. Imported files and the compiled environment are not uploaded.</p></section>
<section class="card"><label for="objective">Research objective</label><textarea id="objective" maxlength="2000" placeholder="What would you like to investigate or improve?"></textarea><p class="hint">Describe the question or result you want the researchers to work on.</p></section>
<section class="card" aria-labelledby="status-title"><div class="row"><h2 id="status-title">Request status</h2><span class="badge" id="stage">Not submitted</span></div><div role="status" aria-live="polite"><p id="status" class="status-text"></p><p id="task" class="hint"></p><p id="checked" class="hint"></p><p id="status-note" class="hint"></p></div><p id="error" class="error" role="alert" hidden></p><div class="actions"><button id="submit" type="button">Submit request</button><button id="retry" type="button" hidden>Retry saved request</button><button id="refresh" class="secondary" type="button" hidden>Refresh status</button><button id="open" class="secondary" type="button" hidden>Open request</button></div><p class="hint">Submission records the notebook and objective. When a supported host is prepared, use the swarm launch controls below. Research does not change proof status.</p></section>
<section class="card" id="launch-card" hidden aria-labelledby="launch-title"><h2 id="launch-title">Swarm launch setup</h2><p class="hint">Review the one team and environment authorized for this notebook request.</p><dl><dt>Team</dt><dd id="launch-team">Not checked</dd><dt>Environment</dt><dd id="launch-environment">Not checked</dd><dt>Limits</dt><dd id="launch-limits">Not checked</dd><dt>Compute</dt><dd id="launch-capacity">Not checked</dd></dl><p id="launch-budget" class="hint"></p><p id="launch-state" role="status" aria-live="polite"></p><p id="launch-checked" class="hint"></p><ul id="launch-blockers" aria-live="polite"></ul><div class="actions"><button id="launchReview" class="secondary" type="button">Check launch readiness</button><button id="launchCredential" class="secondary" type="button">Set launch credential</button><button id="launch-start" type="button" disabled>Launch swarm</button><button id="launchRetry" class="secondary" type="button" hidden>Retry saved launch</button><button id="launchStatus" class="secondary" type="button" hidden>Recover status</button><button id="launchStop" class="secondary" type="button" hidden>Stop swarm</button></div><p class="hint">Launch uses the displayed team, environment and spending ceiling. Stop requests cancellation; cleanup remains unconfirmed until the host reports it. Findings remain proposals until checked in this notebook.</p></section>
<section class="card" id="results-card" hidden><h2>Research results</h2><p id="freshness" class="hint"></p><p id="results-message"></p><p id="results-limit" class="hint"></p><div id="result-sections"></div><button id="markReviewed" class="secondary" type="button">Mark reviewed locally</button><p class="hint">Reviewing does not accept the Taskvalet task, apply source changes, or mark any claim proven.</p></section>
</main><script nonce="${nonce}">
const host=acquireVsCodeApi();const $=id=>document.getElementById(id);let state=null;
const stages={new:'New request',ready:'Queued',ongoing:'In progress',needs_input:'Needs input',review:'In review',finished:'Finished'};
function render(value){state=value;for(const key of ['server','project','notebook','source'])$(key).textContent=value[key]||'';
$('title').textContent=value.providerLabel||'Send notebook';$('objective').value=value.objective||'';$('objective').readOnly=value.phase!=='review';
$('submit').hidden=value.phase!=='review';$('submit').disabled=!$('objective').value.trim();
$('retry').hidden=value.phase!=='uncertain';$('retry').disabled=false;$('refresh').hidden=value.phase!=='submitted'||!value.canRefresh;$('refresh').disabled=!!value.refreshing;$('open').hidden=value.phase!=='submitted';
$('stage').textContent=value.phase==='submitted'?(stages[value.stage]||'Unknown'):({review:'Not submitted',submitting:'Sending…',uncertain:'Outcome unknown',rejected:'Not submitted'}[value.phase]||'Unknown');
$('status').textContent=value.phase==='submitted'?(value.refreshing?'Checking the request…':'Request submitted. Status shown here is the request task, not a running swarm.'):
({review:'Ready when you are. Sending will create an unqueued research request.',submitting:'Sending the reviewed request. You can safely close this panel; its recovery record is saved before submission.',uncertain:'The response was not confirmed. Retry the saved request to recover its original result; do not create a replacement.',rejected:'The server rejected this request. Close this panel and reopen Send Notebook to prepare a fresh review.'}[value.phase]||'');
$('launch-card').hidden=value.phase!=='submitted'||!value.canLaunchReview;
const launch=value.launch;$('launch-team').textContent=launch?launch.team+' · version '+launch.team_version:'Not checked';$('launch-environment').textContent=launch?.environment||'Not checked';$('launch-limits').textContent=launch?launch.max_seconds+' seconds · up to '+launch.max_workers+' workers':'Not checked';$('launch-capacity').textContent=launch?.compute_available==null?'Unknown':launch.compute_available+' compatible slot(s) observed — not reserved';$('launch-checked').textContent=launch?.checkedAt?'Last checked '+launch.checkedAt+'; readiness can change.':'';$('launch-blockers').replaceChildren();for(const text of launch?.blockers||['Set a request-scoped launch credential, then check readiness.']){const item=document.createElement('li');item.textContent=text;$('launch-blockers').append(item);}$('launchReview').disabled=!!launch?.intent;$('launchCredential').disabled=!!launch?.intent;
$('launch-budget').textContent=launch?.budget_micro_usd==null?'Spending ceiling not available':'Spending ceiling: $'+(launch.budget_micro_usd/1000000).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:6})+' USD';
const states={not_started:'Not launched',admission_unknown:'Launch not confirmed',admitted:'Admitted; waiting for host',running:'Running',stopping:'Stopping',stopped:'Stopped',failed:'Host stopped with an error',cancelled:'Cancelled',unknown:'Status unknown',cleanup_pending:'Cleanup not confirmed'};
const cleanups={not_started:'Not started',pending:'In progress',complete:'Confirmed',unknown:'Not confirmed'};
$('launch-state').textContent=launch?'Execution: '+(states[launch.status]||'Status unknown')+(launch.cleanup?' · Cleanup: '+(cleanups[launch.cleanup]||'Not confirmed'):''):'';
if(launch?.exposure_micro_usd!=null)$('launch-budget').textContent+=' · Up to $'+(launch.exposure_micro_usd/1000000).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:6})+' USD reserved or spent; unconfirmed calls stay reserved.';
const executable=value.canLaunchControl&&launch?.ready&&!launch?.intent&&launch?.preview_id&&launch?.preview_expires_at>Date.now()/1000&&!launch?.blockers?.length;
$('launch-start').disabled=!executable;$('launchRetry').hidden=!value.canLaunchControl||!launch?.intent||!['not_started','admission_unknown','unknown'].includes(launch?.status);$('launchRetry').disabled=!(launch?.preview_expires_at>Date.now()/1000);$('launchStatus').hidden=!value.canLaunchControl||!launch?.scope_id;$('launchStatus').disabled=false;$('launchStop').hidden=!value.canLaunchControl||!launch?.scope_id||!(launch?.intent||launch?.campaign_id||['running','stopping','unknown','admission_unknown','cleanup_pending'].includes(launch?.status));$('launchStop').disabled=launch?.cleanup==='complete'||launch?.status==='cancelled';
$('results-card').hidden=value.phase!=='submitted';$('freshness').textContent=value.freshness||'Source freshness has not been checked.';
$('results-message').textContent=value.results?.message||'Refresh to retrieve linked research results.';
$('results-limit').textContent=value.results?.truncated?'This is a bounded preview. Open Taskvalet for the complete record.':'';
$('result-sections').replaceChildren();for(const item of value.results?.sections||[]){const section=document.createElement('section');const title=document.createElement('h3');title.textContent=(item.investigation||'Investigation')+' · '+item.kind.replaceAll('_',' ')+' · version '+item.version;const author=document.createElement('p');author.textContent=item.author;const body=document.createElement('pre');body.textContent=item.text+(item.truncated?'\\n[Excerpt — open Taskvalet for the complete record]':'');section.append(title,author,body);$('result-sections').append(section);}
$('markReviewed').hidden=!value.results?.hasFindings;$('markReviewed').disabled=!!value.reviewed;$('markReviewed').textContent=value.reviewed?'Reviewed locally':'Mark reviewed locally';
$('status-note').textContent=value.phase==='submitted'&&!value.canRefresh?'Live refresh is unavailable for this provider. Open the request on the destination board for its current status.':'';$('task').textContent=value.task||'';$('checked').textContent=value.checkedAt?'Last checked '+value.checkedAt:'';$('error').textContent=value.error||'';$('error').hidden=!value.error;}
$('objective').addEventListener('input',()=>{$('submit').disabled=!$('objective').value.trim();});
$('launch-start').addEventListener('click',()=>{if($('launch-start').disabled)return;$('launch-start').disabled=true;host.postMessage({action:'launchStart'});});
for(const action of ['submit','retry','refresh','open','markReviewed','launchReview','launchCredential','launchRetry','launchStatus','launchStop'])$(action).addEventListener('click',()=>{if($(action).disabled||$(action).hidden)return;if(action!=='open')$(action).disabled=true;host.postMessage({action,objective:$('objective').value});});
window.addEventListener('message',event=>{if(event.data.type==='state')render(event.data.state);});host.postMessage({action:'ready'});
</script></body></html>`;
}

/** Keep credentials and request envelopes in the extension host, outside the webview. */
export class HandoffReviewPanel {
  private readonly panel: vscode.WebviewPanel;
  closed = false;
  constructor(public state: HandoffPanelState, action: (name: HandoffAction, objective: string) => Promise<void>, onClose: () => void) {
    this.panel = vscode.window.createWebviewPanel('glitnir.handoff', 'Notebook request', vscode.ViewColumn.Beside, {enableScripts: true, retainContextWhenHidden: true, localResourceRoots: []});
    this.panel.webview.onDidReceiveMessage(async message => {
      if (this.closed || !message || typeof message.action !== 'string') return;
      if (message.action === 'ready') return this.update({});
      const name = message.action as HandoffAction;
      const allowed = (name === 'submit' && this.state.phase === 'review') || (name === 'retry' && this.state.phase === 'uncertain') || ((name === 'open' || name === 'markReviewed' || ((name === 'launchReview' || name === 'launchCredential') && this.state.canLaunchReview) || (['launchStart', 'launchRetry', 'launchStatus', 'launchStop'].includes(name) && this.state.canLaunchControl) || (name === 'refresh' && this.state.canRefresh)) && this.state.phase === 'submitted');
      if (!allowed || (name === 'refresh' && this.state.refreshing)) return;
      const objective = typeof message.objective === 'string' ? message.objective.trim() : '';
      if (name === 'submit' && (!objective || objective.length > 2000)) return;
      try { await action(name, objective); } catch (error) { this.update({refreshing: false, error: error instanceof Error ? error.message : 'Request could not be completed.'}); }
    });
    this.panel.onDidDispose(() => {this.closed = true; onClose();});
    this.panel.webview.html = handoffHtml();
  }
  update(value: Partial<HandoffPanelState>): void {
    this.state = {...this.state, ...value};
    if (!this.closed) void this.panel.webview.postMessage({type: 'state', state: this.state});
  }
  reveal(): void { this.panel.reveal(vscode.ViewColumn.Beside); }
}
