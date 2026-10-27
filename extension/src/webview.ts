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
  // Every stylesheet and script the page lists, and the host's own scripts at the page's marker.
  const script = (name: string) => `<script nonce="${nonce}" src="${resource(name)}"></script>`;
  html = html.replace(/<link rel="stylesheet" href="\/([^"]+)">/g, (_tag, name: string) => `<link rel="stylesheet" href="${resource(name)}">`);
  html = html.replace(/<script src="\/([^"]+)"><\/script>/g, (_tag, name: string) => script(name));
  html = html.replace('<!-- host scripts -->', script('bridge.js') + script('review.js'));
  html = html.replace('</head>', `<link rel="stylesheet" href="${resource('preview.css')}"></head>`);
  html = html.replace('<body>', '<body class="glitnir-preview"><div id="editor-state" role="status" hidden></div>');
  html = html.replace('<div class="bar-right">', `<div class="bar-right">
      <button id="open-source" class="secondary" type="button" title="Edit the selected section in the VS Code source editor">Edit in VS Code <svg class="ic" aria-hidden="true" viewBox="0 0 24 24"><path d="M14 3h7v7M21 3l-10 10M10 3H3v18h18v-7"/></svg></button>
      <button id="next-unresolved" class="secondary" type="button" hidden>Next unresolved <span aria-hidden="true">↓</span></button>`);
  return html;
}
