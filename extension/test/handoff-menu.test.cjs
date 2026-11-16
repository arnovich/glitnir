const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {JSDOM} = require('jsdom');

test('More offers Taskvalet only after host discovery and sends no source or credentials', () => {
  const dom = new JSDOM('<details id="notebook-view-options" open><div class="view-options-panel"></div></details>', {runScripts: 'outside-only'});
  const messages = [];
  dom.window.acquireVsCodeApi = () => ({postMessage: message => messages.push(message)});
  dom.window.eval(readFileSync(path.resolve(__dirname, '../media/bridge.js'), 'utf8'));
  const menu = dom.window.document.querySelector('.view-options-panel');
  assert.equal(menu.querySelector('#send-taskvalet'), null);
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', {data: {type: 'handoffAvailable', taskvalet: true}}));
  const button = menu.querySelector('#send-taskvalet');
  assert.ok(button);
  assert.equal(button.textContent, 'Send to Taskvalet…');
  dom.window.document.body.classList.add('glitnir-linked-view');
  button.click();
  assert.equal(messages.filter(message => message.type === 'sendNotebook').length, 0);
  dom.window.document.body.classList.remove('glitnir-linked-view');
  button.click();
  assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), {type: 'sendNotebook'});
  assert.equal(dom.window.document.getElementById('notebook-view-options').open, false);
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', {data: {type: 'handoffAvailable', taskvalet: false}}));
  assert.equal(menu.querySelector('#send-taskvalet'), null);
  dom.window.close();
});
