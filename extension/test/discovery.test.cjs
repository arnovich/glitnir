const assert = require('node:assert/strict');
const path = require('node:path');
const {test} = require('node:test');
const {discover, DiscoveryError, probePython} = require('../../dist/discovery');

const input = {cwd: '/work/research', notebook: '/work/research/examples/A.lean', roots: ['/work/research']};
function probe(goodPython, calls = []) {
  return async (...args) => {
    calls.push(args);
    if (args.length !== 1) throw new Error('discovery probes only Python');
    if (args[0] !== goodPython) throw new Error('Glitnir is not installed');
    return {python: args[0], sourceRoot: '/work/glitnir'};
  };
}
test('discovers a virtual environment with the backend and probes only Python', async () => {
  const calls = [];
  const found = await discover(input, probe('/work/research/.venv/bin/python', calls));
  assert.deepEqual(found, {python: '/work/research/.venv/bin/python'});
  assert.ok(calls.every(args => args.length === 1));
});
test('explicit Python override fails without silently falling back', async () => {
  const calls = [];
  await assert.rejects(discover({...input, pythonOverride: '/wrong/python'}, probe('/work/research/.venv/bin/python', calls)),
    error => error instanceof DiscoveryError && /explicit Python/.test(error.message));
  assert.deepEqual(calls, [['/wrong/python']]);
});
test('selected Python precedes discovery, remembered choices are revalidated', async () => {
  const calls = [];
  const found = await discover({...input, selectedPython: '/selected/python', remembered: {python: '/removed/python'}}, probe('/selected/python', calls));
  assert.equal(found.python, '/selected/python');
  assert.deepEqual(calls.slice(0, 2), [['/removed/python'], ['/selected/python']]);
});
test('no working Python gives an actionable setup error', async () => {
  await assert.rejects(discover(input, probe('/nowhere/python')), error => error instanceof DiscoveryError && /No Python environment/.test(error.message));
});
test('cancellation stops further probes', async () => {
  let cancelled = false, count = 0;
  await assert.rejects(discover({...input, cancelled: () => cancelled}, async () => { count++; cancelled = true; throw new Error('missing'); }), /cancelled/);
  assert.equal(count, 1);
});

test('probe timeout and cancellation kill a SIGTERM-resistant interpreter', async () => {
  const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'glitnir-probe-'));
  const executable = path.join(directory, 'python');
  const marker = path.join(directory, 'pid');
  const children = [];
  fs.writeFileSync(executable, `#!${process.execPath}\nrequire('node:fs').writeFileSync(${JSON.stringify(marker)}, String(process.pid)); process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);\n`, {mode: 0o755});
  try {
    for (const cancel of [false, true]) {
      const controller = new AbortController();
      const checking = probePython(executable, 'unused', controller.signal, cancel ? 15000 : 5000);
      const rejected = assert.rejects(checking);
      // Fresh executable startup can exceed 700ms under macOS security scanning.
      // Keep the timeout real, but let the resistant fixture install its handler.
      const deadline = Date.now() + 10000;
      while (!fs.existsSync(marker)) { assert.ok(Date.now() < deadline); await new Promise(resolve => setTimeout(resolve, 20)); }
      const pid = Number(fs.readFileSync(marker, 'utf8'));
      children.push(pid);
      if (cancel) controller.abort();
      await rejected;
      const exited = Date.now() + 2000;
      while (true) {
        try { process.kill(pid, 0); } catch { break; }
        assert.ok(Date.now() < exited, 'probe child exited');
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      fs.unlinkSync(marker);
    }
  } finally {
    for (const pid of children) { try { process.kill(pid, 'SIGKILL'); } catch {} }
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('shared backend follows selected Python but explicit overrides remain exclusive', async () => {
  const args={...input,selectedPython:'/selected/python',sharedPython:'/shared/python'};
  assert.equal((await discover(args,probe('/shared/python'))).python,'/shared/python');
  assert.equal((await discover(args,probe('/selected/python'))).python,'/selected/python');
  await assert.rejects(discover({...args,pythonOverride:'/bad/python'},probe('/shared/python')),/explicit Python/);
});

test('the probe response needs only an interpreter path', async () => {
  const fs = require('node:fs'), os = require('node:os');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'glitnir-probe-'));
  const executable = path.join(directory, 'python');
  try {
    fs.writeFileSync(executable, `#!${process.execPath}\nconsole.log(JSON.stringify({python: '/env/python', sourceRoot: null}));\n`, {mode: 0o755});
    const result = await probePython(executable, 'unused', new AbortController().signal);
    assert.equal(result.python, '/env/python');
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});
