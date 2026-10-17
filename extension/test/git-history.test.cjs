const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {mkdtempSync, writeFileSync, readFileSync, mkdirSync, rmSync, existsSync, chmodSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {test} = require('node:test');
const {GitHistory} = require('../../dist/git-history');

function fixture(t, repository = true) {
  const root = mkdtempSync(path.join(tmpdir(), 'glitnir-history-'));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  const git = (...args) => execFileSync('git', ['-C', root, ...args], {encoding: 'utf8'}).trim();
  if (repository) {
    git('init', '-q', '-b', 'main');
    git('config', 'user.name', 'History Tester');
    git('config', 'user.email', 'history@example.invalid');
    // Commits otherwise start detached auto-maintenance. Since Git 2.54 its
    // geometric repack runs when two loose objects share objects/17, which can
    // pack objects in the background while a test is changing the store.
    git('config', 'maintenance.auto', 'false');
    git('config', 'gc.auto', '0');
  }
  const file = path.join(root, 'Example [1].lean');
  writeFileSync(file, 'theorem first : True := by trivial\n');
  return {root, file, git, commit(message = 'First theorem') { git('add', '--', '.'); git('commit', '-qm', message); return git('rev-parse', 'HEAD'); }};
}

test('loose files, missing Git, untracked files and unborn branches have distinct states', async t => {
  const loose = fixture(t, false);
  assert.equal((await new GitHistory(loose.file).read()).state, 'no_repository');
  assert.equal((await new GitHistory(loose.file, '/missing/glitnir-git').read()).state, 'unavailable');
  const f = fixture(t);
  const snapshot = await new GitHistory(f.file).read();
  assert.equal(snapshot.state, 'repository');
  assert.equal(snapshot.head, null);
  assert.equal(snapshot.branch, 'main');
  assert.equal(snapshot.fileState, 'untracked');
  assert.deepEqual(snapshot.commits, []);
  writeFileSync(path.join(f.root, '.gitignore'), '*.lean\n');
  assert.equal((await new GitHistory(f.file).read()).fileState, 'ignored');
});

test('file history excludes other files and returns bounded, read-only commit diffs', async t => {
  const f = fixture(t);
  const first = f.commit('<first> & theorem');
  writeFileSync(path.join(f.root, 'Other.lean'), 'private theorem other : True := by trivial\n');
  f.commit('Unrelated file');
  writeFileSync(f.file, 'theorem second : True := by trivial\n');
  const second = f.commit('Second theorem');
  const history = new GitHistory(f.file);
  const before = readFileSync(path.join(f.root, '.git/index'));
  const snapshot = await history.read();
  assert.equal(snapshot.fileState, 'clean');
  assert.equal(snapshot.head, second);
  assert.equal(snapshot.branch, 'main');
  assert.deepEqual(snapshot.commits.map(c => c.sha), [second, first]);
  assert.equal(snapshot.commits[1].subject, '<first> & theorem');
  assert.equal(snapshot.commits[0].author, 'History Tester');
  assert.match(snapshot.commits[0].date, /^\d{4}-\d{2}-\d{2}T/);
  const diff = await history.diff(second);
  assert.match(diff, /-theorem first/);
  assert.match(diff, /\+theorem second/);
  assert.doesNotMatch(diff, /private theorem other/);
  assert.match(await history.diff(first), /\+theorem first/);
  assert.deepEqual(readFileSync(path.join(f.root, '.git/index')), before);
  await assert.rejects(history.diff('--help'), /Select a commit/);
  await assert.rejects(history.diff('a'.repeat(40)), /Select a commit/);
  await assert.rejects(new GitHistory(f.file).diff(second), /Select a commit/);
});

test('file state distinguishes staged, unstaged, detached and unrelated changes', async t => {
  const f = fixture(t); f.commit();
  const history = new GitHistory(f.file);
  writeFileSync(path.join(f.root, 'Other.lean'), 'unrelated');
  assert.equal((await history.read()).fileState, 'clean');
  writeFileSync(f.file, 'changed');
  assert.equal((await history.read()).fileState, 'modified');
  f.git('add', '--', f.file);
  assert.equal((await history.read()).fileState, 'staged');
  writeFileSync(f.file, 'changed again');
  assert.equal((await history.read()).fileState, 'staged_and_modified');
  f.git('checkout', '--detach', '-q');
  assert.equal((await history.read()).branch, null);
});

test('history works in linked worktrees and nested directories', async t => {
  const f = fixture(t); const sha = f.commit();
  const worktree = path.join(f.root, 'linked');
  f.git('worktree', 'add', '-q', '--detach', worktree);
  const snapshot = await new GitHistory(path.join(worktree, path.basename(f.file))).read();
  assert.equal(snapshot.head, sha);
  assert.equal(snapshot.branch, null);
  assert.equal(snapshot.commits.length, 1);
  f.git('worktree', 'remove', worktree);
  mkdirSync(path.join(f.root, 'nested'));
  const nested = path.join(f.root, 'nested', '-odd *.lean');
  writeFileSync(nested, 'theorem nested : True := by trivial');
  f.commit('Nested literal filename');
  assert.equal((await new GitHistory(nested).read()).commits.length, 1);
});

test('repository filters, fsmonitor and external diff helpers do not execute', async t => {
  const f = fixture(t); const sha = f.commit();
  const marker = path.join(f.root, 'executed');
  const helper = path.join(f.root, 'helper');
  writeFileSync(helper, `#!/bin/sh\ntouch '${marker}'\ncat\n`);
  chmodSync(helper, 0o755);
  f.git('config', 'core.fsmonitor', helper);
  f.git('config', 'diff.external', helper);
  f.git('config', 'diff.unsafe.textconv', helper);
  f.git('config', 'filter.unsafe.clean', helper);
  f.git('config', 'filter.unsafe.process', helper);
  f.git('config', 'filter.unsafe.required', 'true');
  writeFileSync(path.join(f.root, '.gitattributes'), '*.lean diff=unsafe filter=unsafe\n');
  writeFileSync(f.file, 'changed file\n');
  const history = new GitHistory(f.file);
  assert.equal((await history.read()).fileState, 'filtered');
  assert.match(await history.diff(sha), /theorem first/);
  assert.equal(existsSync(marker), false);
});

test('history caps commits and reports more without changing Git', async t => {
  const f = fixture(t);
  for (let n = 0; n < 32; n++) { writeFileSync(f.file, `-- ${n}\n`); f.commit(`Change ${n}`); }
  const snapshot = await new GitHistory(f.file).read();
  assert.equal(snapshot.commits.length, 30);
  assert.equal(snapshot.more, true);
});

test('a missing historical blob cannot invoke promisor remote helpers or fetch objects', async t => {
  const f = fixture(t); const sha = f.commit();
  const blob = f.git('rev-parse', `HEAD:${path.basename(f.file)}`);
  writeFileSync(f.file, 'theorem second : True := by trivial\n'); f.commit('Second');
  const object = path.join(f.root, '.git/objects', blob.slice(0, 2), blob.slice(2));
  rmSync(object);
  // The blob must be gone from every object store (packs, alternates), not just loose.
  const present = () => execFileSync('git', ['-C', f.root, 'cat-file', '-e', blob], {stdio: 'ignore', env: {...process.env, GIT_NO_LAZY_FETCH: '1'}});
  assert.throws(present);
  const marker = path.join(f.root, 'executed');
  f.git('config', 'remote.origin.url', `ext::sh -c touch% ${marker}`);
  f.git('config', 'remote.origin.promisor', 'true');
  f.git('config', 'remote.origin.partialclonefilter', 'blob:none');
  f.git('config', 'protocol.ext.allow', 'always');
  const history = new GitHistory(f.file);
  assert.equal((await history.read()).state, 'repository');
  const outcome = await history.diff(sha).then(diff => ({diff}), error => ({error}));
  // Check for a helper run first, so a fetch attempt is reported whatever diff returned.
  assert.equal(existsSync(marker), false);
  assert.match(outcome.error?.message ?? `resolved: ${outcome.diff}`, /unavailable/);
  assert.equal(existsSync(object), false);
  assert.throws(present);
});

test('content filters cannot produce a false clean or modified claim', async t => {
  const f = fixture(t);
  writeFileSync(path.join(f.root, '.gitattributes'), '*.lean filter=normal\n');
  f.git('config', 'filter.normal.clean', 'tr a-z A-Z');
  f.commit();
  writeFileSync(f.file, readFileSync(f.file, 'utf8').toUpperCase());
  assert.equal(f.git('status', '--porcelain', '--', path.basename(f.file)), '');
  assert.equal((await new GitHistory(f.file).read()).fileState, 'filtered');
});
