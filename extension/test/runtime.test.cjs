const assert = require('node:assert/strict');
const {test} = require('node:test');
const {spawnSync} = require('node:child_process');
const {mkdtempSync, mkdirSync, copyFileSync, writeFileSync, readFileSync, rmSync} = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';

for (const field of [null, 'apiVersion', 'rendererSha256']) {
  test(`installed runtime probe ${field ? 'rejects mismatched ' + field : 'accepts its built renderer'}`, () => {
    const root = mkdtempSync(path.join(os.tmpdir(), 'glitnir-runtime-'));
    try {
      copyFileSync(path.resolve(__dirname, '../scripts/probe.py'), path.join(root, 'probe.py'));
      const expected = JSON.parse(readFileSync(path.resolve(__dirname, '../../dist/editor-runtime.json'), 'utf8'));
      if (field) expected[field] = field === 'apiVersion' ? -1 : 'wrong';
      writeFileSync(path.join(root, 'editor-runtime.json'), JSON.stringify(expected));
      const result = spawnSync(python, ['-I', path.join(root, 'probe.py')], {encoding: 'utf8', cwd: root});
      const value = JSON.parse(result.stdout);
      assert.equal(result.status, field ? 1 : 0, result.stdout + result.stderr);
      if (field) assert.match(value.error, /incompatible with this Glitnir build/);
      else {
        const installed = spawnSync(python, ['-I', '-c', 'import sys; print(sys.executable)'], {encoding: 'utf8'});
        assert.equal(installed.status, 0, installed.stderr);
        assert.equal(value.python, installed.stdout.trim());
        assert.ok(!('release' in value), 'the probe does not depend on a pinned Lean release');
        assert.ok(!('project' in value));
        assert.deepEqual(value.editor, expected);
      }
    } finally { rmSync(root, {recursive: true, force: true}); }
  });
}

test('installed runtime probe takes no Lean project argument', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'glitnir-project-'));
  try {
    copyFileSync(path.resolve(__dirname, '../scripts/probe.py'), path.join(root, 'probe.py'));
    copyFileSync(path.resolve(__dirname, '../../dist/editor-runtime.json'), path.join(root, 'editor-runtime.json'));
    const result = spawnSync(python, ['-I', path.join(root, 'probe.py'), root], {encoding: 'utf8'});
    assert.equal(result.status, 1, result.stdout + result.stderr);
    assert.match(JSON.parse(result.stdout).error, /no arguments/);
  } finally { rmSync(root, {recursive: true, force: true}); }
});
