import {createWriteStream} from 'node:fs';
import {IncomingMessage} from 'node:http';
import * as https from 'node:https';
import {Readable} from 'node:stream';
import {pipeline} from 'node:stream/promises';

/**
 * A pinned asset has an exact size. A release's small text files (its
 * checksums and manifest) are pinned by digest instead, so only their
 * maximum size is known before the release metadata names it.
 */
export interface AssetDownload {source: {url: string} | {repository: string; tag: string; asset: string}; archive: {size: number} | {maxSize: number}}

/** The repository whose releases this extension downloads from. */
export const RELEASE_REPOSITORY = 'arnovich/glitnir';

/** Where to go when a download cannot finish: the same setup flow, with a wheel the user fetched. */
const DOWNLOADED_WHEEL = 'run Glitnir: Set Up Environment → More Options… → Choose downloaded backend wheel (see the setup guide)';

export interface DownloadOptions {
  signal: AbortSignal;
  githubToken?: () => Promise<string>;
  progress?: (message: string) => void;
  /** Where to go instead when the download cannot finish; defaults to the downloaded-backend-wheel setup path. */
  fallback?: string;
}

/** Redirects may change storage hosts; authorization never travels with them. */
export function redirectTarget(location: string, previous: URL): URL {
  const next = new URL(location, previous);
  if (next.protocol !== 'https:' || next.username || next.password) throw new Error('Runtime download redirected to an unsafe URL.');
  return next;
}

async function response(url: URL, signal: AbortSignal, headers: Record<string, string> = {}, redirects = 0): Promise<IncomingMessage> {
  signal.throwIfAborted();
  const result = await new Promise<IncomingMessage>((resolve, reject) => {
    const request = https.get(url, {headers: {'User-Agent': 'Glitnir-runtime', ...headers}, signal}, resolve);
    request.setTimeout(60000, () => request.destroy(new Error(`Runtime download timed out. Retry, or ${DOWNLOADED_WHEEL}.`)));
    request.on('error', reject);
  });
  if ([301, 302, 303, 307, 308].includes(result.statusCode ?? 0)) {
    result.destroy();
    if (!result.headers.location || redirects >= 5) throw new Error('Runtime download exceeded redirect limits.');
    return response(redirectTarget(result.headers.location, url), signal, {}, redirects + 1);
  }
  return result;
}

async function credential(getToken: () => Promise<string>, signal: AbortSignal): Promise<string> {
  signal.throwIfAborted();
  let abort: () => void = () => {};
  try {
    return await Promise.race([getToken(), new Promise<never>((_resolve, reject) => {
      abort = () => reject(signal.reason);
      signal.addEventListener('abort', abort, {once: true});
      if (signal.aborted) abort();
    })]);
  } finally { signal.removeEventListener('abort', abort); }
}

async function githubAsset(d: AssetDownload, options: DownloadOptions): Promise<{url: URL; headers: Record<string, string>; size: number}> {
  const fallback = options.fallback ?? DOWNLOADED_WHEEL;
  if ('url' in d.source) {
    if (!('size' in d.archive)) throw new Error('Runtime download needs a pinned size.');
    return {url: new URL(d.source.url), headers: {}, size: d.archive.size};
  }
  const source = d.source;
  const url = new URL(`https://api.github.com/repos/${source.repository}/releases/tags/${encodeURIComponent(source.tag)}`);
  let headers: Record<string, string> = {Accept: 'application/vnd.github+json'};
  let result = await response(url, options.signal, headers);
  if ([401, 403, 404].includes(result.statusCode ?? 0) && options.githubToken) {
    result.destroy();
    headers = {...headers, Authorization: `Bearer ${await credential(options.githubToken, options.signal)}`};
    options.signal.throwIfAborted();
    result = await response(url, options.signal, headers);
  }
  if (result.statusCode !== 200) {
    const status = result.statusCode; result.destroy();
    throw new Error(`Runtime release is unavailable (HTTP ${status}). Check GitHub access, or ${fallback}.`);
  }
  const chunks: Buffer[] = []; let size = 0;
  for await (const chunk of result) {
    size += chunk.length;
    if (size > 1024 ** 2) { result.destroy(); throw new Error('Runtime release metadata exceeds size limit.'); }
    chunks.push(chunk);
  }
  const metadata = JSON.parse(Buffer.concat(chunks).toString('utf8')) as {assets?: {id: number; name: string; size: number}[]};
  const matches = metadata.assets?.filter(a => a.name === source.asset) ?? [];
  const asset = matches[0];
  const sized = 'size' in d.archive ? asset?.size === d.archive.size
    : Number.isSafeInteger(asset?.size) && asset.size > 0 && asset.size <= d.archive.maxSize;
  if (matches.length !== 1 || !Number.isSafeInteger(asset.id) || asset.id <= 0 || !sized) throw new Error(`Pinned runtime asset is missing or has an unexpected size. Download the matching ${options.fallback ? 'file' : 'wheel'} from the release and ${fallback}.`);
  return {url: new URL(`https://api.github.com/repos/${source.repository}/releases/assets/${asset.id}`), headers: {...headers, Accept: 'application/octet-stream'}, size: asset.size};
}

/** Stream a bounded download; the installer verifies its pinned SHA-256 before extraction. */
export async function downloadRuntime(destination: string, descriptor: AssetDownload, options: DownloadOptions): Promise<void> {
  const fallback = options.fallback ?? DOWNLOADED_WHEEL;
  const signal = AbortSignal.any([options.signal, AbortSignal.timeout(60 * 60 * 1000)]);
  let input: IncomingMessage | undefined;
  try {
    const source = await githubAsset(descriptor, {...options, signal});
    let bytes = 0, lastPercent = -1;
    async function* chunks(): AsyncGenerator<Buffer> {
      input = await response(source.url, signal, source.headers);
      try {
        if (input.statusCode !== 200) throw new Error(`Runtime download failed (HTTP ${input.statusCode}). Retry, or ${fallback}.`);
        if (input.headers['content-length'] && Number(input.headers['content-length']) !== source.size) throw new Error(`Runtime download size does not match the pinned archive. Retry, or ${fallback}.`);
        for await (const chunk of input) {
          bytes += chunk.length;
          if (bytes > source.size) throw new Error(`Runtime download exceeded its expected size. Retry, or ${fallback}.`);
          const percent = Math.floor(bytes / source.size * 100);
          if (percent !== lastPercent) { options.progress?.(`Downloading Glitnir runtime: ${percent}%`); lastPercent = percent; }
          yield chunk;
        }
      } finally { input.destroy(); }
    }
    await pipeline(Readable.from(chunks(), {objectMode: false}), createWriteStream(destination, {flags: 'wx', mode: 0o600}), {signal});
    if (bytes !== source.size) throw new Error(`Runtime download was incomplete. Retry, or ${fallback}.`);
  } catch (error) {
    if (options.signal.aborted) options.signal.throwIfAborted();
    // Native HTTP errors may include signed URLs. Only expose our own fixed messages.
    const message = error instanceof Error && /^(Runtime|Pinned runtime)/.test(error.message) ? error.message : `Runtime download failed. Check your connection and GitHub access, or ${fallback}.`;
    throw new Error(message);
  } finally { input?.destroy(); }
}
