const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const {previewPage} = require('./webview-page.cjs');

function renderer() {
  const dom = new JSDOM('<!doctype html>', {runScripts: 'outside-only'});
  dom.window.eval(readFileSync(path.resolve(__dirname, '../../dist/media/export.js'), 'utf8'));
  return dom;
}

test('static export retains maths, claims and images offline without workbench controls or scripts', async () => {
  const dom = renderer();
  try {
    const {window} = dom;
    const content = window.document.createElement('div');
    content.innerHTML = `<section class="prose"><h2>Whole page</h2><span class="math typeset"><span class="katex">x²</span></span>
      <figure><img src="data:image/svg+xml;base64,PHN2Zy8+"><figcaption>A figure</figcaption></figure></section>
      <section id="cell-first"><article class="claim" data-selected="true"><span class="pill" data-tone="amber">Out of date</span>
      <pre>theorem first : True := by trivial</pre><div class="uses"><button data-act="goto" data-id="second">second</button></div>
      <div class="claim-actions"><button>Replay</button>Run Lean now</div><details><summary>Proof</summary><pre>trivial</pre></details></article></section>
      <a class="use-link" href="/?notebook=Other">Other:claim</a><script>alert(1)</script>
      <img src="https://example.invalid/tracker" onerror="alert(1)"><a href="javascript:alert(1)">bad</a>`;
    const html = await window.glitnirExport({content, title: '</title><script>bad</script>', file: 'Example.lean', sha256: 'abc', css: 'body{color:black}'});
    const page = new JSDOM(html);
    try {
      const d = page.window.document;
      assert.equal(d.title, '</title><script>bad</script>');
      assert.equal(d.querySelectorAll('script,button,input,textarea,select,iframe,form,details').length, 0);
      assert.equal(d.querySelectorAll('[data-act],[onclick],[onerror]').length, 0);
      assert.ok(d.querySelector('.katex'));
      assert.match(d.body.textContent, /Whole page/);
      assert.match(d.body.textContent, /Out of date/);
      assert.match(d.body.textContent, /trivial/);
      assert.match(d.body.textContent, /Static snapshot/);
      assert.match(d.body.textContent, /abc/);
      assert.doesNotMatch(d.body.textContent, /Run Lean now/);
      assert.equal(d.querySelector('.uses a').getAttribute('href'), '#cell-second');
      assert.equal(d.querySelectorAll('a[href^="/"]').length, 0);
      assert.equal(d.querySelectorAll('a[href^="javascript:"]').length, 0);
      assert.equal(d.querySelector('img').src, 'data:image/svg+xml;base64,PHN2Zy8+');
      assert.equal(d.querySelectorAll('img').length, 1);
      assert.match(d.querySelector('meta[http-equiv]').content, /default-src 'none'/);
      assert.equal(d.querySelectorAll('link,script[src]').length, 0);
    } finally { page.window.close(); }
  } finally { dom.window.close(); }
});

test('export drops images that are not embedded data', async () => {
  const dom = renderer();
  try {
    const content = dom.window.document.createElement('div');
    content.innerHTML = '<img src="/asset?path=missing.png"><img src="assets/local.png"><p>kept</p>';
    const page = new JSDOM(await dom.window.glitnirExport({content, title: 'Test', file: 'Test.lean', sha256: 'abc', css: ''}));
    try {
      assert.equal(page.window.document.querySelectorAll('img').length, 0);
      assert.match(page.window.document.body.textContent, /kept/);
    } finally { page.window.close(); }
  } finally { dom.window.close(); }
});

test('the packaged VS Code page gives the export script a local URI and script nonce', () => {
  const html = previewPage();
  const page = new JSDOM(html);
  try {
    assert.equal(page.window.document.title, 'Glitnir');
    const sourceButton = page.window.document.getElementById('open-source');
    assert.match(sourceButton.textContent, /Edit in VS Code/);
    assert.ok(sourceButton.classList.contains('secondary'));
    assert.ok(sourceButton.closest('.bar-right'));
    assert.equal(page.window.document.getElementById('editor-state').hidden, true);
    assert.equal(page.window.document.documentElement.dataset.theme, 'light', 'notebook paper stays light even when the host prefers dark');
    assert.equal(page.window.document.querySelector('.crumbs span').textContent, 'Glitnir');
    const script = page.window.document.querySelector('script[src$="/export.js"]');
    assert.equal(script.src, 'https://resource.invalid/export.js');
    assert.match(script.nonce, /^[a-f0-9]{48}$/);
    assert.ok(page.window.document.querySelector('meta[http-equiv]').content.includes(`'nonce-${script.nonce}'`));
    assert.ok(page.window.document.getElementById('export-html'));
    const cards = page.window.document.querySelector('script[src$="/cards.js"]');
    assert.equal(cards.nonce, script.nonce);
    assert.equal(page.window.document.querySelector('script[src$="/circuits.js"]').nonce,script.nonce);
    assert.ok(page.window.document.querySelector('link[href="https://resource.invalid/circuits.css"]'));
    assert.equal(cards.src, 'https://resource.invalid/cards.js');
    assert.ok(page.window.document.querySelector('link[href="https://resource.invalid/cards.css"]'));
    assert.ok([...page.window.document.scripts].indexOf(cards) < [...page.window.document.scripts].findIndex(el => el.src.endsWith('/workbench.js')));
  } finally { page.window.close(); }
});
