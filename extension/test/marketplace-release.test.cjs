const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {createWriteStream} = require('node:fs');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {pipeline} = require('node:stream/promises');
const {test} = require('node:test');
const {ZipFile} = require('yazl');
const {candidateIdentity, collectCandidate} = require('../scripts/release-candidate.cjs');
const {releaseVersion, vsixPreRelease, verifyPublishedRelease, marketplaceVersions, publicationPlan} = require('../scripts/marketplace-release.cjs');

const identity = candidateIdentity('0.1.0', {GITHUB_EVENT_NAME: 'push', GITHUB_REF: 'refs/heads/main', GITHUB_SHA: 'a'.repeat(40), GITHUB_RUN_ID: '1234', GITHUB_RUN_NUMBER: '27', GITHUB_REPOSITORY: 'owner/glitnir'});
const PRE_RELEASE = '<Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="true" />';
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
async function zip(file, entries) {
  const z = new ZipFile();
  for (const [name, data] of Object.entries(entries)) z.addBuffer(Buffer.from(typeof data === 'string' ? data : JSON.stringify(data)), name);
  const written = pipeline(z.outputStream, createWriteStream(file));
  z.end(); await written;
}
// A candidate directory as the draft workflow uploads it, with its VSIX packaged
// for the release (default) or pre-release channel.
async function candidate(t, {preRelease = false, plugins = 'pinned'} = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-marketplace-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const wheel = path.join(root, 'glitnir-0.1.0-py3-none-any.whl');
  await zip(wheel, {'glitnir/service/server.py': 'server', 'glitnir-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'});
  const bytes = await fs.readFile(wheel);
  const metadata = {publisher: 'gimle', name: 'glitnir', version: '0.1.0', glitnirBackend: {asset: path.basename(wheel), size: bytes.length, sha256: digest(bytes), tag: identity.tag}};
  const plugin = path.join(root, 'glitnir_forseti-0.1.0-py3-none-any.whl');
  await zip(plugin, {
    'glitnir_forseti/__init__.py': 'providers = []',
    'glitnir_forseti-0.1.0.dist-info/METADATA': 'Metadata-Version: 2.4\nName: glitnir-forseti\nVersion: 0.1.0\nRequires-Dist: glitnir<0.2,>=0.1.0\n\n',
    'glitnir_forseti-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    'glitnir_forseti-0.1.0.dist-info/entry_points.txt': '[glitnir.providers]\nforseti = glitnir_forseti:providers\n',
  });
  const pluginBytes = await fs.readFile(plugin);
  metadata.glitnirPlugins = [{distribution: 'glitnir-forseti', asset: path.basename(plugin), size: pluginBytes.length, sha256: digest(pluginBytes)}];
  const vsix = path.join(root, 'glitnir-0.1.0.vsix');
  const entries = {
    'extension/package.json': metadata, 'extension/dist/editor-runtime.json': {apiVersion: 2, rendererSha256: '1'.repeat(64)},
    'extension/dist/extension.js': 'code', 'extension/dist/probe.py': 'probe', 'extension/dist/providers.py': 'providers',
    'extension.vsixmanifest': `<PackageManifest><Metadata><Identity Version="0.1.0" /><Properties>${preRelease ? PRE_RELEASE : ''}</Properties></Metadata></PackageManifest>`,
  };
  await zip(vsix, entries);
  const guide = path.join(root, 'setup.md'); await fs.writeFile(guide, '# Install prerequisites\n');
  const directory = path.join(root, 'release');
  await collectCandidate(identity, vsix, wheel, plugin, guide, directory);
  // A release whose VSIX pins no plugin wheel, or other bytes: the VSIX is
  // replaced with its checksums rebound, as such a release would carry it.
  if (plugins !== 'pinned') {
    const changed = {...metadata};
    if (plugins === 'none') delete changed.glitnirPlugins;
    else changed.glitnirPlugins = [{...metadata.glitnirPlugins[0], sha256: '0'.repeat(64)}];
    const file = path.join(directory, path.basename(vsix));
    await zip(file, {...entries, 'extension/package.json': changed});
    const bytes = await fs.readFile(file);
    const manifestFile = path.join(directory, 'glitnir-candidate.json');
    const manifest = JSON.parse(await fs.readFile(manifestFile, 'utf8'));
    manifest.assets = manifest.assets.map(a => a.name === path.basename(vsix) ? {...a, size: bytes.length, sha256: digest(bytes)} : a);
    const manifestBytes = Buffer.from(JSON.stringify(manifest, null, 2) + '\n');
    await fs.writeFile(manifestFile, manifestBytes);
    const sums = manifest.assets.map(a => `${a.sha256}  ${a.name}\n`).join('') + `${digest(manifestBytes)}  glitnir-candidate.json\n`;
    await fs.writeFile(path.join(directory, 'SHA256SUMS'), sums);
  }
  return directory;
}
// The published release GitHub reports for those exact files.
async function published(directory, overrides = {}) {
  const assets = [];
  for (const name of (await fs.readdir(directory)).sort()) {
    const bytes = await fs.readFile(path.join(directory, name));
    assets.push({name, size: bytes.length, digest: `sha256:${digest(bytes)}`});
  }
  const release = {id: 7, tag_name: identity.tag, draft: false, prerelease: false, assets, ...overrides};
  return {release, tags: new Map([[identity.tag, identity.sha]]), async publishedRelease(tag) { return tag === this.release?.tag_name ? this.release : undefined; }, async tag(tag) { return this.tags.get(tag); }};
}
const options = {tag: identity.tag, repository: identity.repository};

test('release tags name exact stable versions only', () => {
  assert.equal(releaseVersion('v0.1.2'), '0.1.2');
  for (const bad of ['0.1.2', 'v0.1', 'v01.1.2', 'v0.1.2-rc1', 'v0.1.2\nx=y', 'build-12', undefined]) assert.throws(() => releaseVersion(bad), /Not a release tag/);
});

test('a published release verifies its exact VSIX, checksum and channel', async t => {
  const directory = await candidate(t);
  const api = await published(directory);
  const result = await verifyPublishedRelease(api, directory, {...options, eventSha: identity.sha});
  assert.equal(result.version, '0.1.0'); assert.equal(result.sha, identity.sha); assert.equal(result.preRelease, false);
  assert.equal(result.vsix, path.join(directory, 'glitnir-0.1.0.vsix'));
  assert.equal(result.sha256, digest(await fs.readFile(result.vsix)));
});

test('a pre-release publishes only a VSIX packaged as a pre-release', async t => {
  const stable = await candidate(t);
  await assert.rejects(verifyPublishedRelease(await published(stable, {prerelease: true}), stable, options), /GitHub pre-release but its VSIX was packaged as a release/);
  const pre = await candidate(t, {preRelease: true});
  assert.equal(await vsixPreRelease(path.join(pre, 'glitnir-0.1.0.vsix')), true);
  assert.equal((await verifyPublishedRelease(await published(pre, {prerelease: true}), pre, options)).preRelease, true);
  await assert.rejects(verifyPublishedRelease(await published(pre), pre, options), /packaged as a pre-release/);
});

test('refuses drafts, missing or moved tags and assets that differ from the release', async t => {
  const directory = await candidate(t);
  let api = await published(directory, {draft: true});
  await assert.rejects(verifyPublishedRelease(api, directory, options), /still a draft/);
  api.release = undefined;
  await assert.rejects(verifyPublishedRelease(api, directory, options), /no published release/);
  api = await published(directory);
  api.tags.set(identity.tag, 'b'.repeat(40));
  await assert.rejects(verifyPublishedRelease(api, directory, options), /identity/, 'the manifest binds the original commit');
  await assert.rejects(verifyPublishedRelease(api, directory, {...options, eventSha: identity.sha}), /moved/);
  api.tags.delete(identity.tag);
  await assert.rejects(verifyPublishedRelease(api, directory, options), /no longer exists/);
  api = await published(directory);
  api.release.assets.find(a => a.name.endsWith('.vsix')).digest = `sha256:${'0'.repeat(64)}`;
  await assert.rejects(verifyPublishedRelease(api, directory, options), /differs from the downloaded bytes/);
  api = await published(directory);
  api.release.assets.push({name: 'extra.zip', size: 1, digest: 'sha256:x'});
  await assert.rejects(verifyPublishedRelease(api, directory, options), /inventory/);
  await assert.rejects(verifyPublishedRelease(await published(directory), directory, {...options, repository: 'other/glitnir'}), /identity/);
  api = await published(directory);
  await fs.appendFile(path.join(directory, 'glitnir-0.1.0.vsix'), 'tamper');
  await assert.rejects(verifyPublishedRelease(api, directory, options), /size|checksum/);
});

test('the Marketplace listing reports every version with its channel and fails closed', async () => {
  const requests = [];
  const gallery = versions => async (url, init) => {
    requests.push({url, body: JSON.parse(init.body)});
    return {ok: true, status: 200, async json() { return {results: [{extensions: versions ? [{publisher: {publisherName: 'Gimle'}, extensionName: 'glitnir', versions}] : []}]}; }};
  };
  assert.deepEqual(await marketplaceVersions(gallery()), []);
  assert.equal(requests[0].body.filters[0].criteria[0].value, 'gimle.glitnir');
  assert.deepEqual(await marketplaceVersions(gallery([
    {version: '0.1.1', properties: []},
    {version: '0.1.2', properties: [{key: 'Microsoft.VisualStudio.Code.PreRelease', value: 'true'}]},
    {version: '0.1.3', targetPlatform: 'linux-x64'},
  ])), [
    {version: '0.1.1', targetPlatform: undefined, preRelease: false},
    {version: '0.1.2', targetPlatform: undefined, preRelease: true},
    {version: '0.1.3', targetPlatform: 'linux-x64', preRelease: false},
  ]);
  await assert.rejects(marketplaceVersions(async () => ({ok: false, status: 503})), /HTTP 503/);
  await assert.rejects(marketplaceVersions(async () => ({ok: true, status: 200, async json() { return {}; }})), /Unexpected/);
});

const release = version => ({version, preRelease: false});
const pre = version => ({version, preRelease: true});

test('a version already listed on its channel is skipped; on the other channel it fails', () => {
  assert.deepEqual(publicationPlan('0.1.2', false, [release('0.1.2')]), {publish: false});
  assert.deepEqual(publicationPlan('0.1.2', true, [pre('0.1.2')]), {publish: false});
  assert.throws(() => publicationPlan('0.1.2', true, [release('0.1.2')]), /already lists 0\.1\.2 as a release, not a pre-release/);
  assert.throws(() => publicationPlan('0.1.2', false, [pre('0.1.2')]), /already lists 0\.1\.2 as a pre-release, not a release/);
  assert.throws(() => publicationPlan('0.1.2', false, [pre('0.1.2')], {allowOlder: true}), /already lists/, 'allow_older never permits a channel mismatch');
  assert.throws(() => publicationPlan('0.1.2', false, [{version: '0.1.2', targetPlatform: 'linux-x64', preRelease: false}]), /not newer/, 'a platform-specific listing is not skipped as universal');
});

test('a version must be newer than the latest listed on its own channel unless allowed', () => {
  assert.deepEqual(publicationPlan('0.1.2', false, []), {publish: true, latest: undefined});
  assert.deepEqual(publicationPlan('0.1.10', false, [release('0.1.9'), release('0.1.2')]), {publish: true, latest: '0.1.9'}, 'numeric, not string, order');
  assert.deepEqual(publicationPlan('0.1.2', false, [pre('0.3.0'), release('0.1.1')]), {publish: true, latest: '0.1.1'}, 'the other channel does not count');
  assert.deepEqual(publicationPlan('0.2.1', true, [pre('0.2.0'), release('0.9.0')]), {publish: true, latest: '0.2.0'});
  assert.throws(() => publicationPlan('0.1.2', false, [release('0.1.3')]), /0\.1\.2 is not newer than the latest listed release 0\.1\.3; run the workflow with allow_older/);
  assert.throws(() => publicationPlan('0.1.9', true, [pre('0.1.10')]), /not newer than the latest listed pre-release 0\.1\.10/);
  assert.deepEqual(publicationPlan('0.1.2', false, [release('0.1.3')], {allowOlder: true}), {publish: true, latest: '0.1.3'});
  assert.deepEqual(publicationPlan('0.1.4', false, [release('0.1.3'), release('1.0.0-beta')]), {publish: true, latest: '0.1.3'}, 'nonnumeric listings are ignored');
  assert.throws(() => publicationPlan('0.1', false, []), /Not a release tag/);
});

test('a published VSIX must pin exactly the attached plugin wheel', async t => {
  const unpinned = await candidate(t, {plugins: 'none'});
  await assert.rejects(verifyPublishedRelease(await published(unpinned), unpinned, options), /VSIX plugin pin differs from the candidate plugin wheel/,
    'a VSIX without a plugin pin is never published');
  const other = await candidate(t, {plugins: 'other'});
  await assert.rejects(verifyPublishedRelease(await published(other), other, options), /VSIX plugin pin differs from the candidate plugin wheel/);
});
