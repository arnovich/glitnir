const assert = require('node:assert/strict');
const {test} = require('node:test');
const {Backend, STOP_PATHS, heldWhileDirty, validateRequest} = require('../../dist/backend');
const {readFileSync} = require('node:fs');
const path = require('node:path');

test('unsaved Lean files hold back preview actions except stopping work, in the host and the webview alike', () => {
  for (const stop of ['/api/proof/cancel', '/api/decide/stop']) assert.equal(heldWhileDirty(stop, 'POST', true), false, stop);
  for (const action of ['/api/decide/run', '/api/decide/discard', '/api/evidence/run', '/api/project/check', '/api/proof/start']) {
    assert.equal(heldWhileDirty(action, 'POST', true), true, action);
    assert.equal(heldWhileDirty(action, 'POST', false), false, action);
  }
  assert.equal(heldWhileDirty('/api/notebook', 'GET', true), false, 'reading the saved file is never held back');
  const bridge = readFileSync(path.resolve(__dirname, '../media/bridge.js'), 'utf8');
  const list = /const STOP_PATHS = (\[[^\]]*\]);/.exec(bridge);
  assert.ok(list, 'the webview bridge names its stop paths');
  assert.deepEqual(JSON.parse(list[1].replace(/'/g, '"')), [...STOP_PATHS]);
});

test('preview bridge permits saved-file checks but rejects file edits and arbitrary requests', () => {
  assert.equal(validateRequest('/api/notebook', 'GET'), '/api/notebook');
  assert.equal(validateRequest('/api/view?cell=0&line=1', 'GET'), '/api/view?cell=0&line=1');
  assert.equal(validateRequest('/api/proof/start', 'POST'), '/api/proof/start');
  assert.equal(validateRequest('/api/project/inspect', 'POST'), '/api/project/inspect');
  const check = JSON.stringify({sha256: 'a'.repeat(64)});
  assert.equal(validateRequest('/api/project/check', 'POST', check), '/api/project/check');
  assert.throws(() => validateRequest('/api/project/check', 'GET'));
  assert.throws(() => validateRequest('/api/project/check?file=other', 'POST', check));
  assert.throws(() => validateRequest('/api/project/inspect', 'GET'));
  assert.throws(() => validateRequest('/api/project/inspect?file=other', 'POST'));
  for (const url of ['http://example.org/api/notebook', '//example.org', '/api/cells/edit', '/api/runs/accept', '/api/notebook?notebook=Elsewhere', '/api/../api/notebook']) {
    assert.throws(() => validateRequest(url, 'POST'));
  }
  assert.throws(() => validateRequest('/api/notebook', 'DELETE'));
  assert.equal(validateRequest('/asset?path=assets%2Fa.svg&sha256=x', 'GET'), '/asset?path=assets%2Fa.svg&sha256=x');
  assert.throws(() => validateRequest('/asset?path=assets/a.svg&sha256=x&notebook=Other', 'GET'));
  assert.throws(() => validateRequest('/asset?path=assets/a.svg&sha256=x', 'POST'));
  // Another notebook of the project is read by its link, and only read.
  assert.equal(validateRequest('/api/linked?path=ThreeState.lean', 'GET'), '/api/linked?path=ThreeState.lean');
  assert.equal(validateRequest('/api/linked?path=more%2FOther.lean', 'GET'), '/api/linked?path=more%2FOther.lean');
  for (const [route, method] of [['/api/linked?path=A.lean&notebook=B', 'GET'], ['/api/linked?path=A.lean', 'POST'], ['/api/linked?file=A.lean', 'GET']]) {
    assert.throws(() => validateRequest(route, method), route);
  }
  // Pinned-mode routes are gone from the backend, so the bridge refuses them.
  for (const [route, method] of [['/api/goal?claim=x', 'GET'], ['/api/assets/pin', 'POST'], ['/api/replay', 'POST']]) {
    assert.throws(() => validateRequest(route, method), route);
  }
});

test('preview bridge permits a check of the saved file, or of its lines through one line', () => {
  const sha256 = 'a'.repeat(64);
  for (const body of [{sha256}, {sha256, through_line: 1}, {through_line: 12, sha256}]) {
    assert.equal(validateRequest('/api/project/check', 'POST', JSON.stringify(body)), '/api/project/check', JSON.stringify(body));
  }
  for (const body of [undefined, '', 'null', '[]', '{}', JSON.stringify({sha256: 'A'.repeat(64)}), JSON.stringify({sha256, through_line: 0}),
    JSON.stringify({sha256, through_line: -1}), JSON.stringify({sha256, through_line: 1.5}), JSON.stringify({sha256, through_line: '2'}),
    JSON.stringify({sha256, through_line: true}), JSON.stringify({sha256, through_line: null}), JSON.stringify({sha256, through_line: 2 ** 53}),
    JSON.stringify({sha256, through_line: 2, file: 'Other.lean'}), JSON.stringify({sha256, line: 2})]) {
    assert.throws(() => validateRequest('/api/project/check', 'POST', body), /Invalid request/, String(body));
  }
});

test('preview bridge permits evidence runs only as a saved digest and a directive line', () => {
  const sha256 = 'a'.repeat(64);
  const run = JSON.stringify({sha256, line: 3});
  assert.equal(validateRequest('/api/evidence/run', 'POST', run), '/api/evidence/run');
  assert.equal(validateRequest('/api/evidence/run', 'POST', JSON.stringify({line: 1, sha256})), '/api/evidence/run');
  assert.throws(() => validateRequest('/api/evidence/run', 'GET', run));
  assert.throws(() => validateRequest('/api/evidence/run?line=3', 'POST', run));
  for (const body of [undefined, '', 'null', '[]', '{}', 'not json', JSON.stringify({sha256}), JSON.stringify({line: 3}),
    JSON.stringify({sha256: 'A'.repeat(64), line: 3}), JSON.stringify({sha256: 'a'.repeat(63), line: 3}),
    JSON.stringify({sha256, line: 0}), JSON.stringify({sha256, line: 1.5}), JSON.stringify({sha256, line: '3'}),
    JSON.stringify({sha256, line: true}), JSON.stringify({sha256, line: 3, key: 'x'}), JSON.stringify({key: 'x'})]) {
    assert.throws(() => validateRequest('/api/evidence/run', 'POST', body), String(body));
  }
});

test('preview bridge permits decision runs, stops and discards only with exact bodies', () => {
  const sha256 = 'b'.repeat(64);
  const named = JSON.stringify({sha256, line: 2});
  for (const route of ['/api/decide/run', '/api/decide/discard']) {
    assert.equal(validateRequest(route, 'POST', named), route);
    assert.throws(() => validateRequest(route, 'GET', named), route);
    assert.throws(() => validateRequest(`${route}?line=2`, 'POST', named), route);
    for (const body of [undefined, '', 'null', '[]', '{}', 'not json', JSON.stringify({sha256}), JSON.stringify({line: 2}),
      JSON.stringify({sha256: 'B'.repeat(64), line: 2}), JSON.stringify({sha256, line: 0}), JSON.stringify({sha256, line: -1}),
      JSON.stringify({sha256, line: 2.5}), JSON.stringify({sha256, line: '2'}), JSON.stringify({sha256, line: 2 ** 53}),
      JSON.stringify({sha256, line: 2, key: 'x'}), JSON.stringify({sha256, line: 2, block: 'theorem t : True := trivial'})]) {
      assert.throws(() => validateRequest(route, 'POST', body), `${route} ${String(body)}`);
    }
  }
  assert.equal(validateRequest('/api/decide/stop', 'POST', '{}'), '/api/decide/stop');
  assert.throws(() => validateRequest('/api/decide/stop', 'GET', '{}'));
  for (const body of [undefined, '', 'null', '[]', 'not json', named, JSON.stringify({run: 'x'})]) {
    assert.throws(() => validateRequest('/api/decide/stop', 'POST', body), String(body));
  }
  // The editor reads a candidate's block by its key alone, exactly.
  const candidate = `/api/decide/candidate?key=${sha256}`;
  assert.equal(validateRequest(candidate, 'GET'), candidate);
  assert.throws(() => validateRequest(candidate, 'POST', '{}'));
  for (const url of ['/api/decide/candidate', '/api/decide/candidate?', '/api/decide/candidate?key=', `/api/decide/candidate?key=${'B'.repeat(64)}`,
    `/api/decide/candidate?key=${'b'.repeat(63)}`, `/api/decide/candidate?key=${sha256}&key=${sha256}`, `/api/decide/candidate?key=${sha256}&line=2`,
    `/api/decide/candidate?line=2&key=${sha256}`, `/api/decide/candidate?key=${sha256}#x`, `/api/decide/candidate?key=%62${'b'.repeat(63)}`,
    `/api/decide/candidate?key=${sha256}&`, `/api/decide/candidate?block=x&key=${sha256}`]) {
    assert.throws(() => validateRequest(url, 'GET'), url);
  }
  // Accepting writes the notebook; the backend never does, so nothing routes there.
  for (const route of ['/api/decide/accept', '/api/decide', '/api/decide/parse']) {
    assert.throws(() => validateRequest(route, 'POST', named), route);
  }
});

function fixture(mode = 'ready') {
  return new Backend({python: process.execPath, args: [path.join(__dirname, 'fake-server.cjs'), mode], cwd: __dirname, env: process.env, startupTimeoutMs: mode === 'timeout' ? 300 : 5000});
}

test('services use distinct ports, survive one another closing, and close cleanly', async () => {
  const a = fixture(), b = fixture();
  try {
    await Promise.all([a.ready, b.ready]);
    assert.notEqual(a.port, b.port);
    assert.deepEqual((await a.request('/api/notebook')).json, {claims: [], links: []});
    await a.close();
    assert.deepEqual((await b.request('/api/notebook')).json, {claims: [], links: []});
    await assert.rejects(a.request('/api/notebook'));
  } finally { await Promise.all([a.close(), b.close()]); }
});

test('startup failure, missing executable and timeout are actionable', async () => {
  for (const mode of ['error', 'timeout']) {
    const server = fixture(mode);
    await assert.rejects(server.ready, /exited|timed out/);
    await server.close();
  }
  const missing = new Backend({python: '/no/such/python', args: [], cwd: __dirname, env: process.env});
  await assert.rejects(missing.ready, /ENOENT/);
  await missing.close();
});

test('closing while starting rejects readiness and reaps the child', async () => {
  const server = fixture('timeout');
  const rejected = assert.rejects(server.ready, /closed/);
  await server.close();
  await rejected;
});

test('images come back as base64 of a known image type, and nothing else does', async () => {
  const server = fixture('asset');
  try {
    await server.ready;
    const image = await server.request('/asset?path=assets%2Fa.png&sha256=x');
    assert.deepEqual(image, {status: 200, data: Buffer.from('PNG bytes').toString('base64'), contentType: 'image/png'});
    const refused = await server.request('/asset?path=assets%2Fgone.png&sha256=x');
    assert.deepEqual(refused, {status: 409, json: {error: 'changed'}});
    await assert.rejects(server.request('/asset?path=assets%2Fpage.png&sha256=x'), /Unsupported image type/);
    // JSON routes are still parsed as JSON, never passed through as bytes.
    assert.deepEqual((await server.request('/api/notebook')).json, {claims: [], links: []});
  } finally { await server.close(); }
});

test('requests never follow redirects outside the owned server', async () => {
  const server = fixture('redirect');
  try {
    await server.ready;
    assert.equal((await server.request('/api/notebook')).status, 302);
  } finally { await server.close(); }
});


test('the backend gives each child a fresh capability and authenticates requests', async t => {
  const logs = [];
  const a = new Backend({python: process.execPath, args: [require('node:path').join(__dirname, 'fake-server.cjs'), 'auth'], cwd: __dirname, env: {...process.env, GLITNIR_SERVER_TOKEN: 'untrusted-inherited-value'}, log: value => logs.push(value)});
  const b = fixture('auth');
  t.after(() => b.close());
  t.after(() => a.close());
  assert.match(a.token, /^[a-f0-9]{64}$/);
  assert.notEqual(a.token, b.token);
  assert.equal((await b.request('/api/notebook')).status, 200);
  assert.equal((await a.request('/api/notebook')).status, 200);
  const denied = await fetch(`http://127.0.0.1:${a.port}/api/notebook`, {headers:{'X-Glitnir':'1'}});
  assert.equal(denied.status, 403);
  assert(!logs.join('').includes('untrusted-inherited-value'));
  assert(!logs.join('').includes(a.token));
});
