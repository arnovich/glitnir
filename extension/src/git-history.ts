import {execFile} from 'node:child_process';
import * as path from 'node:path';

interface Commit { sha: string; author: string; date: string; subject: string }
interface RepositoryHistory {
  state: 'repository';
  branch: string | null;
  head: string | null;
  fileState: 'clean' | 'modified' | 'staged' | 'staged_and_modified' | 'conflicted' | 'untracked' | 'ignored' | 'filtered';
  commits: Commit[];
  more: boolean;
}
type History = RepositoryHistory | {state: 'no_repository' | 'unavailable'; reason: string};
const LIMIT = 30;

/** Read local Git observations for one fixed preview file, without changing it. */
export class GitHistory {
  private commits = new Set<string>();
  private reading?: Promise<History>;
  private diffPending = false;

  constructor(private readonly file: string, private readonly executable = 'git') {}

  /** Bound subprocesses, discard inherited Git routing, and disable executable helpers. */
  private git(args: string[]): Promise<string> {
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('GIT_')));
    return new Promise((resolve, reject) => {
      execFile(this.executable, ['--no-pager', '--literal-pathspecs', '-c', 'core.fsmonitor=false', '-c', 'log.showSignature=false', ...args], {
        cwd: path.dirname(this.file), env: {...env, LC_ALL: 'C', GIT_OPTIONAL_LOCKS: '0', GIT_TERMINAL_PROMPT: '0', GIT_NO_LAZY_FETCH: '1', GIT_ALLOW_PROTOCOL: ''},
        encoding: 'utf8', timeout: 5000, maxBuffer: 512 * 1024,
      }, (error, stdout, stderr) => {
        if (error) reject(Object.assign(error, {stderr}));
        else resolve(stdout);
      });
    });
  }

  /** Coalesce refreshes; only commits from a successful read can be selected. */
  read(): Promise<History> {
    if (!this.reading) this.reading = this.inspect().finally(() => { this.reading = undefined; });
    return this.reading;
  }

  private async inspect(): Promise<History> {
    this.commits.clear();
    try {
      await this.git(['rev-parse', '--show-toplevel']);
      const file = path.basename(this.file);
      // Even `status` can execute a repository's clean/process filter. Disable
      // configured filters for this observation, as well as the fsmonitor above.
      let filterNames = '';
      try { filterNames = await this.git(['config', '--null', '--name-only', '--get-regexp', '^filter\\.']); }
      catch (error) { if ((error as {code?: number}).code !== 1) throw error; }
      const filters = new Set(filterNames.split('\0').filter(key => /^filter\..+\.(clean|smudge|process|required)$/.test(key)).map(key => key.slice(0, key.lastIndexOf('.'))));
      const overrides = [...filters].flatMap(name => ['-c', `${name}.clean=`, '-c', `${name}.smudge=`, '-c', `${name}.process=`, '-c', `${name}.required=false`]);
      const attributes = await this.git(['check-attr', '-z', 'filter', '--', file]);
      const filtered = filters.has(`filter.${attributes.split('\0')[2]}`);
      const status = await this.git([...overrides, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored', '--', file]);
      const code = status.slice(0, 2);
      const fileState: RepositoryHistory['fileState'] = code === '??' ? 'untracked' : code === '!!' ? 'ignored'
        : code.includes('U') || ['AA', 'DD'].includes(code) ? 'conflicted' : filtered ? 'filtered'
        : !code ? 'clean' : code[0] !== ' ' && code[1] !== ' ' ? 'staged_and_modified'
        : code[0] !== ' ' ? 'staged' : 'modified';
      let branch: string | null;
      try { branch = (await this.git(['symbolic-ref', '--quiet', '--short', 'HEAD'])).trim(); }
      catch (error) { if ((error as {code?: number}).code !== 1) throw error; branch = null; }
      let head: string | null;
      try { head = (await this.git(['rev-parse', '--verify', '--quiet', 'HEAD'])).trim(); }
      catch (error) { if ((error as {code?: number}).code !== 1) throw error; head = null; }
      // Current path only: do not imply rename-following or whole-repository history.
      const log = head ? await this.git(['log', '-z', `-n${LIMIT + 1}`, '--format=%H%x00%an%x00%aI%x00%s', head, '--', file]) : '';
      const fields = log.split('\0');
      if (fields.at(-1) === '') fields.pop();
      if (fields.length % 4) throw new Error('Could not parse Git history.');
      const commits: Commit[] = [];
      for (let i = 0; i < fields.length; i += 4) {
        const [sha, author, date, subject] = fields.slice(i, i + 4);
        if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(sha)) throw new Error('Could not parse Git commit.');
        commits.push({sha, author, date, subject});
      }
      const listed = commits.slice(0, LIMIT);
      this.commits = new Set(listed.map(commit => commit.sha));
      return {state: 'repository', branch, head, fileState, commits: listed, more: commits.length > LIMIT};
    } catch (error) {
      const failure = error as {code?: string; stderr?: string};
      if (failure.stderr?.includes('not a git repository')) return {state: 'no_repository', reason: 'Not in a Git repository.'};
      return {state: 'unavailable', reason: failure.code === 'ENOENT' ? 'Git is not installed or is not on PATH.' : 'Git history could not be read. The repository may be unavailable or the read exceeded its time or size limit.'};
    }
  }

  /** Show a commit against its first parent, or the empty tree for a root commit. */
  async diff(sha: unknown): Promise<string> {
    if (typeof sha !== 'string' || !this.commits.has(sha)) throw new Error('Select a commit from the current file history.');
    if (this.diffPending) throw new Error('A Git diff is already loading.');
    this.diffPending = true;
    try {
      return await this.git(['show', '--format=', '--first-parent', '--root', '--no-ext-diff', '--no-textconv', '--no-color', sha, '--', path.basename(this.file)]);
    } catch {
      throw new Error('This diff is unavailable or exceeds the 512 KiB / 5 second limit. Open it in your Git client.');
    } finally { this.diffPending = false; }
  }
}
