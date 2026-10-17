const assert = require('node:assert/strict');
const {test} = require('node:test');
const {spawnSync} = require('node:child_process');
const {mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync} = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {Backend} = require('../../dist/backend');

test('installed wheel serves a notebook and offline fonts outside any source checkout', async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'glitnir-installed-'));
  const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';
  const notebook = path.join(root, 'Example.lean');
  writeFileSync(notebook, '/-!\n# Installed runtime\n-/\n\ntheorem installed_example : True := by\n  trivial\n\n-- @evidence nosuchverb model\n-- @decide example : nosuchkind goal\n--   continued\n');
  const installed = spawnSync(python, ['-I', '-c',
    'from pathlib import Path; import sys; import glitnir.notebook as n; p=Path(n.__file__).resolve(); assert p.is_relative_to(Path(sys.prefix).resolve()), p'],
  {cwd: root, encoding: 'utf8', timeout: 30000});
  assert.equal(installed.status, 0, installed.stdout + installed.stderr);
  const log = [];
  const backend = new Backend({
    python, args: ['-I', '-u', '-m', 'glitnir.notebook', 'serve', notebook, '--port', '0'],
    cwd: root, env: process.env, log: line => log.push(line), startupTimeoutMs: 30000,
  });
  try {
    await backend.ready.catch(error => { throw new Error(error.message + '\n' + log.join('')); });
    const reply = await backend.request('/api/notebook');
    assert.equal(reply.status, 200);
    assert.equal(reply.json.error, undefined);
    assert.equal(reply.json.preview.mode, 'project');
    assert.equal(reply.json.cells[0].kind, 'prose');
    assert.deepEqual(reply.json.claims, []);
    assert.equal(reply.json.simulations, undefined);
    // Installed plugins may add evidence verbs and decision kinds; none of
    // them serves these.
    assert.ok(Array.isArray(reply.json.directive_providers.evidence));
    assert.ok(!reply.json.directive_providers.evidence.includes('nosuchverb'));
    assert.ok(Array.isArray(reply.json.directive_providers.decide));
    assert.ok(!reply.json.directive_providers.decide.includes('nosuchkind'));
    assert.deepEqual(reply.json.decisions.map(card => [card.line, card.end_line, card.name, card.kind, card.goal, card.provider, card.status, card.error]),
      [[9, 10, 'example', 'nosuchkind', 'nosuchkind goal continued', null, 'never', 'No installed provider decides `nosuchkind` goals.']]);
    // The editor reads only a current candidate's block; nothing is saved here.
    assert.deepEqual(await backend.request(`/api/decide/candidate?key=${reply.json.decisions[0].key}`),
      {status: 409, json: {error: 'This decision has no saved result; run it first.'}});
    assert.deepEqual(await backend.request(`/api/decide/candidate?key=${'0'.repeat(64)}`),
      {status: 409, json: {error: 'This decision is no longer in the saved notebook.'}});
    assert.deepEqual(reply.json.evidence.map(card => [card.line, card.verb, card.target, card.provider, card.status, card.error]),
      [[8, 'nosuchverb', 'model', null, 'never', 'No installed provider runs `@evidence nosuchverb`.']]);
    const styles = await backend.request('/api/export/styles');
    assert.equal(styles.status, 200);
    assert.match(styles.json.css, /data:font\/woff2;base64,/);
    assert.match(styles.json.css, /KaTeX/);
    const unauthenticated = await fetch(`http://127.0.0.1:${backend.port}/api/notebook`);
    assert.equal(unauthenticated.status, 403);
    const source = readFileSync(notebook, 'utf8');
    for (const route of ['/', '/app.js', '/api/decide/catalog']) {
      const response = await fetch(`http://127.0.0.1:${backend.port}${route}`, {
        headers: {'X-Glitnir': '1'},
      });
      assert.equal(response.status, 403, route);
      await response.arrayBuffer();
    }
    for (const route of ['/api/cells/edit', '/api/cells/insert', '/api/cells/remove',
      '/api/assets/pin', '/api/decide', '/api/decide/parse', '/api/decide/stop',
      '/api/decide/accept', '/api/decide/discard', '/api/decide/candidate']) {
      const response = await fetch(`http://127.0.0.1:${backend.port}${route}`, {
        method: 'POST', headers: {'X-Glitnir': '1', 'Content-Type': 'application/json'},
        body: JSON.stringify({index: 0, source: '/-! overwrite -/'}),
      });
      assert.equal(response.status, 403, route);
      await response.arrayBuffer();
    }
    assert.equal(readFileSync(notebook, 'utf8'), source);
    assert.equal(existsSync(path.join(root, 'Example.forseti.json')), false);
  } finally {
    await backend.close();
    rmSync(root, {recursive: true, force: true});
  }
});

test('installed wheel serves prose images only at their announced hash', async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'glitnir-images-'));
  const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';
  const notebook = path.join(root, 'Pictured.lean');
  writeFileSync(notebook, '/-!\n# Pictured\n\n![Sketch](assets/sketch.svg)\n\n![Gone](assets/gone.png)\n-/\n');
  mkdirSync(path.join(root, 'assets'));
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"/>';
  writeFileSync(path.join(root, 'assets', 'sketch.svg'), svg);
  const log = [];
  const backend = new Backend({
    python, args: ['-I', '-u', '-m', 'glitnir.notebook', 'serve', notebook, '--port', '0'],
    cwd: root, env: process.env, log: line => log.push(line), startupTimeoutMs: 30000,
  });
  try {
    await backend.ready.catch(error => { throw new Error(error.message + '\n' + log.join('')); });
    const {json} = await backend.request('/api/notebook');
    const [sketch, gone] = json.assets;
    assert.equal(sketch.status, 'ok');
    assert.equal(gone.status, 'missing');
    const image = await backend.request(`/asset?path=${encodeURIComponent(sketch.path)}&sha256=${sketch.sha256}`);
    assert.equal(image.status, 200);
    assert.equal(image.contentType, 'image/svg+xml');
    assert.equal(Buffer.from(image.data, 'base64').toString(), svg);
    const changed = await backend.request(`/asset?path=${encodeURIComponent(sketch.path)}&sha256=${'0'.repeat(64)}`);
    assert.equal(changed.status, 409);
    assert.equal(changed.data, undefined);
    const direct = await fetch(`http://127.0.0.1:${backend.port}/asset?path=${encodeURIComponent(sketch.path)}&sha256=${sketch.sha256}`);
    assert.equal(direct.status, 403);
    await direct.arrayBuffer();
  } finally {
    await backend.close();
    rmSync(root, {recursive: true, force: true});
  }
});

test('ordinary Lean source opens without Lean tools and offers no pinned checking routes', async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'glitnir-project-'));
  const python = process.env.GLITNIR_BUILD_PYTHON;
  assert.ok(python, 'installed backend interpreter is required');
  const notebook = path.join(root, 'Term.lean');
  const source = '/-! # Ordinary Lean -/\n\nnamespace Example\n  theorem term : True := True.intro\nend Example\n';
  writeFileSync(notebook, source);
  writeFileSync(path.join(root, 'lakefile.lean'), 'PANIC if executed');
  writeFileSync(path.join(root, 'Term.forseti.json'), 'malformed proof metadata');
  const log = [];
  const backend = new Backend({python, args: ['-I', '-u', '-m', 'glitnir.notebook', 'serve', notebook, '--port', '0'], cwd: root,
    env: {...process.env, PATH: root, FORSETI_LEAN_PROJECT: '/does-not-exist'}, log: line => log.push(line)});
  try {
    await backend.ready.catch(error => { throw new Error(error.message + log.join('')); });
    const {json: snapshot, status} = await backend.request('/api/notebook');
    assert.equal(status, 200);
    assert.equal(snapshot.preview.mode, 'project');
    assert.equal(snapshot.preview.project.root, root);
    assert.equal(snapshot.preview.project.kind, 'lake');
    assert.deepEqual(snapshot.claims, []);
    assert.equal(snapshot.cells.map(c => c.text).join(''), source);
    const {sourceCell} = require('../../dist/navigation');
    assert.deepEqual(sourceCell(snapshot, source, 0, snapshot.sha256), {start: 1, end: 5});
    for (const route of ['/api/goal?claim=term', '/api/replay']) {
      for (const method of ['GET', 'POST']) {
        const response = await fetch(`http://127.0.0.1:${backend.port}${route}`, {
          method, headers: {'X-Glitnir': backend.token, 'Content-Type': 'application/json'}, body: method === 'POST' ? '{}' : undefined,
        });
        assert.equal(response.status, 404, route);
        await response.arrayBuffer();
      }
    }
    // Evidence runs exist in project mode; a line without a directive is refused.
    const run = await backend.request('/api/evidence/run', 'POST', JSON.stringify({sha256: snapshot.sha256, line: 1}));
    assert.deepEqual(run, {status: 409, json: {error: 'No evidence directive at line 1.'}});
    const unrouted = await fetch(`http://127.0.0.1:${backend.port}/api/evidence/run`, {headers: {'X-Glitnir': backend.token}});
    assert.equal(unrouted.status, 404);
    await unrouted.arrayBuffer();
    // So do decision runs, stops and discards; each refuses without a decision.
    const decide = await backend.request('/api/decide/run', 'POST', JSON.stringify({sha256: snapshot.sha256, line: 1}));
    assert.deepEqual(decide, {status: 409, json: {error: 'No decision directive at line 1.'}});
    const discard = await backend.request('/api/decide/discard', 'POST', JSON.stringify({sha256: snapshot.sha256, line: 1}));
    assert.deepEqual(discard, {status: 409, json: {error: 'No decision directive at line 1.'}});
    assert.deepEqual(await backend.request('/api/decide/stop', 'POST', '{}'), {status: 409, json: {error: 'No decision is running.'}});
    // Views are drawn in project mode; an unlisted directive is simply absent.
    assert.equal((await backend.request('/api/view?cell=0&line=1')).status, 404);
    const inspection = await backend.request('/api/project/inspect', 'POST', JSON.stringify({sha256: snapshot.sha256}));
    assert.equal(inspection.status, 200);
    assert.equal(inspection.json.status, 'unavailable');
    assert.match(inspection.json.message, /Prepare/);
    assert.equal((await backend.request('/api/export/styles')).status, 200);
    assert.equal(readFileSync(notebook, 'utf8'), source);
    assert.equal(readFileSync(path.join(root, 'Term.forseti.json'), 'utf8'), 'malformed proof metadata');
  } finally { await backend.close(); rmSync(root, {recursive: true, force: true}); }
});
