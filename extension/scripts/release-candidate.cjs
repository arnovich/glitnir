// Build identity and verified draft uploads. This script never publishes releases.
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const {openPromise} = require('yauzl');
const {checkThin, checkBackendWheel, backendPin} = require('./package-thin.cjs');
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const MANIFEST = 'glitnir-candidate.json';
const SCHEMA = 'glitnir-thin-candidate/v2';
const BACKEND_WHEEL = /^glitnir-([\w.]+)-py3-none-any\.whl$/;
// The provider plugin is released at the candidate's own version, like the VSIX.
const pluginAsset = version => `glitnir_forseti-${version}-py3-none-any.whl`;

function candidateIdentity(base, env = process.env) {
  assert.equal(env.GITHUB_EVENT_NAME, 'push', 'Candidates require a main push');
  assert.equal(env.GITHUB_REF, 'refs/heads/main', 'Candidates require main');
  assert.match(env.GITHUB_SHA || '', /^[a-f0-9]{40}$/);
  assert.match(env.GITHUB_RUN_ID || '', /^[1-9]\d*$/);
  assert.match(env.GITHUB_RUN_NUMBER || '', /^[1-9]\d*$/);
  assert(Number(env.GITHUB_RUN_NUMBER) < 2147483647, 'Candidate version counter is too large');
  assert.match(env.GITHUB_REPOSITORY || '', /^[\w.-]+\/[\w.-]+$/);
  assert.match(base, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?![\s\S])/);
  return {repository: env.GITHUB_REPOSITORY, sha: env.GITHUB_SHA, tag: `v${base}`, version: base, runId: env.GITHUB_RUN_ID};
}
async function boundedFile(file) {
  const info = await fs.lstat(file);
  assert(info.isFile() && !info.isSymbolicLink() && info.size < 25 * 1024 ** 2, `Invalid or oversized candidate asset: ${path.basename(file)}`);
  return fs.readFile(file);
}
// Whether a PEP 440 specifier of plain numeric clauses admits a numeric version.
// Anything else (markers, wildcards, ~=, pre-releases) is refused, not guessed.
function admitsVersion(specifier, version) {
  assert.match(version, /^\d+(\.\d+)*$/, `Unsupported backend version ${version}`);
  const compare = (a, b) => {
    const x = a.split('.').map(Number), y = b.split('.').map(Number);
    for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] ?? 0) !== (y[i] ?? 0)) return Math.sign((x[i] ?? 0) - (y[i] ?? 0));
    return 0;
  };
  if (specifier.trim() === '') return true;
  return specifier.split(',').every(clause => {
    const match = clause.match(/^\s*(==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+)*)\s*$/);
    assert(match, `Unsupported glitnir requirement in the plugin wheel: ${specifier}`);
    const order = compare(version, match[2]);
    return {'==': order === 0, '!=': order !== 0, '<=': order <= 0, '>=': order >= 0, '<': order < 0, '>': order > 0}[match[1]];
  });
}
// Install Extension Package holds glitnir at the installed backend
// (glitnir==<installed>), so the plugin must admit the backend released with it.
async function checkPluginWheel(file, version, backendVersion) {
  assert.equal(path.basename(file), pluginAsset(version), 'Plugin wheel version differs from candidate');
  const info = `glitnir_forseti-${version}.dist-info/`, metadata = {}, names = new Set();
  const zip = await openPromise(file);
  let expanded = 0;
  for await (const entry of zip.eachEntry()) {
    const name = entry.fileName;
    assert(!names.has(name), 'Duplicate plugin wheel entry'); names.add(name);
    assert(name.startsWith('glitnir_forseti/') || name.startsWith(info), 'Unexpected plugin wheel payload');
    assert(!/\.(so|dylib|node|exe|whl|zip|pyc|pth)$/i.test(name), 'Plugin wheel includes a native, bundled or startup payload');
    expanded += entry.uncompressedSize;
    assert(expanded < 25 * 1024 ** 2, 'Plugin wheel exceeds 25 MiB expanded');
    if (['WHEEL', 'METADATA', 'entry_points.txt'].map(n => info + n).includes(name)) {
      assert(entry.uncompressedSize < 1024 ** 2, 'Oversized plugin wheel metadata');
      const chunks = [];
      for await (const chunk of await zip.openReadStreamPromise(entry)) chunks.push(chunk);
      metadata[name.slice(info.length)] = Buffer.concat(chunks).toString('utf8');
    }
  }
  assert(metadata.WHEEL && /^Root-Is-Purelib: true$/m.test(metadata.WHEEL), 'Plugin wheel must be pure Python');
  assert.deepEqual(metadata.WHEEL.split('\n').filter(line => line.startsWith('Tag: ')), ['Tag: py3-none-any'], 'Plugin wheel must be universal');
  assert(/^\[glitnir\.providers\]$/m.test(metadata['entry_points.txt'] ?? ''), 'Plugin wheel declares no glitnir.providers entry point');
  const headers = (metadata.METADATA ?? '').split(/\r?\n\r?\n/)[0].split(/\r?\n/);
  assert(headers.includes('Name: glitnir-forseti') && headers.includes(`Version: ${version}`), 'Plugin wheel metadata differs from its file name');
  const core = headers.filter(line => line.startsWith('Requires-Dist: ')).map(line => line.slice(15).match(/^([A-Za-z0-9][\w.-]*)(.*)$/))
    .filter(match => match && match[1].toLowerCase().replace(/[-_.]+/g, '-') === 'glitnir');
  assert.equal(core.length, 1, 'Plugin wheel must require the glitnir backend exactly once');
  assert(admitsVersion(core[0][2], backendVersion), `Plugin requires glitnir${core[0][2]}, which excludes the released backend ${backendVersion}`);
}
async function collectCandidate(identity, vsix, wheel, plugin, guide, output) {
  assert.equal(path.basename(vsix), `glitnir-${identity.version}.vsix`);
  assert.match(path.basename(wheel), BACKEND_WHEEL);
  const editor = await checkThin(vsix, identity.version, undefined, await backendPin(wheel, identity.tag));
  await checkBackendWheel(wheel);
  await checkPluginWheel(plugin, identity.version, path.basename(wheel).match(BACKEND_WHEEL)[1]);
  await fs.mkdir(output); // A fresh directory prevents mixing candidate builds.
  const assets = [];
  for (const [file, name] of [[vsix, path.basename(vsix)], [wheel, path.basename(wheel)], [plugin, path.basename(plugin)], [guide, 'INSTALL.md']]) {
    const bytes = await boundedFile(file);
    await fs.writeFile(path.join(output, name), bytes, {flag: 'wx'});
    assets.push({name, size: bytes.length, sha256: digest(bytes)});
  }
  const bound = assets.find(a => a.name === path.basename(plugin));
  const manifest = {schema: SCHEMA, ...identity, editor, plugin: {asset: bound.name, version: identity.version, size: bound.size, sha256: bound.sha256}, assets};
  const bytes = Buffer.from(JSON.stringify(manifest, null, 2) + '\n');
  await fs.writeFile(path.join(output, MANIFEST), bytes, {flag: 'wx'});
  const sums = [...assets, {name: MANIFEST, sha256: digest(bytes)}].map(a => `${a.sha256}  ${a.name}\n`).join('');
  await fs.writeFile(path.join(output, 'SHA256SUMS'), sums, {flag: 'wx'});
  await verifyCandidate(output, identity);
}
async function verifyCandidate(directory, identity) {
  const raw = await boundedFile(path.join(directory, MANIFEST));
  const manifest = JSON.parse(raw);
  assert.equal(manifest.schema, SCHEMA);
  for (const [key, value] of Object.entries(identity)) assert.equal(manifest[key], value, 'Candidate identity does not match this run');
  assert(Array.isArray(manifest.assets) && manifest.assets.length === 4, 'Invalid candidate asset inventory');
  const vsixName = `glitnir-${identity.version}.vsix`, pluginName = pluginAsset(identity.version);
  const names = manifest.assets.map(a => a.name);
  assert.equal(new Set(names).size, 4, 'Duplicate candidate assets');
  const backendName = names.find(n => BACKEND_WHEEL.test(n));
  assert(names.includes(vsixName) && names.includes(pluginName) && names.includes('INSTALL.md') && backendName, 'Invalid candidate asset inventory');
  const files = [...names, MANIFEST, 'SHA256SUMS'];
  assert.deepEqual((await fs.readdir(directory)).sort(), [...files].sort(), 'Candidate asset inventory differs');
  const verified = [];
  for (const a of manifest.assets) {
    const bytes = await boundedFile(path.join(directory, a.name));
    assert.equal(bytes.length, a.size, 'Candidate asset size changed');
    assert.equal(digest(bytes), a.sha256, 'Candidate asset checksum changed');
    verified.push(a);
  }
  verified.push({name: MANIFEST, size: raw.length, sha256: digest(raw)});
  const sums = await boundedFile(path.join(directory, 'SHA256SUMS'));
  assert.equal(sums.toString('utf8'), verified.map(a => `${a.sha256}  ${a.name}\n`).join(''), 'Candidate checksums differ');
  verified.push({name: 'SHA256SUMS', size: sums.length, sha256: digest(sums)});
  const plugin = manifest.assets.find(a => a.name === pluginName);
  assert.deepEqual(manifest.plugin, {asset: pluginName, version: identity.version, size: plugin.size, sha256: plugin.sha256}, 'Candidate plugin binding differs');
  await checkThin(path.join(directory, vsixName), identity.version, manifest.editor, await backendPin(path.join(directory, backendName), identity.tag));
  await checkBackendWheel(path.join(directory, backendName));
  await checkPluginWheel(path.join(directory, pluginName), identity.version, backendName.match(BACKEND_WHEEL)[1]);
  return verified;
}
async function ensureTag(api, identity) {
  const existing = await api.tag(identity.tag);
  if (existing !== undefined) assert.equal(existing, identity.sha, 'Candidate tag points at a different commit');
  else {
    await api.createTag(identity.tag, identity.sha);
    assert.equal(await api.tag(identity.tag), identity.sha, 'Created candidate tag points at a different commit');
  }
}
function releaseNotes(identity, ready) {
  return `${ready ? 'Ready for review' : 'Upload incomplete — do not publish'}\n\n` +
    `Glitnir ${identity.version}; source commit ${identity.sha}.\n` +
    `Validated build: https://github.com/${identity.repository}/actions/runs/${identity.runId}\n\n` +
    'Universal thin VSIX and matching Python backend wheel for local macOS/Linux. Python 3.12+ and Lean 4.32.1 are prerequisites; see INSTALL.md. No Python interpreter, Lean distribution or Mathlib build is included.\n\n' +
    `The optional glitnir-forseti provider wheel (${pluginAsset(identity.version)}) is the one tested with this backend; install it with Glitnir: Install Extension Package… after verifying SHA256SUMS.\n\n` +
    'Verify SHA256SUMS, review the release notes, then publish this existing draft. Keep its tag and assets unchanged; publishing requires no rebuild.\n';
}
async function uploadDraft(api, directory, identity) {
  assert(!await api.hasPublishedVersion(identity.version), 'Package version is already published; bump package.json before the next release');
  const assets = await verifyCandidate(directory, identity);
  assert.equal(await api.tag(identity.tag), identity.sha, 'Candidate tag points at a different commit');
  let release = await api.getRelease(identity.tag);
  if (release) {
    assert.equal(release.draft, true, 'Refusing to modify a published release');
    assert.equal(release.tag_name, identity.tag);
    for (const remote of release.assets) {
      const local = assets.find(a => a.name === remote.name);
      assert(local && remote.size === local.size && remote.digest === `sha256:${local.sha256}`, `Release asset conflict: ${remote.name}`);
    }
    assert.equal(new Set(release.assets.map(a => a.name)).size, release.assets.length, 'Duplicate release assets');
    assert([releaseNotes(identity, false), releaseNotes(identity, true)].includes(release.body), 'Draft release notes were edited; preserve the owner’s changes');
  } else release = await api.createDraft(identity.tag, releaseNotes(identity, false));
  for (const asset of assets) if (!release.assets.some(a => a.name === asset.name)) await api.upload(identity.tag, path.join(directory, asset.name));
  const uploaded = await api.getRelease(identity.tag);
  async function verifyUploaded(current) {
    assert.equal(current.id, release.id, 'Release identity changed during upload');
    assert.equal(current.tag_name, identity.tag, 'Release tag changed during upload');
    assert.equal(current.draft, true, 'Release was published during upload');
    assert.equal(await api.tag(identity.tag), identity.sha, 'Candidate tag moved during upload');
    assert.equal(current.assets.length, assets.length, 'Incomplete release upload');
    assert.equal(new Set(current.assets.map(a => a.name)).size, assets.length, 'Duplicate release assets');
    for (const local of assets) {
      const remote = current.assets.find(a => a.name === local.name);
      assert(remote && remote.size === local.size && remote.digest === `sha256:${local.sha256}`, `Uploaded asset checksum conflict: ${local.name}`);
    }
    assert([releaseNotes(identity, false), releaseNotes(identity, true)].includes(current.body), 'Draft release notes were edited during upload; preserve the owner’s changes');
  }
  await verifyUploaded(uploaded);
  if (uploaded.body !== releaseNotes(identity, true)) await api.ready(uploaded.id, identity.tag, releaseNotes(identity, true));
  // The final PATCH is a mutation too: verify persisted identity and assets by ID,
  // so a detached tag cannot hide the release from the tag-based lookup.
  const ready = await api.getReleaseById(uploaded.id);
  await verifyUploaded(ready);
  assert.equal(ready.body, releaseNotes(identity, true), 'Release notes update did not persist');
}
async function prepareCandidate(api, identity) {
  // A normal main push can retain the current version without replacing a release.
  if (await api.hasPublishedVersion(identity.version)) return {skip:true, reuse:false};
  const reuse = await api.hasArtifact(identity.runId, 'glitnir-thin-candidate');
  const release = await api.getRelease(identity.tag);
  if (release) {
    assert(release.draft, 'Refusing to rebuild a published candidate');
    assert(reuse, 'Original candidate artifact is unavailable; do not rebuild assets already attached to a draft');
  }
  await ensureTag(api, identity);
  return {skip:false, reuse};
}
function github(repository, run = (args, input) => execFileSync('gh', args, {input, encoding: 'utf8', maxBuffer: 8 * 1024 ** 2})) {
  const gh = (args, input) => run(args, input === undefined ? undefined : JSON.stringify(input));
  const api = (endpoint, method = 'GET', data) => JSON.parse(gh(['api', `repos/${repository}/${endpoint}`, '--method', method, ...(data ? ['--input', '-'] : [])], data));
  const optional = endpoint => {
    try { return api(endpoint); }
    catch (error) { if (String(error.stderr).includes('(HTTP 404)')) return undefined; throw error; }
  };
  return {
    async hasArtifact(runId, name) {
      for (let page = 1; page <= 1000; page++) {
        const result = api(`actions/runs/${runId}/artifacts?per_page=100&page=${page}`);
        const matches = result.artifacts.filter(artifact => artifact.name === name);
        assert(matches.length <= 1, 'Duplicate candidate artifact');
        if (matches.length) {
          assert(!matches[0].expired, 'Original candidate artifact has expired; do not overwrite its identity');
          return true;
        }
        if (result.artifacts.length < 100) return false;
      }
      throw new Error('Artifact lookup exceeded its page limit');
    },
    async hasPublishedVersion(version) {
      for (let page = 1; page <= 1000; page++) {
        const releases = api(`releases?per_page=100&page=${page}`);
        if (releases.some(release => !release.draft && release.assets.some(asset => asset.name === `glitnir-${version}.vsix`))) return true;
        if (releases.length < 100) return false;
      }
      throw new Error('Published version lookup exceeded its page limit');
    },
    async tag(tag) { const ref = optional(`git/ref/tags/${tag}`); if (ref) assert.equal(ref.object.type, 'commit', 'Candidate tags must point directly at a commit'); return ref?.object.sha; },
    async createTag(tag, sha) { api('git/refs', 'POST', {ref: `refs/tags/${tag}`, sha}); },
    async getRelease(tag) {
      // The by-tag REST endpoint only returns published releases. Listing with
      // this job's write token also exposes drafts, including on upload retries.
      for (let page = 1; page <= 1000; page++) {
        const releases = api(`releases?per_page=100&page=${page}`);
        const found = releases.find(release => release.tag_name === tag);
        if (found) return found;
        if (releases.length < 100) return undefined;
      }
      throw new Error('Release lookup exceeded its page limit');
    },
    // The by-tag endpoint never returns drafts, which marketplace publication refuses.
    async publishedRelease(tag) { return optional(`releases/tags/${tag}`); },
    async createDraft(tag, body) { return api('releases', 'POST', {tag_name: tag, name: `Glitnir ${tag}`, body, draft: true, prerelease: true}); },
    async upload(tag, file) { gh(['release', 'upload', tag, file, '--repo', repository]); },
    async getReleaseById(id) { return api(`releases/${id}`); },
    async ready(id, tag, body) { return api(`releases/${id}`, 'PATCH', {tag_name: tag, body}); },
  };
}
async function main() {
  const identity = candidateIdentity(require('../../package.json').version);
  const [command, directory] = process.argv.slice(2);
  if (command === 'prepare') {
    const plan = await prepareCandidate(github(identity.repository), identity);
    if (process.env.GITHUB_OUTPUT) await fs.appendFile(process.env.GITHUB_OUTPUT, `tag=${identity.tag}\nversion=${identity.version}\nreuse=${plan.reuse}\nskip=${plan.skip}\n`);
  } else if (command === 'artifact') {
    assert(['glitnir-tested-backend', 'glitnir-tested-plugin', 'glitnir-tested-vsix', 'glitnir-thin-candidate'].includes(directory));
    const exists = await github(identity.repository).hasArtifact(identity.runId, directory);
    await fs.appendFile(process.env.GITHUB_OUTPUT, `exists=${exists}\n`);
  } else if (command === 'collect') {
    const wheels = (await fs.readdir('backend/dist')).filter(n => /^glitnir-.*-py3-none-any\.whl$/.test(n));
    assert.equal(wheels.length, 1, 'Expected exactly one tested backend wheel');
    const plugins = (await fs.readdir('plugins/forseti/dist')).filter(n => /^glitnir_forseti-.*-py3-none-any\.whl$/.test(n));
    assert.equal(plugins.length, 1, 'Expected exactly one tested plugin wheel');
    await collectCandidate(identity, `glitnir-${identity.version}.vsix`, path.join('backend/dist', wheels[0]), path.join('plugins/forseti/dist', plugins[0]), 'extension/media/setup.md', directory);
  } else if (command === 'upload') await uploadDraft(github(identity.repository), directory, identity);
  else throw new Error('Usage: release-candidate.cjs prepare | artifact NAME | collect DIRECTORY | upload DIRECTORY');
}
module.exports = {candidateIdentity, ensureTag, collectCandidate, verifyCandidate, uploadDraft, prepareCandidate, github, admitsVersion, checkPluginWheel, releaseNotes};
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
