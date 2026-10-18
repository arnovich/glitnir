import {ChildProcess, spawn} from 'node:child_process';
import * as http from 'node:http';
import {randomBytes} from 'node:crypto';
import {createInterface} from 'node:readline';

export interface BackendOptions {
  python: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  startupTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  log?: (text: string) => void;
}

export interface Reply {
  status: number;
  json?: any;
  /** An image's bytes as base64, from a successful `/asset` request. */
  data?: string;
  contentType?: string;
}

/**
 * How long one request may take. No route waits for Lean: checks, inspections, view drawings, proof steps,
 * evidence and decisions run in backend workers bounded by the check time limit, and the preview polls them.
 */
export const REQUEST_TIMEOUT_MS = 130000;

/** Image types the preview shows; anything else from `/asset` is refused. */
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'];

function object(body: string | undefined): any {
  let value: any;
  try { value = JSON.parse(body ?? ''); } catch { return undefined; }
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value : undefined;
}

/** A run or discard names exactly the saved source digest and a directive line. */
function directive(body: string | undefined): boolean {
  const value = object(body);
  return value !== undefined && Object.keys(value).sort().join(',') === 'line,sha256'
    && typeof value.sha256 === 'string' && /^[0-9a-f]{64}$/.test(value.sha256)
    && Number.isSafeInteger(value.line) && value.line > 0;
}

/**
 * A check names the saved source digest and, for a check of lines 1 through one line only, that line as a
 * positive integer; nothing else.
 */
function check(body: string | undefined): boolean {
  const value = object(body);
  if (value === undefined || typeof value.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.sha256)) return false;
  const keys = Object.keys(value).sort().join(',');
  if (keys === 'sha256') return true;
  return keys === 'sha256,through_line' && Number.isSafeInteger(value.through_line) && value.through_line > 0;
}

/** Stopping names nothing: there is at most one decision running. */
function empty(body: string | undefined): boolean {
  const value = object(body);
  return value !== undefined && Object.keys(value).length === 0;
}

/** Exact body shapes of the routes that start or end provider work, and of checks. */
const BODIES: Record<string, (body: string | undefined) => boolean> = {
  '/api/evidence/run': directive, '/api/decide/run': directive, '/api/decide/discard': directive,
  '/api/decide/stop': empty, '/api/view/cancel': directive, '/api/project/inspect/cancel': empty,
  '/api/project/check': check,
};

/**
 * Posts that only stop work; they never depend on the editor's text, so unsaved
 * Lean files do not hold them back. `media/bridge.js` keeps the same list.
 */
export const STOP_PATHS: readonly string[] = ['/api/proof/cancel', '/api/decide/stop', '/api/view/cancel', '/api/project/inspect/cancel'];

/** Whether a preview request must wait until unsaved Lean files are saved or reverted. */
export function heldWhileDirty(path: string, method: string, dirty: boolean): boolean {
  return dirty && method === 'POST' && !STOP_PATHS.includes(path);
}

/** Exact queries of the reads that name one thing: a decision candidate names only its key. */
const QUERIES: Record<string, RegExp> = {
  '/api/decide/candidate': /^\?key=[0-9a-f]{64}$/,
  // A poll for one view job names only its id.
  '/api/view/job': /^\?id=[0-9a-f]{32}$/,
};

/**
 * The preview can observe, check, trace, run evidence and decisions for saved files, and read another notebook of
 * the same project by its link, but cannot edit Lean source: a decision's block reaches the file only through the
 * editor, which reads it from the candidate route.
 */
export function validateRequest(path: string, method: string, body?: string): string {
  const allowed: Record<string, string[]> = {
    '/api/notebook': [], '/api/proof/trace': [], '/api/export/styles': [], '/api/view': ['cell', 'line'],
    '/asset': ['path', 'sha256'], '/api/linked': ['path'],
  };
  const post = ['/api/proof/start', '/api/proof/cancel', '/api/project/inspect', ...Object.keys(BODIES)];
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) throw new Error('Invalid preview path');
  const url = new URL(path, 'http://127.0.0.1');
  if (url.pathname !== path.split('?')[0] || url.hash) throw new Error('Invalid preview path');
  if (method === 'GET' && Object.hasOwn(QUERIES, url.pathname)) {
    if (!QUERIES[url.pathname].test(path.slice(url.pathname.length))) throw new Error('Invalid preview query');
  } else if (method === 'GET' && Object.hasOwn(allowed, url.pathname)) {
    for (const key of url.searchParams.keys()) {
      if (!allowed[url.pathname].includes(key)) throw new Error('Invalid preview query');
    }
  } else if (!(method === 'POST' && post.includes(path))) {
    throw new Error('This action is unavailable in the saved-file preview');
  }
  if (Object.hasOwn(BODIES, path) && !BODIES[path](body)) throw new Error(`Invalid request to ${path}`);
  return path;
}

/** One owned Python service, using its OS-assigned port and no shell. */
export class Backend {
  readonly ready: Promise<void>;
  port = 0;
  private child: ChildProcess;
  private readonly token = randomBytes(32).toString('hex');
  private closed = false;
  private exited = false;
  private closing?: Promise<void>;
  private rejectReady!: (error: Error) => void;
  private requests = new Set<http.ClientRequest>();
  private shutdownTimeoutMs: number;

  get running(): boolean { return !this.closed && !this.exited; }

  constructor(options: BackendOptions) {
    this.shutdownTimeoutMs = options.shutdownTimeoutMs ?? 7000;
    this.child = spawn(options.python, options.args, {
      cwd: options.cwd, env: {...options.env, GLITNIR_SERVER_TOKEN: this.token}, shell: false,
      detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.ready = new Promise((resolve, reject) => {
      this.rejectReady = reject;
      const timeout = setTimeout(() => {
        reject(new Error('Glitnir server startup timed out. Check the Glitnir output channel.'));
        void this.close();
      }, options.startupTimeoutMs ?? 30000);
      const lines = createInterface({input: this.child.stdout!});
      lines.on('line', line => {
        options.log?.(line + '\n');
        const match = /^Serving .+ at http:\/\/127\.0\.0\.1:(\d+)\/  \(Ctrl-C to stop\)$/.exec(line);
        if (match && !this.closed) {
          const port = Number(match[1]);
          if (port > 0 && port < 65536) {
            this.port = port;
            clearTimeout(timeout);
            resolve();
          }
        }
      });
      this.child.stderr!.on('data', data => options.log?.(data.toString()));
      this.child.on('error', error => { clearTimeout(timeout); this.exited = true; reject(error); });
      this.child.on('exit', (code, signal) => {
        clearTimeout(timeout);
        this.exited = true;
        lines.close();
        reject(new Error(`Glitnir server exited (${signal ?? code}). Check the Glitnir output channel.`));
      });
    });
  }

  async request(path: string, method = 'GET', body?: string): Promise<Reply> {
    validateRequest(path, method, body);
    if (body && Buffer.byteLength(body) > 1_000_000) throw new Error('Preview request too large');
    await this.ready;
    if (this.closed || this.exited) throw new Error('Glitnir server is closed. Reopen the preview.');
    return new Promise((resolve, reject) => {
      const request = http.request({
        hostname: '127.0.0.1', port: this.port, path, method,
        headers: {'X-Glitnir': this.token, ...(body ? {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body)} : {})},
      }, response => {
        const chunks: Buffer[] = [];
        let size = 0;
        response.on('data', (chunk: Buffer) => {
          size += chunk.length;
          if (size > 20_000_000) response.destroy(new Error('Preview response too large'));
          else chunks.push(chunk);
        });
        response.on('error', reject);
        response.on('end', () => {
          const data = Buffer.concat(chunks);
          const status = response.statusCode ?? 500;
          if (path.startsWith('/asset?') && status === 200) {
            const contentType = response.headers['content-type']?.split(';')[0].trim() ?? '';
            if (!IMAGE_TYPES.includes(contentType)) { reject(new Error('Unsupported image type')); return; }
            resolve({status, data: data.toString('base64'), contentType});
            return;
          }
          try { resolve({status, json: data.length ? JSON.parse(data.toString()) : {}}); }
          catch { reject(new Error(`Glitnir backend returned an invalid response (${status})`)); }
        });
      });
      this.requests.add(request);
      request.once('close', () => this.requests.delete(request));
      request.once('error', reject);
      request.setTimeout(REQUEST_TIMEOUT_MS, () => request.destroy(new Error('Glitnir request timed out')));
      request.end(body);
    });
  }

  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    this.rejectReady(new Error('Glitnir preview closed'));
    for (const request of this.requests) request.destroy(new Error('Glitnir preview closed'));
    this.closing = new Promise(resolve => {
      if (this.exited || !this.child.pid) { resolve(); return; }
      const signal = (value: NodeJS.Signals) => {
        try {
          if (process.platform === 'win32') this.child.kill(value);
          else process.kill(-this.child.pid!, value);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ESRCH') this.child.kill(value);
        }
      };
      // Interrupt the Python main loop so its finally block closes the service.
      signal('SIGINT');
      const force = setTimeout(() => signal('SIGKILL'), this.shutdownTimeoutMs);
      this.child.once('exit', () => { clearTimeout(force); resolve(); });
    });
    return this.closing;
  }
}
