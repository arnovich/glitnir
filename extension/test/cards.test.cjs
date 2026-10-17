const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');

test('a new visual kind uses the shared shell without extending its renderer', () => {
  const dom = new JSDOM('', {runScripts: 'outside-only'});
  try {
    dom.window.eval(readFileSync(path.resolve(__dirname, '../../dist/media/cards.js'), 'utf8'));
    const {render, declarations} = dom.window.glitnirCards;
    dom.window.document.body.innerHTML = render({kind: 'observation', label: 'Observation', name: '<img src=x onerror=alert(1)>', signature: '<b>Type</b>',
      bodyHTML: '<p>A trusted rendered observation.</p>', sourceHTML: '<details><summary>Source</summary>Exact source</details>'});
    const card = dom.window.document.querySelector('.reading-card.card-observation');
    assert.equal(card.querySelector('dfn').textContent, '<img src=x onerror=alert(1)>');
    assert.equal(card.querySelector('img,b'), null);
    assert.equal(card.querySelector('.card-signature').textContent, '<b>Type</b>');
    assert.equal(card.querySelector('.card-body p').textContent, 'A trusted rendered observation.');
    assert.ok(card.querySelector('.card-source details'));
    assert.equal(declarations.example.kind, 'example');
    assert.equal(declarations.axiom.label, 'Assumption');
    assert.ok(Object.isFrozen(declarations.def));
    dom.window.document.body.innerHTML = render({kind: '" onclick="alert(1)', label: '<b>unsafe label</b>', classes: ['" onclick="alert(1)']});
    assert.equal(dom.window.document.querySelector('[onclick],b'), null);
    assert.ok(dom.window.document.querySelector('.card-context'));
  } finally { dom.window.close(); }
});
