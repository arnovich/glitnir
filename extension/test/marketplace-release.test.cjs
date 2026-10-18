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
const {releaseVersion, vsixPreRelease, verifyPublishedRelease, marketplaceListing, openVsxListing} = require('../scripts/marketplace-release.cjs');

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
async function candidate(t, {preRelease = false} = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-marketplace-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const wheel = path.join(root, 'glitnir-0.1.0-py3-none-any.whl');
  await zip(wheel, {'glitnir/notebook/server.py': 'server', 'glitnir-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'});
  const bytes = await fs.readFile(wheel);
  const metadata = {publisher: 'gimle', name: 'glitnir', version: '0.1.0', glitnirBackend: {asset: path.basename(wheel), size: bytes.length, sha256: digest(bytes), tag: identity.tag}};
  const vsix = path.join(root, 'glitnir-0.1.0.vsix');
  await zip(vsix, {
    'extension/package.json': metadata, 'extension/dist/editor-runtime.json': {apiVersion: 2, rendererSha256: '1'.repeat(64)},
    'extension/dist/extension.js': 'code', 'extension/dist/probe.py': 'probe', 'extension/dist/providers.py': 'providers',
    'extension.vsixmanifest': `<PackageManifest><Metadata><Identity Version="0.1.0" /><Properties>${preRelease ? PRE_RELEASE : ''}</Properties></Metadata></PackageManifest>`,
  });
  const plugin = path.join(root, 'glitnir_forseti-0.1.0-py3-none-any.whl');
  await zip(plugin, {
    'glitnir_forseti/__init__.py': 'providers = []',
    'glitnir_forseti-0.1.0.dist-info/METADATA': 'Metadata-Version: 2.4\nName: glitnir-forseti\nVersion: 0.1.0\nRequires-Dist: glitnir<0.2,>=0.1.0\n\n',
    'glitnir_forseti-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    'glitnir_forseti-0.1.0.dist-info/entry_points.txt': '[glitnir.providers]\nforseti = glitnir_forseti:providers\n',
  });
  const guide = path.join(root, 'setup.md'); await fs.writeFile(guide, '# Install prerequisites\n');
  const directory = path.join(root, 'release');
  await collectCandidate(identity, vsix, wheel, plugin, guide, directory);
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

test('marketplace lookups find existing versions, including pre-releases, and fail closed', async () => {
  const requests = [];
  const gallery = versions => async (url, init) => {
    requests.push({url, body: JSON.parse(init.body)});
    return {ok: true, status: 200, async json() { return {results: [{extensions: versions ? [{publisher: {publisherName: 'Gimle'}, extensionName: 'glitnir', versions}] : []}]}; }};
  };
  assert.equal(await marketplaceListing('0.1.2', gallery()), undefined);
  assert.equal(requests[0].body.filters[0].criteria[0].value, 'gimle.glitnir');
  assert.equal(await marketplaceListing('0.1.2', gallery([{version: '0.1.1', properties: []}])), undefined);
  assert.deepEqual(await marketplaceListing('0.1.2', gallery([{version: '0.1.2', properties: []}])), {preRelease: false});
  assert.deepEqual(await marketplaceListing('0.1.2', gallery([{version: '0.1.2', properties: [{key: 'Microsoft.VisualStudio.Code.PreRelease', value: 'true'}]}])), {preRelease: true});
  assert.equal(await marketplaceListing('0.1.2', gallery([{version: '0.1.2', targetPlatform: 'linux-x64'}])), undefined);
  await assert.rejects(marketplaceListing('0.1.2', async () => ({ok: false, status: 503})), /HTTP 503/);
  await assert.rejects(marketplaceListing('0.1.2', async () => ({ok: true, status: 200, async json() { return {}; }})), /Unexpected/);
  const openVsx = status => async url => { assert.equal(url, 'https://open-vsx.org/api/gimle/glitnir/0.1.2'); return {ok: status === 200, status}; };
  assert.equal(await openVsxListing('0.1.2', openVsx(200)), true);
  assert.equal(await openVsxListing('0.1.2', openVsx(404)), false);
  await assert.rejects(openVsxListing('0.1.2', openVsx(500)), /HTTP 500/);
});
