import {execFile} from 'node:child_process';
import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {mkdir, mkdtemp, open, rm, writeFile} from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';
import {pathToFileURL} from 'node:url';
import {promisify} from 'node:util';
import {crc32, deflateRaw} from 'node:zlib';
import * as vscode from 'vscode';
import {BackendDescriptor, backendDescriptor, backendStore, findUv, runUv, UvNotStartedError} from './backend-install';
import {Environment} from './discovery';
import {KNOWN_PACKAGES, KnownPackage} from './known-packages';
import {fetchPlugin, fetchRelease, ReleaseFetch} from './release-assets';
import {githubToken} from './setup';

export interface WheelLimits {wheelBytes: number; expandedBytes: number; entries: number; metadataBytes: number; recordBytes: number}
/** Provider wheels are small Python packages; their dependencies come from the index. */
export const WHEEL_LIMITS: WheelLimits = {wheelBytes: 50 * 1024 ** 2, expandedBytes: 200 * 1024 ** 2, entries: 20000, metadataBytes: 1024 ** 2, recordBytes: 8 * 1024 ** 2};
export const MAX_WHEEL_BYTES = WHEEL_LIMITS.wheelBytes;
export const PROVIDER_GROUP = 'glitnir.providers';
/**
 * Packages an extra may download from PyPI. The installer uses only PyPI, so a
 * name that is not published there could later be registered by anyone; extras
 * needing other packages are explained, never installed.
 */
export const PUBLISHED_EXTRA_PACKAGES: ReadonlySet<string> = new Set(['z3-solver']);
/** Conventional development extras; installing them never adds a notebook feature. */
const DEVELOPMENT_EXTRAS = new Set(['dev', 'docs', 'test', 'tests']);
/** Script names that would replace the environment's own tools. */
const RESERVED_SCRIPT = /^(?:python|pip|pydoc|activate|deactivate)|^uvx?$/;
/** Entry point groups whose names uv writes as files in the environment's `bin` directory. */
const SCRIPT_GROUPS = ['console_scripts', 'gui_scripts'];
/**
 * A script name that is a plain file name; uv writes it to `bin/` without
 * checking. It may not end in `.`, which some file systems drop.
 */
const SCRIPT_NAME = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9_-])?$/;
const DOTTED = '[A-Za-z_][A-Za-z0-9_]*(?:\\.[A-Za-z_][A-Za-z0-9_]*)*';
const EXTRAS = '(?: \\[[A-Za-z0-9_.-]+(?:, ?[A-Za-z0-9_.-]+)*\\])?';
/** An `entry_points.txt` group header, with nothing around it. */
const GROUP_LINE = /^\[([A-Za-z0-9._-]+)\]$/;
/** An `entry_points.txt` entry: `name = module[:attribute] [extras]`, spaced exactly so. */
const ENTRY_LINE = new RegExp(`^([A-Za-z0-9][A-Za-z0-9._-]{0,63}) = (${DOTTED}(?::${DOTTED})?${EXTRAS})$`);
/** The object reference of a script, which must name a function. */
const SCRIPT_REFERENCE = new RegExp(`^${DOTTED}:${DOTTED}${EXTRAS}$`);
/** Wheel data schemes Glitnir installs, spelled as uv requires. */
const PACKAGE_SCHEMES = ['purelib', 'platlib'];
const NAME = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;
const VERSION = /^[A-Za-z0-9.!+_-]+$/;
const WHEEL_FILE = /^([A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-([A-Za-z0-9.!+_]+)(?:-\d[A-Za-z0-9_.]*)?-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl$/;

export interface Extra {
  name: string;
  requirements: string[];
  /** Why the extra is not offered, or undefined when it may be installed. */
  unavailable?: string;
}
export interface WheelInfo {
  name: string;
  version: string;
  /** Entry point names in the `glitnir.providers` group. */
  entryPoints: string[];
  /** Declared extras, without development extras such as `test`. */
  extras: Extra[];
  /** Runtime requirements other than the Glitnir core. */
  requirements: string[];
  /** Console and GUI script names, which uv writes to the environment's `bin/`. */
  commands: string[];
}
export interface InstalledPackage {
  name: string;
  version: string;
  entryPoints: string[];
  /** Declared extras whose requirements are all installed (installers record no extras). */
  extras?: string[];
}
export interface ProviderReport {
  core: string | null;
  packages: InstalledPackage[];
  views?: Record<string, string>;
  evidence?: Record<string, string>;
  decisions?: Record<string, string>;
  errors?: Record<string, string>;
  /** Why an evidence verb or decision kind cannot run yet, by word. */
  unavailable?: {evidence: Record<string, string>; decisions: Record<string, string>};
  /** Files in the environment's scripts directory, each with the normalized names of the distributions whose RECORD lists it. */
  commands?: Record<string, string[]>;
}

/** PEP 503 name normalization, also used for extras (PEP 685). */
export function normalized(name: string): string { return name.replace(/[-_.]+/g, '-').toLowerCase(); }
const isCore = (name: string): boolean => normalized(name) === 'glitnir';
const requirementName = (requirement: string): string => normalized(/^[A-Za-z0-9._-]+/.exec(requirement)?.[0] ?? '');

interface ZipEntry {fileName: string; fileNameRaw: Buffer; extraFields: {id: number}[]; uncompressedSize: number; externalFileAttributes: number}
interface ZipFile {
  eachEntry(): AsyncIterable<ZipEntry>;
  openReadStreamPromise(entry: ZipEntry): Promise<AsyncIterable<Buffer>>;
  readLocalFileHeaderPromise(entry: ZipEntry): Promise<{fileName: Buffer; extraField: Buffer}>;
  close(): void;
}
const yauzl = require('yauzl') as {fromBufferPromise(buffer: Buffer, options?: object): Promise<ZipFile>};

class WheelError extends Error {}

/**
 * Names and RECORD rows are limited to printable ASCII. Zip readers decode
 * other bytes differently (UTF-8 or CP437, Info-ZIP Unicode path fields), and
 * line readers split RECORD at U+0085, U+2028 and U+2029, which uv does not.
 */
const PRINTABLE_ASCII = /^[\x20-\x7e]+$/;
const ASCII_ONLY = 'Glitnir only installs wheels whose file names and file list are plain ASCII.';
/** Text with every character outside printable ASCII shown as an escape. */
const visible = (text: string): string => text.replace(/[^\x20-\x7e]/gu, character => `\\u{${character.codePointAt(0)!.toString(16)}}`);
/** Info-ZIP Unicode Path extra field, which some readers use instead of the name. */
const UNICODE_PATH_FIELD = 0x7075;
/** ZIP64 extended information extra field, which some readers use instead of the sizes. */
const ZIP64_FIELD = 0x0001;
const END_RECORD = 0x06054b50, ZIP64_LOCATOR = 0x07064b50, CENTRAL_RECORD = 0x02014b50, LOCAL_RECORD = 0x04034b50;
const NOT_PLAIN = 'Glitnir only installs wheels that are plain zip archives:';

/**
 * Refuse an archive whose layout zip readers could resolve differently. Readers
 * find the archive through its end record and may honour a ZIP64 record, a
 * second end record in the comment or data before the central directory, so
 * the end record must be the last 22 bytes, have no comment and no ZIP64
 * locator, and its central directory must hold exactly its entries and end
 * where the end record starts.
 */
function checkArchiveLayout(bytes: Buffer): void {
  const end = bytes.length - 22;
  if (end < 0 || bytes.readUInt32LE(end) !== END_RECORD) {
    if (bytes.lastIndexOf(Buffer.from('PK\x05\x06', 'latin1')) < 0) throw new WheelError('It is not a valid wheel archive (it has no end of central directory record).');
    throw new WheelError(`${NOT_PLAIN} it has a comment or data after its end record.`);
  }
  if (end >= 20 && bytes.readUInt32LE(end - 20) === ZIP64_LOCATOR) throw new WheelError(`${NOT_PLAIN} it has ZIP64 end records.`);
  const disk = bytes.readUInt16LE(end + 4), centralDisk = bytes.readUInt16LE(end + 6);
  const diskEntries = bytes.readUInt16LE(end + 8), entries = bytes.readUInt16LE(end + 10);
  const size = bytes.readUInt32LE(end + 12), offset = bytes.readUInt32LE(end + 16);
  if (entries === 0xffff || size === 0xffffffff || offset === 0xffffffff) throw new WheelError(`${NOT_PLAIN} it needs ZIP64 end records.`);
  if (disk || centralDisk || diskEntries !== entries) throw new WheelError(`${NOT_PLAIN} it spans several disks.`);
  if (offset + size !== end) throw new WheelError(`${NOT_PLAIN} its central directory does not end at its end record.`);
  let position = offset;
  for (let index = 0; index < entries; index++) {
    if (position + 46 > end || bytes.readUInt32LE(position) !== CENTRAL_RECORD) throw new WheelError(`${NOT_PLAIN} its central directory does not hold ${entries} entries.`);
    position += 46 + bytes.readUInt16LE(position + 28) + bytes.readUInt16LE(position + 30) + bytes.readUInt16LE(position + 32);
  }
  if (position !== end) throw new WheelError(`${NOT_PLAIN} its central directory holds more than ${entries} entries.`);
}

/** Extra field ids in a raw local-header extra field. */
function extraFieldIds(raw: Buffer): number[] {
  const ids: number[] = [];
  for (let offset = 0; offset + 4 <= raw.length; offset += 4 + raw.readUInt16LE(offset + 2)) ids.push(raw.readUInt16LE(offset));
  return ids;
}

/**
 * Refuse an entry whose name another zip reader could see differently: a name
 * that is not printable ASCII, a Unicode path field, or a local file header
 * (read by streaming readers) naming a different file than the central directory.
 */
async function checkEntryName(zip: ZipFile, entry: ZipEntry): Promise<void> {
  const raw = entry.fileNameRaw.toString('latin1');
  if (!PRINTABLE_ASCII.test(raw) || entry.fileName !== raw) throw new WheelError(`${ASCII_ONLY} It contains ${visible(entry.fileName)}.`);
  const local = await zip.readLocalFileHeaderPromise(entry);
  if (entry.extraFields.some(field => field.id === UNICODE_PATH_FIELD) || extraFieldIds(local.extraField).includes(UNICODE_PATH_FIELD)) {
    throw new WheelError(`${ASCII_ONLY} ${raw} has a second, Unicode file name.`);
  }
  if (entry.extraFields.some(field => field.id === ZIP64_FIELD) || extraFieldIds(local.extraField).includes(ZIP64_FIELD)) {
    throw new WheelError(`${NOT_PLAIN} ${raw} has ZIP64 sizes.`);
  }
  if (!local.fileName.equals(entry.fileNameRaw)) throw new WheelError(`Its archive headers disagree about the name of ${raw}.`);
}

/** Header fields of a core-metadata file; continuation lines are folded. */
function headers(text: string): Map<string, string[]> {
  const result = new Map<string, string[]>();
  let last: string[] | undefined;
  for (const line of text.replace(/\r\n/g, '\n').split('\n')) {
    if (!line) break;
    if (/^[ \t]/.test(line) && last) { last[last.length - 1] += ' ' + line.trim(); continue; }
    const match = /^([A-Za-z0-9-]+):\s*(.*)$/.exec(line);
    if (!match) throw new WheelError('Its METADATA file is malformed.');
    const key = match[1].toLowerCase();
    last = result.get(key) ?? [];
    last.push(match[2].trim());
    result.set(key, last);
  }
  return result;
}

/**
 * Check a RECORD strictly. uv copies it into the installed file list, and CSV
 * readers disagree about quotes and lone carriage returns, so only plain rows
 * `path,hash,size` ending in a newline are accepted, and the rows must name
 * exactly the archive's files, each once, with RECORD's own row unhashed and
 * every other row giving the SHA-256 hash and size of the file's contents.
 */
function checkRecord(text: string, record: string, files: Map<string, {hash: string; size: number}>): void {
  const malformed = (detail: string): never => { throw new WheelError(`Its RECORD file is malformed (${detail}).`); };
  const other = /[^\x20-\x7e\r\n]/u.exec(text);
  if (other) throw new WheelError(`${ASCII_ONLY} Its RECORD file contains ${visible(other[0])}.`);
  if (!text.endsWith('\n')) malformed('it does not end with a newline');
  const listed = new Set<string>(), folded = new Set<string>();
  for (const raw of text.slice(0, -1).split('\n')) {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
    if (/[\r"]/.test(line)) malformed('a row contains quotes or a carriage return');
    const fields = line.split(',');
    if (fields.length !== 3 || !fields[0]) malformed(`a row is not path,hash,size: ${line}`);
    const [file, hash, size] = fields;
    if (folded.has(file.toLowerCase())) malformed(`${file} is listed twice`);
    listed.add(file); folded.add(file.toLowerCase());
    if (file === record && (hash || size)) malformed('its own row has a hash or size');
    const contents = files.get(file);
    if (!contents) throw new WheelError(`Its RECORD lists a file it does not contain (${file}).`);
    if (file !== record && (hash !== contents.hash || size !== String(contents.size))) {
      throw new WheelError(`Its RECORD does not give the SHA-256 hash and size of ${file} (${hash || 'no hash'}, ${size || 'no size'}).`);
    }
  }
  for (const file of files.keys()) if (!listed.has(file)) throw new WheelError(`Its RECORD does not list ${file}.`);
}

/** A Requires-Dist value split into its requirement and its environment marker. */
function splitRequirement(line: string): [string, string] {
  const separator = line.indexOf(';');
  return separator < 0 ? [line.trim(), ''] : [line.slice(0, separator).trim(), line.slice(separator + 1).trim()];
}

/**
 * The extra a marker limits a requirement to, or undefined when it names none.
 * Only `extra == "X"` (either operand order), alone or `and`-combined with
 * clauses that do not mention extras, is accepted: any other use of `extra`
 * could make uv install the requirement although no extra was chosen.
 */
function markerExtra(marker: string): string | undefined {
  if (!/\bextra\b/.test(marker)) return undefined;
  const refuse = (): never => { throw new WheelError(`It limits a requirement to an extra in a way Glitnir cannot check (${marker}).`); };
  if (/[()]/.test(marker) || /\bor\b/.test(marker)) refuse();
  const clauses = marker.split(/\s+and\s+/).map(clause => clause.trim());
  const named = clauses.map(clause => /^(?:extra\s*==\s*(["'])([^"']+)\1|(["'])([^"']+)\3\s*==\s*extra)$/.exec(clause));
  const matches = named.filter(match => match);
  if (matches.length !== 1 || clauses.some((clause, index) => !named[index] && /\bextra\b/.test(clause))) refuse();
  return normalized(matches[0]![2] ?? matches[0]![4]);
}

interface EntryPoint {name: string; value: string}

/**
 * Entry points by group from an `entry_points.txt` file. Parsers disagree about
 * this INI-like format: uv, for one, strips `#` and `;` comments anywhere in a
 * line, takes any line starting with `[` and containing `]` as a group header
 * (named up to its last `]`), and splits entries at their first `=` or `:`.
 * So the file is not parsed leniently but checked against a grammar every
 * parser reads alike: each line, ended by `\n` or `\r\n`, is empty, a header
 * `[group]`, or an entry `name = module:attribute [extras]` spaced exactly so.
 * Comments, indentation and anything else are refused.
 */
function entryPoints(text: string): Map<string, EntryPoint[]> {
  const malformed = (detail: string): never => { throw new WheelError(`Its entry_points.txt file is malformed (${detail}).`); };
  if (text && !text.endsWith('\n')) malformed('it does not end with a newline');
  const lines = text.split('\n');
  lines.pop();
  const groups = new Map<string, EntryPoint[]>();
  let section: string | undefined;
  lines.forEach((raw, index) => {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
    if (!line) return;
    const header = GROUP_LINE.exec(line);
    if (header) { section = header[1]; return; }
    const entry = ENTRY_LINE.exec(line);
    if (!entry) malformed(`line ${index + 1} is not blank, a [group] header or a name = module:attribute entry: ${visible(line.slice(0, 120))}`);
    if (section === undefined) malformed(`line ${index + 1} is not in a group`);
    groups.set(section!, [...groups.get(section!) ?? [], {name: entry![1], value: entry![2]}]);
  });
  return groups;
}

/**
 * Console and GUI script names by group, refusing scripts uv could write
 * anywhere but a new plain file in `bin/`: uv uses each name as a path, so
 * `../x.pth` or `/tmp/x` would write outside it, and a reserved or repeated
 * name would replace an existing tool.
 */
function checkScripts(groups: Map<string, EntryPoint[]>): string[] {
  for (const group of groups.keys()) {
    // uv reads only these exact group names; another spelling is refused so no installer reads it differently.
    if (SCRIPT_GROUPS.includes(group.toLowerCase()) && !SCRIPT_GROUPS.includes(group)) throw new WheelError(`Its entry_points.txt spells a script group ${group}; only ${SCRIPT_GROUPS.join(' and ')} are accepted.`);
  }
  const seen = new Map<string, string>();
  for (const group of SCRIPT_GROUPS) {
    for (const {name, value} of groups.get(group) ?? []) {
      if (!SCRIPT_NAME.test(name)) throw new WheelError(`It installs a command named ${visible(name)}; command names must be at most 64 letters, digits, '.', '_' or '-', starting with a letter or digit and not ending in '.'.`);
      if (RESERVED_SCRIPT.test(name.toLowerCase())) throw new WheelError(`It installs a ${name} command, which would replace the environment's own.`);
      if (seen.has(name.toLowerCase())) throw new WheelError(`It installs the ${name} command twice.`);
      seen.set(name.toLowerCase(), name);
      if (!SCRIPT_REFERENCE.test(value)) throw new WheelError(`Its ${name} command does not name a Python function as module:function (${value}).`);
    }
  }
  return [...seen.values()];
}

/**
 * Refuse commands that would replace a file in the environment's scripts
 * directory (`bin/`), compared case-insensitively as macOS does, unless the
 * installed distribution being reinstalled owns that file in its RECORD.
 * `existing` maps each file there to the normalized names of its owners.
 */
export function checkCommands(info: WheelInfo, existing: Record<string, string[]>): void {
  for (const command of info.commands) {
    for (const [file, owners] of Object.entries(existing)) {
      if (file.toLowerCase() !== command.toLowerCase() || owners.includes(normalized(info.name))) continue;
      const owner = owners.length ? `which belongs to ${owners.join(', ')}` : 'which no installed package owns';
      throw new Error(`Cannot install ${info.name}: its ${command} command would replace ${file} in the environment's commands, ${owner}.`);
    }
  }
}

/** An entry's uncompressed bytes, at most `limit` of them. */
async function entryBytes(zip: ZipFile, entry: ZipEntry, limit: number): Promise<Buffer> {
  const file = path.posix.basename(entry.fileName);
  if (entry.uncompressedSize > limit) throw new WheelError(`Its ${file} file is too large.`);
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of await zip.openReadStreamPromise(entry)) {
    size += chunk.length;
    if (size > limit) throw new WheelError(`Its ${file} file is too large.`);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function entryText(name: string, bytes: Buffer): string {
  // A byte order mark is kept, as Python's UTF-8 readers keep it.
  try { return new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes); }
  catch { throw new WheelError(`Its ${path.posix.basename(name)} file is not UTF-8 text.`); }
}

/** One file of the archive Glitnir writes for uv. */
interface ArchiveFile {name: Buffer; data: Buffer; method: 0 | 8; crc: number; size: number; executable: boolean}
/** The wheel spec's RECORD hash: unpadded URL-safe base64 of the SHA-256 digest. */
const recordHash = (bytes: Buffer): string => `sha256=${createHash('sha256').update(bytes).digest('base64url')}`;
const deflate = promisify(deflateRaw);

/** An archive file holding `bytes`, deflated unless that does not make it smaller. */
async function archiveFile(name: string, bytes: Buffer, executable: boolean): Promise<ArchiveFile> {
  const deflated = await deflate(bytes);
  const stored = deflated.length >= bytes.length;
  return {name: Buffer.from(name, 'latin1'), data: stored ? bytes : deflated, method: stored ? 0 : 8, crc: crc32(bytes), size: bytes.length, executable};
}

/** 1980-01-01 00:00 in MS-DOS date format, the earliest a zip can record. */
const DOS_DATE = (1 << 5) | 1;

/**
 * A minimal zip archive of `files` in order: one local header per file, then
 * the central directory and a 32-bit end record. It has no extra fields, data
 * descriptors, comment or ZIP64 records, so every zip reader sees the same
 * files; contents that would need ZIP64 are refused.
 */
function zipArchive(files: ArchiveFile[]): Buffer {
  if (files.length >= 0xffff) throw new WheelError('It has too many files to install.');
  const locals: Buffer[] = [], centrals: Buffer[] = [];
  let offset = 0;
  for (const file of files) {
    if (file.size >= 0xffffffff || file.data.length >= 0xffffffff || offset >= 0xffffffff) throw new WheelError('It is too large to install.');
    const local = Buffer.alloc(30);
    local.writeUInt32LE(LOCAL_RECORD, 0);
    local.writeUInt16LE(20, 4); // version needed: 2.0 (deflate)
    local.writeUInt16LE(file.method, 8);
    local.writeUInt16LE(DOS_DATE, 12);
    local.writeUInt32LE(file.crc, 14);
    local.writeUInt32LE(file.data.length, 18);
    local.writeUInt32LE(file.size, 22);
    local.writeUInt16LE(file.name.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(CENTRAL_RECORD, 0);
    central.writeUInt16LE((3 << 8) | 20, 4); // made by Unix, zip 2.0, so the mode below is read
    local.copy(central, 6, 4, 30); // version needed through name length; no extra or comment
    central.writeUInt32LE(((file.executable ? 0o100755 : 0o100644) << 16) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    locals.push(local, file.name, file.data);
    centrals.push(central, file.name);
    offset += local.length + file.name.length + file.data.length;
  }
  const directory = Buffer.concat(centrals);
  if (offset + directory.length >= 0xffffffff) throw new WheelError('It is too large to install.');
  const end = Buffer.alloc(22);
  end.writeUInt32LE(END_RECORD, 0);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

/**
 * Refuse archive paths that could change anything but the provider's own files.
 * Paths are compared case-folded: macOS and Windows file systems ignore case.
 * Returns the path's top-level `.dist-info` directory, if any.
 */
function checkPath(name: string): string | undefined {
  const parts = name.replace(/\/$/, '').split('/');
  const lower = parts.map(part => part.toLowerCase());
  if (lower.some(part => part === '' || part === '.' || part === '..')) throw new WheelError(`It contains an unsafe path (${name}).`);
  if (lower.some(part => part.endsWith('.pth'))) throw new WheelError(`It contains a .pth file (${name}), which would run code whenever Python starts.`);
  let installed = lower, prefixed = false;
  if (lower[0].endsWith('.data') && lower.length > 1) {
    // Only package files are accepted from the data directory, never scripts or data.
    if (!PACKAGE_SCHEMES.includes(lower[1])) throw new WheelError(`It installs files outside its packages (${name}).`);
    // uv matches scheme names exactly and stops partway through the install on any other spelling.
    if (!PACKAGE_SCHEMES.includes(parts[1])) throw new WheelError(`It spells the ${parts[1]} data directory in a way uv does not install (${name}); only lowercase ${PACKAGE_SCHEMES.join(' and ')} are accepted.`);
    if (lower.length === 2 && !name.endsWith('/')) throw new WheelError(`It installs files outside its packages (${name}).`);
    installed = lower.slice(2); prefixed = true;
  }
  if (installed[0] === 'glitnir' || installed[0]?.startsWith('glitnir.')) throw new WheelError(`It contains files of the Glitnir backend itself (${name}).`);
  if (/^(?:site|user)customize(?:\.|$)/.test(installed[0] ?? '')) throw new WheelError(`It contains ${installed[0]}, which would run code whenever Python starts.`);
  installed.forEach((part, index) => {
    if (!part.endsWith('.dist-info') && !part.endsWith('.egg-info')) return;
    // Only the wheel's own top-level .dist-info directory may describe a package.
    const own = !prefixed && index === 0 && part.endsWith('.dist-info') && (lower.length > 1 || name.endsWith('/'));
    if (!own) throw new WheelError(`It contains package metadata other than its own .dist-info directory (${name}).`);
  });
  return !prefixed && lower[0].endsWith('.dist-info') ? parts[0] : undefined;
}

/** Read a wheel's metadata without executing or installing anything from it. */
export async function inspectWheel(fileName: string, bytes: Buffer, limits: WheelLimits = WHEEL_LIMITS): Promise<WheelInfo> {
  return (await prepareWheel(fileName, bytes, limits)).info;
}

/**
 * Inspect a wheel and write the archive uv installs: the files Glitnir read, in
 * the same order with the same names and contents, in a plain zip without
 * directory entries. uv then installs exactly what was inspected, however the
 * selected archive could be read differently by another zip reader.
 */
export async function prepareWheel(fileName: string, bytes: Buffer, limits: WheelLimits = WHEEL_LIMITS): Promise<{info: WheelInfo; wheel: Buffer}> {
  try {
    const file = WHEEL_FILE.exec(fileName);
    if (!file) throw new WheelError('Select a Python wheel (.whl) file with a standard wheel file name.');
    if (bytes.length > limits.wheelBytes) throw new WheelError(`It is larger than ${limits.wheelBytes / 1024 ** 2} MiB.`);
    if (isCore(file[1])) throw new WheelError('This is the Glitnir backend itself. Install or change the backend with Glitnir: Set Up Environment.');
    checkArchiveLayout(bytes);
    let zip: ZipFile;
    try { zip = await yauzl.fromBufferPromise(bytes, {strictFileNames: true}); }
    catch (error) { throw new WheelError(`It is not a valid wheel archive (${(error as Error).message}).`); }
    const names = new Set<string>();
    /** RECORD hash and size of each file's contents. */
    const files = new Map<string, {hash: string; size: number}>();
    const archive: ArchiveFile[] = [];
    const directories = new Set<string>();
    /** Top-level directories named like wheel data directories, as spelled. */
    const dataDirectories = new Set<string>();
    const texts = new Map<string, string>();
    let expanded = 0;
    try {
      for await (const entry of zip.eachEntry()) {
        const name = entry.fileName;
        await checkEntryName(zip, entry);
        if (names.has(name.toLowerCase())) throw new WheelError(`It contains duplicate files (${name}).`);
        names.add(name.toLowerCase());
        if (names.size > limits.entries) throw new WheelError('It contains too many files.');
        expanded += entry.uncompressedSize;
        if (expanded > limits.expandedBytes) throw new WheelError('Its contents are too large.');
        const directory = checkPath(name);
        if (directory !== undefined) directories.add(directory);
        const top = name.split('/')[0];
        if (name.includes('/') && top.toLowerCase().endsWith('.data')) dataDirectories.add(top);
        if (name.endsWith('/')) {
          // Directories are implied by file paths, and the installed archive has none.
          if (entry.uncompressedSize) throw new WheelError(`Its directory entry ${name} has contents.`);
          continue;
        }
        const metadata = directory !== undefined && /^[^/]+\.dist-info\/(?:METADATA|entry_points\.txt)$/.test(name);
        const record = directory !== undefined && /^[^/]+\.dist-info\/RECORD$/.test(name);
        const content = await entryBytes(zip, entry, metadata ? limits.metadataBytes : record ? limits.recordBytes : limits.expandedBytes);
        if (metadata || record) texts.set(name, entryText(name, content));
        files.set(name, {hash: recordHash(content), size: content.length});
        archive.push(await archiveFile(name, content, ((entry.externalFileAttributes >>> 16) & 0o111) !== 0));
      }
    } catch (error) {
      if (error instanceof WheelError) throw error;
      throw new WheelError(`It is not a valid wheel archive (${(error as Error).message}).`);
    } finally { zip.close(); }
    // A path that is a file and also a directory of another entry installs ambiguously.
    const lowerFiles = new Set([...files.keys()].map(file => file.toLowerCase()));
    for (const name of names) {
      for (let index = name.indexOf('/'); index > 0; index = name.indexOf('/', index + 1)) {
        if (lowerFiles.has(name.slice(0, index))) throw new WheelError(`It contains ${name.slice(0, index)} both as a file and as a directory.`);
      }
    }
    if (directories.size !== 1) throw new WheelError('It must contain exactly one .dist-info directory.');
    const [directory] = directories;
    // uv installs only the data directory named like its .dist-info directory, matched exactly.
    const dataDirectory = directory.replace(/\.dist-info$/, '.data');
    for (const top of dataDirectories) {
      if (top !== dataDirectory) throw new WheelError(`Its data directory ${top} is not named exactly after its .dist-info directory (${dataDirectory}).`);
    }
    const metadata = texts.get(`${directory}/METADATA`);
    if (metadata === undefined) throw new WheelError(`It has no ${directory}/METADATA file.`);
    // uv copies RECORD into the installed file list, which uninstalling deletes.
    const record = texts.get(`${directory}/RECORD`);
    if (record === undefined) throw new WheelError(`It has no ${directory}/RECORD file.`);
    if (!files.has(`${directory}/RECORD`)) throw new WheelError(`It has no ${directory}/RECORD file.`);
    checkRecord(record, `${directory}/RECORD`, files);
    const fields = headers(metadata);
    const name = fields.get('name')?.[0] ?? '', version = fields.get('version')?.[0] ?? '';
    if (!NAME.test(name) || !VERSION.test(version)) throw new WheelError('Its METADATA has no valid Name and Version.');
    if (isCore(name)) throw new WheelError('This is the Glitnir backend itself. Install or change the backend with Glitnir: Set Up Environment.');
    const folder = /^(.+)-([^-]+)\.dist-info$/.exec(directory);
    if (!folder || normalized(folder[1]) !== normalized(name) || normalized(file[1]) !== normalized(name)) throw new WheelError(`Its file name, .dist-info directory and METADATA disagree about the package name (${name}).`);
    if (folder[2].toLowerCase() !== version.toLowerCase() || file[2].toLowerCase() !== version.toLowerCase()) throw new WheelError(`Its file name, .dist-info directory and METADATA disagree about the version (${version}).`);
    const groups = entryPoints(texts.get(`${directory}/entry_points.txt`) ?? '');
    const commands = checkScripts(groups);
    const providers = (groups.get(PROVIDER_GROUP) ?? []).map(entry => entry.name);
    if (!providers.length) throw new WheelError(`${name} declares no ${PROVIDER_GROUP} entry point, so it adds nothing to Glitnir.`);
    const extras = new Map<string, Extra>();
    for (const extra of fields.get('provides-extra') ?? []) {
      if (!NAME.test(extra)) throw new WheelError(`Its METADATA declares an invalid extra (${extra}).`);
      if (!DEVELOPMENT_EXTRAS.has(normalized(extra))) extras.set(normalized(extra), {name: normalized(extra), requirements: []});
    }
    const requirements: string[] = [];
    const declared = new Set((fields.get('provides-extra') ?? []).map(normalized));
    for (const line of fields.get('requires-dist') ?? []) {
      const [requirement, marker] = splitRequirement(line);
      // A direct reference would download from wherever the wheel says.
      if (requirement.includes('@')) throw new WheelError(`It requires a package from a direct URL (${requirement}).`);
      const parsed = /^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?/.exec(requirement);
      if (!parsed) throw new WheelError(`Its METADATA has an invalid requirement (${line}).`);
      const required = normalized(parsed[1]);
      // Extras named through another requirement would bypass the extras check.
      if (required === normalized(name)) throw new WheelError(`It requires itself (${requirement}).`);
      if (isCore(required) && parsed[2]) throw new WheelError(`It requires the Glitnir backend with extras (${requirement}).`);
      const extra = markerExtra(marker);
      if (extra === undefined) { if (!isCore(required)) requirements.push(requirement); continue; }
      if (!declared.has(extra)) throw new WheelError(`It has a requirement for an undeclared extra (${line}).`);
      const target = extras.get(extra);
      if (!target) continue; // a development extra, never installed
      target.requirements.push(requirement);
      if (parsed[2]) target.unavailable = `Installs ${requirement}, whose own extras Glitnir does not install. Install it into the backend yourself.`;
    }
    for (const extra of extras.values()) {
      const unpublished = extra.requirements.filter(requirement => !PUBLISHED_EXTRA_PACKAGES.has(requirementName(requirement)));
      if (unpublished.length) extra.unavailable = `Installs ${unpublished.join(', ')}, which Glitnir does not install from PyPI. Install it into the backend yourself.`;
    }
    return {info: {name, version, entryPoints: providers, extras: [...extras.values()], requirements, commands}, wheel: zipArchive(archive)};
  } catch (error) {
    if (error instanceof WheelError) throw new Error(`Cannot install ${fileName}: ${error.message}`);
    throw error;
  }
}

/**
 * Install one inspected provider wheel with its chosen extras. The core is held at
 * its installed version by a constraint: uv keeps an installed package that
 * satisfies it, and only the provider itself is reinstalled.
 */
export function installArguments(python: string, wheel: string, info: WheelInfo, extras: string[], constraints: string): string[] {
  for (const extra of extras) {
    if (!NAME.test(extra)) throw new Error(`Invalid extra ${extra}.`);
    const declared = info.extras.find(candidate => candidate.name === extra);
    if (!declared || declared.unavailable) throw new Error(`The ${extra} extra cannot be installed by Glitnir. ${declared?.unavailable ?? ''}`.trim());
  }
  if (isCore(info.name) || !NAME.test(info.name)) throw new Error('Refusing to install the Glitnir backend as an extension package.');
  const requirement = `${info.name}${extras.length ? `[${extras.join(',')}]` : ''} @ ${pathToFileURL(wheel).href}`;
  return ['pip', 'install', '--no-config', '--no-python-downloads', '--python', python,
    '--index-url', 'https://pypi.org/simple', '--only-binary', ':all:',
    '--constraint', constraints, '--reinstall-package', info.name, requirement];
}

function table(value: unknown, field: string): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !Object.values(value).every(v => typeof v === 'string')) throw new Error(`Invalid provider report ${field}.`);
  return value as Record<string, string>;
}

/** Validate the JSON printed by `providers.py`. */
export function parseReport(stdout: string, providers: boolean): ProviderReport {
  const value = JSON.parse(stdout);
  if (!value || typeof value !== 'object' || value.error) throw new Error(value?.error || 'Invalid provider report.');
  if (value.core !== null && (typeof value.core !== 'string' || !VERSION.test(value.core))) throw new Error('Invalid provider report core.');
  if (!Array.isArray(value.packages) || !value.packages.every((p: InstalledPackage) =>
    p && typeof p.name === 'string' && NAME.test(p.name) && !isCore(p.name) && typeof p.version === 'string' &&
    Array.isArray(p.entryPoints) && p.entryPoints.every(e => typeof e === 'string') &&
    (p.extras === undefined || (Array.isArray(p.extras) && p.extras.every(e => typeof e === 'string' && NAME.test(e)))))) throw new Error('Invalid provider report packages.');
  const report: ProviderReport = {core: value.core, packages: value.packages};
  if (!providers) {
    const commands = value.commands;
    if (!commands || typeof commands !== 'object' || Array.isArray(commands) ||
      !Object.values(commands).every(owners => Array.isArray(owners) && owners.every(owner => typeof owner === 'string'))) throw new Error('Invalid provider report commands.');
    report.commands = commands;
  }
  if (providers) {
    for (const field of ['views', 'evidence', 'decisions', 'errors'] as const) report[field] = table(value[field], field);
    report.unavailable = {evidence: table(value.unavailable?.evidence, 'unavailable'), decisions: table(value.unavailable?.decisions, 'unavailable')};
  }
  return report;
}

/** Run the bundled report script in the backend's Python; it never starts a server. */
export async function readReport(python: string, script: string, mode: 'packages' | 'providers', signal: AbortSignal, timeoutMs = 60000): Promise<ProviderReport> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const child = execFile(python, ['-I', '-B', script, mode], {timeout: timeoutMs, maxBuffer: 1024 ** 2, killSignal: 'SIGKILL'}, (error, stdout, stderr) => {
      signal.removeEventListener('abort', abort);
      if (signal.aborted) { reject(signal.reason); return; }
      if (error?.killed) { reject(new Error(`The Glitnir backend timed out after ${Math.round(timeoutMs / 1000)} s while listing its providers. See Output → Glitnir and retry.`)); return; }
      try { resolve(parseReport(stdout, mode === 'providers')); }
      catch (failure) { reject(new Error(`The Glitnir backend could not list its providers: ${stdout.trim() ? (failure as Error).message : error?.message || stderr || String(failure)}`)); }
    });
    const abort = (): void => { child.kill('SIGKILL'); };
    signal.addEventListener('abort', abort, {once: true});
    if (signal.aborted) abort();
  });
}

/** Read a selected wheel once through an ordinary-file descriptor, bounded in size. */
export async function readWheel(file: string): Promise<Buffer> {
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile()) throw new Error(`Cannot install ${path.basename(file)}: select an ordinary .whl file.`);
    if (info.size > MAX_WHEEL_BYTES) throw new Error(`Cannot install ${path.basename(file)}: it is larger than ${MAX_WHEEL_BYTES / 1024 ** 2} MiB.`);
    const buffer = Buffer.alloc(info.size + 1);
    let length = 0;
    while (length < buffer.length) {
      const read = await handle.read(buffer, length, buffer.length - length, null);
      if (!read.bytesRead) break;
      length += read.bytesRead;
    }
    if (length !== info.size) throw new Error(`Cannot install ${path.basename(file)}: it changed while being read.`);
    return buffer.subarray(0, length);
  } finally { await handle.close(); }
}

/** Words of one table, with the words that cannot run yet grouped by reason. */
function words(names: Record<string, string> | undefined, unavailable: Record<string, string> = {}): string {
  const list = Object.keys(names ?? {});
  if (!list.length) return 'none';
  const ready = list.filter(word => !(word in unavailable));
  const reasons = new Map<string, string[]>();
  for (const word of list.filter(word => word in unavailable)) reasons.set(unavailable[word], [...reasons.get(unavailable[word]) ?? [], word]);
  return [...(ready.length ? [ready.join(', ')] : []), ...[...reasons].map(([reason, blocked]) => `${blocked.join(', ')} (unavailable: ${reason})`)].join(', ');
}

/** One line naming what the installed providers now serve. */
export function describeProviders(report: ProviderReport): string {
  return `views ${words(report.views)}; evidence ${words(report.evidence, report.unavailable?.evidence)}; decisions ${words(report.decisions, report.unavailable?.decisions)}`;
}

function missingCommands(python: string): never {
  throw new Error(`The backend at ${python} did not list the commands in its environment, so Glitnir cannot check that a package's commands replace none of them.`);
}

export interface PackageDependencies {
  findUv?: () => Promise<string>;
  run?: typeof runUv;
  report?: (python: string, mode: 'packages' | 'providers', signal: AbortSignal) => Promise<ProviderReport>;
  /** Where managed backends live; their setup lock also guards package changes. */
  store?: () => string;
  /** Replaces release downloads, for tests. */
  download?: ReleaseFetch['download'];
}
interface Discovery {discovered(uri: vscode.Uri): Promise<Environment | undefined>}
/** The Other wheel file… choice. */
const OTHER = 'other';
interface SelectedWheel {fileName: string; bytes: Buffer; source?: string}
/** The outcome of installing an already verified wheel. */
export interface VerifiedInstall {info: WheelInfo; extras: string[]; dropped: string[]; after: ProviderReport}

/**
 * Install optional provider packages into the discovered backend
 * environment. `restart` closes the previews served by that Python and
 * returns whether any were open.
 */
export class ExtensionPackages {
  private controller = new AbortController();
  private busy = false;
  constructor(private context: vscode.ExtensionContext, private output: vscode.OutputChannel,
    private setup: Discovery, private restart: (python: string) => boolean, private dependencies: PackageDependencies = {}) {}

  dispose(): void { this.controller.abort(); }

  private check(): void {
    if (this.controller.signal.aborted) throw new Error('Glitnir is shutting down.');
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before changing Glitnir extension packages.');
  }

  private report(python: string, mode: 'packages' | 'providers', signal: AbortSignal): Promise<ProviderReport> {
    if (this.dependencies.report) return this.dependencies.report(python, mode, signal);
    return readReport(python, vscode.Uri.joinPath(this.context.extensionUri, 'dist/providers.py').fsPath, mode, signal);
  }

  /** The backend environment to change, or undefined after offering setup. */
  private async environment(requested?: vscode.Uri): Promise<Environment | undefined> {
    this.check();
    if (vscode.env.remoteName || !['darwin', 'linux'].includes(process.platform)) throw new Error('Extension packages can be changed in local macOS and Linux windows.');
    const uri = requested?.scheme ? requested : vscode.window.activeTextEditor?.document.uri ?? vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!uri || uri.scheme !== 'file') throw new Error('Open a local workspace or Lean file first.');
    const environment = await this.setup.discovered(uri);
    this.check();
    if (environment) return environment;
    const action = await vscode.window.showInformationMessage('Set up the Glitnir backend first.', 'Set Up Environment');
    if (action === 'Set Up Environment') await vscode.commands.executeCommand('glitnir.setup', uri);
    return undefined;
  }

  /** One package change at a time in this window. */
  private async exclusive<T>(work: () => Promise<T>): Promise<T> {
    if (this.busy) throw new Error('Another Glitnir extension package change is in progress. Retry after it finishes.');
    this.busy = true;
    try { return await work(); } finally { this.busy = false; }
  }

  /**
   * A managed backend is shared by every window; hold its setup lock while uv
   * changes it. Returns a release function.
   */
  private async lock(python: string): Promise<() => Promise<void>> {
    const directory = path.dirname(path.dirname(python));
    const store = (this.dependencies.store ?? backendStore)();
    if (path.dirname(directory) !== store || !/^[a-f0-9]{64}$/.test(path.basename(directory))) return async () => {};
    const lock = directory + '.lock';
    try { await mkdir(lock, {mode: 0o700}); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new Error(`Backend setup or another package change is using ${directory}. Retry after it finishes. If no Glitnir window is changing it, delete ${lock}.`);
      throw error;
    }
    return () => rm(lock, {recursive: true, force: true});
  }

  /** Run `work` with a cancellable notification; extension shutdown also cancels it. */
  private progress<T>(title: string, work: (signal: AbortSignal, report: (message: string) => void) => Promise<T>): Thenable<T> {
    return vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title, cancellable: true}, async (progress, token) => {
      const controller = new AbortController();
      const stop = (): void => controller.abort();
      this.controller.signal.addEventListener('abort', stop, {once: true});
      const subscription = token.onCancellationRequested(stop);
      if (token.isCancellationRequested || this.controller.signal.aborted) stop();
      try { return await work(controller.signal, message => progress.report({message})); }
      finally { subscription.dispose(); this.controller.signal.removeEventListener('abort', stop); }
    });
  }

  /**
   * Run uv on the backend. Once uv has started, a failure or cancellation may
   * leave the environment partly changed, so its previews close as after
   * success and the error says how to recover.
   */
  private async change(python: string, args: string[], cwd: string, signal: AbortSignal, failure: string, recovery: string, check?: () => Promise<void>): Promise<boolean> {
    const release = await this.lock(python);
    try {
      signal.throwIfAborted();
      // Checked under the lock, so no other Glitnir change can invalidate it before uv runs.
      await check?.();
      signal.throwIfAborted();
      try {
        await (this.dependencies.run ?? runUv)((await this.uv()), args, cwd, signal, {log: text => this.output.append(text), timeoutMs: 900000, failure});
        signal.throwIfAborted();
      } catch (error) {
        if (error instanceof UvNotStartedError) { this.uvPath = undefined; throw error; }
        const closed = this.restart(python);
        const reason = signal.aborted ? 'It was cancelled.' : (error as Error).message;
        throw new Error(`${reason} The backend environment at ${python} may be partly changed${closed ? ' and its previews were closed' : ''}. ${recovery}`);
      }
      return this.restart(python);
    } finally { await release(); }
  }

  private uvPath?: string;
  private async uv(): Promise<string> { return this.uvPath ??= await (this.dependencies.findUv ?? findUv)(); }

  private async announce(action: string, report: ProviderReport, closed: boolean): Promise<void> {
    const reopen = closed ? ' Open the closed previews again to use them.' : '';
    const errors = Object.entries(report.errors ?? {});
    for (const [entry, message] of errors) this.output.appendLine(`Glitnir provider ${entry} did not load: ${message}`);
    if (errors.length) {
      void vscode.window.showWarningMessage(`${action}, but some providers did not load: ${errors.map(([entry, message]) => `${entry} (${message})`).join('; ')}. Now available: ${describeProviders(report)}.${reopen}`);
    } else void vscode.window.showInformationMessage(`${action}: ${describeProviders(report)}.${reopen}`);
  }

  install(requested?: vscode.Uri): Promise<void> { return this.exclusive(() => this.installPackage(requested)); }

  /** The provider packages in a backend environment, with the extras that are installed. */
  packages(python: string, signal: AbortSignal): Promise<ProviderReport> { return this.report(python, 'packages', signal); }

  /**
   * Install a wheel whose bytes the caller already verified, such as a release
   * asset, into `python` within the caller's progress: the same inspection,
   * command check, extras allowlist and core constraint as an interactive
   * install. Requested extras the wheel does not offer are returned as dropped.
   */
  installVerified(python: string, fileName: string, bytes: Buffer, requested: string[], signal: AbortSignal, report: (message: string) => void): Promise<VerifiedInstall> {
    return this.exclusive(async () => {
      this.check();
      const before = await this.report(python, 'packages', signal);
      if (!before.core) throw new Error(`The backend at ${python} has no installed glitnir package metadata.`);
      const {info, wheel} = await prepareWheel(fileName, bytes);
      checkCommands(info, before.commands ?? missingCommands(python));
      const offered = new Set(info.extras.filter(extra => !extra.unavailable).map(extra => extra.name));
      const extras = requested.filter(extra => offered.has(extra)), dropped = requested.filter(extra => !offered.has(extra));
      const {after} = await this.installInspected(python, before.core, fileName, info, wheel, extras, signal, report);
      return {info, extras, dropped, after};
    });
  }

  private async installPackage(requested?: vscode.Uri): Promise<void> {
    const environment = await this.environment(requested);
    if (!environment) return;
    const python = environment.python;
    await this.uv();
    this.check();
    const before = await this.report(python, 'packages', this.controller.signal);
    if (!before.core) throw new Error(`The backend at ${python} has no installed glitnir package metadata. Install extension packages into it manually (see the README's Extensions section).`);
    this.check();
    const known = await this.pickPackage(before);
    if (!known) return;
    this.check();
    const selected = known === OTHER ? await this.chooseWheel() : await this.knownWheel(known, before);
    if (!selected) return;
    const {info, wheel: inspected} = await prepareWheel(selected.fileName, selected.bytes);
    if (known !== OTHER && normalized(info.name) !== known.distribution) throw new Error(`Cannot install ${selected.fileName}: it is ${info.name}, not ${known.distribution}.`);
    checkCommands(info, before.commands ?? missingCommands(python));
    this.check();
    const offered = info.extras.filter(extra => !extra.unavailable), withheld = info.extras.filter(extra => extra.unavailable);
    const notOffered = withheld.map(extra => `${extra.name}: ${extra.unavailable}`);
    let extras: string[] = [];
    // A known package asks its own questions, only about extras the wheel offers.
    const asked = known === OTHER ? [] : known.extras.filter(extra => offered.some(o => o.name === extra.name));
    for (const extra of asked) {
      const requirements = offered.find(o => o.name === extra.name)!.requirements;
      const answer = await vscode.window.showInformationMessage(extra.prompt, {
        modal: true, detail: requirements.length ? `Downloads from PyPI: ${requirements.join(', ')}.` : 'Adds no packages.',
      }, 'Yes', 'No');
      if (!answer) return;
      if (answer === 'Yes') extras.push(extra.name);
    }
    const rest = offered.filter(extra => !asked.some(a => a.name === extra.name));
    if (rest.length) {
      const picked = await vscode.window.showQuickPick(rest.map(extra => ({
        label: extra.name, picked: false, extra: extra.name,
        detail: extra.requirements.length ? `Downloads from PyPI: ${extra.requirements.join(', ')}` : 'Adds no packages.',
      })), {title: `Install ${info.name} ${info.version}: optional extras`, canPickMany: true,
        placeHolder: `Select extras to install with it, or none.${withheld.length ? ` Not offered: ${withheld.map(extra => extra.name).join(', ')} (see the confirmation).` : ''}`});
      if (!picked) return;
      extras = [...extras, ...picked.map(item => item.extra)];
    }
    this.check();
    const downloads = [...info.requirements, ...offered.filter(extra => extras.includes(extra.name)).flatMap(extra => extra.requirements)];
    const accepted = await vscode.window.showInformationMessage(`Install ${info.name} ${info.version} into the Glitnir backend?`, {
      modal: true,
      detail: [
        `Python: ${python}`,
        ...(selected.source ? [selected.source] : []),
        `Provider entry points: ${info.entryPoints.join(', ')}.`,
        extras.length ? `Extras: ${extras.join(', ')}.` : 'No extras.',
        ...(notOffered.length ? [`Extras not offered: ${notOffered.join(' ')}`] : []),
        downloads.length ? `Its dependencies come from PyPI as prebuilt wheels: ${downloads.join(', ')}.` : 'It needs no packages from PyPI.',
        ...(known === OTHER ? [] : known.notes),
        `The installed Glitnir backend ${before.core} is kept. This changes the environment for every window using it; previews in this window that use it are closed.`,
      ].join('\n'),
    }, 'Install');
    if (accepted !== 'Install') return;
    this.check();
    const core = before.core;
    const result = await this.progress(`Installing ${info.name}`, (signal, report) => this.installInspected(python, core, selected.fileName, info, inspected, extras, signal, report));
    await this.announce(`Installed ${info.name} ${info.version}`, result.after, result.closed);
  }

  /** Step 1: a known package, marked when installed, or any other wheel file. */
  private async pickPackage(before: ProviderReport): Promise<KnownPackage | typeof OTHER | undefined> {
    const choice = await vscode.window.showQuickPick([
      ...KNOWN_PACKAGES.map(known => {
        const installed = before.packages.find(p => normalized(p.name) === known.distribution);
        return {label: known.label, description: installed ? `installed (${installed.version})` : known.distribution, detail: known.description, known: known as KnownPackage | typeof OTHER};
      }),
      {label: 'Other wheel file…', description: '', detail: 'Select a third-party or development .whl file.', known: OTHER as KnownPackage | typeof OTHER},
    ], {title: 'Install Extension Package', placeHolder: 'Choose a package to install, reinstall or update'});
    return choice?.known;
  }

  /** Today's flow: any local wheel file, read once. */
  private async chooseWheel(): Promise<SelectedWheel | undefined> {
    const files = await vscode.window.showOpenDialog({title: 'Select a Glitnir extension package wheel', canSelectMany: false, canSelectFiles: true, canSelectFolders: false, openLabel: 'Inspect Package', filters: {'Python wheel': ['whl']}});
    if (!files?.[0]) return undefined;
    if (files[0].scheme !== 'file') throw new Error('Select a local .whl file.');
    const fileName = path.basename(files[0].fsPath);
    if (!fileName.endsWith('.whl')) throw new Error(`Cannot install ${fileName}: select a Python wheel (.whl) file.`);
    return {fileName, bytes: await readWheel(files[0].fsPath)};
  }

  /** Step 2: a known package from this build's release (verified), or from a wheel the user downloaded. */
  private async knownWheel(known: KnownPackage, before: ProviderReport): Promise<SelectedWheel | undefined> {
    const version: string = this.context.extension?.packageJSON.version;
    let pin: BackendDescriptor | undefined;
    try { pin = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend); } catch { /* A build without a pin has no release. */ }
    const installed = before.packages.find(p => normalized(p.name) === known.distribution);
    const asset = known.asset(version);
    const action = !installed ? `Install ${version}` : installed.version === version ? `Reinstall ${version}` : `Update ${installed.version} → ${version}`;
    const source = await vscode.window.showQuickPick([
      ...(pin?.tag ? [{label: 'Download from this release (recommended)', description: action, detail: `${asset} from GitHub release ${pin.tag}, verified against its SHA256SUMS and glitnir-candidate.json.`, download: true}] : []),
      {label: 'Choose downloaded wheel', description: '', detail: `Select a ${known.distribution} .whl file you downloaded or built.`, download: false},
    ], {title: `Install ${known.label}`});
    if (!source) return undefined;
    this.check();
    if (!source.download) {
      const selected = await this.chooseWheel();
      const name = selected && WHEEL_FILE.exec(selected.fileName)?.[1];
      if (selected && (!name || normalized(name) !== known.distribution)) throw new Error(`Cannot install ${selected.fileName}: select a ${known.distribution} wheel for ${known.label}.`);
      return selected;
    }
    const tag = pin!.tag!;
    return this.progress(`Downloading ${known.label}`, async (signal, report) => {
      const scratch = await mkdtemp(path.join(os.tmpdir(), 'glitnir-release-'));
      try {
        const download: ReleaseFetch = {signal, githubToken, download: this.dependencies.download};
        report('Verifying the release');
        const release = await fetchRelease(pin!, version, scratch, download);
        const plugin = release.plugins.find(p => p.name === known.distribution && p.asset === asset);
        if (!plugin) throw new Error(`Release ${tag} does not publish ${asset}. Choose a downloaded wheel instead.`);
        report(`Downloading ${asset}`);
        const file = await fetchPlugin(pin!, plugin, scratch, download);
        return {fileName: asset, bytes: await readWheel(file), source: `Source: ${asset} from release ${tag}, verified against its SHA256SUMS and glitnir-candidate.json.`};
      } finally { await rm(scratch, {recursive: true, force: true}); }
    });
  }

  /**
   * Install the archive written from the inspected files, never the selected
   * one, holding the core at `core`; then check that the core is unchanged and
   * the package visible, and return the providers now loaded.
   */
  private async installInspected(python: string, core: string, fileName: string, info: WheelInfo, inspected: Buffer, extras: string[],
    signal: AbortSignal, report: (message: string) => void): Promise<{closed: boolean; after: ProviderReport}> {
    await this.uv();
    const scratch = await mkdtemp(path.join(os.tmpdir(), 'glitnir-package-'));
    let closed: boolean;
    try {
      const wheel = path.join(scratch, fileName), constraints = path.join(scratch, 'constraints.txt');
      await writeFile(wheel, inspected, {flag: 'wx', mode: 0o600});
      await writeFile(constraints, `glitnir==${core}\n`, {flag: 'wx', mode: 0o600});
      report('Installing with uv');
      this.output.appendLine(`Installing ${info.name} ${info.version}${extras.length ? ` [${extras.join(',')}]` : ''} into ${python}`);
      closed = await this.change(python, installArguments(python, wheel, info, extras, constraints), scratch, signal,
        `uv could not install ${info.name}. See Output → Glitnir for its message; PyPI must be reachable for dependencies.`,
        `Run Glitnir: Install Extension Package again, or uninstall it with uv pip uninstall --python ${python} ${info.name}.`,
        async () => { checkCommands(info, (await this.report(python, 'packages', signal)).commands ?? missingCommands(python)); });
    } finally { await rm(scratch, {recursive: true, force: true}); }
    report('Loading providers');
    const after = await this.report(python, 'providers', signal);
    if (after.core !== core) throw new Error(`Installing ${info.name} changed the Glitnir backend from ${core} to ${after.core}. Reinstall the backend with Glitnir: Set Up Environment.`);
    if (!after.packages.some(p => normalized(p.name) === normalized(info.name))) throw new Error(`uv finished, but ${info.name} is not visible in ${python}. See Output → Glitnir.`);
    return {closed, after};
  }
}
