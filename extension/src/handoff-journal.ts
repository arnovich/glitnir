import {createHash, randomUUID} from 'node:crypto';
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
  /** Read every durable receipt with its original request, including interrupted completion. */
  async submitted(): Promise<{id: string; value: T; receipt: any}[]> {
    let entries: string[];
    try { entries = await readdir(this.directory); }
    catch (error: any) { if (error.code === 'ENOENT') return []; throw error; }
    const found: {id: string; value: T; receipt: any}[] = [];
    for (const name of entries.filter(n => /^[a-f0-9-]{36}\.completed$/.test(n)).sort()) {
      const id = name.slice(0, -10);
      const receipt = JSON.parse(await readFile(path.join(this.directory, name), 'utf8'));
      let request: string;
      try { request = await readFile(path.join(this.directory, id + '.submitted'), 'utf8'); }
      catch (error: any) {
        if (error.code !== 'ENOENT') throw error;
        request = await readFile(path.join(this.directory, id + '.pending'), 'utf8');
      }
      try { receipt.reviewedDigest = await readFile(path.join(this.directory, id + '.reviewed'), 'utf8'); }
      catch (error: any) { if (error.code !== 'ENOENT') throw error; }
      found.push({id, value: JSON.parse(request), receipt});
    }
    return found;
  }
  /** Keep local review independent of in-flight status receipt replacements. */
  async markReviewed(id: string, digest: string): Promise<void> {
    if (!/^[a-f0-9-]{36}$/.test(id) || typeof digest !== 'string' || digest.length > 512) throw new Error('Invalid review identity.');
    const temporary = path.join(this.directory, id + '.' + randomUUID() + '.reviewing');
    await writeFile(temporary, digest, {flag: 'wx', mode: 0o600});
    await rename(temporary, path.join(this.directory, id + '.reviewed'));
  }

  /** Claim one notification across reloads and editor windows without racing receipt writes. */
  async claimNotification(id: string, digest: string): Promise<boolean> {
    if (!/^[a-f0-9-]{36}$/.test(id) || typeof digest !== 'string' || digest.length > 512) throw new Error('Invalid notification identity.');
    const hash = createHash('sha256').update(digest).digest('hex');
    try { await writeFile(path.join(this.directory, id + '.' + hash + '.notified'), '', {flag: 'wx', mode: 0o600}); return true; }
    catch (error: any) { if (error.code === 'EEXIST') return false; throw error; }
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
