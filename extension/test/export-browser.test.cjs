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

test('header views and Explore controls reflow in narrow panels', {skip: !chrome}, async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'glitnir-header-browser-'));
  const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${path.join(directory, 'profile')}`, 'about:blank'], {
    detached: useProcessGroup, stdio: ['ignore', 'ignore', 'pipe'],
  });
  const closed = new Promise(resolve => child.once('close', resolve));
  let client;
  try {
    const media = path.resolve(__dirname, '../../dist/media');
    const dom = new JSDOM(readFileSync(path.join(media, 'index.html'), 'utf8'));
    const d = dom.window.document;
    d.querySelectorAll('link,script').forEach(el => el.remove());
    const style = d.createElement('style'); style.textContent = readFileSync(path.join(media, 'app.css'), 'utf8'); d.head.append(style);
    d.getElementById('panel-show').hidden = false;
    d.querySelector('.bar-right').insertAdjacentHTML('afterbegin', '<button class="secondary">Edit in VS Code ↗</button>');
    const file = path.join(directory, 'header.html'); writeFileSync(file, '<!doctype html>' + d.documentElement.outerHTML); dom.window.close();
    client = await pageClient(await devtools(child));
    await client.send('Page.enable');
    const loaded = client.event('Page.loadEventFired');
    await client.send('Page.navigate', {url: pathToFileURL(file).href}); await loaded;
    const evaluate = async expression => (await client.send('Runtime.evaluate', {expression, returnByValue: true})).result.value;
    for (const width of [1440, 1100, 1051, 1050, 900, 500, 360]) {
      await client.send('Emulation.setDeviceMetricsOverride', {width, height: 800, deviceScaleFactor: 1, mobile: false});
      for (const name of ['Demo.lean', 'AnExtremelyLongNotebookFilenameThatMustNotDisplaceTheViewSwitcher.lean']) {
        await evaluate(`document.getElementById('file-name').textContent = ${JSON.stringify(name)}`);
        const box = await evaluate(`(() => {
          const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,centre:(r.left+r.right)/2}; };
          return {header:rect('.bar'),views:rect('.view-switcher'),brand:rect('.brand'),actions:rect('.bar-right'),status:rect('.header-status'),filename:rect('#file-name'),overflow:document.documentElement.scrollWidth > innerWidth};
        })()`);
        assert.ok(Math.abs(box.views.centre - box.header.centre) < 1, `${width}: views centred for ${name}`);
        assert.equal(box.overflow, false, `${width}: no horizontal overflow`);
        assert.ok(box.filename.right - box.filename.left > 20, `${width}: filename remains visible`);
        if (width > 1050) {
          assert.ok(box.brand.right < box.views.left && box.views.right < box.actions.left, `${width}: header zones do not overlap`);
        } else {
          assert.ok(box.views.top >= Math.max(box.brand.bottom, box.actions.bottom), `${width}: views have their own row`);
        }
        assert.ok(box.status.top >= box.views.bottom, `${width}: status stays below primary navigation`);
      }
    }
    const narrow = await evaluate(`(() => {
      const mobile = document.getElementById('claim-graph-mobile');
      mobile.hidden = false;
      const graph = document.getElementById('graph-view');
      graph.hidden = false;
      graph.innerHTML = '<div class="proof-graph-layout"><aside class="proof-graph-detail"><label class="proof-graph-detail-picker">Choose a claim<select><option>Demo.first</option></select></label></aside><div class="proof-graph-stage">Graph</div></div>';
      const detail = graph.querySelector('.proof-graph-detail').getBoundingClientRect();
      const stage = graph.querySelector('.proof-graph-stage').getBoundingClientRect();
      return {
        mobile: getComputedStyle(mobile).display,
        rail: getComputedStyle(document.querySelector('.rail')).display,
        pickerFirst: detail.bottom <= stage.top,
      };
    })()`);
    assert.deepEqual(narrow, {mobile: 'block', rail: 'none', pickerFirst: true});
    for (const [width, height] of [[360, 800], [1440, 800], [1100, 240]]) {
      await client.send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
      for (const [mapWidth, mapHeight] of [[2400, 260], [260, 1800]]) {
        const fitted = await evaluate(`(() => {
          const graph = document.getElementById('graph-view');
          graph.innerHTML = '<section class="proof-graph-view"><div class="proof-graph-layout"><div class="proof-graph-stage"><div class="proof-graph-canvas" data-fit="true" data-scope="full"><svg class="proof-map" width="100%" viewBox="0 0 ${mapWidth} ${mapHeight}"><rect width="${mapWidth}" height="${mapHeight}"/></svg></div></div></div></section>';
          const canvas = graph.querySelector('.proof-graph-canvas').getBoundingClientRect();
          const map = graph.querySelector('rect').getBoundingClientRect();
          return canvas.height <= innerHeight && map.left >= canvas.left - 1 && map.right <= canvas.right + 1
            && map.top >= canvas.top - 1 && map.bottom <= canvas.bottom + 1
            && Math.min(Math.abs(canvas.width - map.width), Math.abs(canvas.height - map.height)) < 1;
        })()`);
        assert.equal(fitted, true, `${width}: ${mapWidth}×${mapHeight} graph fits both dimensions`);
      }
    }
  } finally {
    await stopBrowser(child, closed, client);
    await rm(directory, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
  }
});

test('claim graph help appears above the adjacent theorem card', {skip: !chrome}, async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'glitnir-help-browser-'));
  const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
    `--user-data-dir=${directory}`, '--remote-debugging-port=0', 'about:blank'], {
    stdio: ['ignore', 'ignore', 'pipe'], detached: useProcessGroup,
  });
  const closed = new Promise(resolve => child.once('close', resolve));
  let client;
  try {
    const css = readFileSync(path.resolve(__dirname, '../../dist/media/app.css'), 'utf8');
    const file = path.join(directory, 'help.html');
    writeFileSync(file, `<!doctype html><style>${css}</style><div class="workspace no-panel">
      <nav class="rail"><h4 class="label">Claim graph <span class="context-help">
        <button class="help-icon" aria-describedby="claim-graph-help">?</button>
        <span class="help-tooltip" id="claim-graph-help" role="tooltip" style="left:100px;top:60px" hidden>Recorded claim dependencies.</span>
      </span></h4></nav>
      <main class="document"><section class="cell"><div class="claim-head" style="height:160px">Theorem</div></section></main>
    </div>`);
    client = await pageClient(await devtools(child));
    await client.send('Page.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 800, deviceScaleFactor: 1, mobile: false});
    const loaded = client.event('Page.loadEventFired');
    await client.send('Page.navigate', {url: pathToFileURL(file).href});
    await loaded;
    const {result} = await client.send('Runtime.evaluate', {returnByValue: true, expression: `(() => {
      const tip = document.getElementById('claim-graph-help');
      const theorem = document.querySelector('.claim-head');
      const x = theorem.getBoundingClientRect().left + 12, y = 70;
      const hit = () => document.elementFromPoint(x, y);
      const before = theorem.contains(hit());
      tip.hidden = false;
      const during = tip.contains(hit());
      tip.hidden = true;
      return {before, during, after: theorem.contains(hit())};
    })()`});
    assert.deepEqual(result.value, {before: true, during: true, after: true});
  } finally {
    await stopBrowser(child, closed, client);
    await rm(directory, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
  }
});
