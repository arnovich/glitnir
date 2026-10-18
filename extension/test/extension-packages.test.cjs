const assert = require('node:assert/strict');
const {test} = require('node:test');
const Module = require('node:module');
const {spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const {ZipFile} = require('yazl');

const METADATA = [
  'Metadata-Version: 2.4', 'Name: glitnir-forseti', 'Version: 0.1.0',
  'Summary: Forseti views', 'Requires-Python: >=3.12',
  'Requires-Dist: glitnir<0.2,>=0.1.0', 'Requires-Dist: lark>=1.2.2',
  'Provides-Extra: simulation', 'Requires-Dist: gimle-asgard; extra == "simulation"',
  'Provides-Extra: test', 'Requires-Dist: pytest>=9.0; extra == \'test\'',
  'Provides-Extra: z3', 'Requires-Dist: z3-solver==5.1.0.0; extra == "z3"',
  '', 'A long description.\nName: not-a-header',
].join('\n');
const ENTRY_POINTS = '[console_scripts]\nforseti-tool = glitnir_forseti.cli:main\n\n[glitnir.providers]\nforseti = glitnir_forseti:providers\n';

/** A wheel RECORD row giving the SHA-256 hash and size of `content`. */
const recordRow = (file, content) => {
  const bytes = Buffer.from(content);
  return `${file},sha256=${createHash('sha256').update(bytes).digest('base64url')},${bytes.length}\n`;
};

/**
 * Build a wheel archive in memory; `files` maps archive paths to text. A RECORD
 * listing every file with its hash and size is added beside each METADATA
 * unless `files` names one (`undefined` leaves it out).
 */
function wheel(files) {
  const zip = new ZipFile();
  const entries = Object.entries(files).filter(([, text]) => text !== undefined);
  for (const [name] of Object.entries(files)) {
    const record = name.replace(/METADATA$/, 'RECORD');
    if (!/^[^/]+\.dist-info\/METADATA$/.test(name) || record in files) continue;
    entries.push([record, [...entries.filter(([file]) => !file.endsWith('/')).map(([file, text]) => recordRow(file, text)), `${record},,\n`].join('')]);
  }
  for (const [name, text] of entries) {
    if (name.endsWith('/')) zip.addEmptyDirectory(name);
    else zip.addBuffer(Buffer.from(text), name);
  }
  return archive(zip);
}
/** The bytes of a yazl archive. */
function archive(zip) {
  zip.end();
  return new Promise((resolve, reject) => {
    const chunks = [];
    zip.outputStream.on('data', chunk => chunks.push(chunk)).on('end', () => resolve(Buffer.concat(chunks))).on('error', reject);
  });
}
const FORSETI_FILES = {
  'glitnir_forseti/__init__.py': 'def providers(): return []\n',
  'glitnir_forseti-0.1.0.dist-info/METADATA': METADATA,
  'glitnir_forseti-0.1.0.dist-info/entry_points.txt': ENTRY_POINTS,
  'glitnir_forseti-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
};
const forseti = (overrides = {}) => wheel({...FORSETI_FILES, ...overrides});
const FORSETI = 'glitnir_forseti-0.1.0-py3-none-any.whl';

function load(fake) {
  const original = Module._load;
  Module._load = function(name, ...args) { return name === 'vscode' ? fake : original.call(this, name, ...args); };
  try {
    delete require.cache[require.resolve('../../dist/extension-packages')];
    return require('../../dist/extension-packages');
  } finally { Module._load = original; }
}
const packages = load({});

const SIMULATION = {name: 'simulation', requirements: ['gimle-asgard'], unavailable: 'Installs gimle-asgard, which Glitnir does not install from PyPI. Install it into the backend yourself.'};
test('wheel inspection reads name, version, provider entry points and extras without running the wheel', async () => {
  const info = await packages.inspectWheel(FORSETI, await forseti());
  assert.deepEqual(info, {
    name: 'glitnir-forseti', version: '0.1.0', entryPoints: ['forseti'],
    extras: [SIMULATION, {name: 'z3', requirements: ['z3-solver==5.1.0.0']}],
    requirements: ['lark>=1.2.2'], commands: ['forseti-tool'],
  }, 'development extras and the core requirement are not offered; unpublished extras are explained');
  assert.deepEqual([...packages.PUBLISHED_EXTRA_PACKAGES], ['z3-solver']);
  const mixed = METADATA.replace('Requires-Dist: z3-solver==5.1.0.0; extra == "z3"', 'Requires-Dist: z3-solver==5.1.0.0; extra == "z3"\nRequires-Dist: z3-helpers; extra == "z3"');
  const blocked = await packages.inspectWheel(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/METADATA': mixed}));
  assert.match(blocked.extras[1].unavailable, /Installs z3-helpers, which Glitnir does not install from PyPI/, 'one unpublished package withholds the whole extra');
});

test('wheel inspection refuses the core, non-providers and malformed archives', async () => {
  const refuse = async (name, bytes, pattern) => assert.rejects(packages.inspectWheel(name, bytes), pattern);
  await refuse('glitnir-0.1.0-py3-none-any.whl', await wheel({'glitnir-0.1.0.dist-info/METADATA': 'Name: glitnir\nVersion: 0.1.0\n'}), /Glitnir backend itself/);
  const core = METADATA.replace('Name: glitnir-forseti', 'Name: Glitnir');
  await refuse(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/METADATA': core}), /Glitnir backend itself/);
  await refuse(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/entry_points.txt': '[console_scripts]\nx = y:z\n'}), /declares no glitnir\.providers entry point/);
  const bare = await wheel({'glitnir_forseti-0.1.0.dist-info/METADATA': METADATA});
  await refuse(FORSETI, bare, /declares no glitnir\.providers entry point/);
  await refuse('other_package-0.1.0-py3-none-any.whl', await forseti(), /disagree about the package name/);
  await refuse(FORSETI, await wheel({'README': 'no metadata'}), /exactly one \.dist-info directory/);
  await refuse(FORSETI, Buffer.from('PK\x03\x04 this is not a zip archive'), /not a valid wheel archive/);
  await refuse(FORSETI, Buffer.alloc(0), /not a valid wheel archive/);
  await refuse('glitnir_forseti.zip', await forseti(), /\.whl/);
  await refuse(FORSETI, Buffer.alloc(packages.MAX_WHEEL_BYTES + 1), /larger than 50 MiB/);
  await refuse(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/METADATA': 'not metadata at all'}), /METADATA file is malformed/);
  const direct = METADATA.replace('Requires-Dist: lark>=1.2.2', 'Requires-Dist: lark @ https://example.com/lark.whl');
  await refuse(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/METADATA': direct}), /direct URL/);
  await refuse(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/entry_points.txt': ENTRY_POINTS + '[console_scripts]\npython3 = evil:main\n'}), /python3 command/);
});

test('wheel inspection refuses every path that could write outside the provider', async () => {
  const refuse = async (files, pattern, name = FORSETI) => assert.rejects(packages.inspectWheel(name, await forseti(files)), pattern, JSON.stringify(files));
  // Case-insensitive file systems (macOS) would let these overwrite the core.
  for (const file of ['glitnir/notebook/server.py', 'Glitnir/__init__.py', 'GLITNIR/x.py', 'glitnir.py', 'Glitnir.cpython-312-darwin.so',
    'glitnir_forseti-0.1.0.data/purelib/glitnir/__init__.py', 'glitnir_forseti-0.1.0.data/platlib/Glitnir/__init__.py']) {
    await refuse({[file]: ''}, /files of the Glitnir backend itself/);
  }
  for (const file of ['glitnir-0.1.0.dist-info/METADATA', 'Glitnir-0.2.0.dist-info/RECORD', 'other-1.0.dist-info/METADATA',
    'glitnir_forseti-0.1.0.data/purelib/glitnir-9.dist-info/METADATA', 'glitnir_forseti-0.1.0.data/platlib/glitnir_forseti-0.1.0.dist-info/METADATA',
    'glitnir_forseti/vendored-1.0.dist-info/METADATA', 'glitnir-0.1.0.egg-info/PKG-INFO', 'GLITNIR_FORSETI-0.1.0.DIST-INFO/OTHER']) {
    await refuse({[file]: 'Name: glitnir\nVersion: 9\n'}, /package metadata other than its own|exactly one \.dist-info/);
  }
  for (const file of ['glitnir_forseti-0.1.0.data/data/lib/python3.12/site-packages/x.py', 'glitnir_forseti-0.1.0.data/scripts/python', 'glitnir_forseti-0.1.0.data/headers/x.h']) {
    await refuse({[file]: ''}, /installs files outside its packages/);
  }
  for (const file of ['evil.pth', 'glitnir_forseti/Startup.PTH', 'glitnir_forseti-0.1.0.data/purelib/x.pth']) await refuse({[file]: 'import os'}, /\.pth file/);
  for (const file of ['./glitnir_forseti/x.py', 'glitnir_forseti/./x.py', 'glitnir_forseti//x.py']) await refuse({[file]: ''}, /unsafe path/);
  await refuse({'GLITNIR_FORSETI/__init__.py': ''}, /duplicate files/);
  const named = async directory => packages.inspectWheel(FORSETI, await wheel({
    'glitnir_forseti/__init__.py': '', [`${directory}/METADATA`]: METADATA, [`${directory}/entry_points.txt`]: ENTRY_POINTS,
  }));
  await assert.rejects(named('glitnir_forseti-0.2.0.dist-info'), /disagree about the version/);
  await assert.rejects(named('glitnirforseti.dist-info'), /disagree about the package name/);
  await assert.rejects(named('other-0.1.0.dist-info'), /disagree about the package name/);
  await assert.rejects(packages.inspectWheel('glitnir_forseti-0.2.0-py3-none-any.whl', await forseti()), /disagree about the version/);
  assert.equal((await packages.inspectWheel(FORSETI, await forseti({'glitnir_forseti/': '', 'glitnir_forseti-0.1.0.dist-info/': ''}))).name, 'glitnir-forseti', 'directory entries are accepted');
});

test('entry_points.txt must follow a strict grammar that every parser reads alike', async () => {
  const points = 'glitnir_forseti-0.1.0.dist-info/entry_points.txt';
  const inspect = async text => packages.inspectWheel(FORSETI, await forseti({[points]: text}));
  const provider = '[glitnir.providers]\nforseti = glitnir_forseti:providers\n';
  const refuse = (text, pattern = /entry_points\.txt file is malformed/) => assert.rejects(inspect(text), pattern, JSON.stringify(text));
  // uv takes any line starting with '[' and containing ']' as a header named up to its last ']',
  // so each of these started a console_scripts group for uv and hid the escaping lines after it.
  const escape = '../lib/python3.12/site-packages/zz.pth = os:getcwd\n';
  for (const header of ['[console_scripts]x=y', '[gui_scripts]=', '[console_scripts]=', '[console_scripts] = q', '[console_scripts]\t= y',
    '[console_scripts]x]=y', '[console_scripts]x:y', '[console_scripts];=x', '[console_scripts]#x', '[console_scripts]]', ' [console_scripts]',
    '[console_scripts] ', '[ console_scripts ]', '[\tconsole_scripts\t]', '[console scripts]', '[]']) {
    await refuse(`${provider}${header}\n${escape}`);
  }
  // uv splits entries at their first ':' as well as '=', and strips comments anywhere.
  for (const line of ['zz:python = os:getcwd', 'zz = os:getcwd # note', 'zz = os:getcwd ; note', '# comment', '; comment', 'zz=os:getcwd', 'zz  = os:getcwd',
    'zz = os : getcwd', ' zz = os:getcwd', '\tzz = os:getcwd', 'zz = os:getcwd ', 'zz', 'zz =', 'zz = os:getcwd=x', 'zz = os:getcwd [a b]', 'zz = os:getcwd [a,]',
    'zz = os:getcwd[a]', 'zz = os:getcwd  [a]', 'zz = os.:getcwd', 'zz = 1os:getcwd', 'zz = os:getcwd()', 'zz = os-path:getcwd',
    '../zz_gui = os:getcwd', '/tmp/rr5_abs_script = os:getcwd', 'sub/tool = os:getcwd', 'back\\slash = os:getcwd', '.hidden = os:getcwd', '-flag = os:getcwd',
    `${'a'.repeat(65)} = os:getcwd`, 'two words = os:getcwd', 'café = os:getcwd', 'zz = os:getcwd ', 'zz = os:get\rcwd']) {
    await refuse(`${provider}[console_scripts]\n${line}\n`);
  }
  await refuse(`﻿${provider}`);
  await refuse(`zz = os:getcwd\n${provider}`, /line 1 is not in a group/);
  await refuse(provider.slice(0, -1), /does not end with a newline/);
  await refuse(`${provider}[console_scripts]\rzz = os:getcwd\n`);
  await refuse(`${provider}[console_scripts]\nzz = os:getcwd\r\r\n`);
});

test('console and GUI scripts are plain, unreserved, unrepeated names of functions', async () => {
  const points = 'glitnir_forseti-0.1.0.dist-info/entry_points.txt';
  const inspect = async text => packages.inspectWheel(FORSETI, await forseti({[points]: text}));
  const provider = '[glitnir.providers]\nforseti = glitnir_forseti:providers\n';
  const refuse = (text, pattern) => assert.rejects(inspect(text), pattern, JSON.stringify(text));
  for (const script of ['z.', 'z..', 'tool.']) await refuse(`${provider}[console_scripts]\n${script} = os:getcwd\n`, /command named .* not ending in '\.'/);
  for (const group of ['Console_Scripts', 'GUI_SCRIPTS']) await refuse(`${provider}[${group}]\npython = os:getcwd\n`, /spells a script group/);
  for (const script of ['python', 'Python', 'Python3.12', 'pip', 'pip3', 'pydoc.bat', 'activate', 'activate.fish', 'deactivate.bat', 'uv', 'UVX']) {
    await refuse(`${provider}[gui_scripts]\n${script} = os:getcwd\n`, /command, which would replace the environment's own/);
  }
  await refuse(`${provider}[console_scripts]\ntool = os:getcwd\nTool = os:getcwd\n`, /installs the Tool command twice/);
  await refuse(`${provider}[console_scripts]\ntool = os:getcwd\n[gui_scripts]\ntool = os:getcwd\n`, /installs the tool command twice/);
  await refuse(`${provider}[console_scripts]\ntool = os:getcwd\n[console_scripts]\ntool = os:getcwd\n`, /installs the tool command twice/);
  for (const reference of ['os', 'os.path', 'os [extra]']) await refuse(`${provider}[console_scripts]\ntool = ${reference}\n`, /does not name a Python function as module:function/);
  const accepted = ['glitnir-forseti-tool = glitnir_forseti.cli:main', 'forseti_tool2 = glitnir_forseti.cli:main.run', 'Forseti.Tool = a._b:_c [extra1, extra-2]',
    'x = os:getcwd [a,b]', `${'a'.repeat(64)} = os:getcwd`, 'tool- = os:getcwd'];
  const info = await inspect(`${provider}\n[console_scripts]\n${accepted.slice(0, 4).join('\n')}\n\n[gui_scripts]\n${accepted.slice(4).join('\n')}\nglitnir-view = glitnir_forseti:view\n[pytest11]\nplugin = glitnir_forseti.testing\n`);
  assert.deepEqual(info.entryPoints, ['forseti']);
  assert.deepEqual(info.commands, ['glitnir-forseti-tool', 'forseti_tool2', 'Forseti.Tool', 'x', 'a'.repeat(64), 'tool-', 'glitnir-view']);
  const crlf = await inspect(provider.replace(/\n/g, '\r\n') + '\r\n[console_scripts]\r\nforseti-tool = glitnir_forseti.cli:main\r\n');
  assert.deepEqual([crlf.entryPoints, crlf.commands], [['forseti'], ['forseti-tool']], 'CRLF line ends are accepted');
});

test('commands may not replace another package\'s file in the environment\'s bin/', async t => {
  const info = await packages.inspectWheel(FORSETI, await forseti({'glitnir_forseti-0.1.0.dist-info/entry_points.txt': '[glitnir.providers]\nforseti = glitnir_forseti:providers\n[console_scripts]\nZ3 = glitnir_forseti:main\nforseti-tool = glitnir_forseti.cli:main\n'}));
  assert.throws(() => packages.checkCommands(info, {z3: ['z3-solver']}), /its Z3 command would replace z3 in the environment's commands, which belongs to z3-solver/);
  assert.throws(() => packages.checkCommands(info, {'FORSETI-TOOL': []}), /would replace FORSETI-TOOL .* which no installed package owns/);
  assert.throws(() => packages.checkCommands(info, {z3: ['glitnir-forseti'], Z3: ['z3-solver']}), /belongs to z3-solver/, 'every case variant must be its own');
  packages.checkCommands(info, {'forseti-tool': ['glitnir-forseti'], z3: ['z3-solver', 'glitnir-forseti'], python: [], other: ['other']});
  // In the install flow: refused before confirming, and again under the lock just before uv runs.
  const blocked = await flow(t, {commands: {'Forseti-Tool': ['someone-else']}});
  blocked.state.dialogs.push([blocked.file(blocked.wheelFile)]);
  await assert.rejects(blocked.manager.install(), /its forseti-tool command would replace Forseti-Tool .* belongs to someone-else/);
  assert.equal(blocked.calls.runs.length + blocked.state.modalDetails.length, 0, 'nothing is installed or offered');
  let reports = 0;
  const raced = await flow(t, {commands: () => (++reports > 1 ? {'forseti-tool': ['late']} : {})});
  raced.state.dialogs.push([raced.file(raced.wheelFile)]);
  raced.state.picks.push([]);
  raced.state.modals.push('Install');
  await assert.rejects(raced.manager.install(), /belongs to late/);
  assert.equal(raced.calls.runs.length, 0, 'uv never runs');
  assert.deepEqual(raced.calls.restarts, [], 'nothing was changed, so no preview closes');
  const reinstall = await flow(t, {commands: {'forseti-tool': ['glitnir-forseti'], z3: ['z3-solver']}});
  reinstall.state.dialogs.push([reinstall.file(reinstall.wheelFile)]);
  reinstall.state.picks.push([]);
  reinstall.state.modals.push('Install');
  await reinstall.manager.install();
  assert.equal(reinstall.calls.runs.length, 1, 'reinstalling replaces its own command');
});

test('wheel data directories are named exactly as uv installs them', async () => {
  const refuse = async (files, pattern) => assert.rejects(packages.inspectWheel(FORSETI, await forseti(files)), pattern, JSON.stringify(files));
  for (const file of ['glitnir_forseti-0.1.0.data/PURELIB/zz.py', 'glitnir_forseti-0.1.0.data/Platlib/zz.py', 'glitnir_forseti-0.1.0.data/PURELIB/']) {
    await refuse({[file]: ''}, /spells the (?:PURELIB|Platlib) data directory in a way uv does not install/);
  }
  for (const file of ['glitnir_forseti-0.1.0.data/purelib', 'glitnir_forseti-0.1.0.data/zz.py']) await refuse({[file]: 'x'}, /installs files outside its packages/);
  for (const file of ['other.data/purelib/x.py', 'GLITNIR_FORSETI-0.1.0.DATA/purelib/x.py', 'glitnir_forseti-0.1.0.Data/purelib/x.py', 'glitnir_forseti-0.2.0.data/purelib/x.py']) {
    await refuse({[file]: ''}, /data directory .* is not named exactly after its \.dist-info directory \(glitnir_forseti-0\.1\.0\.data\)/);
  }
  for (const file of ['glitnir_forseti-0.1.0.data/purelib/extra.py', 'glitnir_forseti-0.1.0.data/platlib/plat/x.py']) {
    assert.equal((await packages.inspectWheel(FORSETI, await forseti({[file]: 'x = 1\n'}))).name, 'glitnir-forseti', file);
  }
  assert.equal((await packages.inspectWheel(FORSETI, await forseti({'glitnir_forseti-0.1.0.data/': '', 'glitnir_forseti-0.1.0.data/purelib/': '', 'glitnir_forseti-0.1.0.data/purelib/x.py': ''}))).name, 'glitnir-forseti', 'data directory entries are accepted');
  assert.equal((await packages.inspectWheel(FORSETI, await forseti({'glitnir_forseti/models.data': 'x'}))).name, 'glitnir-forseti', 'a nested .data name is an ordinary file');
});

/** Rewrite the uncompressed size a central-directory record declares for `name`. */
function declareSize(bytes, name, size) {
  const copy = Buffer.from(bytes);
  for (let offset = copy.indexOf('PK\x01\x02'); offset >= 0; offset = copy.indexOf('PK\x01\x02', offset + 4)) {
    const length = copy.readUInt16LE(offset + 28);
    if (copy.toString('utf8', offset + 46, offset + 46 + length) === name) { copy.writeUInt32LE(size, offset + 24); return copy; }
  }
  throw new Error(`no record for ${name}`);
}

test('wheel inspection bounds entries, expanded size and metadata, and rejects size mismatches', async () => {
  const limits = {...packages.WHEEL_LIMITS};
  const inspect = (bytes, overrides) => packages.inspectWheel(FORSETI, bytes, {...limits, ...overrides});
  const bytes = await forseti();
  assert.equal((await inspect(bytes, {entries: 5})).name, 'glitnir-forseti');
  await assert.rejects(inspect(bytes, {entries: 4}), /too many files/);
  await assert.rejects(inspect(bytes, {expandedBytes: 100}), /contents are too large/);
  await assert.rejects(inspect(bytes, {metadataBytes: 100}), /METADATA file is too large/);
  await assert.rejects(inspect(bytes, {wheelBytes: 100}), /larger than/);
  await assert.rejects(inspect(declareSize(bytes, 'glitnir_forseti-0.1.0.dist-info/METADATA', 10)), /not a valid wheel archive/);
  await assert.rejects(inspect(declareSize(bytes, 'glitnir_forseti/__init__.py', 0xffffffff)), /contents are too large|not a valid wheel archive/);
});

test('selected wheels are read once, as ordinary files, within the size limit', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-wheel-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const file = path.join(root, FORSETI);
  const bytes = await forseti();
  await fs.writeFile(file, bytes);
  assert.deepEqual(await packages.readWheel(file), bytes);
  const large = path.join(root, 'large.whl');
  await fs.writeFile(large, '');
  await fs.truncate(large, packages.MAX_WHEEL_BYTES + 1);
  await assert.rejects(packages.readWheel(large), /larger than 50 MiB/);
  await fs.symlink(file, path.join(root, 'link.whl'));
  await assert.rejects(packages.readWheel(path.join(root, 'link.whl')), /ELOOP|symbolic/i);
});

test('install commands are argument arrays that never replace the core', async () => {
  const info = await packages.inspectWheel(FORSETI, await forseti());
  const args = packages.installArguments('/env/bin/python', '/tmp/x y/' + FORSETI, info, ['z3'], '/tmp/x y/constraints.txt');
  assert.deepEqual(args, ['pip', 'install', '--no-config', '--no-python-downloads', '--python', '/env/bin/python',
    '--index-url', 'https://pypi.org/simple', '--only-binary', ':all:', '--constraint', '/tmp/x y/constraints.txt',
    '--reinstall-package', 'glitnir-forseti', `glitnir-forseti[z3] @ file:///tmp/x%20y/${FORSETI}`]);
  for (const flag of ['--reinstall', '--force-reinstall', '--upgrade', '-U', '--upgrade-package', '-P', '--no-deps', '--system', '--break-system-packages', '--extra-index-url', '--find-links']) assert.ok(!args.includes(flag), flag);
  assert.ok(!args.some(arg => /^glitnir(?:[=<>\[ @]|$)/.test(arg)), 'the core is never a requirement or reinstalled package');
  assert.equal(packages.installArguments('/py', '/w.whl', info, [], '/c').at(-1), 'glitnir-forseti @ file:///w.whl');
  assert.throws(() => packages.installArguments('/py', '/w.whl', info, ['simulation'], '/c'), /simulation extra cannot be installed by Glitnir\. Installs gimle-asgard/);
  assert.throws(() => packages.installArguments('/py', '/w.whl', info, ['undeclared'], '/c'), /cannot be installed/);
  assert.throws(() => packages.installArguments('/py', '/w.whl', info, ['z3]; rm'], '/c'), /Invalid extra/);
  assert.throws(() => packages.installArguments('/py', '/w.whl', {...info, name: 'glitnir'}, [], '/c'), /Refusing/);
});

test('provider reports are validated and summarised', () => {
  const unavailable = {evidence: {}, decisions: {entailment: 'Install glitnir-forseti[z3] to decide.', stream: 'Install glitnir-forseti[z3] to decide.'}};
  const report = packages.parseReport(JSON.stringify({core: '0.1.0', packages: [{name: 'glitnir-forseti', version: '0.1.0', entryPoints: ['forseti']}],
    views: {circuit: 'forseti'}, evidence: {}, decisions: {entailment: 'forseti', stream: 'forseti', other: 'x'}, errors: {}, unavailable}), true);
  assert.equal(packages.describeProviders(report), 'views circuit; evidence none; decisions other, entailment, stream (unavailable: Install glitnir-forseti[z3] to decide.)');
  assert.throws(() => packages.parseReport('{"error": "ModuleNotFoundError: glitnir"}', false), /ModuleNotFoundError/);
  assert.throws(() => packages.parseReport('{"core": "0.1.0", "packages": [{"name": "glitnir", "version": "0.1.0", "entryPoints": []}]}', false), /packages/);
  assert.throws(() => packages.parseReport('{"core": "0.1.0", "packages": []}', true), /views/);
  assert.deepEqual(packages.parseReport('{"core": "0.1.0", "packages": [], "commands": {"z3": ["z3-solver"], "python": []}}', false).commands, {z3: ['z3-solver'], python: []});
  for (const commands of ['', '{"commands": []}', '{"commands": {"z3": "z3-solver"}}', '{"commands": {"z3": [1]}}']) {
    const extra = commands ? `, ${commands.slice(1, -1)}` : '';
    assert.throws(() => packages.parseReport(`{"core": "0.1.0", "packages": []${extra}}`, false), /commands/, commands);
  }
});

test('a report that does not finish in time says so', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-slow-report-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const script = path.join(root, 'slow.py');
  await fs.writeFile(script, 'import time\ntime.sleep(30)\n');
  const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';
  await assert.rejects(packages.readReport(python, script, 'packages', new AbortController().signal, 500), /timed out after 1 s while listing its providers/);
});

test('the bundled report lists provider packages and loads them without starting a server', async t => {
  const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-providers-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  await fs.writeFile(path.join(root, 'fake_provider.py'), [
    'from glitnir.providers import DecisionProvider, ViewProvider',
    'def providers():',
    '    print("noise on stdout")',
    '    return [ViewProvider("fake", frozenset({"fakeview"}), (), "", "MARK", lambda *a: ""),',
    '            DecisionProvider("fake", frozenset({"fakekind"}), (), lambda *a: None, lambda *a: {}, lambda: "Install fake[solver] to decide.")]',
    'def broken():',
    '    raise RuntimeError("missing optional dependency")',
  ].join('\n'));
  const info = path.join(root, 'fake_provider-1.0.dist-info');
  await fs.mkdir(info);
  await fs.writeFile(path.join(info, 'METADATA'), ['Metadata-Version: 2.1', 'Name: fake-provider', 'Version: 1.0',
    // Extras are reported when every requirement is installed; the backend is, the other package is not.
    'Provides-Extra: Core_Extra', 'Requires-Dist: Glitnir; extra == "core-extra"',
    'Provides-Extra: missing', 'Requires-Dist: glitnir; extra == "missing"', 'Requires-Dist: not-installed-anywhere>=1; "missing" == extra',
    'Provides-Extra: empty', ''].join('\n'));
  await fs.writeFile(path.join(info, 'entry_points.txt'), '[glitnir.providers]\nfake = fake_provider:providers\nbroken = fake_provider:broken\n');
  // The fixture's RECORD claims one existing file in the interpreter's scripts directory.
  const scripts = spawnSync(python, ['-I', '-c', 'import sysconfig; print(sysconfig.get_path("scripts"))'], {encoding: 'utf8'}).stdout.trim();
  const claimed = (await fs.readdir(scripts)).sort()[0];
  await fs.writeFile(path.join(info, 'RECORD'), `fake_provider.py,,\n${path.relative(await fs.realpath(root), path.join(scripts, claimed))},,\n`);
  const script = path.resolve(__dirname, '../../dist/providers.py');
  // Not isolated here (-I), so the fixture on PYTHONPATH is importable.
  const run = mode => spawnSync(python, ['-B', script, mode], {encoding: 'utf8', env: {...process.env, PYTHONPATH: root}, timeout: 60000});
  const listed = run('packages');
  assert.equal(listed.status, 0, listed.stdout + listed.stderr);
  const list = JSON.parse(listed.stdout);
  assert.match(list.core, /^\d/);
  assert.deepEqual(list.packages.find(p => p.name === 'fake-provider'), {name: 'fake-provider', version: '1.0', entryPoints: ['fake', 'broken'], extras: ['core-extra']});
  assert.ok(!list.packages.some(p => p.name.toLowerCase() === 'glitnir'));
  assert.equal(list.views, undefined, 'listing packages does not load them');
  assert.ok(list.commands[claimed].includes('fake-provider'), JSON.stringify(list.commands[claimed]));
  assert.ok(Object.values(list.commands).every(owners => Array.isArray(owners)));
  assert.deepEqual(packages.parseReport(listed.stdout, false).commands, list.commands);
  const loaded = run('providers');
  assert.equal(loaded.status, 0, loaded.stdout + loaded.stderr);
  const report = packages.parseReport(loaded.stdout, true);
  assert.equal(report.views.fakeview, 'fake');
  assert.equal(report.decisions.fakekind, 'fake');
  assert.equal(report.unavailable.decisions.fakekind, 'Install fake[solver] to decide.');
  assert.match(packages.describeProviders(report), /fakekind \(unavailable: Install fake\[solver\] to decide\.\)/);
  assert.match(report.errors.broken, /RuntimeError: missing optional dependency/);
  assert.match(loaded.stderr, /noise on stdout/, 'provider output never corrupts the report');
  const bad = run('serve');
  assert.equal(bad.status, 1);
  assert.match(JSON.parse(bad.stdout).error, /Usage/);
});

function fakeVscode() {
  const file = fsPath => ({fsPath, scheme: 'file', path: fsPath, toString: () => `file://${fsPath}`});
  const state = {dialogs: [], picks: [], modals: [], infos: [], warnings: [], commands: [], pickItems: [], modalDetails: []};
  const fake = {
    Uri: {file, joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts))},
    ProgressLocation: {Notification: 15}, env: {},
    commands: {executeCommand: async (...args) => { state.commands.push(args); }},
    workspace: {isTrusted: true, workspaceFolders: [{uri: file('/work')}]},
    window: {
      activeTextEditor: undefined,
      showOpenDialog: async () => state.dialogs.shift(),
      showQuickPick: async items => { state.pickItems.push(items); return state.picks.shift(); },
      showInformationMessage: async (message, options, ...actions) => {
        if (options?.modal) { state.modalDetails.push([message, options.detail, actions]); return state.modals.shift(); }
        state.infos.push([message, options, ...actions]);
        return state.infoAction;
      },
      showWarningMessage: async message => { state.warnings.push(message); },
      withProgress: async (options, work) => work({report() {}}, {isCancellationRequested: false, onCancellationRequested: () => ({dispose() {}})}),
    },
  };
  return {fake, file, state};
}

async function flow(t, options = {}) {
  const {fake, file, state} = fakeVscode();
  const {ExtensionPackages} = load(fake);
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-package-flow-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const wheelFile = path.join(root, FORSETI);
  await fs.writeFile(wheelFile, await forseti());
  const calls = {runs: [], reports: [], restarts: [], discovered: 0, log: []};
  const installed = [];
  const environment = options.environment === undefined ? {python: '/env/bin/python'} : options.environment;
  const manager = new ExtensionPackages({extensionUri: file('/extension')}, {append: text => calls.log.push(text), appendLine: text => calls.log.push(text + '\n')},
    {discovered: async uri => { calls.discovered++; calls.uri = uri; return environment ?? undefined; }},
    python => { calls.restarts.push(python); return true; }, {
      findUv: async () => '/installed/uv',
      store: () => options.store ?? '/no-managed-store',
      run: async (uv, args, cwd, signal, uvOptions) => {
        const requirement = args.at(-1);
        const run = {uv, args, cwd, uvOptions};
        if (args[1] === 'install') {
          run.constraints = await fs.readFile(args[args.indexOf('--constraint') + 1], 'utf8');
          const copied = new URL(requirement.split(' @ ')[1]).pathname;
          run.copied = copied;
          run.bytesMatch = (await fs.readFile(copied)).equals((await packages.prepareWheel(FORSETI, await fs.readFile(wheelFile))).wheel);
          if (options.during) await options.during(signal, run);
          if (options.uvFails) throw new Error(uvOptions.failure);
          if (!options.invisible) installed.push({name: 'glitnir-forseti', version: '0.1.0', entryPoints: ['forseti']});
        } else installed.splice(0);
        uvOptions.log('Installed 2 packages\n');
        calls.runs.push(run);
      },
      report: async (python, mode) => {
        calls.reports.push([python, mode]);
        const core = mode === 'providers' && options.coreAfter ? options.coreAfter : options.core === undefined ? '0.1.0' : options.core;
        const report = {core, packages: [...installed, ...(options.alsoInstalled ?? [])]};
        if (mode === 'packages') report.commands = typeof options.commands === 'function' ? options.commands() : options.commands ?? {};
        if (mode === 'providers') Object.assign(report, installed.length ? {
          views: {circuit: 'forseti', model: 'forseti'}, evidence: {simulate: 'forseti'}, decisions: {entailment: 'forseti'}, errors: options.errors ?? {},
          unavailable: {evidence: {}, decisions: options.unavailable ?? {}},
        } : {views: {}, evidence: {}, decisions: {}, errors: {}});
        return report;
      },
    });
  return {manager, fake, file, state, calls, wheelFile, installed};
}

test('installing an extension package requires trust and a set-up backend', async t => {
  const untrusted = await flow(t);
  untrusted.fake.workspace.isTrusted = false;
  await assert.rejects(untrusted.manager.install(), /Trust this workspace/);
  assert.equal(untrusted.manager.remove, undefined, 'Glitnir does not remove packages');
  assert.equal(untrusted.calls.discovered, 0, 'an untrusted workspace never runs Python');
  const missing = await flow(t, {environment: null});
  missing.state.infoAction = 'Set Up Environment';
  await missing.manager.install();
  assert.deepEqual(missing.state.infos[0], ['Set up the Glitnir backend first.', 'Set Up Environment']);
  assert.equal(missing.state.commands[0][0], 'glitnir.setup');
  assert.equal(missing.state.commands[0][1].fsPath, '/work');
  assert.equal(missing.state.dialogs.length + missing.calls.runs.length + missing.calls.reports.length, 0);
  missing.state.infoAction = undefined;
  await missing.manager.install();
  assert.equal(missing.state.commands.length, 1, 'dismissing the prompt runs nothing');
  const noCore = await flow(t, {core: null});
  await assert.rejects(noCore.manager.install(), /no installed glitnir package metadata/);
});

test('installing a provider wheel installs the inspected copy with chosen extras, restarts and reports providers', async t => {
  const f = await flow(t);
  f.state.dialogs.push([f.file(f.wheelFile)]);
  f.state.picks.push([{extra: 'z3'}]);
  f.state.modals.push('Install');
  await f.manager.install(f.file('/work/A.lean'));
  assert.equal(f.calls.uri.fsPath, '/work/A.lean');
  const items = f.state.pickItems[0];
  assert.deepEqual(items.map(item => [item.label, item.picked]), [['z3', false]], 'no extra is selected by default and unpublished ones are not offered');
  assert.match(items[0].detail, /Downloads from PyPI: z3-solver==5\.1\.0\.0/);
  const [message, detail, actions] = f.state.modalDetails[0];
  assert.equal(message, 'Install glitnir-forseti 0.1.0 into the Glitnir backend?');
  assert.deepEqual(actions, ['Install']);
  assert.match(detail, /lark>=1\.2\.2, z3-solver==5\.1\.0\.0/);
  assert.match(detail, /installed Glitnir backend 0\.1\.0 is kept/);
  assert.match(detail, /Extras not offered: simulation: Installs gimle-asgard, which Glitnir does not install from PyPI/);
  assert.match(detail, /every window using it/);
  assert.equal(f.calls.runs.length, 1);
  const run = f.calls.runs[0];
  assert.equal(run.uv, '/installed/uv');
  assert.equal(run.constraints, 'glitnir==0.1.0\n', 'the core is held at its installed version');
  assert.ok(run.bytesMatch, 'uv installs the archive written from the inspected files');
  assert.notEqual(run.copied, f.wheelFile);
  assert.equal(path.basename(run.copied), FORSETI, 'the written archive keeps the wheel file name uv requires');
  assert.match(run.args.at(-1), /^glitnir-forseti\[z3\] @ file:\/\//);
  assert.ok(!run.args.includes('--reinstall') && !run.args.includes('--upgrade'));
  await assert.rejects(fs.access(path.dirname(run.copied)), /ENOENT/, 'the private copy is removed');
  assert.deepEqual(f.calls.restarts, ['/env/bin/python']);
  assert.deepEqual(f.calls.reports, [['/env/bin/python', 'packages'], ['/env/bin/python', 'packages'], ['/env/bin/python', 'providers']],
    'the commands are checked again under the lock');
  assert.equal(f.state.infos.at(-1)[0], 'Installed glitnir-forseti 0.1.0: views circuit, model; evidence simulate; decisions entailment. Open the closed previews again to use them.');
  assert.ok(f.calls.log.join('').includes('Installed 2 packages'), "uv's output goes to the output channel");
});

test('install stops on dismissal, refused wheels or uv failure, and reports provider load errors', async t => {
  const f = await flow(t);
  await f.manager.install();
  assert.equal(f.calls.runs.length, 0, 'closing the file dialog installs nothing');
  f.state.dialogs.push([f.file(f.wheelFile)]); f.state.picks.push(undefined);
  await f.manager.install();
  f.state.dialogs.push([f.file(f.wheelFile)]); f.state.picks.push([]); f.state.modals.push(undefined);
  await f.manager.install();
  assert.equal(f.calls.runs.length, 0, 'dismissing the extras or the confirmation installs nothing');
  const core = path.join(path.dirname(f.wheelFile), 'glitnir-0.1.0-py3-none-any.whl');
  await fs.writeFile(core, await wheel({'glitnir-0.1.0.dist-info/METADATA': 'Name: glitnir\nVersion: 0.1.0\n'}));
  f.state.dialogs.push([f.file(core)]);
  await assert.rejects(f.manager.install(), /Glitnir backend itself/);
  f.state.dialogs.push([{scheme: 'untitled', fsPath: f.wheelFile}]);
  await assert.rejects(f.manager.install(), /local \.whl/);
  assert.equal(f.calls.runs.length, 0);
  const failing = await flow(t, {uvFails: true});
  failing.state.dialogs.push([failing.file(failing.wheelFile)]); failing.state.picks.push([]); failing.state.modals.push('Install');
  await assert.rejects(failing.manager.install(), /uv could not install glitnir-forseti\..* may be partly changed and its previews were closed\. Run Glitnir: Install Extension Package again, or uninstall it with uv pip uninstall --python \/env\/bin\/python glitnir-forseti\./);
  assert.deepEqual(failing.calls.restarts, ['/env/bin/python'], 'a failed install closes previews as a changed environment');
  const errors = await flow(t, {errors: {forseti: 'ImportError: lark'}});
  errors.state.dialogs.push([errors.file(errors.wheelFile)]); errors.state.picks.push([]); errors.state.modals.push('Install');
  await errors.manager.install();
  assert.match(errors.state.warnings[0], /^Installed glitnir-forseti 0\.1\.0, but some providers did not load: forseti \(ImportError: lark\)/);
  assert.ok(errors.calls.log.join('').includes('Glitnir provider forseti did not load: ImportError: lark'));
});

test('a verified release wheel installs through the same checks without prompts, keeping only offered extras', async t => {
  const f = await flow(t);
  const signal = new AbortController().signal;
  const result = await f.manager.installVerified('/env/bin/python', FORSETI, await fs.readFile(f.wheelFile), ['z3', 'simulation', 'gone'], signal, () => {});
  assert.deepEqual([result.extras, result.dropped], [['z3'], ['simulation', 'gone']], 'withheld and unknown extras are never installed');
  assert.equal(result.info.name, 'glitnir-forseti');
  const run = f.calls.runs[0];
  assert.equal(run.constraints, 'glitnir==0.1.0\n');
  assert.ok(run.bytesMatch, 'uv installs the archive written from the inspected files');
  assert.match(run.args.at(-1), /^glitnir-forseti\[z3\] @ file:\/\//);
  assert.equal(f.state.dialogs.length + f.state.modalDetails.length + f.state.pickItems.length + f.state.infos.length, 0, 'the caller reports the outcome');
  const core = await wheel({'glitnir-0.1.0.dist-info/METADATA': 'Name: glitnir\nVersion: 0.1.0\n'});
  await assert.rejects(f.manager.installVerified('/env/bin/python', 'glitnir-0.1.0-py3-none-any.whl', core, [], signal, () => {}), /Glitnir backend itself/);
  const changed = await flow(t, {coreAfter: '0.2.0'});
  await assert.rejects(changed.manager.installVerified('/env/bin/python', FORSETI, await fs.readFile(changed.wheelFile), [], signal, () => {}), /changed the Glitnir backend/);
});

const install = f => { f.state.dialogs.push([f.file(f.wheelFile)]); f.state.picks.push([]); f.state.modals.push('Install'); return f.manager.install(); };

test('install verifies the core is unchanged and the package is visible afterwards', async t => {
  const changed = await flow(t, {coreAfter: '0.2.0'});
  await assert.rejects(install(changed), /changed the Glitnir backend from 0\.1\.0 to 0\.2\.0/);
  const invisible = await flow(t, {invisible: true});
  await assert.rejects(install(invisible), /uv finished, but glitnir-forseti is not visible in \/env\/bin\/python/);
  const missing = await flow(t, {unavailable: {entailment: 'Install glitnir-forseti[z3] to decide.'}});
  await install(missing);
  assert.match(missing.state.infos.at(-1)[0], /decisions entailment \(unavailable: Install glitnir-forseti\[z3\] to decide\.\)/);
});

test('cancelling a running install closes previews and explains recovery', async t => {
  const controller = {};
  const f = await flow(t, {during: async (signal, run) => {
    controller.signal = signal;
    f.fake.window.cancel();
    assert.ok(signal.aborted, 'the progress cancellation reaches uv');
    throw signal.reason;
  }});
  f.fake.window.withProgress = async (options, work) => {
    assert.equal(options.cancellable, true);
    const listeners = [];
    f.fake.window.cancel = () => listeners.forEach(listener => listener());
    return work({report() {}}, {isCancellationRequested: false, onCancellationRequested: listener => { listeners.push(listener); return {dispose() {}}; }});
  };
  await assert.rejects(install(f), /It was cancelled\. The backend environment at \/env\/bin\/python may be partly changed/);
  assert.deepEqual(f.calls.restarts, ['/env/bin/python']);
  assert.ok(controller.signal.aborted);
});

test('package changes run one at a time and hold a managed backend\'s setup lock', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-store-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const environment = path.join(root, 'a'.repeat(64));
  let resume, observedLock;
  const f = await flow(t, {store: root, environment: {python: path.join(environment, 'bin/python')}, during: async () => {
    observedLock = await fs.stat(environment + '.lock').then(info => info.isDirectory());
    await new Promise(resolve => { resume = resolve; });
  }});
  const first = install(f);
  while (!resume) await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(f.manager.install(), /Another Glitnir extension package change is in progress/);
  resume();
  await first;
  assert.equal(observedLock, true, 'uv runs while the backend setup lock is held');
  await assert.rejects(fs.stat(environment + '.lock'), /ENOENT/, 'the lock is released');
  await fs.mkdir(environment + '.lock');
  const runs = f.calls.runs.length;
  await assert.rejects(install(f), /Backend setup or another package change is using/);
  assert.equal(f.calls.runs.length, runs, 'a held lock runs no uv');
  assert.equal(f.calls.restarts.length, 1, 'a refused change closes nothing');
});

test('requirements cannot reach a withheld extra or hide behind a marker', async () => {
  const withMetadata = text => forseti({'glitnir_forseti-0.1.0.dist-info/METADATA': text});
  const add = lines => METADATA.replace('Provides-Extra: z3\n', `Provides-Extra: z3\n${lines}\n`);
  const refuse = async (lines, pattern) => assert.rejects(packages.inspectWheel(FORSETI, await withMetadata(add(lines))), pattern, lines);
  await refuse('Requires-Dist: glitnir-forseti[simulation]', /requires itself/);
  await refuse('Requires-Dist: Glitnir_Forseti[simulation]; python_version >= "3"', /requires itself/);
  await refuse('Requires-Dist: glitnir-forseti', /requires itself/);
  await refuse('Requires-Dist: glitnir[dev]', /Glitnir backend with extras/);
  await refuse('Requires-Dist: hiddendep; extra == "test" or python_version >= "3"', /cannot check/);
  await refuse('Requires-Dist: hiddendep; python_version >= "3" or extra == "z3"', /cannot check/);
  await refuse('Requires-Dist: hiddendep; (extra == "z3")', /cannot check/);
  await refuse('Requires-Dist: hiddendep; extra == "z3" and extra == "test"', /cannot check/);
  await refuse('Requires-Dist: hiddendep; extra != "z3"', /cannot check/);
  await refuse('Requires-Dist: hiddendep; extra == "nope"', /undeclared extra/);
  await refuse('Requires-Dist: hiddendep; "nope" == extra', /undeclared extra/);
  const accepted = await packages.inspectWheel(FORSETI, await withMetadata(add([
    'Requires-Dist: z3-solver>=5; "z3" == extra',
    'Requires-Dist: z3-solver<6; python_version >= "3.12" and extra == \'z3\'',
    'Requires-Dist: hiddendep; python_version >= "3"',
    'Requires-Dist: testonly; extra == "test" and python_version >= "3"',
  ].join('\n'))));
  assert.deepEqual(accepted.extras.find(extra => extra.name === 'z3'), {name: 'z3', requirements: ['z3-solver>=5', 'z3-solver<6', 'z3-solver==5.1.0.0']});
  assert.deepEqual(accepted.requirements, ['lark>=1.2.2', 'hiddendep'], 'every requirement uv installs without extras is shown');
  const nested = await packages.inspectWheel(FORSETI, await withMetadata(add('Requires-Dist: z3-solver[all]; extra == "z3"')));
  assert.match(nested.extras.find(extra => extra.name === 'z3').unavailable, /whose own extras Glitnir does not install/);
});

test('the wheel RECORD must list exactly the archive files in plain rows', async () => {
  const record = 'glitnir_forseti-0.1.0.dist-info/RECORD';
  const rows = Object.entries(FORSETI_FILES).map(([file, text]) => recordRow(file, text)).join('') + `${record},,\n`;
  const [hash, size] = recordRow('glitnir_forseti/__init__.py', FORSETI_FILES['glitnir_forseti/__init__.py']).trim().split(',').slice(1);
  const inspect = async text => packages.inspectWheel(FORSETI, await forseti({[record]: text}));
  assert.equal((await inspect(rows)).name, 'glitnir-forseti');
  assert.equal((await inspect(rows.replace(/\n/g, '\r\n'))).name, 'glitnir-forseti', 'CRLF line ends are accepted');
  const malformed = async (text, pattern, why) => assert.rejects(inspect(text), pattern, why);
  // uv's CSV reader ends a record at a lone carriage return, hiding what follows.
  await malformed(rows.replace(`${record},,\n`, `${record},,\r../../../../escape-target.txt,,\n`), /malformed/, 'lone CR');
  await malformed(rows.replace('glitnir_forseti/__init__.py,', '"glitnir_forseti/__init__.py",'), /quotes/, 'quoted path');
  await malformed(rows.replace(hash, `"${hash}"`), /quotes/, 'quote in a field');
  await malformed(rows + '../../../escape-target.txt,,\n', /lists a file it does not contain \(\.\.\/\.\.\/\.\.\/escape-target\.txt\)/, 'extra row');
  await malformed(rows + 'glitnir/__init__.py,,\n', /lists a file it does not contain/, 'core row');
  await malformed(rows.replace(recordRow('glitnir_forseti-0.1.0.dist-info/WHEEL', FORSETI_FILES['glitnir_forseti-0.1.0.dist-info/WHEEL']), ''), /RECORD does not list glitnir_forseti-0\.1\.0\.dist-info\/WHEEL/, 'missing row');
  await malformed(rows.replace(`${record},,\n`, ''), /does not list glitnir_forseti-0\.1\.0\.dist-info\/RECORD/, 'missing own row');
  await malformed(rows.replace(`${record},,\n`, `${record},sha256=abc,3\n`), /its own row has a hash/, 'hashed own row');
  await malformed(rows + 'glitnir_forseti/__init__.py,,\n', /listed twice/, 'duplicate row');
  await malformed(rows + 'GLITNIR_FORSETI/__init__.py,,\n', /listed twice/, 'case-folded duplicate row');
  await malformed(rows.replace(`${hash},${size}`, `${hash},${size},extra`), /not path,hash,size/, 'four fields');
  await malformed(rows.replace(`${hash},${size}`, hash), /not path,hash,size/, 'two fields');
  await malformed(rows + '\n', /not path,hash,size/, 'empty row');
  await malformed(rows.slice(0, -1), /does not end with a newline/, 'no final newline');
  await malformed(rows.replace(hash, hash.replace('sha256=', 'sha256=\t')), /only installs wheels whose file names and file list are plain ASCII\. Its RECORD file contains \\u\{9\}/, 'tab');
  await assert.rejects(packages.inspectWheel(FORSETI, await forseti({[record]: Buffer.from([0x66, 0xff, 0x0a])})), /RECORD file is not UTF-8/);
  await assert.rejects(packages.inspectWheel(FORSETI, await forseti({[record]: undefined})), /has no glitnir_forseti-0\.1\.0\.dist-info\/RECORD/);
  await assert.rejects(packages.inspectWheel(FORSETI, await forseti({[record]: rows}), {...packages.WHEEL_LIMITS, recordBytes: 10}), /RECORD file is too large/);
  for (const [file, other] of [['glitnir_forseti/data', 'glitnir_forseti/data/x.py'], ['glitnir_forseti/Data', 'glitnir_forseti/data/x.py'], ['glitnir_forseti/dir', 'glitnir_forseti/dir/']]) {
    await assert.rejects(packages.inspectWheel(FORSETI, await forseti({[file]: '', [other]: ''})), /both as a file and as a directory/, file);
  }
  for (const file of ['sitecustomize.py', 'UserCustomize.py', 'sitecustomize/__init__.py', 'glitnir_forseti-0.1.0.data/purelib/sitecustomize.py', 'glitnir_forseti-0.1.0.data/platlib/usercustomize.so']) {
    await assert.rejects(packages.inspectWheel(FORSETI, await forseti({[file]: ''})), /run code whenever Python starts/, file);
  }
});

/** Rewrite the local file header name of `name` to `replacement` (same length). */
function renameLocal(bytes, name, replacement) {
  const copy = Buffer.from(bytes);
  for (let offset = copy.indexOf('PK\x03\x04'); offset >= 0; offset = copy.indexOf('PK\x03\x04', offset + 4)) {
    const length = copy.readUInt16LE(offset + 26);
    if (copy.toString('latin1', offset + 30, offset + 30 + length) === name) { copy.write(replacement, offset + 30, 'latin1'); return copy; }
  }
  throw new Error(`no local header for ${name}`);
}

/** Give the central-directory record of `name` an Info-ZIP Unicode Path field naming `unicode`. */
function addUnicodePath(bytes, name, unicode) {
  const {crc32} = require('node:zlib');
  const end = bytes.lastIndexOf('PK\x05\x06');
  for (let offset = bytes.indexOf('PK\x01\x02'); offset >= 0 && offset < end; offset = bytes.indexOf('PK\x01\x02', offset + 4)) {
    const length = bytes.readUInt16LE(offset + 28);
    if (bytes.toString('latin1', offset + 46, offset + 46 + length) !== name || bytes.readUInt16LE(offset + 30)) continue;
    const text = Buffer.from(unicode), field = Buffer.alloc(9 + text.length);
    field.writeUInt16LE(0x7075, 0); field.writeUInt16LE(5 + text.length, 2); field.writeUInt8(1, 4);
    field.writeUInt32LE(crc32(Buffer.from(name, 'latin1')), 5); text.copy(field, 9);
    const copy = Buffer.concat([bytes.subarray(0, offset + 46 + length), field, bytes.subarray(offset + 46 + length)]);
    copy.writeUInt16LE(field.length, offset + 30);
    copy.writeUInt32LE(copy.readUInt32LE(end + field.length + 12) + field.length, end + field.length + 12);
    return copy;
  }
  throw new Error(`no central record for ${name}`);
}

test('archive names and RECORD rows must be printable ASCII, the same in every zip reader', async () => {
  const ascii = /only installs wheels whose file names and file list are plain ASCII/;
  const inspect = async bytes => packages.inspectWheel(FORSETI, bytes);
  // Python's str.splitlines() (pip, importlib.metadata) splits RECORD rows at these, uv does not.
  for (const separator of ['\u0085', '\u2028', '\u2029']) {
    await assert.rejects(inspect(await forseti({[`glitnir_forseti/x${separator}../site-packages`]: 'x'})), ascii, `name with ${JSON.stringify(separator)}`);
    await assert.rejects(inspect(await forseti({[`glitnir_forseti/x${separator}`]: 'x'})), ascii, `name ending in ${JSON.stringify(separator)}`);
  }
  // JavaScript leaves U+017F (long s) unchanged when lowercasing, so it hid a startup hook.
  await assert.rejects(inspect(await forseti({'ſitecustomize.py': 'print(1)'})), /plain ASCII\. It contains \\u\{17f\}itecustomize\.py\./);
  for (const name of ['glitnir_forseti/café.py', 'glitnir_forseti/a\u0001.py', 'glitnir_forseti/a\u007f.py', 'glitnir_forseti/\u{1f600}.py', 'glitnir_forseti/a\u00a0.py']) {
    await assert.rejects(inspect(await forseti({[name]: ''})), ascii, JSON.stringify(name));
  }
  const record = 'glitnir_forseti-0.1.0.dist-info/RECORD';
  const files = ['glitnir_forseti/__init__.py', 'glitnir_forseti-0.1.0.dist-info/METADATA', 'glitnir_forseti-0.1.0.dist-info/entry_points.txt', 'glitnir_forseti-0.1.0.dist-info/WHEEL', record];
  const rows = files.map(file => `${file},,\n`).join('');
  for (const separator of ['\u0085', '\u2028', '\u2029', '\ufeff', 'é', '\u000b', '\u000c']) {
    const text = rows.replace(`${record},,\n`, `${record},,${separator}../../site-packages,,\n`);
    await assert.rejects(inspect(await forseti({[record]: text})), /plain ASCII\. Its RECORD file contains \\u\{/, `RECORD with ${JSON.stringify(separator)}`);
  }
  await assert.rejects(inspect(await forseti({[record]: '\ufeff' + rows})), /Its RECORD file contains \\u\{feff\}/, 'byte order mark');
  await assert.rejects(inspect(await forseti({'glitnir_forseti-0.1.0.dist-info/METADATA': '\ufeff' + METADATA})), /METADATA file is malformed/, 'byte order mark in METADATA');
  // Streaming readers use the local file header; central-directory readers do not.
  const bytes = await forseti();
  assert.equal((await inspect(bytes)).name, 'glitnir-forseti');
  await assert.rejects(inspect(renameLocal(bytes, 'glitnir_forseti/__init__.py', 'glitnir_forseti/__init__.pz')), /archive headers disagree about the name of glitnir_forseti\/__init__\.py/);
  await assert.rejects(inspect(renameLocal(bytes, 'glitnir_forseti/__init__.py', 'glitnir_forseti/__init__.p\xff')), /archive headers disagree/);
  // yauzl takes a Unicode Path field's name over the header name; other readers may not.
  await assert.rejects(inspect(addUnicodePath(bytes, 'glitnir_forseti/__init__.py', 'glitnir_forseti/__init__.pz')), /plain ASCII\. It contains glitnir_forseti\/__init__\.pz\./);
  await assert.rejects(inspect(addUnicodePath(bytes, 'glitnir_forseti/__init__.py', 'glitnir_forseti/__init__.py')), /plain ASCII\. glitnir_forseti\/__init__\.py has a second, Unicode file name\./);
});

test('a uv that cannot start changes nothing and is looked up again', async t => {
  const {UvNotStartedError} = require('../../dist/backend-install');
  let lookups = 0, fail = true;
  const f = await flow(t, {during: async () => { if (fail) { fail = false; throw new UvNotStartedError('uv could not be started at /installed/uv (ENOENT). Install uv (on macOS: brew install uv) and retry.'); } }});
  const findUv = f.manager.dependencies.findUv;
  f.manager.dependencies.findUv = async () => { lookups++; return findUv(); };
  await assert.rejects(install(f), /^Error: uv could not be started at \/installed\/uv \(ENOENT\)\. Install uv/);
  assert.deepEqual(f.calls.restarts, [], 'nothing ran, so previews stay open');
  await install(f);
  assert.equal(lookups, 2, 'uv is found again after it could not start');
  assert.deepEqual(f.calls.restarts, ['/env/bin/python']);
});

test('uv installs a plain archive written from the inspected files, which Python reads the same', async t => {
  const record = 'glitnir_forseti-0.1.0.dist-info/RECORD';
  const files = {...FORSETI_FILES, 'glitnir_forseti/tool.sh': '#!/bin/sh\n', 'glitnir_forseti/big.py': '# x\n'.repeat(5000), 'glitnir_forseti/empty.py': ''};
  const zip = new ZipFile();
  zip.addEmptyDirectory('glitnir_forseti/');
  for (const [name, text] of Object.entries(files)) zip.addBuffer(Buffer.from(text), name, {mode: name.endsWith('.sh') ? 0o100755 : 0o100644, compress: !name.endsWith('empty.py')});
  zip.addBuffer(Buffer.from(Object.entries(files).map(([file, text]) => recordRow(file, text)).join('') + `${record},,\n`), record);
  const {info, wheel: written} = await packages.prepareWheel(FORSETI, await archive(zip));
  assert.equal(info.name, 'glitnir-forseti');
  const names = [...Object.keys(files), record];
  // Every header in order, as streaming readers see it: no flags (data descriptors), extra fields or comment.
  const end = written.length - 22;
  assert.equal(written.readUInt32LE(end), 0x06054b50, 'the end record is last');
  assert.equal(written.readUInt16LE(end + 20), 0, 'no archive comment');
  assert.equal(written.readUInt16LE(end + 10), names.length, 'directory entries are left out');
  let offset = 0;
  for (const name of names) {
    assert.equal(written.readUInt32LE(offset), 0x04034b50);
    assert.equal(written.readUInt16LE(offset + 6), 0, `${name}: no flags`);
    assert.equal(written.readUInt16LE(offset + 28), 0, `${name}: no local extra field`);
    assert.equal(written.toString('latin1', offset + 30, offset + 30 + written.readUInt16LE(offset + 26)), name);
    offset += 30 + written.readUInt16LE(offset + 26) + written.readUInt32LE(offset + 18);
  }
  assert.equal(offset, written.readUInt32LE(end + 16), 'the central directory follows the last file');
  assert.equal(offset + written.readUInt32LE(end + 12), end, 'and ends at the end record');
  // Python's zipfile (pip, installer) reads the same names, order, contents and modes.
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-written-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  await fs.writeFile(path.join(root, FORSETI), written);
  const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';
  const script = 'import hashlib, json, sys, zipfile\nz = zipfile.ZipFile(sys.argv[1])\n' +
    'print(json.dumps({"bad": z.testzip(), "comment": len(z.comment), "entries": [[i.filename, hashlib.sha256(z.read(i)).hexdigest(), i.external_attr >> 16, len(i.extra), i.flag_bits, i.compress_type] for i in z.infolist()]}))';
  const read = spawnSync(python, ['-I', '-c', script, path.join(root, FORSETI)], {encoding: 'utf8', timeout: 60000});
  assert.equal(read.status, 0, read.stderr);
  const result = JSON.parse(read.stdout);
  assert.equal(result.bad, null, 'every CRC matches');
  assert.equal(result.comment, 0);
  const contents = {...files, [record]: Object.entries(files).map(([file, text]) => recordRow(file, text)).join('') + `${record},,\n`};
  assert.deepEqual(result.entries.map(entry => entry[0]), names);
  for (const [name, digest, mode, extra, flags, method] of result.entries) {
    assert.equal(digest, createHash('sha256').update(contents[name]).digest('hex'), name);
    assert.equal(mode, name.endsWith('.sh') ? 0o100755 : 0o100644, `${name}: only the executable bit is kept`);
    assert.deepEqual([extra, flags], [0, 0], name);
    assert.ok(method === 0 || method === 8, name);
  }
  assert.equal(result.entries.find(entry => entry[0].endsWith('big.py'))[5], 8, 'compressible files are deflated');
  assert.ok((await packages.prepareWheel(FORSETI, written)).wheel.equals(written), 'writing is deterministic and idempotent');
});

test('every RECORD row must give the SHA-256 hash and size of the file it names', async () => {
  const record = 'glitnir_forseti-0.1.0.dist-info/RECORD';
  const init = 'glitnir_forseti/__init__.py';
  const rows = Object.entries(FORSETI_FILES).map(([file, text]) => recordRow(file, text)).join('') + `${record},,\n`;
  const row = recordRow(init, FORSETI_FILES[init]).trim();
  const [, hash, size] = row.split(',');
  const refuse = async (replacement, why) => assert.rejects(packages.inspectWheel(FORSETI, await forseti({[record]: rows.replace(row, replacement)})),
    /Its RECORD does not give the SHA-256 hash and size of glitnir_forseti\/__init__\.py/, why);
  await refuse(`${init},,`, 'no hash or size');
  await refuse(`${init},${hash},`, 'no size');
  await refuse(`${init},,${size}`, 'no hash');
  await refuse(`${init},${hash},${Number(size) + 1}`, 'wrong size');
  await refuse(`${init},${hash},0${size}`, 'size with a leading zero');
  await refuse(`${init},${recordRow(init, 'import os\n').split(',')[1]},${size}`, 'hash of other contents');
  await refuse(`${init},${hash}=,${size}`, 'padded hash');
  await refuse(`${init},sha256=${createHash('sha256').update(FORSETI_FILES[init]).digest('hex')},${size}`, 'hex digest');
  await refuse(`${init},sha512=${createHash('sha512').update(FORSETI_FILES[init]).digest('base64url')},${size}`, 'other algorithm');
  // RECORD vouching for benign contents while the archive holds others.
  const lying = Object.entries(FORSETI_FILES).map(([file, text]) => recordRow(file, text)).join('') + `${record},,\n`;
  await assert.rejects(packages.inspectWheel(FORSETI, await forseti({[init]: 'import os; os.system("x")\n', [record]: lying})), /does not give the SHA-256 hash and size of glitnir_forseti\/__init__\.py/);
});

/** The local headers, central directory and entry count of a plain archive. */
function split(bytes) {
  const end = bytes.length - 22, size = bytes.readUInt32LE(end + 12), offset = bytes.readUInt32LE(end + 16);
  return {locals: bytes.subarray(0, offset), central: Buffer.from(bytes.subarray(offset, offset + size)), count: bytes.readUInt16LE(end + 10)};
}
/** `central` with each record's local header offset moved by `shift`. */
function rebase(central, shift) {
  for (let offset = 0; offset < central.length; offset += 46 + central.readUInt16LE(offset + 28) + central.readUInt16LE(offset + 30) + central.readUInt16LE(offset + 32)) {
    central.writeUInt32LE(central.readUInt32LE(offset + 42) + shift, offset + 42);
  }
  return central;
}
function endRecord(count, size, offset, comment = Buffer.alloc(0)) {
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(count, 8); end.writeUInt16LE(count, 10);
  end.writeUInt32LE(size, 12); end.writeUInt32LE(offset, 16); end.writeUInt16LE(comment.length, 20);
  return Buffer.concat([end, comment]);
}
/**
 * The round-4 reproducer: benign archive A and archive B with a startup hook,
 * concatenated. A ZIP64 end record names one, the 32-bit end record the other;
 * yauzl honoured ZIP64 while uv used the 32-bit record.
 */
async function zip64Swap(thirtyTwoBitToHook) {
  const a = split(await forseti()), b = split(await forseti({'zzzzzz.pth': 'import os\n'}));
  const shift = a.locals.length + a.central.length;
  const body = Buffer.concat([a.locals, a.central, b.locals, rebase(b.central, shift)]);
  const benign = [a.count, a.central.length, a.locals.length], hook = [b.count, b.central.length, shift + b.locals.length];
  const [narrow, wide] = thirtyTwoBitToHook ? [hook, benign] : [benign, hook];
  const record = Buffer.alloc(56), locator = Buffer.alloc(20);
  record.writeUInt32LE(0x06064b50, 0); record.writeBigUInt64LE(44n, 4); record.writeUInt16LE(45, 12); record.writeUInt16LE(45, 14);
  record.writeBigUInt64LE(BigInt(wide[0]), 24); record.writeBigUInt64LE(BigInt(wide[0]), 32);
  record.writeBigUInt64LE(BigInt(wide[1]), 40); record.writeBigUInt64LE(BigInt(wide[2]), 48);
  locator.writeUInt32LE(0x07064b50, 0); locator.writeBigUInt64LE(BigInt(body.length), 8); locator.writeUInt32LE(1, 16);
  return Buffer.concat([body, record, locator, endRecord(...narrow)]);
}
/** Insert an extra field into the local header of `name`, moving what follows. */
function addLocalExtra(bytes, name, field) {
  for (let offset = bytes.indexOf('PK\x03\x04'); offset >= 0; offset = bytes.indexOf('PK\x03\x04', offset + 4)) {
    const length = bytes.readUInt16LE(offset + 26);
    if (bytes.toString('latin1', offset + 30, offset + 30 + length) !== name) continue;
    const at = offset + 30 + length + bytes.readUInt16LE(offset + 28);
    const copy = Buffer.concat([bytes.subarray(0, at), field, bytes.subarray(at)]);
    copy.writeUInt16LE(copy.readUInt16LE(offset + 28) + field.length, offset + 28);
    const end = copy.length - 22, start = copy.readUInt32LE(end + 16) + field.length;
    copy.writeUInt32LE(start, end + 16);
    for (let record = start; record < end; record += 46 + copy.readUInt16LE(record + 28) + copy.readUInt16LE(record + 30) + copy.readUInt16LE(record + 32)) {
      if (copy.readUInt32LE(record + 42) > offset) copy.writeUInt32LE(copy.readUInt32LE(record + 42) + field.length, record + 42);
    }
    return copy;
  }
  throw new Error(`no local header for ${name}`);
}
/** Insert an extra field into the central-directory record of `name`. */
function addCentralExtra(bytes, name, field) {
  const end = bytes.length - 22;
  for (let offset = bytes.readUInt32LE(end + 16); offset < end; offset += 46 + bytes.readUInt16LE(offset + 28) + bytes.readUInt16LE(offset + 30) + bytes.readUInt16LE(offset + 32)) {
    const length = bytes.readUInt16LE(offset + 28);
    if (bytes.toString('latin1', offset + 46, offset + 46 + length) !== name) continue;
    const at = offset + 46 + length + bytes.readUInt16LE(offset + 30);
    const copy = Buffer.concat([bytes.subarray(0, at), field, bytes.subarray(at)]);
    copy.writeUInt16LE(copy.readUInt16LE(offset + 30) + field.length, offset + 30);
    copy.writeUInt32LE(copy.readUInt32LE(copy.length - 22 + 12) + field.length, copy.length - 22 + 12);
    return copy;
  }
  throw new Error(`no central record for ${name}`);
}

test('archives that zip readers could resolve differently are refused', async () => {
  const plain = /only installs wheels that are plain zip archives/;
  const inspect = bytes => packages.inspectWheel(FORSETI, bytes);
  const bytes = await forseti();
  assert.equal((await inspect(bytes)).name, 'glitnir-forseti');
  // yauzl honours the ZIP64 record and sees only archive A; uv installed B's startup hook.
  const swapped = await zip64Swap(true);
  const yauzl = await require('yauzl').fromBufferPromise(swapped);
  const seen = []; for await (const entry of yauzl.eachEntry()) seen.push(entry.fileName); yauzl.close();
  assert.ok(seen.length && !seen.includes('zzzzzz.pth'), 'the reproducer shows yauzl the benign archive');
  await assert.rejects(inspect(swapped), /plain zip archives: it has ZIP64 end records/);
  await assert.rejects(inspect(await zip64Swap(false)), /plain zip archives: it has ZIP64 end records/);
  const {central, locals, count} = split(bytes);
  const comment = Buffer.concat([bytes.subarray(0, -22), endRecord(count, central.length, locals.length, Buffer.from('x'))]);
  await assert.rejects(inspect(comment), /plain zip archives: it has a comment or data after its end record/);
  const innerEnd = Buffer.concat([bytes.subarray(0, -22), endRecord(count, central.length, locals.length, endRecord(count, central.length, locals.length))]);
  await assert.rejects(inspect(innerEnd), plain, 'an end record hidden in the comment');
  await assert.rejects(inspect(Buffer.concat([bytes, Buffer.from('trailing')])), plain);
  await assert.rejects(inspect(Buffer.concat([Buffer.from('prepended data'), bytes])), /central directory does not end at its end record/);
  const saturated = Buffer.from(bytes); saturated.writeUInt16LE(0xffff, saturated.length - 12); saturated.writeUInt16LE(0xffff, saturated.length - 14);
  await assert.rejects(inspect(saturated), /plain zip archives: it needs ZIP64 end records/);
  const short = Buffer.from(bytes); short.writeUInt16LE(count - 1, short.length - 12); short.writeUInt16LE(count - 1, short.length - 14);
  await assert.rejects(inspect(short), new RegExp(`central directory holds more than ${count - 1} entries`), 'an entry hidden after the counted ones');
  const disks = Buffer.from(bytes); disks.writeUInt16LE(1, disks.length - 18);
  await assert.rejects(inspect(disks), /spans several disks/);
  // A ZIP64 extra field can override the 32-bit sizes in some readers and not others.
  const zip64 = Buffer.alloc(20); zip64.writeUInt16LE(0x0001, 0); zip64.writeUInt16LE(16, 2);
  await assert.rejects(inspect(addCentralExtra(bytes, 'glitnir_forseti/__init__.py', zip64)), /plain zip archives: glitnir_forseti\/__init__\.py has ZIP64 sizes/);
  await assert.rejects(inspect(addLocalExtra(bytes, 'glitnir_forseti/__init__.py', zip64)), /plain zip archives: glitnir_forseti\/__init__\.py has ZIP64 sizes/);
  const other = Buffer.alloc(8); other.writeUInt16LE(0xcafe, 0); other.writeUInt16LE(4, 2);
  assert.equal((await inspect(addLocalExtra(bytes, 'glitnir_forseti/__init__.py', other))).name, 'glitnir-forseti', 'the local-extra helper keeps the archive valid');
});
