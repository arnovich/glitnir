import {randomUUID} from 'node:crypto';
import {mkdir, readFile, readdir, rename, writeFile} from 'node:fs/promises';
import * as path from 'node:path';

/** Each request owns its file, so separate editor windows cannot erase one another. */
export class HandoffJournal<T> {
  constructor(private readonly directory: string) {}
  async persist(value: T): Promise<string> {
    await mkdir(this.directory, {recursive: true, mode: 0o700});
    const id = randomUUID();
    const temporary = path.join(this.directory, id + '.writing');
    await writeFile(temporary, JSON.stringify(value), {flag: 'wx', mode: 0o600});
    await rename(temporary, path.join(this.directory, id + '.pending'));
    return id;
  }
  async pending(): Promise<{id: string; value: T}[]> {
    let entries: string[];
    try { entries = await readdir(this.directory); }
    catch (error: any) { if (error.code === 'ENOENT') return []; throw error; }
    const found: {id: string; value: T}[] = [];
    for (const name of entries.filter(n => /^[a-f0-9-]{36}\.pending$/.test(n))) {
      try { found.push({id: name.slice(0, -8), value: JSON.parse(await readFile(path.join(this.directory, name), 'utf8'))}); }
      catch (error: any) { if (error.code !== 'ENOENT') throw error; }
    }
    return found;
  }
  async finish(id: string, receipt: object): Promise<void> {
    if (!/^[a-f0-9-]{36}$/.test(id)) throw new Error('Invalid handoff journal identity.');
    const temporary = path.join(this.directory, id + '.' + randomUUID() + '.receipt');
    await writeFile(temporary, JSON.stringify(receipt), {flag: 'wx', mode: 0o600});
    await rename(temporary, path.join(this.directory, id + '.completed'));
    try { await rename(path.join(this.directory, id + '.pending'), path.join(this.directory, id + '.submitted')); }
    catch (error: any) { if (error.code !== 'ENOENT') throw error; }
  }
}
