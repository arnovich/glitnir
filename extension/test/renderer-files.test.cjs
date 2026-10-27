// The page's script tags are the one list of renderer files; the webview and the
// workbench files are pinned to it here.
const assert = require('node:assert/strict');
const {existsSync, readdirSync, readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const {previewPage} = require('./webview-page.cjs');

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
    for (const item of names((text.match(/^  Object\.assign\(workbench, \{([\s\S]*?)\}\);/m) || [, ''])[1])) {
      assert.ok(!published.has(item), `${item} is published by ${published.get(item)} and ${name}`);
      published.set(item, name);
    }
  }
  for (const [name, text] of sources) {
    for (const item of names((text.match(/^  const \{([\s\S]*?)\} = workbench;/m) || [, ''])[1])) {
      assert.ok(published.has(item), `${name} takes ${item}, which no file publishes`);
      assert.ok(order(published.get(item)) < order(name), `${name} takes ${item} from ${published.get(item)}, which loads later`);
    }
    for (const [, item] of text.matchAll(/^  const [\w$]+ = \(\.\.\.a\) => workbench\.([\w$]+)\(\.\.\.a\);/gm)) {
      assert.ok(published.has(item), `${name} stubs ${item}, which no file publishes`);
      assert.ok(order(published.get(item)) > order(name), `${name} stubs ${item}, but ${published.get(item)} loads earlier`);
    }
  }
  assert.ok(published.size > 100, 'the namespace carries the shared names');
  // The lists are exact: every shared name a file uses it takes or stubs, and it takes nothing it does not use.
  // A file reaching a name it did not take would get the window's global instead (`fetch` past the editor host).
  const strip = text => text.replace(/^\s*\/\/.*$/gm, '').replace(/^  const \{[\s\S]*?\} = workbench;/m, '')
    .replace(/^  const [\w$]+ = \(\.\.\.a\) => workbench\.[\w$]+\(\.\.\.a\);$/gm, '').replace(/^  Object\.assign\(workbench, \{[\s\S]*?\}\);/m, '');
  const uses = (text, item) => new RegExp(`(?<![\\w$.'"\`-])${item.replace(/\$/g, '\\$')}(?![\\w$'"\`-])`).test(text);
  for (const [name, text] of sources) {
    const taken = new Set([...names((text.match(/^  const \{([\s\S]*?)\} = workbench;/m) || [, ''])[1]),
      ...[...text.matchAll(/^  const [\w$]+ = \(\.\.\.a\) => workbench\.([\w$]+)\(\.\.\.a\);/gm)].map(match => match[1])]);
    const body = strip(text);
    const problems = [];
    for (const [item, owner] of published) {
      if (owner === name) continue;
      const used = uses(body, item);
      if (used && !taken.has(item)) problems.push(`${name} uses ${item} without taking it from the namespace`);
      if (!used && taken.has(item)) problems.push(`${name} takes ${item} but never uses it`);
    }
    assert.deepEqual(problems, []);
  }
});

test('the webview rewrites every local tag and inserts the host scripts first', () => {
  const html = previewPage(media);
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
