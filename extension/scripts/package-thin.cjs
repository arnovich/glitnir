// Default distribution: portable extension code using an existing Python backend.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const {openPromise} = require('yauzl');

function thinMetadata(original, version = original.version) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) throw new Error('Invalid thin package version');
  const metadata = {...original, version};
  for (const key of ['glitnirManagedRuntime', 'glitnirRuntimeManifestSha256', 'glitnirRuntimeDistribution']) delete metadata[key];
  return metadata;
}
function thinIgnore(text) {
  return text.split('\n').filter(line => !line.startsWith('!dist/managed/')).join('\n');
}
async function checkThin(file, version, expectedEditor, expectedBackend, expectedPlugins) {
  assert((await fs.stat(file)).size < 10 * 1024 ** 2, 'Thin VSIX exceeds 10 MiB');
  const zip = await openPromise(file), names = new Set(), data = {};
  let expanded = 0;
  for await (const entry of zip.eachEntry()) {
    const name = entry.fileName;
    assert(!names.has(name), 'Duplicate VSIX entry'); names.add(name);
    assert(!name.startsWith('extension/dist/managed/'), 'Thin VSIX includes a managed runtime');
    assert(!/\.(so|dylib|node|exe|whl|zip)$/.test(name), 'Thin VSIX includes a native binary or nested runtime archive');
    expanded += entry.uncompressedSize;
    assert(expanded < 25 * 1024 ** 2, 'Thin VSIX expanded size exceeds 25 MiB');
    if (['extension/package.json', 'extension/dist/editor-runtime.json', 'extension.vsixmanifest'].includes(name)) {
      assert(entry.uncompressedSize < 1024 * 1024, 'Oversized VSIX metadata');
      const chunks = [];
      for await (const chunk of await zip.openReadStreamPromise(entry)) chunks.push(chunk);
      data[name] = Buffer.concat(chunks).toString('utf8');
    }
  }
  const metadata = JSON.parse(data['extension/package.json']);
  assert.equal(`${metadata.publisher}.${metadata.name}`, 'gimle.glitnir');
  assert.equal(metadata.version, version, 'VSIX version differs from candidate');
  if (expectedBackend) assert.deepEqual(metadata.glitnirBackend, expectedBackend, 'VSIX backend binding differs from candidate');
  // A VSIX downloads only the plugin wheels it pins; a candidate's pins must equal its attached wheels.
  if (expectedPlugins !== undefined) assert.deepEqual(metadata.glitnirPlugins ?? null, expectedPlugins, 'VSIX plugin pin differs from the candidate plugin wheel');
  for (const key of ['glitnirManagedRuntime', 'glitnirRuntimeManifestSha256', 'glitnirRuntimeDistribution']) assert.equal(metadata[key], undefined, 'Thin VSIX binds a managed runtime');
  assert(data['extension.vsixmanifest'] && !/\bTargetPlatform\s*=/.test(data['extension.vsixmanifest']), 'Thin VSIX must be universal');
  assert.equal(data['extension.vsixmanifest'].match(/<Identity\b[^>]*\bVersion="([^"]+)"/)?.[1], version, 'VSIX installation version differs from candidate');
  for (const name of ['extension/dist/extension.js', 'extension/dist/probe.py', 'extension/dist/providers.py']) assert(names.has(name), `Missing ${name}`);
  const editor = JSON.parse(data['extension/dist/editor-runtime.json']);
  assert.equal(editor.apiVersion, 2); assert.match(editor.rendererSha256, /^[a-f0-9]{64}$/);
  if (expectedEditor) assert.deepEqual(editor, expectedEditor, 'VSIX renderer identity differs');
  return editor;
}
async function checkBackendWheel(file) {
  const zip = await openPromise(file), names = new Set();
  let wheelMetadata, expanded = 0;
  for await (const entry of zip.eachEntry()) {
    const name = entry.fileName;
    assert(!names.has(name), 'Duplicate backend wheel entry'); names.add(name);
    assert(name.startsWith('glitnir/') || /^glitnir-[\w.]+\.dist-info\//.test(name), 'Unexpected backend wheel payload');
    assert(!/\.(so|dylib|node|exe|whl|zip|pyc)$/.test(name), 'Backend wheel includes a native or bundled runtime payload');
    expanded += entry.uncompressedSize;
    assert(expanded < 25 * 1024 ** 2, 'Backend wheel exceeds 25 MiB expanded');
    if (name.endsWith('.dist-info/WHEEL')) {
      assert(entry.uncompressedSize < 65536, 'Oversized wheel metadata');
      const chunks = [];
      for await (const chunk of await zip.openReadStreamPromise(entry)) chunks.push(chunk);
      wheelMetadata = Buffer.concat(chunks).toString('utf8');
    }
  }
  // The notebook entry point is a shim from the package layering onward, so it alone proves nothing:
  // also require the server it starts, in its old (notebook) or current (service) place.
  assert(names.has('glitnir/notebook/__main__.py'), 'Backend wheel has no notebook entry point');
  assert(names.has('glitnir/notebook/server.py') || names.has('glitnir/service/server.py'), 'Backend wheel has no notebook server');
  assert(wheelMetadata && /^Root-Is-Purelib: true$/m.test(wheelMetadata), 'Backend wheel must be pure Python');
  assert.deepEqual(wheelMetadata.split('\n').filter(line => line.startsWith('Tag: ')), ['Tag: py3-none-any'], 'Backend wheel must be universal');
}
async function backendPin(file, tag) {
  const info = await fs.lstat(file);
  assert(info.isFile() && !info.isSymbolicLink() && info.size > 0 && info.size < 25 * 1024 ** 2, 'Invalid backend wheel');
  assert.match(path.basename(file), /^glitnir-[\w.]+-py3-none-any\.whl$/);
  if (tag !== undefined) assert.match(tag, /^(?:build-[1-9]\d*|v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?![\s\S])/);
  await checkBackendWheel(file);
  const bytes = await fs.readFile(file);
  return {asset: path.basename(file), size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), ...(tag ? {tag} : {})};
}
/** Where the plugin wheel built with this extension lands; packaging pins it when present. */
const PLUGIN_DIST = 'plugins/forseti/dist';
const PLUGIN_WHEEL = /^glitnir_forseti-((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-py3-none-any\.whl$/;
/** The VSIX pin of a plugin wheel, like the backend's: the extension downloads only these exact bytes. */
async function pluginPin(file, version) {
  const info = await fs.lstat(file);
  assert(info.isFile() && !info.isSymbolicLink() && info.size > 0 && info.size < 25 * 1024 ** 2, 'Invalid plugin wheel');
  const match = path.basename(file).match(PLUGIN_WHEEL);
  assert(match, 'Plugin wheel must be glitnir_forseti-VERSION-py3-none-any.whl');
  if (version !== undefined) assert.equal(match[1], version, 'Plugin wheel version differs from the package version; rebuild it or remove it before packaging');
  const bytes = await fs.readFile(file);
  return {distribution: 'glitnir-forseti', asset: path.basename(file), size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex')};
}
async function main() {
  const root = path.resolve(__dirname, '../..');
  const packageFile = path.join(root, 'package.json'), ignoreFile = path.join(root, '.vscodeignore');
  const original = await fs.readFile(packageFile), ignore = await fs.readFile(ignoreFile);
  const metadata = thinMetadata(JSON.parse(original), process.env.GLITNIR_PACKAGE_VERSION || undefined);
  const output = path.join(root, `glitnir-${metadata.version}.vsix`);
  const wheels = (await fs.readdir(path.join(root, 'backend/dist'))).filter(name => name.endsWith('.whl'));
  assert.equal(wheels.length, 1, 'Build exactly one matching backend wheel in backend/dist before packaging');
  metadata.glitnirBackend = await backendPin(path.join(root, 'backend/dist', wheels[0]), process.env.GLITNIR_BACKEND_RELEASE_TAG);
  // Without a built plugin wheel the VSIX pins none and never downloads one.
  const plugins = await fs.readdir(path.join(root, PLUGIN_DIST)).then(names => names.filter(name => name.endsWith('.whl')), () => []);
  assert(plugins.length <= 1, `Build at most one plugin wheel in ${PLUGIN_DIST} before packaging`);
  if (plugins.length) metadata.glitnirPlugins = [await pluginPin(path.join(root, PLUGIN_DIST, plugins[0]), metadata.version)];
  try {
    await fs.writeFile(packageFile, JSON.stringify(metadata, null, 2) + '\n');
    await fs.writeFile(ignoreFile, thinIgnore(ignore.toString('utf8')));
    const result = spawnSync(process.execPath, ['--import', path.join(__dirname, 'package-scan-profiling.mjs'), path.join(root, 'node_modules/.bin/vsce'), 'package', '--out', output], {cwd: root, stdio: 'inherit'});
    if (result.error || result.status) throw result.error ?? new Error(`Thin VSIX packaging failed (${result.status})`);
    const editor = JSON.parse(await fs.readFile(path.join(root, 'dist/editor-runtime.json'), 'utf8'));
    await checkThin(output, metadata.version, editor, metadata.glitnirBackend, metadata.glitnirPlugins ?? null);
    console.log(`Verified universal thin VSIX: ${output}${metadata.glitnirPlugins ? `, pinning ${metadata.glitnirPlugins[0].asset}` : ', pinning no plugin wheel'}`);
  } finally { await fs.writeFile(packageFile, original); await fs.writeFile(ignoreFile, ignore); }
}
module.exports = {thinMetadata, thinIgnore, checkThin, checkBackendWheel, backendPin, pluginPin, PLUGIN_DIST};
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
