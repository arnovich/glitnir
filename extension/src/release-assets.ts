import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import * as path from 'node:path';
import {BackendDescriptor} from './backend-install';
import {DownloadOptions, downloadRuntime, RELEASE_REPOSITORY} from './runtime-download';

/** The candidate manifest every Glitnir release carries, and its schema. */
export const MANIFEST = 'glitnir-candidate.json';
export const MANIFEST_SCHEMA = 'glitnir-thin-candidate/v2';
export const SUMS = 'SHA256SUMS';
const MAX_MANIFEST_BYTES = 1024 ** 2, MAX_SUMS_BYTES = 64 * 1024, MAX_PLUGIN_BYTES = 50 * 1024 ** 2;
const DIGEST = /^[a-f0-9]{64}$/;
const ASSET_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const DISTRIBUTION = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;
/** A release plugin wheel: `<distribution>-<version>-py3-none-any.whl`. */
const PLUGIN_WHEEL = /^([A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-py3-none-any\.whl$/;
const BACKEND_WHEEL = /^glitnir-([\w.]+)-py3-none-any\.whl$/;

/** Where to go when a release file cannot be downloaded or verified. */
export const RELEASE_FALLBACK = 'download the wheel and SHA256SUMS from the release yourself, verify them, and choose it with Glitnir: Install Extension Package… → Choose downloaded wheel';

/**
 * A provider wheel this VSIX pins, like its backend: `glitnirPlugins` in its
 * package.json, written at packaging time from the plugin wheel built with it.
 * Only a pinned wheel is ever downloaded from a release; the release's own
 * manifest and checksums must agree with the pin, never replace it.
 */
export interface PluginPin {distribution: string; asset: string; size: number; sha256: string}
/** A pinned plugin wheel, verified to be the one the release binds. */
export interface ReleasePlugin {name: string; asset: string; version: string; size: number; sha256: string}
export interface VerifiedRelease {tag: string; version: string; plugins: ReleasePlugin[]}

/** PEP 503 name normalization. */
export const normalizedName = (name: string): string => name.replace(/[-_.]+/g, '-').toLowerCase();

/** The plugin pins of this VSIX; none for a build packaged without a plugin wheel. A malformed pin is refused. */
export function pluginPins(value: unknown): PluginPin[] {
  if (value === undefined) return [];
  const invalid = (): never => { throw new Error('This extension has an invalid plugin pin. Install an official VSIX, or install extension packages from a wheel file.'); };
  if (!Array.isArray(value)) return invalid();
  const pins = value.map(item => {
    const pin = item as PluginPin;
    const file = typeof pin?.asset === 'string' ? PLUGIN_WHEEL.exec(pin.asset) : null;
    if (!file || typeof pin.distribution !== 'string' || !DISTRIBUTION.test(pin.distribution) || normalizedName(file[1]) !== pin.distribution ||
      !Number.isSafeInteger(pin.size) || pin.size <= 0 || pin.size > MAX_PLUGIN_BYTES || typeof pin.sha256 !== 'string' || !DIGEST.test(pin.sha256)) invalid();
    return {distribution: pin.distribution, asset: pin.asset, size: pin.size, sha256: pin.sha256};
  });
  if (new Set(pins.map(pin => pin.distribution)).size !== pins.length) invalid();
  return pins;
}

/** `SHA256SUMS` as written by the release: one `<sha256>  <name>` line per file, each name once. */
export function parseSums(text: string): Map<string, string> {
  const sums = new Map<string, string>();
  if (!text.endsWith('\n')) throw new Error('The release SHA256SUMS file is malformed.');
  for (const line of text.slice(0, -1).split('\n')) {
    const match = /^([a-f0-9]{64}) {2}([^\s/\\]+)$/.exec(line);
    if (!match || sums.has(match[2])) throw new Error('The release SHA256SUMS file is malformed.');
    sums.set(match[2], match[1]);
  }
  return sums;
}

const refuse = (detail: string): never => { throw new Error(`The release ${MANIFEST} does not match this Glitnir build: ${detail}. Nothing was installed from it.`); };

/**
 * Check a release's manifest and checksums against this VSIX's pins: the same
 * repository, tag and version, the pinned backend wheel among its assets,
 * every asset (and the manifest itself) listed in `SHA256SUMS` with the same
 * digest, and a plugin binding equal to each plugin wheel the VSIX pins.
 * Returns the pinned plugin wheels; anything that does not match is refused.
 */
export function verifyRelease(manifestBytes: Buffer, sumsText: string, pin: BackendDescriptor, version: string, pins: PluginPin[]): VerifiedRelease {
  let manifest: Record<string, unknown>;
  try { manifest = JSON.parse(manifestBytes.toString('utf8')); } catch { return refuse('it is not JSON'); }
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) refuse('it is not an object');
  if (manifest.schema !== MANIFEST_SCHEMA) refuse(`its schema is ${String(manifest.schema)}, not ${MANIFEST_SCHEMA}`);
  if (manifest.repository !== RELEASE_REPOSITORY) refuse(`it names the repository ${String(manifest.repository)}`);
  if (!pin.tag || manifest.tag !== pin.tag) refuse(`its tag ${String(manifest.tag)} is not this build's ${pin.tag ?? '(none)'}`);
  if (manifest.version !== version) refuse(`its version ${String(manifest.version)} is not this build's ${version}`);
  const backendVersion = BACKEND_WHEEL.exec(pin.asset)?.[1];
  if (backendVersion !== version) refuse(`this build's backend ${pin.asset} is not version ${version}`);
  const assets = manifest.assets;
  if (!Array.isArray(assets) || !assets.length) return refuse('it lists no assets');
  const byName = new Map<string, {size: number; sha256: string}>();
  for (const asset of assets as Record<string, unknown>[]) {
    if (!asset || typeof asset.name !== 'string' || !ASSET_NAME.test(asset.name) || asset.name === MANIFEST || asset.name === SUMS ||
      !Number.isSafeInteger(asset.size) || (asset.size as number) <= 0 || typeof asset.sha256 !== 'string' || !DIGEST.test(asset.sha256) || byName.has(asset.name)) refuse('its asset list is malformed');
    byName.set(asset.name as string, {size: asset.size as number, sha256: asset.sha256 as string});
  }
  const backend = byName.get(pin.asset);
  if (!backend || backend.size !== pin.size || backend.sha256 !== pin.sha256) refuse(`its backend ${pin.asset} differs from the one this build pins`);
  const sums = parseSums(sumsText);
  const expected = new Map([...[...byName].map(([name, asset]) => [name, asset.sha256] as const), [MANIFEST, createHash('sha256').update(manifestBytes).digest('hex')]]);
  if (sums.size !== expected.size || [...expected].some(([name, digest]) => sums.get(name) !== digest)) refuse(`it differs from the release ${SUMS}`);
  const plugin = manifest.plugin as Record<string, unknown> | undefined;
  let bound: PluginPin | undefined;
  if (plugin !== undefined) {
    const file = typeof plugin?.asset === 'string' ? PLUGIN_WHEEL.exec(plugin.asset) : null;
    const asset = file ? byName.get(plugin.asset as string) : undefined;
    if (!file || !asset || plugin.version !== version || file[2] !== version || plugin.size !== asset.size || plugin.sha256 !== asset.sha256) refuse('its plugin binding differs from its assets or version');
    bound = {distribution: normalizedName(file![1]), asset: plugin.asset as string, size: asset!.size, sha256: asset!.sha256};
  }
  // The VSIX pin is the authority: the release must bind exactly the pinned bytes.
  for (const pinned of pins) {
    if (!bound || bound.distribution !== pinned.distribution) refuse(`it does not bind ${pinned.asset}, which this build pins`);
    if (bound!.asset !== pinned.asset || bound!.size !== pinned.size || bound!.sha256 !== pinned.sha256) refuse(`its ${pinned.asset} differs from the one this build pins`);
  }
  return {tag: pin.tag!, version, plugins: pins.map(p => ({name: p.distribution, asset: p.asset, version, size: p.size, sha256: p.sha256}))};
}

export interface ReleaseFetch extends Omit<DownloadOptions, 'fallback'> {
  /** Replaces the download, for tests. */
  download?: typeof downloadRuntime;
}

const source = (pin: BackendDescriptor, asset: string) => ({repository: RELEASE_REPOSITORY, tag: pin.tag!, asset});

/** Download and verify the manifest and checksums of the release this VSIX's backend comes from, into `directory`. */
export async function fetchRelease(pin: BackendDescriptor, version: string, pins: PluginPin[], directory: string, options: ReleaseFetch): Promise<VerifiedRelease> {
  if (!pin.tag) throw new Error('This Glitnir build names no release to download from. Use a wheel you downloaded instead.');
  const download = options.download ?? downloadRuntime;
  const files: Buffer[] = [];
  for (const [name, maxSize] of [[MANIFEST, MAX_MANIFEST_BYTES], [SUMS, MAX_SUMS_BYTES]] as const) {
    const file = path.join(directory, name);
    await download(file, {source: source(pin, name), archive: {maxSize}}, {...options, fallback: RELEASE_FALLBACK});
    files.push(await readFile(file));
  }
  return verifyRelease(files[0], files[1].toString('utf8'), pin, version, pins);
}

/** The SHA-256 of `bytes`, hex. */
export const digest = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex');

/**
 * Download one pinned plugin wheel and return its bytes, read once: the same
 * buffer is checked against the pinned size and SHA-256 and then installed.
 */
export async function fetchPlugin(pin: BackendDescriptor, plugin: ReleasePlugin, directory: string, options: ReleaseFetch): Promise<Buffer> {
  const file = path.join(directory, plugin.asset);
  await (options.download ?? downloadRuntime)(file, {source: source(pin, plugin.asset), archive: {size: plugin.size}}, {...options, fallback: RELEASE_FALLBACK});
  const bytes = await readFile(file);
  if (bytes.length !== plugin.size || digest(bytes) !== plugin.sha256) throw new Error(`${plugin.asset} does not match the SHA-256 this extension records for it. Nothing was installed from it.`);
  return bytes;
}
