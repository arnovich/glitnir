const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {test} = require('node:test');
const {ZipFile} = require('yazl');
const {createWriteStream} = require('node:fs');
const {pipeline} = require('node:stream/promises');
const {candidateIdentity, ensureTag, collectCandidate, verifyCandidate, uploadDraft} = require('../scripts/release-candidate.cjs');
const {thinMetadata, thinIgnore, checkThin} = require('../scripts/package-thin.cjs');

const env = {GITHUB_EVENT_NAME: 'push', GITHUB_REF: 'refs/heads/main', GITHUB_SHA: 'a'.repeat(40), GITHUB_RUN_ID: '1234', GITHUB_RUN_NUMBER: '27', GITHUB_REPOSITORY: 'owner/glitnir'};
const identity = candidateIdentity('0.1.0', env);
const editor = {apiVersion: 2, rendererSha256: '1'.repeat(64)};
async function zip(file, entries) {
  const z = new ZipFile();
  for (const [name, data] of Object.entries(entries)) z.addBuffer(Buffer.from(typeof data === 'string' ? data : JSON.stringify(data)), name);
  const written = pipeline(z.outputStream, createWriteStream(file));
  z.end(); await written;
}
const pluginEntries = {
  'glitnir_forseti/__init__.py': 'providers = []',
  'glitnir_forseti-0.1.0.dist-info/METADATA': 'Metadata-Version: 2.4\nName: glitnir-forseti\nVersion: 0.1.0\nRequires-Dist: glitnir<0.2,>=0.1.0\nRequires-Dist: lark>=1.2.2\n\n# glitnir-forseti\n',
  'glitnir_forseti-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
  'glitnir_forseti-0.1.0.dist-info/entry_points.txt': '[glitnir.providers]\nforseti = glitnir_forseti:providers\n',
};
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-thin-release-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const vsix = path.join(root, `glitnir-${identity.version}.vsix`);
  const metadata = {publisher: 'gimle', name: 'glitnir', version: identity.version};
  const entries = {'extension/package.json': metadata, 'extension/dist/editor-runtime.json': editor, 'extension/dist/extension.js': 'code', 'extension/dist/probe.py': 'probe', 'extension/dist/providers.py': 'providers', 'extension.vsixmanifest': '<Identity Version="0.1.0" />'};
  await zip(vsix, entries);
  const wheel = path.join(root, 'glitnir-0.1.0-py3-none-any.whl');
  await zip(wheel, {'glitnir/notebook/__main__.py': 'server', 'glitnir-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'});
  const bytes = await fs.readFile(wheel);
  metadata.glitnirBackend = {asset:path.basename(wheel),size:bytes.length,sha256:require('node:crypto').createHash('sha256').update(bytes).digest('hex'),tag:identity.tag};
  const plugin = path.join(root, 'glitnir_forseti-0.1.0-py3-none-any.whl');
  await zip(plugin, pluginEntries);
  const pluginBytes = await fs.readFile(plugin);
  metadata.glitnirPlugins = [{distribution: 'glitnir-forseti', asset: path.basename(plugin), size: pluginBytes.length, sha256: require('node:crypto').createHash('sha256').update(pluginBytes).digest('hex')}];
  await zip(vsix, entries);
  const guide = path.join(root, 'setup.md'); await fs.writeFile(guide, '# Install prerequisites\n');
  const output = path.join(root, 'release');
  return {root, vsix, wheel, plugin, guide, output, entries};
}
function github() {
  return {
    tags: new Map(), release: undefined, writes: [], publishedVersions: [],
    async hasPublishedVersion(version) { return this.publishedVersions.includes(version); },
    async tag(name) { return this.tags.get(name); },
    async createTag(name, sha) { this.writes.push('tag'); this.tags.set(name, sha); },
    async getRelease() { return this.release; },
    async getReleaseById(id) { assert.equal(id, this.release.id); return this.release; },
    async createDraft(tag, body) { this.writes.push('draft'); this.release = {id: 1, tag_name: tag, draft: true, body, assets: []}; return this.release; },
    async upload(_tag, file) {
      this.writes.push(path.basename(file));
      const bytes = await fs.readFile(file);
      this.release.assets.push({name: path.basename(file), size: bytes.length, digest: `sha256:${require('node:crypto').createHash('sha256').update(bytes).digest('hex')}`});
    },
    async ready(_id, tag, body) { this.writes.push('ready'); this.release.tag_name = tag; this.release.body = body; },
  };
}

test('candidate identity binds exact main commit and stable numeric version; PRs cannot release', () => {
  assert.equal(identity.tag, 'v0.1.0'); assert.equal(identity.version, '0.1.0');
  assert.deepEqual(candidateIdentity('0.1.0', {...env, GITHUB_RUN_ATTEMPT: '2'}), identity);
  for (const change of [{GITHUB_EVENT_NAME: 'pull_request'}, {GITHUB_EVENT_NAME: 'workflow_dispatch'}, {GITHUB_REF: 'refs/heads/feature'}, {GITHUB_SHA: 'main'}, {GITHUB_RUN_NUMBER: '1\nBAD=x'}, {GITHUB_RUN_ID: 'x'}]) assert.throws(() => candidateIdentity('0.9.0', {...env, ...change}));
});

test('thin packaging removes managed bindings and stale payload inclusion', async t => {
  const {vsix, entries} = await fixture(t);
  const original = {name: 'glitnir', version: '0.9.0', glitnirManagedRuntime: true, glitnirRuntimeManifestSha256: 'x', glitnirRuntimeDistribution: {}};
  const clean = thinMetadata(original, '0.9.27');
  assert.deepEqual(clean, {name: 'glitnir', version: '0.9.27'}); assert.equal(original.glitnirManagedRuntime, true);
  assert.equal(thinIgnore('**\n!dist/*.js\n!dist/managed/runtime/**\n!dist/managed/runtime.json\n'), '**\n!dist/*.js\n');
  await checkThin(vsix, identity.version, editor);
  for (const bad of [{'extension/dist/managed/runtime/python/bin/python': 'binary'}, {'extension/package.json': {...entries['extension/package.json'], glitnirRuntimeDistribution: {}}}, {'extension.vsixmanifest': '<Identity TargetPlatform="linux-x64" />'}, {'extension/dist/extra.so': 'native'}, {'extension.vsixmanifest': '<Identity Version="0.9.99" />'}]) {
    await zip(vsix, {...entries, ...bad});
    await assert.rejects(checkThin(vsix, identity.version, editor));
  }
});

test('candidate files bind source, version, renderer and exact bytes before any draft write', async t => {
  const f = await fixture(t);
  await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  await verifyCandidate(f.output, identity);
  const api = github(); await ensureTag(api, identity);
  await uploadDraft(api, f.output, identity);
  assert.equal(api.release.draft, true); assert.match(api.release.body, /Ready for review/);
  const firstUploads = api.writes.filter(v => v !== 'tag' && v !== 'draft' && v !== 'ready');
  assert.equal(firstUploads.length, 6);
  await uploadDraft(api, f.output, identity);
  assert.equal(api.writes.filter(v => v.endsWith('.vsix')).length, 1, 'retry never overwrites assets');
  await fs.appendFile(path.join(f.output, path.basename(f.vsix)), 'tamper');
  const before = api.writes.length;
  await assert.rejects(uploadDraft(api, f.output, identity), /checksum|size/);
  assert.equal(api.writes.length, before);
});

test('refuses moved tags, published releases, conflicting assets and wrong source without writes', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const api = github(); api.tags.set(identity.tag, 'b'.repeat(40));
  await assert.rejects(ensureTag(api, identity), /different commit/); assert.deepEqual(api.writes, []);
  api.tags.set(identity.tag, identity.sha);
  api.release = {id: 1, tag_name: identity.tag, draft: false, assets: []};
  await assert.rejects(uploadDraft(api, f.output, identity), /published/); assert.deepEqual(api.writes, []);
  api.release.draft = true; api.release.assets = [{name: path.basename(f.vsix), size: 1, digest: 'sha256:bad'}];
  await assert.rejects(uploadDraft(api, f.output, identity), /conflict/); assert.deepEqual(api.writes, []);
  await assert.rejects(verifyCandidate(f.output, {...identity, sha: 'c'.repeat(40)}), /identity/);
  await fs.writeFile(path.join(f.output, 'surprise.zip'), 'extra');
  await assert.rejects(verifyCandidate(f.output, identity), /inventory/);
});


test('GitHub adapter finds draft releases across pages rather than using the published-only endpoint', async () => {
  const calls = [];
  const api = require('../scripts/release-candidate.cjs').github('owner/glitnir', args => {
    calls.push(args);
    if (args[1].endsWith('page=1')) return JSON.stringify(Array.from({length: 100}, (_, i) => ({tag_name: `older-${i}`})));
    if (args[1].endsWith('page=2')) return JSON.stringify([{id: 5, tag_name: identity.tag, draft: true, assets: []}]);
    throw new Error('Unexpected API request');
  });
  assert.equal((await api.getRelease(identity.tag)).draft, true);
  assert.equal(calls.length, 2);
  assert.equal(await api.getRelease('missing'), undefined);
});


test('whole-run retry reuses original candidate bytes and refuses expired draft inputs', async () => {
  const {prepareCandidate} = require('../scripts/release-candidate.cjs');
  const api = github();
  api.hasArtifact = async () => false;
  assert.deepEqual(await prepareCandidate(api, identity), {skip:false, reuse:false});
  api.hasArtifact = async () => true;
  assert.deepEqual(await prepareCandidate(api, identity), {skip:false, reuse:true});
  api.release = {draft: true};
  assert.deepEqual(await prepareCandidate(api, identity), {skip:false, reuse:true});
  api.hasArtifact = async () => false;
  await assert.rejects(prepareCandidate(api, identity), /Original candidate artifact/);
  api.release.draft = false;
  await assert.rejects(prepareCandidate(api, identity), /published/);
});

test('artifact lookup preserves retained same-run assets and rejects expired ones', async () => {
  let artifacts = [];
  const api = require('../scripts/release-candidate.cjs').github('owner/glitnir', args => {
    assert.match(args[1], /^repos\/owner\/glitnir\/actions\/runs\/1234\/artifacts\?/);
    return JSON.stringify({artifacts});
  });
  assert.equal(await api.hasArtifact('1234', 'glitnir-tested-backend'), false);
  artifacts = [{name: 'glitnir-tested-backend', expired: false}];
  assert.equal(await api.hasArtifact('1234', 'glitnir-tested-backend'), true);
  artifacts[0].expired = true;
  await assert.rejects(api.hasArtifact('1234', 'glitnir-tested-backend'), /expired/);
});


test('candidate wheel must contain the portable server and no native runtime', async t => {
  const f = await fixture(t);
  const {checkBackendWheel} = require('../scripts/package-thin.cjs');
  await checkBackendWheel(f.wheel);
  for (const bad of [
    {'gimle/native.so': 'binary'},
    {'python/bin/python': 'interpreter'},
    {'glitnir-0.1.0.dist-info/WHEEL': 'Root-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n'},
  ]) {
    await zip(f.wheel, {'glitnir/notebook/__main__.py': 'server', ...bad});
    await assert.rejects(checkBackendWheel(f.wheel));
  }
});


test('an interrupted draft upload resumes identical bytes and never overwrites owner notes', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const api = github(); await ensureTag(api, identity);
  const upload = api.upload.bind(api);
  let fail = true;
  api.upload = async (tag, file) => {
    if (fail && path.basename(file).endsWith('.whl')) { fail = false; throw new Error('Connection interrupted'); }
    return upload(tag, file);
  };
  await assert.rejects(uploadDraft(api, f.output, identity), /interrupted/);
  assert.match(api.release.body, /Upload incomplete/);
  assert.equal(api.release.assets.length, 1);
  await uploadDraft(api, f.output, identity);
  assert.equal(api.release.assets.length, 6);
  assert.equal(api.writes.filter(n => n.endsWith('.vsix')).length, 1);
  api.release.body = 'Owner release notes';
  const before = api.writes.length;
  await assert.rejects(uploadDraft(api, f.output, identity), /owner’s changes/);
  assert.equal(api.writes.length, before);
  assert.equal(api.release.body, 'Owner release notes');
});


test('owner notes edited during upload are preserved', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const api = github(); await ensureTag(api, identity);
  const upload = api.upload.bind(api);
  api.upload = async (tag, file) => {
    await upload(tag, file);
    api.release.body = 'Owner edited during upload';
  };
  await assert.rejects(uploadDraft(api, f.output, identity), /owner’s changes/);
  assert.equal(api.release.body, 'Owner edited during upload');
  assert(!api.writes.includes('ready'));
});


test('candidate rejects a VSIX bound to a different backend or release', async t => {
  const f = await fixture(t);
  for (const change of [{sha256:'f'.repeat(64)}, {tag:'build-999'}]) {
    const metadata = {...f.entries['extension/package.json'], glitnirBackend:{...f.entries['extension/package.json'].glitnirBackend,...change}};
    await zip(f.vsix,{...f.entries,'extension/package.json':metadata});
    await assert.rejects(collectCandidate(identity,f.vsix,f.wheel,f.plugin,f.guide,f.output), /backend/i);
  }
});


test('final release update preserves the tag and reads back the same release by ID', async () => {
  const calls = [];
  const api = require('../scripts/release-candidate.cjs').github('owner/glitnir', (args, input) => {
    calls.push({args, input: input && JSON.parse(input)});
    return JSON.stringify({id: 7, tag_name: identity.tag, draft: true});
  });
  await api.ready(7, identity.tag, 'Ready for review');
  assert.deepEqual(calls[0].input, {tag_name: identity.tag, body: 'Ready for review'});
  assert.equal((await api.getReleaseById(7)).id, 7);
  assert.equal(calls[1].args[1], 'repos/owner/glitnir/releases/7');
});

test('final release update cannot silently detach its tag, publish or change assets', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  for (const change of [
    api => { api.release.tag_name = 'untagged-123'; },
    api => { api.release.draft = false; },
    api => { api.release.assets[0].digest = 'sha256:bad'; },
    api => { api.tags.set(identity.tag, 'b'.repeat(40)); },
  ]) {
    const api = github(); await ensureTag(api, identity);
    const ready = api.ready.bind(api);
    api.ready = async (...args) => { await ready(...args); change(api); };
    await assert.rejects(uploadDraft(api, f.output, identity));
  }
});


test('published package versions require a source version bump before another candidate', async () => {
  const {prepareCandidate} = require('../scripts/release-candidate.cjs');
  const api = github();
  api.hasArtifact = async () => false;
  api.publishedVersions.push(identity.version);
  assert.deepEqual(await prepareCandidate(api, identity), {skip:true, reuse:false});
  await assert.rejects(uploadDraft(api, '/unused', identity), /version.*published/);
  assert.deepEqual(api.writes, []);
  assert.equal(candidateIdentity('0.1.1', env).version, '0.1.1');
});


test('published-version lookup ignores drafts and checks all release pages', async () => {
  const {github: client} = require('../scripts/release-candidate.cjs');
  const api = client('owner/glitnir', args => {
    const page2 = args.some(arg => arg.includes('page=2'));
    return JSON.stringify(page2 ? [{draft:false, assets:[{name:'glitnir-0.1.0.vsix'}]}] : Array.from({length:100}, () => ({draft:true, assets:[{name:'glitnir-0.1.0.vsix'}]})));
  });
  assert.equal(await api.hasPublishedVersion('0.1.0'), true);
  assert.equal(await api.hasPublishedVersion('0.1.1'), false);
});

test('version tags cannot be moved by another main build using the same version', async () => {
  const api = github();
  await ensureTag(api, identity);
  const other = candidateIdentity(identity.version, {...env, GITHUB_SHA:'b'.repeat(40), GITHUB_RUN_ID:'5678'});
  assert.equal(other.tag, identity.tag);
  await assert.rejects(ensureTag(api, other), /different commit/);
  assert.equal(api.tags.get(identity.tag), identity.sha);
});

test('release versions reject noncanonical or trailing input', () => {
  for (const version of ['01.2.3', '1.2', '1.2.3\n', '1.2.3/asset']) {
    assert.throws(() => candidateIdentity(version, env));
    assert.throws(() => thinMetadata({version}, version));
  }
});

async function rebind(output, change) {
  // Rewrite the manifest and its checksum line so that only the binding differs.
  const file = path.join(output, 'glitnir-candidate.json');
  const manifest = JSON.parse(await fs.readFile(file, 'utf8')); change(manifest);
  const bytes = Buffer.from(JSON.stringify(manifest, null, 2) + '\n'); await fs.writeFile(file, bytes);
  const sha = require('node:crypto').createHash('sha256').update(bytes).digest('hex');
  const sums = (await fs.readFile(path.join(output, 'SHA256SUMS'), 'utf8')).replace(/^[a-f0-9]{64}  glitnir-candidate\.json$/m, `${sha}  glitnir-candidate.json`);
  await fs.writeFile(path.join(output, 'SHA256SUMS'), sums);
}

test('the plugin wheel is a checksummed candidate asset bound by name, version and digest', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const bytes = await fs.readFile(f.plugin), sha256 = require('node:crypto').createHash('sha256').update(bytes).digest('hex');
  const manifest = JSON.parse(await fs.readFile(path.join(f.output, 'glitnir-candidate.json'), 'utf8'));
  assert.equal(manifest.schema, 'glitnir-thin-candidate/v2');
  assert.deepEqual(manifest.plugin, {asset: path.basename(f.plugin), version: identity.version, size: bytes.length, sha256});
  assert.match(await fs.readFile(path.join(f.output, 'SHA256SUMS'), 'utf8'), new RegExp(`^${sha256}  ${path.basename(f.plugin).replace(/\./g, '\\.')}$`, 'm'));
  const api = github(); await ensureTag(api, identity);
  await uploadDraft(api, f.output, identity);
  assert(api.writes.includes(path.basename(f.plugin)));
  assert.match(api.release.body, /glitnir_forseti-0\.1\.0-py3-none-any\.whl/);
  assert.match(api.release.body, /Install Extension Package/);
});

test('a missing plugin wheel fails collection and verification without writes', async t => {
  const f = await fixture(t);
  await assert.rejects(collectCandidate(identity, f.vsix, f.wheel, path.join(f.root, 'missing.whl'), f.guide, f.output));
  await assert.rejects(fs.access(f.output), 'nothing is collected');
  await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  await fs.rm(path.join(f.output, path.basename(f.plugin)));
  const api = github(); await ensureTag(api, identity); const before = api.writes.length;
  await assert.rejects(verifyCandidate(f.output, identity), /inventory|ENOENT/);
  await assert.rejects(uploadDraft(api, f.output, identity));
  assert.equal(api.writes.length, before);
  const g = await fixture(t); await collectCandidate(identity, g.vsix, g.wheel, g.plugin, g.guide, g.output);
  await fs.rm(path.join(g.output, path.basename(g.plugin)));
  await rebind(g.output, manifest => { manifest.assets = manifest.assets.filter(a => a.name !== path.basename(g.plugin)); });
  await assert.rejects(verifyCandidate(g.output, identity), /inventory/);
});

test('a changed plugin wheel fails its digest check before any draft write', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  await fs.appendFile(path.join(f.output, path.basename(f.plugin)), 'tamper');
  const api = github(); await ensureTag(api, identity); const before = api.writes.length;
  await assert.rejects(uploadDraft(api, f.output, identity), /checksum|size/);
  assert.equal(api.writes.length, before);
  assert.equal(api.release, undefined);
});

test('a conflicting plugin wheel already on the draft is refused, never overwritten', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const api = github(); await ensureTag(api, identity);
  const notes = require('../scripts/release-candidate.cjs').releaseNotes(identity, false);
  for (const remote of [{size: (await fs.stat(f.plugin)).size, digest: `sha256:${'0'.repeat(64)}`}, {size: 1, digest: 'sha256:bad'}]) {
    api.release = {id: 1, tag_name: identity.tag, draft: true, body: notes, assets: [{name: path.basename(f.plugin), ...remote}]};
    const before = api.writes.length;
    await assert.rejects(uploadDraft(api, f.output, identity), /conflict: glitnir_forseti/);
    assert.equal(api.writes.length, before);
    assert.equal(api.release.assets[0].digest, remote.digest);
  }
});

test('an upload interrupted at the plugin wheel resumes and uploads each asset once', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const api = github(); await ensureTag(api, identity);
  const upload = api.upload.bind(api);
  let fail = true;
  api.upload = async (tag, file) => {
    if (fail && path.basename(file) === path.basename(f.plugin)) { fail = false; throw new Error('Connection interrupted'); }
    return upload(tag, file);
  };
  await assert.rejects(uploadDraft(api, f.output, identity), /interrupted/);
  assert.match(api.release.body, /Upload incomplete/);
  assert(!api.release.assets.some(a => a.name === path.basename(f.plugin)));
  await uploadDraft(api, f.output, identity);
  assert.equal(api.release.assets.length, 6);
  for (const name of new Set(api.writes.filter(w => !['tag', 'draft', 'ready'].includes(w)))) assert.equal(api.writes.filter(w => w === name).length, 1, `${name} uploaded once`);
  const before = api.writes.length;
  await uploadDraft(api, f.output, identity);
  assert.equal(api.writes.length, before, 'a complete retry writes nothing');
});

test('a plugin binding that differs from the attached wheel is refused', async t => {
  for (const change of [
    plugin => { plugin.sha256 = 'f'.repeat(64); },
    plugin => { plugin.size += 1; },
    plugin => { plugin.version = '0.1.1'; },
    plugin => { plugin.asset = 'glitnir_forseti-0.1.1-py3-none-any.whl'; },
    plugin => { plugin.tag = identity.tag; },
  ]) {
    const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
    await rebind(f.output, manifest => change(manifest.plugin));
    await assert.rejects(verifyCandidate(f.output, identity), /plugin binding/);
  }
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  await rebind(f.output, manifest => { delete manifest.plugin; });
  const api = github(); await ensureTag(api, identity); const before = api.writes.length;
  await assert.rejects(uploadDraft(api, f.output, identity), /plugin binding/);
  assert.equal(api.writes.length, before);
});

test('the plugin wheel must carry the candidate version and admit the released backend', async t => {
  const f = await fixture(t);
  const renamed = path.join(f.root, 'glitnir_forseti-0.2.0-py3-none-any.whl');
  await fs.copyFile(f.plugin, renamed);
  await assert.rejects(collectCandidate(identity, f.vsix, f.wheel, renamed, f.guide, f.output), /version differs/);
  const metadata = pluginEntries['glitnir_forseti-0.1.0.dist-info/METADATA'];
  for (const [bad, message] of [
    [{'glitnir_forseti-0.1.0.dist-info/METADATA': metadata.replace('Version: 0.1.0', 'Version: 0.1.1')}, /metadata differs/],
    [{'glitnir_forseti-0.1.0.dist-info/METADATA': metadata.replace('glitnir<0.2,>=0.1.0', 'glitnir>=0.2')}, /excludes the released backend 0\.1\.0/],
    [{'glitnir_forseti-0.1.0.dist-info/METADATA': metadata.replace('glitnir<0.2,>=0.1.0', 'glitnir~=0.1')}, /Unsupported glitnir requirement/],
    [{'glitnir_forseti-0.1.0.dist-info/METADATA': metadata.replace('Requires-Dist: glitnir<0.2,>=0.1.0\n', '')}, /exactly once/],
    [{'glitnir_forseti-0.1.0.dist-info/entry_points.txt': '[console_scripts]\nx = y:z\n'}, /glitnir\.providers/],
    [{'glitnir_forseti-0.1.0.dist-info/WHEEL': 'Root-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n'}, /pure Python/],
    [{'glitnir_forseti/native.so': 'binary'}, /native/],
    [{'glitnir_forseti/startup.pth': 'import os'}, /startup/],
    [{'glitnir/__init__.py': 'shadow the backend'}, /payload/],
  ]) {
    await zip(f.plugin, {...pluginEntries, ...bad});
    await assert.rejects(collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output), message);
    await assert.rejects(fs.access(f.output), 'nothing is collected');
  }
});

test('plugin requirement comparison accepts only plain numeric clauses', () => {
  const {admitsVersion} = require('../scripts/release-candidate.cjs');
  assert.equal(admitsVersion('<0.2,>=0.1.0', '0.1.0'), true);
  assert.equal(admitsVersion('<0.2,>=0.1.0', '0.1.9'), true);
  assert.equal(admitsVersion('<0.2,>=0.1.0', '0.2.0'), false);
  assert.equal(admitsVersion('>=0.1.1', '0.1.0'), false);
  assert.equal(admitsVersion('==0.1', '0.1.0'), true);
  assert.equal(admitsVersion('!=0.1.0', '0.1.0'), false);
  assert.equal(admitsVersion('', '0.1.0'), true);
  for (const bad of ['~=0.1', '==0.1.*', '>=0.1; python_version > "3"', '>=0.1rc1', '(>=0.1)']) assert.throws(() => admitsVersion(bad, '0.1.0'), /Unsupported/);
  assert.throws(() => admitsVersion('>=0.1', '0.1.0rc1'), /Unsupported backend version/);
});

test('the source plugin version equals the package version and admits the source backend', async () => {
  // Checked on every pull request, so a mismatch fails before main tags a commit
  // whose candidate could not be collected.
  const {admitsVersion} = require('../scripts/release-candidate.cjs');
  const root = path.join(__dirname, '../..');
  const project = async file => (await fs.readFile(path.join(root, file), 'utf8')).split(/^\[project\]$/m)[1].split(/^\[/m)[0];
  const plugin = await project('plugins/forseti/pyproject.toml'), backend = await project('backend/pyproject.toml');
  const version = text => text.match(/^version = "([^"]+)"$/m)[1];
  assert.equal(version(plugin), require('../../package.json').version, 'Bump plugins/forseti/pyproject.toml with package.json');
  const requirement = plugin.match(/^dependencies = \[(.*)\]$/m)[1].match(/"glitnir([^"\w.-][^"]*)"/);
  assert(requirement, 'The plugin must require the glitnir backend');
  assert(admitsVersion(requirement[1], version(backend)), `The plugin's glitnir${requirement[1]} excludes backend ${version(backend)}`);
});

async function replaceAsset(output, oldName, newName, bytes) {
  // Forge a self-consistent directory: new file, digests, binding and checksums.
  const sha256 = require('node:crypto').createHash('sha256').update(bytes).digest('hex');
  await fs.rm(path.join(output, oldName)); await fs.writeFile(path.join(output, newName), bytes);
  const file = path.join(output, 'glitnir-candidate.json');
  const manifest = JSON.parse(await fs.readFile(file, 'utf8'));
  manifest.assets = manifest.assets.map(a => a.name === oldName ? {name: newName, size: bytes.length, sha256} : a);
  manifest.plugin = {asset: newName, version: manifest.plugin.version, size: bytes.length, sha256};
  const raw = Buffer.from(JSON.stringify(manifest, null, 2) + '\n'); await fs.writeFile(file, raw);
  const sums = [...manifest.assets, {name: 'glitnir-candidate.json', sha256: require('node:crypto').createHash('sha256').update(raw).digest('hex')}];
  await fs.writeFile(path.join(output, 'SHA256SUMS'), sums.map(a => `${a.sha256}  ${a.name}\n`).join(''));
}

test('verification rechecks the attached plugin wheel and its exact candidate name', async t => {
  const f = await fixture(t); await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  const name = path.basename(f.plugin);
  const bad = path.join(f.root, 'bad.whl');
  await zip(bad, {...pluginEntries, 'glitnir_forseti-0.1.0.dist-info/METADATA': pluginEntries['glitnir_forseti-0.1.0.dist-info/METADATA'].replace('glitnir<0.2,>=0.1.0', 'glitnir>=0.2')});
  await replaceAsset(f.output, name, name, await fs.readFile(bad));
  await assert.rejects(verifyCandidate(f.output, identity), /excludes the released backend/);
  const g = await fixture(t); await collectCandidate(identity, g.vsix, g.wheel, g.plugin, g.guide, g.output);
  await replaceAsset(g.output, name, 'glitnir_forseti-0.1.1-py3-none-any.whl', await fs.readFile(g.plugin));
  await assert.rejects(verifyCandidate(g.output, identity), /Invalid candidate asset inventory/);
});

test('the VSIX plugin pin must equal the attached plugin wheel at collection and verification', async t => {
  const {pluginPin} = require('../scripts/package-thin.cjs');
  for (const change of [pins => pins.map(pin => ({...pin, sha256: '0'.repeat(64)})), pins => pins.map(pin => ({...pin, size: pin.size + 1})), () => undefined, () => []]) {
    const f = await fixture(t);
    const metadata = f.entries['extension/package.json'];
    const changed = change(metadata.glitnirPlugins);
    await zip(f.vsix, {...f.entries, 'extension/package.json': {...metadata, glitnirPlugins: changed}});
    await assert.rejects(collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output), /plugin pin differs/, JSON.stringify(changed));
    await assert.rejects(fs.stat(f.output), /ENOENT/, 'a refused candidate writes nothing');
  }
  const f = await fixture(t);
  await collectCandidate(identity, f.vsix, f.wheel, f.plugin, f.guide, f.output);
  assert.deepEqual(f.entries['extension/package.json'].glitnirPlugins, [await pluginPin(f.plugin, identity.version)]);
  // A coherent release whose attached plugin wheel is not the pinned one is refused too.
  const other = path.join(f.root, 'other.whl');
  await zip(other, {...pluginEntries, 'glitnir_forseti/extra.py': 'x = 1'});
  await fs.copyFile(other, path.join(f.output, path.basename(f.plugin)));
  await rebind(f.output, manifest => {
    const bytes = require('node:fs').readFileSync(other), sha256 = require('node:crypto').createHash('sha256').update(bytes).digest('hex');
    manifest.plugin = {...manifest.plugin, size: bytes.length, sha256};
    manifest.assets = manifest.assets.map(a => a.name === path.basename(f.plugin) ? {...a, size: bytes.length, sha256} : a);
  });
  const sumsFile = path.join(f.output, 'SHA256SUMS'), otherSha = require('node:crypto').createHash('sha256').update(await fs.readFile(other)).digest('hex');
  await fs.writeFile(sumsFile, (await fs.readFile(sumsFile, 'utf8')).replace(/^[a-f0-9]{64}(?=  glitnir_forseti-)/m, otherSha));
  await assert.rejects(verifyCandidate(f.output, identity), /plugin pin differs/, 'only the VSIX pin tells the coherent tampered release apart');
  await assert.rejects(pluginPin(f.plugin, '0.2.0'), /version differs/);
});

test('verification refuses a VSIX without a plugin pin or with a different one', async t => {
  const {checkThin, pluginPin} = require('../scripts/package-thin.cjs');
  const f = await fixture(t);
  const pins = [await pluginPin(f.plugin, identity.version)];
  const metadata = f.entries['extension/package.json'];
  await checkThin(f.vsix, identity.version, editor, metadata.glitnirBackend, pins);
  const {glitnirPlugins, ...unpinned} = metadata;
  await zip(f.vsix, {...f.entries, 'extension/package.json': unpinned});
  await assert.rejects(checkThin(f.vsix, identity.version, editor, metadata.glitnirBackend, pins), /plugin pin differs/, 'the pin must be present');
  await zip(f.vsix, {...f.entries, 'extension/package.json': {...metadata, glitnirPlugins: [{...glitnirPlugins[0], size: 1}]}});
  await assert.rejects(checkThin(f.vsix, identity.version, editor, metadata.glitnirBackend, pins), /plugin pin differs/);
});
