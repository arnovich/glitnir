import {randomBytes} from 'node:crypto';
import {readFileSync} from 'node:fs';
import * as vscode from 'vscode';

/** Reuse the packaged browser renderer with local resources and a narrow CSP. */
export function previewHtml(webview: vscode.Webview, media: vscode.Uri): string {
  const nonce = randomBytes(24).toString('hex');
  const resource = (name: string) => webview.asWebviewUri(vscode.Uri.joinPath(media, name)).toString();
  let html = readFileSync(vscode.Uri.joinPath(media, 'index.html').fsPath, 'utf8');
  html = html.replace('<html lang="en">', '<html lang="en" data-theme="light">');
  html = html.replace(/<link[^>]+href="https:[^>]+>\n/g, '');
  html = html.replace('<meta charset="utf-8">', `<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; font-src ${webview.cspSource};">`);
  html = html.replace('href="/cards.css"', `href="${resource('cards.css')}"`);
  html = html.replace('href="/circuits.css"', `href="${resource('circuits.css')}"`);
  html = html.replace('<script src="/circuits.js"></script>', `<script nonce="${nonce}" src="${resource('circuits.js')}"></script>`);
  html = html.replace('<script src="/cards.js"></script>', `<script nonce="${nonce}" src="${resource('cards.js')}"></script>`);
  html = html.replace('href="/app.css"', `href="${resource('app.css')}"`);
  html = html.replace('href="/katex/katex.min.css"', `href="${resource('katex/katex.min.css')}"`);
  html = html.replace('<script src="/katex/katex.min.js"></script>', `<script nonce="${nonce}" src="${resource('katex/katex.min.js')}"></script>`);
  html = html.replace('<script src="/export.js"></script>', `<script nonce="${nonce}" src="${resource('export.js')}"></script>`);
  html = html.replace('</head>', `<link rel="stylesheet" href="${resource('preview.css')}"></head>`);
  html = html.replace('<body>', '<body class="glitnir-preview"><div id="editor-state" role="status" hidden></div>');
  html = html.replace('<div class="bar-right">', `<div class="bar-right">
      <button id="open-source" class="secondary" type="button" title="Edit the selected section in the VS Code source editor">Edit in VS Code <svg class="ic" aria-hidden="true" viewBox="0 0 24 24"><path d="M14 3h7v7M21 3l-10 10M10 3H3v18h18v-7"/></svg></button>
      <button id="next-unresolved" class="secondary" type="button" hidden>Next unresolved <span aria-hidden="true">↓</span></button>`);
  html = html.replace("Edit the selected claim's cell", 'Open the Lean source in VS Code');
  html = html.replace(/<dt><kbd>Ctrl<\/kbd>\+<kbd>Enter<\/kbd><\/dt><dd>Save the cell you are editing<\/dd>/, '');
  html = html.replace('<script src="/app.js"></script>', `<script nonce="${nonce}" src="${resource('bridge.js')}"></script><script nonce="${nonce}" src="${resource('review.js')}"></script><script nonce="${nonce}" src="${resource('app.js')}"></script>`);
  return html;
}
