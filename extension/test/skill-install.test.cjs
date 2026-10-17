const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const {installSkill} = require('../../dist/skill-install');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-skill-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const bundle = path.join(root, 'bundle'), workspace = path.join(root, 'workspace');
  await fs.mkdir(path.join(bundle, 'references'), {recursive: true});
  await fs.mkdir(workspace);
  await fs.writeFile(path.join(bundle, 'SKILL.md'), '---\nname: glitnir-notebooks\ndescription: Write Lean notebooks\n---\nVersion one\n');
  await fs.writeFile(path.join(bundle, 'references/runtime.md'), 'Runtime instructions');
  return {bundle, workspace, destination: path.join(workspace, '.agents/skills/glitnir-notebooks')};
}

test('install, repeat and update only an untouched managed skill', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  assert.equal(await installSkill(bundle, workspace, '.agents/skills', '0.8.3'), 'installed');
  assert.equal(await installSkill(bundle, workspace, '.agents/skills', '0.8.3'), 'current');
  await fs.writeFile(path.join(bundle, 'references/runtime.md'), 'Updated runtime');
  assert.equal(await installSkill(bundle, workspace, '.agents/skills', '0.8.4'), 'updated');
  assert.equal(await fs.readFile(path.join(destination, 'references/runtime.md'), 'utf8'), 'Updated runtime');
  const recovery = path.join(workspace, '.glitnir-skill-backups');
  const [backup] = await fs.readdir(recovery);
  assert.equal(await fs.readFile(path.join(recovery, backup, 'references/runtime.md'), 'utf8'), 'Runtime instructions');
});

test('preserves edited files and unrelated agent instructions', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await fs.writeFile(path.join(workspace, 'AGENTS.md'), 'Owner instructions');
  await installSkill(bundle, workspace, '.agents/skills', '0.8.3');
  await fs.writeFile(path.join(destination, 'SKILL.md'), 'User edit');
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.4'), /edited|modified/);
  assert.equal(await fs.readFile(path.join(destination, 'SKILL.md'), 'utf8'), 'User edit');
  assert.equal(await fs.readFile(path.join(workspace, 'AGENTS.md'), 'utf8'), 'Owner instructions');
});

test('refuses unmanaged destinations, added files and symlink parents', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await fs.mkdir(destination, {recursive: true});
  await fs.writeFile(path.join(destination, 'notes.txt'), 'Keep');
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.3'), /existing|managed/);
  assert.equal(await fs.readFile(path.join(destination, 'notes.txt'), 'utf8'), 'Keep');
  await fs.rm(destination, {recursive: true});
  await installSkill(bundle, workspace, '.agents/skills', '0.8.3');
  await fs.writeFile(path.join(destination, 'notes.txt'), 'Keep');
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.4'), /added|modified/);
  await fs.symlink(path.join(workspace, '.agents'), path.join(workspace, '.claude'));
  await assert.rejects(installSkill(bundle, workspace, '.claude/skills', '0.8.3'), /symbolic|symlink/);
});

test('rejects cancellation and traversal before installation', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await assert.rejects(installSkill(bundle, workspace, '../outside', '0.8.3'), /destination/);
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.3', () => false), /cancelled/);
  await assert.rejects(fs.stat(destination), {code: 'ENOENT'});
});


test('preserves added files with JavaScript object-property names', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await installSkill(bundle, workspace, '.agents/skills', '0.8.3');
  await fs.writeFile(path.join(destination, '__proto__'), 'Owner data');
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.4'), /added|modified/);
  assert.equal(await fs.readFile(path.join(destination, '__proto__'), 'utf8'), 'Owner data');
});

test('preserves added empty directories', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await installSkill(bundle, workspace, '.agents/skills', '0.8.3');
  await fs.mkdir(path.join(destination, 'owner-notes'));
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.4'), /added empty directory/);
  assert.ok((await fs.stat(path.join(destination, 'owner-notes'))).isDirectory());
});

test('rolls back cancellation after moving the previous installation', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await installSkill(bundle, workspace, '.agents/skills', '0.8.3');
  let checks = 0;
  await assert.rejects(installSkill(bundle, workspace, '.agents/skills', '0.8.4', () => ++checks < 6), /cancelled/);
  assert.match(await fs.readFile(path.join(destination, '.glitnir-install.json'), 'utf8'), /0.8.3/);
  assert.ok(!(await fs.readdir(path.dirname(destination))).some(name => name.includes('stage') || name.includes('lock')));
});

test('retains a concurrent write to the previous tree after its final check', async t => {
  const {bundle, workspace, destination} = await fixture(t);
  await installSkill(bundle, workspace, '.agents/skills', '0.8.3');
  const sync = require('node:fs');
  let checks = 0, backup;
  const allowed = () => {
    if (++checks === 6) {
      const recovery = path.join(workspace, '.glitnir-skill-backups');
      backup = path.join(recovery, sync.readdirSync(recovery)[0]);
      sync.writeFileSync(path.join(backup, 'SKILL.md'), 'Concurrent owner edit');
    }
    return true;
  };
  assert.equal(await installSkill(bundle, workspace, '.agents/skills', '0.8.4', allowed), 'updated');
  assert.equal(await fs.readFile(path.join(backup, 'SKILL.md'), 'utf8'), 'Concurrent owner edit');
});
