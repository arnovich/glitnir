import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';

/** Hash through an ordinary-file descriptor, with bounded size and cancellation. */
export async function fileDigest(file: string, signal: AbortSignal, expectedSize?: number): Promise<string> {
  signal.throwIfAborted();
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || (expectedSize !== undefined && info.size !== expectedSize)) throw new Error('Runtime archive size or file type does not match the descriptor.');
    const hash = createHash('sha256');
    let bytes = 0;
    for await (const chunk of handle.createReadStream({autoClose: false, signal})) {
      bytes += chunk.length;
      if (expectedSize !== undefined && bytes > expectedSize) throw new Error('Runtime archive exceeded its expected size.');
      hash.update(chunk);
    }
    if (expectedSize !== undefined && bytes !== expectedSize) throw new Error('Runtime archive size changed.');
    return hash.digest('hex');
  } finally { await handle.close(); }
}

export async function privateDirectory(directory: string): Promise<void> {
  await fs.mkdir(directory, {recursive: true, mode: 0o700});
  const info = await fs.lstat(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid()) || (info.mode & 0o022)) throw new Error('Glitnir runtime storage must be a private, user-owned directory.');
  // Check both spellings: a safe resolved leaf does not make a writable symlink ancestor safe.
  for (const start of [path.resolve(directory), await fs.realpath(directory)]) {
    let current = start;
    while (true) {
      const ancestor = await fs.lstat(current);
      if (ancestor.isSymbolicLink() && !(ancestor.uid === 0 && ['/tmp', '/var'].includes(current) && await fs.realpath(current) === `/private${current}`)) throw new Error('Glitnir runtime storage has an unsafe symlink ancestor. Use a directory without symlink ancestors.');
      const trustedOwner = ancestor.uid === 0 || ancestor.uid === process.getuid?.();
      const stickyRoot = ancestor.isDirectory() && ancestor.uid === 0 && (ancestor.mode & 0o1000) !== 0;
      if (!trustedOwner || (!ancestor.isSymbolicLink() && (ancestor.mode & 0o022) !== 0 && !stickyRoot)) throw new Error('Glitnir runtime storage has an unsafe writable ancestor. Choose private application storage.');
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
  }
}
