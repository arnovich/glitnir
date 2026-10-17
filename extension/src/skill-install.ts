import {createHash, randomUUID} from 'node:crypto';
import {constants} from 'node:fs';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';

const skillName = 'glitnir-notebooks';
const manifestName = '.glitnir-install.json';
const schema = 'glitnir-skill/v1';
type Files = Record<string, Buffer>;
interface Manifest {schema: string; extensionVersion: string; files: Record<string, string>}
const digest = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex');
const hashes = (files: Files): Record<string, string> => Object.fromEntries(Object.keys(files).sort().map(name => [name, digest(files[name])]));
const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);
const missing = (error: unknown): boolean => (error as NodeJS.ErrnoException).code === 'ENOENT';

/** Read a small skill tree, rejecting links and unexpected filesystem objects. */
async function readTree(root: string): Promise<Files> {
  const result: Files = Object.create(null);
  let bytes = 0;
  let entries = 0;
  async function visit(relative: string): Promise<void> {
    if (++entries > 128) throw new Error('Skill contents exceed the installation limits.');
    const file = path.join(root, relative);
    const info = await fs.lstat(file);
    if (info.isSymbolicLink()) throw new Error('A symbolic link prevents safe skill installation. Choose another folder.');
    if (info.isDirectory()) {
      if (relative.split(path.sep).length > 8) throw new Error('Skill directory is too deeply nested.');
      const children: string[] = [];
      for await (const child of await fs.opendir(file)) {
        if (children.length + entries >= 128) throw new Error('Skill contents exceed the installation limits.');
        children.push(child.name);
      }
      children.sort();
      if (relative && !children.length) throw new Error('Skill contents have an added empty directory. Export to another folder to preserve it.');
      for (const child of children) await visit(path.join(relative, child));
    } else if (info.isFile()) {
      if (Object.keys(result).length >= 32 || info.size > 256 * 1024) throw new Error('Skill contents exceed the installation limits.');
      const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      let content: Buffer;
      try {
        const opened = await handle.stat();
        if (!opened.isFile() || opened.dev !== info.dev || opened.ino !== info.ino || opened.size > 256 * 1024) throw new Error('Skill files changed during installation.');
        const buffer = Buffer.alloc(256 * 1024 + 1);
        let length = 0;
        while (length < buffer.length) {
          const read = await handle.read(buffer, length, buffer.length - length, null);
          if (!read.bytesRead) break;
          length += read.bytesRead;
        }
        if (length > 256 * 1024) throw new Error('Skill contents exceed the installation limits.');
        content = buffer.subarray(0, length);
      } finally { await handle.close(); }
      bytes += content.length;
      if (bytes > 1024 * 1024) throw new Error('Skill contents exceed the installation limits.');
      result[relative.split(path.sep).join('/')] = content;
    } else throw new Error('Skill contents must be ordinary files and directories.');
  }
  await visit('');
  return result;
}

/** Refuse existing unmanaged files, user edits, additions and deletions. */
function owned(files: Files): Manifest {
  let manifest: Manifest;
  try { manifest = JSON.parse(files[manifestName]?.toString('utf8') ?? ''); }
  catch { throw new Error('An existing unmanaged skill occupies this destination. Choose another folder.'); }
  const contents = {...files};
  delete contents[manifestName];
  if (manifest.schema !== schema || typeof manifest.extensionVersion !== 'string' || !same(hashes(contents), manifest.files)) {
    throw new Error('The installed skill was edited, modified or has added files. Export to another folder to preserve your changes.');
  }
  return manifest;
}

/** Export or update only a complete, unmodified Glitnir-managed skill directory. */
export async function installSkill(
  bundle: string, selectedRoot: string, relativeParent: string, version: string,
  canWrite: () => boolean = () => true,
): Promise<'installed' | 'updated' | 'current'> {
  const check = (): void => { if (!canWrite()) throw new Error('Skill installation cancelled.'); };
  check();
  if (!['.agents/skills', '.claude/skills', ''].includes(relativeParent)) throw new Error('Invalid skill destination.');
  const files = await readTree(bundle);
  if (!files['SKILL.md'] || files[manifestName]) throw new Error('The bundled authoring skill is incomplete.');
  const root = await fs.realpath(selectedRoot);
  let parent = root;
  for (const segment of relativeParent.split('/').filter(Boolean)) {
    check();
    parent = path.join(parent, segment);
    try { await fs.mkdir(parent); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; }
    const info = await fs.lstat(parent);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new Error('A symbolic link or file blocks the skill destination. Choose another folder.');
  }
  const destination = path.join(parent, skillName);
  const lock = path.join(parent, '.glitnir-skill-install.lock');
  try { await fs.mkdir(lock); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new Error(`Another skill installation is active. If an earlier installation was interrupted, remove ${lock} after checking it has stopped.`);
    throw error;
  }
  let stage: string | undefined;
  let backup: string | undefined;
  try {
    check();
    let before: Files | undefined;
    try { before = await readTree(destination); } catch (error) { if (!missing(error)) throw error; }
    const manifest: Manifest = {schema, extensionVersion: version, files: hashes(files)};
    if (before) {
      const previous = owned(before);
      if (same(previous, manifest)) return 'current';
    }
    stage = await fs.mkdtemp(path.join(parent, '.glitnir-skill-stage-'));
    for (const [name, content] of Object.entries(files)) {
      const file = path.join(stage, name);
      await fs.mkdir(path.dirname(file), {recursive: true});
      await fs.writeFile(file, content, {flag: 'wx'});
    }
    await fs.writeFile(path.join(stage, manifestName), JSON.stringify(manifest, null, 2) + '\n', {flag: 'wx'});
    check();
    if (before) {
      if (!same(hashes(await readTree(destination)), hashes(before))) throw new Error('Skill files changed during installation; your files were preserved.');
      const recovery = path.join(root, '.glitnir-skill-backups');
      try { await fs.mkdir(recovery); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; }
      const recoveryInfo = await fs.lstat(recovery);
      if (recoveryInfo.isSymbolicLink() || !recoveryInfo.isDirectory()) throw new Error('A symbolic link or file blocks the recovery folder.');
      backup = path.join(recovery, randomUUID());
      await fs.rename(destination, backup);
      if (!same(hashes(await readTree(backup)), hashes(before))) throw new Error('Skill files changed during installation; your files were preserved.');
    } else {
      try { await fs.lstat(destination); throw new Error('The skill destination appeared during installation.'); }
      catch (error) { if (!missing(error)) throw error; }
    }
    check();
    await fs.rename(stage, destination);
    stage = undefined;
    // Preserve the previous tree for recovery, including a concurrent editor
    // write through an open descriptor after its directory was renamed.
    backup = undefined;
    return before ? 'updated' : 'installed';
  } catch (error) {
    if (backup) {
      try {
        await fs.lstat(destination);
        throw new Error(`Skill installation stopped. Your previous files are preserved at ${backup}.`);
      } catch (restoreError) {
        if (!missing(restoreError)) throw restoreError;
        await fs.rename(backup, destination);
        backup = undefined;
      }
    }
    throw error;
  } finally {
    if (stage) await fs.rm(stage, {recursive: true, force: true});
    await fs.rmdir(lock);
  }
}
