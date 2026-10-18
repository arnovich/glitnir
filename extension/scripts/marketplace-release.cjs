// Verify a published GitHub release before its exact VSIX goes to a marketplace,
// and look up whether a marketplace already lists that version. This script never
// builds, changes or uploads anything; the workflow publishes the verified VSIX.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {openPromise} = require('yauzl');
const {verifyCandidate, github} = require('./release-candidate.cjs');

const EXTENSION = 'gimle.glitnir';
const RELEASE_TAG = /^v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$/;
const PRE_RELEASE = 'Microsoft.VisualStudio.Code.PreRelease';
const GALLERY = 'https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery';
const OPEN_VSX = 'https://open-vsx.org/api';

function releaseVersion(tag) {
  const match = RELEASE_TAG.exec(tag ?? '');
  assert(match, `Not a release tag: ${JSON.stringify(tag)}`);
  return match[1];
}
// vsce, like the Marketplace, treats the property's presence as a pre-release package.
async function vsixPreRelease(file) {
  const zip = await openPromise(file);
  for await (const entry of zip.eachEntry()) {
    if (entry.fileName !== 'extension.vsixmanifest') continue;
    assert(entry.uncompressedSize < 1024 ** 2, 'Oversized VSIX manifest');
    const chunks = [];
    for await (const chunk of await zip.openReadStreamPromise(entry)) chunks.push(chunk);
    await zip.close();
    return new RegExp(`<Property\\s+Id="${PRE_RELEASE.replace(/\./g, '\\.')}"`).test(Buffer.concat(chunks).toString('utf8'));
  }
  throw new Error('VSIX has no extension.vsixmanifest');
}
// The release must be published and its tag must still point at the commit its
// candidate manifest binds. Every downloaded byte must match both the manifest and
// the asset digests GitHub reports, and the VSIX must be packaged for the same
// channel (pre-release or release) as the GitHub release, since publishing
// cannot change that without a rebuild.
async function verifyPublishedRelease(api, directory, {tag, repository, eventSha}) {
  const version = releaseVersion(tag);
  const release = await api.publishedRelease(tag);
  assert(release, `${tag} has no published release; drafts are never published to a marketplace`);
  assert.equal(release.draft, false, `${tag} is still a draft`);
  assert.equal(release.tag_name, tag, 'Release tag differs from the requested tag');
  const sha = await api.tag(tag);
  assert(sha, `Tag ${tag} no longer exists`);
  if (eventSha) assert.equal(sha, eventSha, `Tag ${tag} moved after its release was published`);
  const verified = await verifyCandidate(directory, {repository, sha, tag, version});
  assert.equal(release.assets.length, verified.length, 'Release assets differ from the candidate inventory');
  for (const local of verified) {
    const remote = release.assets.find(asset => asset.name === local.name);
    assert(remote && remote.size === local.size && remote.digest === `sha256:${local.sha256}`, `Release asset differs from the downloaded bytes: ${local.name}`);
  }
  const vsix = `glitnir-${version}.vsix`;
  const preRelease = await vsixPreRelease(path.join(directory, vsix));
  assert.equal(preRelease, release.prerelease === true, release.prerelease
    ? `${tag} is a GitHub pre-release but its VSIX was packaged as a release; mark the GitHub release as a full release or prepare a new version`
    : `${tag} is a full GitHub release but its VSIX was packaged as a pre-release; prepare a new version`);
  return {version, sha, preRelease, vsix: path.join(directory, vsix), sha256: verified.find(asset => asset.name === vsix).sha256};
}
// Versions in validation are listed too: the Marketplace rejects any reuse of a version.
async function marketplaceListing(version, request = fetch) {
  const response = await request(GALLERY, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', Accept: 'application/json;api-version=3.0-preview.1'},
    body: JSON.stringify({filters: [{criteria: [{filterType: 7, value: EXTENSION}, {filterType: 8, value: 'Microsoft.VisualStudio.Code'}], pageNumber: 1, pageSize: 1}], flags: 0x1 | 0x10}),
  });
  assert(response.ok, `Marketplace query failed (HTTP ${response.status})`);
  const extensions = (await response.json())?.results?.[0]?.extensions;
  assert(Array.isArray(extensions), 'Unexpected Marketplace query response');
  const extension = extensions.find(e => `${e.publisher?.publisherName}.${e.extensionName}`.toLowerCase() === EXTENSION);
  const listed = extension?.versions?.find(v => v.version === version && !v.targetPlatform);
  return listed ? {preRelease: (listed.properties ?? []).some(p => p.key === PRE_RELEASE)} : undefined;
}
async function openVsxListing(version, request = fetch) {
  const response = await request(`${OPEN_VSX}/${EXTENSION.replace('.', '/')}/${version}`, {headers: {Accept: 'application/json'}});
  if (response.status === 404) return false;
  assert(response.ok, `Open VSX query failed (HTTP ${response.status})`);
  return true;
}
async function output(values) {
  const lines = Object.entries(values).map(([key, value]) => `${key}=${value}\n`).join('');
  if (process.env.GITHUB_OUTPUT) await fs.appendFile(process.env.GITHUB_OUTPUT, lines);
  else process.stdout.write(lines);
}
async function main() {
  const [command, argument] = process.argv.slice(2);
  if (command === 'verify') {
    const repository = process.env.GITHUB_REPOSITORY;
    assert.match(repository ?? '', /^[\w.-]+\/[\w.-]+$/);
    const result = await verifyPublishedRelease(github(repository), argument, {tag: process.env.RELEASE_TAG, repository, eventSha: process.env.RELEASE_EVENT_SHA || undefined});
    // A publishing job re-downloads the assets; they must be the bytes verified first.
    if (process.env.VERIFIED_VSIX_SHA256 !== undefined) assert.equal(result.sha256, process.env.VERIFIED_VSIX_SHA256, 'Release VSIX changed since it was verified');
    console.log(`Verified ${path.basename(result.vsix)} (sha256 ${result.sha256}) from ${process.env.RELEASE_TAG} at ${result.sha}${result.preRelease ? ' as a pre-release' : ''}`);
    await output({version: result.version, vsix: result.vsix, sha256: result.sha256, prerelease: result.preRelease});
  } else if (command === 'marketplace') {
    const listed = await marketplaceListing(releaseVersion(`v${argument}`));
    console.log(listed ? `The Marketplace already lists ${EXTENSION} ${argument}${listed.preRelease ? ' (pre-release)' : ''}` : `The Marketplace does not list ${EXTENSION} ${argument}`);
    await output({listed: Boolean(listed)});
  } else if (command === 'open-vsx') {
    const listed = await openVsxListing(releaseVersion(`v${argument}`));
    console.log(`Open VSX ${listed ? 'already lists' : 'does not list'} ${EXTENSION} ${argument}`);
    await output({listed});
  } else throw new Error('Usage: marketplace-release.cjs verify DIRECTORY | marketplace VERSION | open-vsx VERSION');
}
module.exports = {releaseVersion, vsixPreRelease, verifyPublishedRelease, marketplaceListing, openVsxListing};
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
