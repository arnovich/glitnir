import * as path from 'node:path';
import {execFile} from 'node:child_process';
import {access} from 'node:fs/promises';

export interface Environment {python: string; processEnv?: NodeJS.ProcessEnv}
export interface ProbeResult {python: string}
export type Probe = (python: string) => Promise<ProbeResult>;
export interface DiscoveryInput {
  cwd: string;
  notebook: string;
  roots: string[];
  pythonOverride?: string;
  selectedPython?: string;
  sharedPython?: string;
  remembered?: Environment;
  cancelled?: () => boolean;
  log?: (text: string) => void;
}

export class DiscoveryError extends Error {}

/** An inspection probe runs no Lean jobs, so timeout/cancellation can kill it. */
export async function probePython(python: string, script: string, signal: AbortSignal, timeoutMs = 15000, env?: NodeJS.ProcessEnv): Promise<ProbeResult> {
  if (python.includes(path.sep)) await access(python);
  return new Promise((resolve, reject) => {
    const child = execFile(python, ['-I', '-B', script], {
      timeout: timeoutMs, maxBuffer: 65536, killSignal: 'SIGKILL', env,
    }, (error, stdout, stderr) => {
      signal.removeEventListener('abort', abort);
      try {
        const value = JSON.parse(stdout);
        if (error || value.error) throw new Error(value.error || stderr || error?.message);
        if (typeof value.python !== 'string') throw new Error('Invalid environment probe response');
        resolve(value);
      } catch (failure) { reject(new Error(`${python}: ${stdout.trim() ? String(failure) : error?.message || stderr || String(failure)}`)); }
    });
    // Node's execFile AbortSignal path can use SIGTERM independently of the
    // timeout's killSignal. Own cancellation explicitly for this bounded probe.
    const abort = () => { child.kill('SIGKILL'); };
    signal.addEventListener('abort', abort, {once: true});
    if (signal.aborted) abort();
  });
}

function parents(directory: string): string[] {
  const result: string[] = [];
  for (let i = 0; i < 6 && path.dirname(directory) !== directory; i++) {
    result.push(directory);
    directory = path.dirname(directory);
  }
  return result;
}

/** Discover an existing backend installation. An explicit override never silently falls back. */
export async function discover(input: DiscoveryInput, probe: Probe): Promise<Environment> {
  const check = () => { if (input.cancelled?.()) throw new Error('Environment discovery cancelled'); };
  const executable = (value: string) => value.includes(path.sep) ? path.resolve(input.cwd, value) : value;
  const roots = [...new Set([input.cwd, ...input.roots, ...parents(path.dirname(input.notebook))])];
  const pythonPaths = input.pythonOverride ? [executable(input.pythonOverride)] : [
    input.remembered?.python, input.selectedPython, input.sharedPython,
    ...roots.flatMap(root => [
      path.join(root, '.venv/bin/python'),
      path.join(root, 'glitnir/.venv/bin/python'),
      path.join(path.dirname(root), 'glitnir/.venv/bin/python'),
    ]), 'python3',
  ];
  let lastError = '';
  for (const python of new Set(pythonPaths.filter((p): p is string => !!p))) {
    check();
    let selected: ProbeResult;
    try { selected = await probe(python); }
    catch (error) { lastError = String(error); input.log?.(`${python}: ${lastError}\n`); continue; }
    check();
    return {python: selected.python};
  }
  check();
  throw new DiscoveryError(input.pythonOverride
    ? `The explicit Python override could not load the Glitnir backend: ${lastError}`
    : 'No Python environment with the Glitnir backend was found. Select an interpreter with glitnir installed.');
}
