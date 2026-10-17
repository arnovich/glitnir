const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const test = require('node:test');

// Cold-loading the packaging tool can exceed 30 seconds on macOS. This test
// verifies scanner behavior, not dependency startup speed.
const STARTUP_TIMEOUT_MS = 120000;

test('managed packaging still scans clean text and rejects synthetic private keys', () => {
  const program = `
    const {lintText} = require('@vscode/vsce/out/secretLint');
    (async () => {
      const {secretLintProfiler} = await import('@secretlint/profiler');
      const clean = await lintText('A harmless notebook description.', 'README.md', true, true);
      const fakeKey = ['-----BEGIN RSA PRIVATE KEY-----', 'A'.repeat(128), '-----END RSA PRIVATE KEY-----'].join('\\n');
      const secret = await lintText(fakeKey, 'synthetic-key.pem', true, true);
      await new Promise(resolve => setImmediate(resolve));
      console.log(JSON.stringify({clean: clean.ok, secret: secret.ok,
        rules: secret.results.map(result => result.ruleId),
        marks: (await secretLintProfiler.getEntries()).length}));
    })().catch(error => {console.error(error); process.exitCode = 1;});
  `;
  const run = spawnSync(process.execPath, ['--import', path.resolve(__dirname, '../scripts/package-scan-profiling.mjs'), '-e', program], {encoding: 'utf8', timeout: STARTUP_TIMEOUT_MS});
  assert.equal(run.status, 0, run.stderr);
  const result = JSON.parse(run.stdout);
  assert.equal(result.clean, true);
  assert.equal(result.secret, false);
  assert.ok(result.rules.some(rule => rule.endsWith('@secretlint/secretlint-rule-privatekey')));
  assert.equal(result.marks, 0);
  const baseline = spawnSync(process.execPath, ['-e', program], {encoding: 'utf8', timeout: STARTUP_TIMEOUT_MS});
  assert.equal(baseline.status, 0, baseline.stderr);
  const original = JSON.parse(baseline.stdout);
  assert.ok(original.marks > 0);
  assert.deepEqual({...original, marks: 0}, result);
});
