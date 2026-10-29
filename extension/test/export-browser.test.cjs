const assert = require('node:assert/strict');
const {existsSync, mkdtempSync, readFileSync, writeFileSync} = require('node:fs');
const {rm} = require('node:fs/promises');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');

const candidates = process.platform === 'darwin'
  ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
  : ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'];
const chrome = process.env.GLITNIR_TEST_CHROME || candidates.find(existsSync);
const useProcessGroup = process.platform === 'linux';

function devtools(child) {
  return new Promise((resolve, reject) => {
    let stderr = '';
    const timeout = setTimeout(() => reject(new Error(`Chrome did not expose DevTools: ${stderr}`)), 30000);
    child.stderr.on('data', chunk => {
      stderr += chunk;
      const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) { clearTimeout(timeout); resolve(match[1]); }
    });
    child.once('exit', code => { clearTimeout(timeout); reject(new Error(`Chrome exited before DevTools was ready (${code}): ${stderr}`)); });
    child.once('error', error => { clearTimeout(timeout); reject(error); });
  });
}

function within(promise, timeoutMs) {
  let timer;
  return Promise.race([
    promise.then(() => true, () => false),
    new Promise(resolve => { timer = setTimeout(() => resolve(false), timeoutMs); }),
  ]).finally(() => clearTimeout(timer));
}

function browserAlive(child) {
  if (!Number.isInteger(child.pid)) return false;
  if (!useProcessGroup) return child.exitCode === null && child.signalCode === null;
  try {
    process.kill(-child.pid, 0);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    throw error;
  }
}

function signalBrowser(child, signal) {
  if (!browserAlive(child)) return;
  try {
    if (!useProcessGroup) child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
}

async function waitForBrowserShutdown(child, closed, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (browserAlive(child) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  if (browserAlive(child)) return false;
  return within(closed, Math.max(0, deadline - Date.now()));
}

async function stopBrowser(child, closed, client) {
  if (client) await within(client.send('Browser.close'), 1000);
  client?.socket.close();
  if (!Number.isInteger(child.pid)) return;
  if (await waitForBrowserShutdown(child, closed, 2000)) return;
  signalBrowser(child, 'SIGTERM');
  if (await waitForBrowserShutdown(child, closed, 2000)) return;
  signalBrowser(child, 'SIGKILL');
  if (await waitForBrowserShutdown(child, closed, 2000)) return;
  throw new Error(`Chrome process group ${child.pid} did not close after SIGKILL`);
}

async function pageClient(browserURL) {
  const {hostname, port} = new URL(browserURL);
  const target = await fetch(`http://${hostname}:${port}/json/new?about:blank`, {method: 'PUT'}).then(response => response.json());
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  let sequence = 0;
  const pending = new Map();
  const waiting = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id);
      if (item) { pending.delete(message.id); message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result); }
    } else {
      const listeners = waiting.get(message.method) || [];
      waiting.delete(message.method);
      listeners.forEach(resolve => resolve(message.params));
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
  const event = method => new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Timed out waiting for ${method}`)), 15000);
    const listeners = waiting.get(method) || [];
    listeners.push(value => { clearTimeout(timeout); resolve(value); });
    waiting.set(method, listeners);
  });
  return {socket, send, event};
}

test('offline export switches views and follows source and sibling fragments in a browser', {skip: !chrome}, async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'glitnir-export-browser-'));
  const profile = path.join(directory, 'profile');
  const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], {
    detached: useProcessGroup,
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  const closed = new Promise(resolve => child.once('close', resolve));
  let client;
  let testError;
  try {
    const browserURL = await devtools(child);
    const dom = new JSDOM('<!doctype html><html><body></body></html>', {runScripts: 'outside-only'});
    const exportScript = readFileSync(path.resolve(__dirname, '../../backend/src/glitnir/preview/app/export.js'), 'utf8');
    dom.window.eval(exportScript);
    const content = dom.window.document.createElement('div');
    content.innerHTML = '<section id="cell-first"><p>DOCUMENT_ONLY_MARKER</p><a class="lines" href="#source-L2" data-act="source-lines">L2</a></section>'
      + '<a class="use-link" data-claim="Other.claim" href="/?notebook=Other">Other claim</a>'
      + '<a class="notebook-reference" href="/?reference=Missing">Missing notebook</a>';
    const source = dom.window.document.createElement('section');
    source.innerHTML = '<p class="source-note">SOURCE_ONLY_MARKER</p><div class="source"><table><tr id="source-L2"><td class="ln"><a href="#cell-first">2</a></td><td>theorem first : True</td></tr></table></div>';
    const css = readFileSync(path.resolve(__dirname, '../../backend/src/glitnir/preview/app/export.css'), 'utf8');
    const html = await dom.window.glitnirExport({content, source, title: 'Offline export', file: 'Root.lean', sha256: 'a'.repeat(64), css, availableExports: ['Other']});
    dom.window.close();
    const root = path.join(directory, 'Root.html');
    writeFileSync(root, html);
    writeFileSync(path.join(directory, 'Other.html'), '<!doctype html><html><body><main id="cell-Other.claim">SIBLING_CLAIM_MARKER</main></body></html>');

    client = await pageClient(browserURL);
    const requests = [];
    await client.send('Network.enable');
    await client.send('Network.setBlockedURLs', {urls: ['http://*', 'https://*']});
    await client.send('Page.enable');
    client.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Network.requestWillBeSent') requests.push(message.params.request.url);
    });
    const evaluate = async expression => (await client.send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true})).result.value;
    const loaded = client.event('Page.loadEventFired');
    await client.send('Page.navigate', {url: pathToFileURL(root).href});
    await loaded;
    assert.deepEqual(await evaluate(`(() => {
      const source = document.querySelector('.source-pane'), documentPane = document.querySelector('.document-pane');
      return [getComputedStyle(documentPane).display, getComputedStyle(source).display, location.hash];
    })()`), ['block', 'none', '']);
    await evaluate(`document.querySelector('.export-tabs a[href="#source-view"]').click()`);
    assert.deepEqual(await evaluate(`[getComputedStyle(document.querySelector('.document-pane')).display, getComputedStyle(document.querySelector('.source-pane')).display, location.hash]`), ['none', 'block', '#source-view']);
    await evaluate(`document.querySelector('#source-L2 a').click()`);
    assert.deepEqual(await evaluate(`[getComputedStyle(document.querySelector('.document-pane')).display, getComputedStyle(document.querySelector('.source-pane')).display, location.hash]`), ['block', 'none', '#cell-first']);
    await evaluate(`document.querySelector('.lines').click()`);
    assert.deepEqual(await evaluate(`[getComputedStyle(document.querySelector('.document-pane')).display, getComputedStyle(document.querySelector('.source-pane')).display, location.hash]`), ['none', 'block', '#source-L2']);
    assert.equal(await evaluate(`document.querySelector('.notebook-reference').tagName`), 'SPAN');
    const siblingLoaded = client.event('Page.loadEventFired');
    await evaluate(`document.querySelector('.use-link').click()`);
    await siblingLoaded;
    assert.equal(await evaluate(`document.querySelector(':target').textContent`), 'SIBLING_CLAIM_MARKER');
    assert.ok(requests.every(url => url.startsWith('file:')), requests.join('\n'));
  } catch (error) {
    testError = error;
  }
  let cleanupError;
  try {
    await stopBrowser(child, closed, client);
    await rm(directory, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
  } catch (error) {
    cleanupError = error;
  }
  if (testError && cleanupError) throw new AggregateError([testError, cleanupError], 'Browser export test and cleanup failed');
  if (testError) throw testError;
  if (cleanupError) throw cleanupError;
});
