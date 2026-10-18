const assert = require('node:assert/strict');
const {test} = require('node:test');
const {redirectTarget} = require('../../dist/runtime-download');

test('download redirects only permit credential-free HTTPS targets', () => {
  const source = new URL('https://api.github.com/asset');
  assert.equal(redirectTarget('https://release-assets.githubusercontent.com/file', source).hostname, 'release-assets.githubusercontent.com');
  for (const target of ['http://example.com/file', 'https://secret@example.com/file', 'file:///tmp/file']) assert.throws(() => redirectTarget(target, source), /unsafe/);
});

const https = require('node:https');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {downloadRuntime} = require('../../dist/runtime-download');

async function downloads(t, replies, task) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-download-'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  const original = https.get, requests = [];
  https.get = (url, options, callback) => {
    requests.push({url: String(url), headers: options.headers});
    const request = new EventEmitter();
    request.setTimeout = () => request;
    request.destroy = error => { if (error) request.emit('error', error); return request; };
    const reply = replies.shift();
    process.nextTick(() => {
      if (!reply) return request.emit('error', new Error('Unexpected extra request'));
      if (reply.error) return request.emit('error', new Error(reply.error));
      const response = new PassThrough(); response.statusCode = reply.status ?? 200; response.headers = reply.headers ?? {};
      callback(response);
      setImmediate(() => response.end(reply.body ?? ''));
    });
    return request;
  };
  try { await task(path.join(directory, 'runtime.zip'), requests); }
  finally { https.get = original; }
}

test('release downloads are anonymous and sign in only at GitHub\'s rate limit, dropping credentials at redirect', async t => {
  const d = {archive: {size: 4}, source: {repository: 'owner/public', tag: 'runtime-v1', asset: 'runtime.zip'}};
  const limited = {status: 403, headers: {'x-ratelimit-remaining': '0'}};
  let asked = 0;
  await downloads(t, [
    limited,
    {body: JSON.stringify({assets: [{id: 123, name: 'runtime.zip', size: 4}]})},
    {status: 302, headers: {location: 'https://release-assets.githubusercontent.com/file?signature=private'}},
    {headers: {'content-length': '4'}, body: 'data'},
  ], async (file, requests) => {
    await downloadRuntime(file, d, {signal: new AbortController().signal, githubToken: async () => { asked++; return 'secret'; }});
    assert.equal(await fs.readFile(file, 'utf8'), 'data');
    assert.equal(asked, 1);
    assert.equal(requests[0].headers.Authorization, undefined, 'the first request is anonymous');
    assert.equal(requests[1].headers.Authorization, 'Bearer secret');
    assert.equal(requests[2].headers.Authorization, 'Bearer secret');
    assert.equal(requests[3].headers.Authorization, undefined, 'credentials never follow a redirect');
    assert.equal(requests[2].url, 'https://api.github.com/repos/owner/public/releases/assets/123');
  });
  for (const reply of [{status: 404}, {status: 403}, {status: 401}]) {
    await downloads(t, [reply], async file => {
      await assert.rejects(downloadRuntime(file, d, {signal: new AbortController().signal, githubToken: async () => { asked++; return 'secret'; }}),
        reply.status === 404 ? /runtime-v1 is not published in owner\/public \(HTTP 404\)/ : /unavailable \(HTTP 40[13]\)/);
    });
  }
  assert.equal(asked, 1, 'a missing release or another refusal never asks for sign-in');
  await downloads(t, [limited], async file => {
    await assert.rejects(downloadRuntime(file, d, {signal: new AbortController().signal}), /anonymous rate limit/);
  });
  await downloads(t, [limited, limited], async file => {
    await assert.rejects(downloadRuntime(file, d, {signal: new AbortController().signal, githubToken: async () => 'secret'}), /rate limit even when signed in/);
  });
});

test('downloads reject oversized/truncated bodies, incorrect metadata and redirect loops without leaking URLs', async t => {
  const d = {archive: {size: 4}, source: {url: 'https://example.com/runtime.zip'}};
  for (const reply of [{body: 'longer'}, {body: 'x'}, {headers: {'content-length': '12'}}, {error: 'request https://example.com/?token=secret failed'}]) {
    await downloads(t, [reply], async file => {
      await assert.rejects(downloadRuntime(file, d, {signal: new AbortController().signal}), error => !error.message.includes('secret') && /runtime download/i.test(error.message)
        && /Choose downloaded backend wheel/.test(error.message) && !/offline archive/.test(error.message));
    });
  }
  await downloads(t, Array.from({length: 6}, () => ({status: 302, headers: {location: '/again'}})), async file => {
    await assert.rejects(downloadRuntime(file, d, {signal: new AbortController().signal}), /redirect limits/);
  });
  await downloads(t, [{body: JSON.stringify({assets: [{id: 123, name: 'runtime.zip', size: 5}]})}], async file => {
    await assert.rejects(downloadRuntime(file, {...d, source: {repository: 'owner/repo', tag: 'v1', asset: 'runtime.zip'}}, {signal: new AbortController().signal}), /unexpected size.*Choose downloaded backend wheel/);
  });
});

test('cancelling while GitHub sign-in is pending does not wait for the dialog', async t => {
  const controller = new AbortController();
  await downloads(t, [{status: 403, headers: {'x-ratelimit-remaining': '0'}}], async file => {
    const request = downloadRuntime(file, {archive: {size: 4}, source: {repository: 'owner/public', tag: 'v1', asset: 'runtime.zip'}}, {
      signal: controller.signal,
      githubToken: () => { setImmediate(() => controller.abort()); return new Promise(() => {}); },
    });
    await assert.rejects(request, /abort/i);
  });
});

test('release metadata failures point at the downloaded-wheel setup path', async t => {
  await downloads(t, [{status: 500}], async file => {
    await assert.rejects(downloadRuntime(file, {archive: {size: 4}, source: {repository: 'owner/repo', tag: 'v1', asset: 'glitnir.whl'}}, {signal: new AbortController().signal}),
      /HTTP 500.*Set Up Environment → More Options… → Choose downloaded backend wheel/);
  });
});
