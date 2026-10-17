// Exercise the installer shipped in an extracted, checksum-verified candidate.
// Usage: node extension/scripts/check-thin-install.cjs EXTENSION_DIRECTORY WHEEL STORE
// STORE must be a new temporary directory. stdout returns the installed Python.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

async function main() {
  const [extensionArg, wheelArg, storeArg] = process.argv.slice(2);
  assert(extensionArg && wheelArg && storeArg, 'Provide extension directory, wheel and temporary store');
  const extension = path.resolve(extensionArg), wheel = path.resolve(wheelArg), store = path.resolve(storeArg);
  await fs.mkdir(store, {mode: 0o700}); // Refuse any existing destination.
  const {installBackend, findUv} = require(path.join(extension, 'dist/backend-install.js'));
  const {probePython, discover} = require(path.join(extension, 'dist/discovery.js'));
  const descriptor = JSON.parse(await fs.readFile(path.join(extension, 'package.json'))).glitnirBackend;
  const signal = new AbortController().signal;
  const probe = python => probePython(python, path.join(extension, 'dist/probe.py'), signal);
  const uv = await findUv();
  const options = {wheel, uv, signal, probe};
  const empty = async () => assert.deepEqual(await fs.readdir(store), [], 'Failed setup left a partial environment');
  await assert.rejects(findUv([path.join(store, 'missing-uv')]), /Install uv/);
  await assert.rejects(installBackend(store, descriptor, {...options, uv: path.join(store, 'missing-uv')}), /uv could not be started/);
  await empty();
  const cancellation = new AbortController();
  await assert.rejects(installBackend(store, descriptor, {...options, signal: cancellation.signal,
    progress: message => { if (message === 'Installing the backend') cancellation.abort(); }}), /abort/i);
  await empty();
  const python = await installBackend(store, descriptor, options);
  const marker = path.join(store, descriptor.sha256, '.glitnir-backend');
  const before = (await fs.stat(marker)).mtimeMs;
  assert.equal(await installBackend(store, descriptor, {...options, wheel: '/missing-wheel', uv: '/missing-uv'}), python);
  assert.equal((await fs.stat(marker)).mtimeMs, before, 'Reuse must not reinstall');
  assert.equal((await discover({cwd: store, notebook: path.join(store, 'A.lean'), roots: [], pythonOverride: python}, probe)).python, python);
  const inventory = spawnSync(python, ['-I', '-c', 'import importlib.metadata as m, json; print(json.dumps(sorted(d.metadata["Name"].lower() for d in m.distributions())))'], {encoding: 'utf8'});
  assert.equal(inventory.status, 0, inventory.stderr);
  assert.deepEqual(JSON.parse(inventory.stdout), ['glitnir']);
  console.error('Thin install passed: real uv, cancellation cleanup, reuse, explicit Python and core-only inventory.');
  console.log(python);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
