const assert = require('node:assert/strict');
const {test} = require('node:test');
const {spawnSync} = require('node:child_process');
const {validationCommand} = require('../../dist/validation-command');

test('validation command preserves special filenames and exposes no unrelated environment values', () => {
  const file = "/tmp/notebook '$(echo injected).lean";
  const command = validationCommand({python: '/usr/bin/printf', processEnv: {HOME: '/home/owner', PATH: '/usr/bin:/bin', TOKEN: 'secret', FORSETI_LEAN_PROJECT: '/release'}}, file);
  assert.ok(!command.includes('secret'));
  assert.ok(!command.includes('FORSETI_LEAN_PROJECT'), 'no pinned Lean release is passed');
  assert.ok(!command.includes('--no-write'), 'check never writes, so no flag is passed');
  // Replace only the fixed executable invocation with a shell argv printer.
  const args = command.replace("'/usr/bin/printf'", "'/bin/sh' '-c' 'printf \"%s\\n\" \"$@\"' 'sh'");
  const result = spawnSync('/bin/sh', ['-c', args], {encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.stdout.split('\n').slice(-4), ['glitnir.notebook', 'check', file, ''], 'check takes only the file');
});
