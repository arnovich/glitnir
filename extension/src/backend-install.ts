import {constants, createWriteStream} from 'node:fs';
import {Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {access, lstat, mkdir, mkdtemp, open, readFile, rm, writeFile} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import * as os from 'node:os';
import * as path from 'node:path';
import {ProbeResult} from './discovery';
import {fileDigest, privateDirectory} from './runtime-store';

export interface BackendDescriptor {asset: string; size: number; sha256: string; tag?: string}
/** This pin comes from the installed extension, never the selected wheel or workspace. */
export function backendDescriptor(value: unknown): BackendDescriptor {
  const d = value as BackendDescriptor;
  if (!d || !/^glitnir-[\w.]+-py3-none-any\.whl$/.test(d.asset ?? '') ||
      !/^[a-f0-9]{64}$/.test(d.sha256 ?? '') || !Number.isSafeInteger(d.size) || d.size <= 0 || d.size > 25 * 1024 ** 2 ||
      (d.tag !== undefined && !/^(?:build-[1-9]\d*|v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?![\s\S])/.test(d.tag))) throw new Error('This extension has no valid backend installation pin. Install an official thin VSIX or use an existing environment.');
  return d;
}
export function backendStore(): string { return path.join(os.homedir(), '.local/share/glitnir/backends'); }
const installedPaths = (): string[] => [path.join(os.homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'];
/** GUI apps may not inherit Homebrew's PATH. Never discover uv in a workspace. */
export async function findUv(candidates = installedPaths().map(dir => path.join(dir, 'uv'))): Promise<string> {
  for (const file of candidates) {
    try { await access(file, constants.X_OK); return file; } catch { /* Try the next installed location. */ }
  }
  throw new Error('Install uv first (on macOS: brew install uv), then run Set Up Environment again. See the installation guide.');
}
/** The uv release whose official installer Install uv runs when Homebrew is not installed. */
export const UV_VERSION = '0.12.23';
/** Homebrew, looked up where it installs itself, never in a workspace. */
export async function findBrew(candidates = ['/opt/homebrew/bin/brew', '/usr/local/bin/brew', '/home/linuxbrew/.linuxbrew/bin/brew']): Promise<string | undefined> {
  for (const file of candidates) {
    try { await access(file, constants.X_OK); return file; } catch { /* Try the next installed location. */ }
  }
  return undefined;
}
/** The only directories on PATH while Install uv runs, besides Homebrew's own. */
export const INSTALLER_PATH = '/usr/bin:/bin:/usr/sbin:/sbin';
export interface UvInstaller {
  /** What runs, shown before the click: absolute paths only. */
  command: string;
  /** The complete environment it runs with; nothing else is inherited. */
  env: Record<string, string>;
}
/**
 * What Install uv runs: Homebrew from its fixed location when installed,
 * otherwise the official installer of the pinned uv version, fetched by
 * /usr/bin/curl and run by /bin/sh. Never an unversioned URL. The environment
 * is built here (a minimal PATH, HOME, no HOMEBREW_* or other parent
 * variables). The official installer puts uv in ~/.local/bin, where Glitnir
 * looks without PATH, so it is told not to edit shell profiles.
 */
export function uvInstaller(brew?: string, home = os.homedir()): UvInstaller {
  if (!/^\d+\.\d+\.\d+$/.test(UV_VERSION)) throw new Error('The uv installer version must be pinned.');
  if (brew && !/^\/[\w./-]+\/brew$/.test(brew)) throw new Error('Homebrew must be at an absolute path without spaces.');
  const base = {HOME: home, LANG: 'en_US.UTF-8'};
  if (brew) return {command: `${brew} install uv`, env: {...base, PATH: `${path.dirname(brew)}:${INSTALLER_PATH}`}};
  return {command: `/usr/bin/curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | /bin/sh`, env: {...base, PATH: INSTALLER_PATH, UV_NO_MODIFY_PATH: '1'}};
}
/** The terminal Install uv runs in: its environment is exactly `env` (strictEnv), so no parent or terminal setting adds to it. */
export function uvInstallerTerminal(installer: UvInstaller): {name: string; shellPath: string; shellArgs: string[]; env: Record<string, string>; strictEnv: true} {
  return {name: 'Install uv', shellPath: '/bin/sh', shellArgs: ['-c', installer.command], env: installer.env, strictEnv: true};
}
/** Do not inherit workspace venvs, uv configuration, indexes or Python injection. */
export function uvEnvironment(original: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  return {HOME: os.homedir(), PATH: installedPaths().join(path.delimiter),
    ...(original.TMPDIR ? {TMPDIR: original.TMPDIR} : {}), LANG: 'en_US.UTF-8'};
}
export interface UvOptions {
  /** Receives uv's output as it arrives. */
  log?: (text: string) => void;
  timeoutMs?: number;
  /** The message of the error thrown when uv fails. */
  failure?: string;
}
/** uv could not be started at all, so nothing was changed. */
export class UvNotStartedError extends Error {}
/** Run uv without a shell; a timeout or cancellation kills it. */
export async function runUv(uv: string, args: string[], cwd: string, signal: AbortSignal, options: UvOptions = {}): Promise<void> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const child = spawn(uv, args, {cwd, env: uvEnvironment(), stdio: ['ignore', 'pipe', 'pipe']});
    let timedOut = false, started = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, options.timeoutMs ?? 300000);
    for (const stream of [child.stdout, child.stderr]) {
      stream.setEncoding('utf8');
      stream.on('data', (text: string) => options.log?.(text));
    }
    const abort = (): void => { child.kill('SIGKILL'); };
    let settled = false;
    const finish = (error?: Error): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      if (error instanceof UvNotStartedError) reject(error);
      else if (signal.aborted) reject(signal.reason);
      else if (error) reject(error);
      else resolve();
    };
    const failure = (): Error => new Error(options.failure ?? 'uv could not prepare the backend. Ensure Python 3.12+ is installed and PyPI is reachable, then retry. See the installation guide.');
    child.on('spawn', () => { started = true; });
    child.on('error', error => finish(started ? failure() : new UvNotStartedError(`uv could not be started at ${uv} (${(error as NodeJS.ErrnoException).code ?? error.message}). Install uv (on macOS: brew install uv) and retry.`)));
    child.on('close', code => finish(timedOut ? new Error(`${failure().message} uv did not finish within ${Math.round((options.timeoutMs ?? 300000) / 1000)} s.`) : code === 0 ? undefined : failure()));
    signal.addEventListener('abort', abort, {once: true});
    if (signal.aborted) abort();
  });
}
/** Hold the imported file open; growth and cancellation stop before unbounded writes. */
export async function copyBackendWheel(source: string, destination: string, size: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  const input = await open(source, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await input.stat();
    if (!info.isFile() || info.size !== size) throw new Error('Backend wheel size or file type does not match this extension.');
    let copied = 0;
    const bound = new Transform({transform(chunk, _encoding, callback) {
      copied += chunk.length;
      callback(copied > size ? new Error('Backend wheel grew beyond its pinned size during import.') : null, chunk);
    }});
    await pipeline(input.createReadStream({autoClose: false, signal}), bound, createWriteStream(destination, {flags: 'wx', mode: 0o600}), {signal});
    if (copied !== size) throw new Error('Backend wheel size changed during import.');
  } finally { await input.close(); }
}
interface InstallOptions {
  signal: AbortSignal;
  uv: string;
  wheel?: string;
  acquire?: (destination: string, signal: AbortSignal) => Promise<void>;
  probe: (python: string) => Promise<ProbeResult>;
  progress?: (message: string) => void;
  run?: typeof runUv;
}
/** Keep venv paths stable (scripts contain absolute paths) and never overwrite one. */
export async function installBackend(store: string, value: BackendDescriptor, options: InstallOptions): Promise<string> {
  const d = backendDescriptor(value), signal = options.signal;
  signal.throwIfAborted();
  await privateDirectory(store);
  const directory = path.join(store, d.sha256), lock = directory + '.lock';
  try { await mkdir(lock, {mode: 0o700}); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new Error(`Backend setup is already in progress. Retry after it finishes. If a previous setup process terminated, remove ${lock} and its incomplete environment only after confirming no setup is running.`);
    throw error;
  }
  const python = path.join(directory, 'bin/python');
  let scratch: string | undefined, created = false;
  try {
    signal.throwIfAborted();
    let existing;
    try { existing = await lstat(directory); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    if (existing) {
      if (!existing.isDirectory() || existing.isSymbolicLink()) throw new Error('Backend environment must be a real directory.');
      let marker;
      try { marker = await readFile(path.join(directory, '.glitnir-backend'), 'utf8'); } catch { /* Never repair an unknown directory in place. */ }
      if (marker !== d.sha256) throw new Error('Backend directory is incomplete or unrecognized. Choose an existing environment or remove that unused directory manually before retrying.');
      const result = await options.probe(python); signal.throwIfAborted();
      if (path.resolve(result.python) !== python) throw new Error('Backend probe returned a different Python environment.');
      return python;
    }
    scratch = await mkdtemp(path.join(store, '.install-'));
    const wheel = path.join(scratch, d.asset);
    options.progress?.('Verifying the matching backend');
    if (options.wheel) {
      await copyBackendWheel(options.wheel, wheel, d.size, signal);
    } else if (options.acquire) await options.acquire(wheel, signal);
    else throw new Error('Select the matching backend wheel from the release.');
    if (await fileDigest(wheel, signal, d.size) !== d.sha256) throw new Error('Backend wheel checksum does not match this extension.');
    signal.throwIfAborted();
    options.progress?.('Creating an environment with installed Python');
    // Reserve this path exclusively before uv writes it; cleanup owns only this directory.
    await mkdir(directory, {mode: 0o700}); created = true;
    const run = options.run ?? runUv;
    await run(options.uv, ['venv', '--no-config', '--no-python-downloads', '--python', '>=3.12', directory], scratch, signal);
    signal.throwIfAborted();
    options.progress?.('Installing the backend');
    await run(options.uv, ['pip', 'install', '--no-config', '--no-python-downloads', '--python', python, '--index-url', 'https://pypi.org/simple', '--only-binary', ':all:', '--no-deps', wheel], scratch, signal);
    signal.throwIfAborted();
    const result = await options.probe(python);
    if (path.resolve(result.python) !== python) throw new Error('Backend probe returned a different Python environment.');
    signal.throwIfAborted();
    await writeFile(path.join(directory, '.glitnir-backend'), d.sha256, {flag: 'wx', mode: 0o600});
    signal.throwIfAborted();
    created = false;
    return python;
  } finally {
    try {
      if (created) await rm(directory, {recursive: true, force: true});
      if (scratch) await rm(scratch, {recursive: true, force: true});
    } finally { await rm(lock, {recursive: true, force: true}); }
  }
}
