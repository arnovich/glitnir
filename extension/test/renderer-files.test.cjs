// The page's script tags are the one list of renderer files; the webview and the
// workbench files are pinned to it here.
const assert = require('node:assert/strict');
const {existsSync, readdirSync, readFileSync} = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');

const media = path.resolve(__dirname, '../../dist/media');
const app = path.resolve(__dirname, '../../backend/src/glitnir/preview/app');
const WORKBENCH = ['workbench.js', 'figures.js', 'reading.js', 'inspector.js', 'chrome.js'];

function pageFiles() {
  const html = readFileSync(path.join(media, 'index.html'), 'utf8');
  return {
    scripts: [...html.matchAll(/<script src="\/([^"]+)"><\/script>/g)].map(m => m[1]),
    styles: [...html.matchAll(/<link rel="stylesheet" href="\/([^"]+)">/g)].map(m => m[1]),
  };
}

test('the page lists every workbench script and names only files that ship', () => {
  const {scripts, styles} = pageFiles();
  for (const name of [...scripts, ...styles]) assert.ok(existsSync(path.join(media, name)), `${name} is listed but does not ship`);
  for (const name of readdirSync(app).filter(name => name.endsWith('.js'))) assert.ok(scripts.includes(name), `${name} ships but the page does not load it`);
  assert.deepEqual(scripts.slice(-WORKBENCH.length), WORKBENCH, 'the workbench files load last, in order');
});

test('the files share names only through the namespace', () => {
  const sources = WORKBENCH.map(name => [name, readFileSync(path.join(app, name), 'utf8')]);
  const names = list => list.split(',').map(item => item.trim().split(':')[0].trim()).filter(Boolean);
  const order = name => WORKBENCH.indexOf(name);
  const published = new Map();
  for (const [name, text] of sources) {
    for (const item of names((text.match(/^  Object\.assign\(W, \{([\s\S]*?)\}\);/m) || [, ''])[1])) {
      assert.ok(!published.has(item), `${item} is published by ${published.get(item)} and ${name}`);
      published.set(item, name);
    }
  }
  for (const [name, text] of sources) {
    for (const item of names((text.match(/^  const \{([\s\S]*?)\} = W;/m) || [, ''])[1])) {
      assert.ok(published.has(item), `${name} takes ${item}, which no file publishes`);
      assert.ok(order(published.get(item)) < order(name), `${name} takes ${item} from ${published.get(item)}, which loads later`);
    }
    for (const [, item] of text.matchAll(/^  const [\w$]+ = \(\.\.\.a\) => W\.([\w$]+)\(\.\.\.a\);/gm)) {
      assert.ok(published.has(item), `${name} stubs ${item}, which no file publishes`);
      assert.ok(order(published.get(item)) > order(name), `${name} stubs ${item}, but ${published.get(item)} loads earlier`);
    }
  }
  assert.ok(published.size > 100, 'the namespace carries the shared names');
});

test('the webview rewrites every local tag and inserts the host scripts first', () => {
  const file = fsPath => ({fsPath});
  const load = Module._load;
  let previewHtml;
  Module._load = function (name, parent, ...rest) {
    if (name === 'vscode') return {Uri: {joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts))}};
    return load.call(this, name, parent, ...rest);
  };
  try { ({previewHtml} = require('../../dist/webview')); } finally { Module._load = load; }
  const html = previewHtml({cspSource: 'https://resource.invalid', asWebviewUri: uri => ({toString: () => 'https://resource.invalid/' + path.basename(uri.fsPath)})}, file(media));
  assert.doesNotMatch(html, /(src|href)="\//, 'a local reference survived the rewrite');
  assert.ok(!html.includes('<!-- host scripts -->'), 'the marker is replaced by the host scripts');
  const {window} = new JSDOM(html);
  try {
    const scripts = [...window.document.scripts];
    const nonces = new Set(scripts.map(script => script.nonce));
    assert.equal(nonces.size, 1, 'every script carries the one nonce');
    assert.match([...nonces][0], /^[a-f0-9]{48}$/);
    assert.deepEqual(scripts.map(script => path.basename(script.src)), ['katex.min.js', 'export.js', 'circuits.js', 'cards.js', 'bridge.js', 'review.js', ...WORKBENCH]);
  } finally { window.close(); }
});
