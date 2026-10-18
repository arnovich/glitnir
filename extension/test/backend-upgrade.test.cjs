const assert = require('node:assert/strict');
const {test} = require('node:test');
const Module = require('node:module');
const {createHash} = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const file = fsPath => ({fsPath, scheme: 'file', path: fsPath, toString: () => `file://${fsPath}`});

function fakeVscode() {
  const state = {infos: [], warnings: [], errors: [], errorActions: [], infoAction: undefined, config: {}, updates: [], inspect: {}, progress: 0};
  const fake = {
    env: {}, ProgressLocation: {Notification: 15}, ConfigurationTarget: {Global: 1, Workspace: 2, WorkspaceFolder: 3},
    Uri: {file, joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts))},
    extensions: {getExtension: () => undefined},
    authentication: {getSession: async () => { throw new Error('GitHub sign-in is not expected'); }},
    commands: {executeCommand: async () => {}},
    workspace: {
      isTrusted: true, workspaceFolders: [],
      getWorkspaceFolder: () => ({uri: file('/work')}),
      getConfiguration: (namespace, scope) => ({
        get: (key, fallback) => key in state.config ? state.config[key] : fallback,
        inspect: key => (scope ? state.inspect.folder : state.inspect.top)?.[key],
        update: async (...args) => { state.updates.push(args); },
      }),
    },
    window: {
      showInformationMessage: async (message, ...rest) => { state.infos.push([message, ...rest]); return typeof state.infoAction === 'function' ? state.infoAction(message, ...rest) : state.infoAction; },
      showWarningMessage: async message => { state.warnings.push(message); },
      showErrorMessage: async (message, ...actions) => { state.errors.push([message, ...actions]); return state.errorActions.shift(); },
      withProgress: async (options, work) => {
        state.progress++;
        const listeners = [];
        state.cancel = () => listeners.forEach(listener => listener());
        return work({report() {}}, {isCancellationRequested: false, onCancellationRequested: listener => { listeners.push(listener); return {dispose() {}}; }});
      },
    },
  };
  return {fake, state};
}

function load(fake, ...modules) {
  const original = Module._load;
  Module._load = function(name, ...args) { return name === 'vscode' ? fake : original.call(this, name, ...args); };
  try {
    for (const name of ['backend-upgrade', 'setup', 'release-assets', 'extension-packages']) delete require.cache[require.resolve(`../../dist/${name}`)];
    return Object.assign({}, ...modules.map(name => require(`../../dist/${name}`)));
  } finally { Module._load = original; }
}
const plain = load({}, 'backend-upgrade', 'release-assets');

/** A release like the candidate script writes: backend, plugin, manifest and SHA256SUMS, with this VSIX's pin. */
function release(overrides = {}) {
  const backend = Buffer.from('backend wheel'), plugin = Buffer.from(overrides.pluginBytes ?? 'plugin wheel');
  const version = '0.1.1', tag = 'v0.1.1';
  const pin = {asset: `glitnir-${version}-py3-none-any.whl`, size: backend.length, sha256: sha(backend), tag};
  const pluginAsset = `glitnir_forseti-${version}-py3-none-any.whl`;
  const assets = [
    {name: `glitnir-${version}.vsix`, size: 3, sha256: sha('vsx')},
    {name: pin.asset, size: pin.size, sha256: pin.sha256},
    {name: pluginAsset, size: plugin.length, sha256: sha(plugin)},
    {name: 'INSTALL.md', size: 5, sha256: sha('guide')},
  ];
  const manifest = {schema: 'glitnir-thin-candidate/v2', repository: 'arnovich/glitnir', sha: 'a'.repeat(40), tag, version, runId: '1',
    editor: {apiVersion: 2, rendererSha256: 'b'.repeat(64)}, plugin: {asset: pluginAsset, version, size: plugin.length, sha256: sha(plugin)}, assets, ...overrides.manifest};
  const manifestBytes = Buffer.from(JSON.stringify(manifest, null, 2) + '\n');
  const sums = overrides.sums ?? [...assets, {name: 'glitnir-candidate.json', sha256: sha(manifestBytes)}].map(a => `${a.sha256}  ${a.name}\n`).join('');
  const served = {'glitnir-candidate.json': manifestBytes, SHA256SUMS: Buffer.from(sums), [pluginAsset]: Buffer.from(overrides.servedPlugin ?? plugin)};
  const requests = [];
  const download = async (destination, descriptor, options) => {
    requests.push({asset: descriptor.source.asset, repository: descriptor.source.repository, tag: descriptor.source.tag, archive: descriptor.archive, fallback: options.fallback});
    options.signal.throwIfAborted();
    const bytes = served[descriptor.source.asset];
    if (!bytes) throw new Error(`Pinned runtime asset is missing (${descriptor.source.asset})`);
    await fs.writeFile(destination, bytes, {flag: 'wx'});
  };
  return {pin, version, manifest, manifestBytes, sums, pluginAsset, plugin, download, requests};
}

async function store(t, shas) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-upgrade-store-')));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  for (const [index, name] of shas.entries()) {
    const directory = path.join(root, name);
    await fs.mkdir(path.join(directory, 'bin'), {recursive: true});
    await fs.writeFile(path.join(directory, 'bin/python'), '');
    await fs.writeFile(path.join(directory, 'lib-marker'), 'old packages');
    await fs.writeFile(path.join(directory, '.glitnir-backend'), name);
    const when = new Date(Date.now() - (shas.length - index) * 60000);
    await fs.utimes(path.join(directory, '.glitnir-backend'), when, when);
  }
  return root;
}

test('managed backends are complete, marked directories, newest first', async t => {
  const older = '1'.repeat(64), newer = '2'.repeat(64);
  const root = await store(t, [older, newer]);
  await fs.mkdir(path.join(root, '3'.repeat(64)));
  await fs.mkdir(path.join(root, '4'.repeat(64)));
  await fs.writeFile(path.join(root, '4'.repeat(64), '.glitnir-backend'), '5'.repeat(64));
  await fs.symlink(path.join(root, older), path.join(root, '6'.repeat(64)));
  await fs.mkdir(path.join(root, '.install-x'));
  await fs.mkdir(path.join(root, older + '.lock'));
  assert.deepEqual((await plain.managedBackends(root)).map(b => b.sha256), [newer, older], 'incomplete, mismarked, symlinked and scratch directories are ignored');
  assert.deepEqual(await plain.managedBackends(path.join(root, 'missing')), []);
  const state = await plain.upgradeState(root, '9'.repeat(64));
  assert.equal(state.current, undefined);
  assert.equal(state.previous.sha256, newer);
  assert.equal(state.previous.python, path.join(root, newer, 'bin/python'));
  assert.equal((await plain.upgradeState(root, older)).current.sha256, older);
});

test('the offer needs an earlier backend, no matching one, the setting, and fires once per version', () => {
  const previous = {sha256: '1'.repeat(64)}, current = {sha256: '2'.repeat(64)};
  const base = {enabled: true, version: '0.1.1', supported: true, state: {previous}};
  assert.equal(plain.shouldOffer(base), true);
  assert.equal(plain.shouldOffer({...base, offered: '0.1.0'}), true, 'an offer for an earlier version does not count');
  assert.equal(plain.shouldOffer({...base, offered: '0.1.1'}), false, 'once per version');
  assert.equal(plain.shouldOffer({...base, enabled: false}), false, 'the setting turns it off');
  assert.equal(plain.shouldOffer({...base, state: {}}), false, 'a first installation has nothing to upgrade');
  assert.equal(plain.shouldOffer({...base, state: {previous, current}}), false, 'this build\'s backend already exists');
  assert.equal(plain.shouldOffer({...base, supported: false}), false);
});

/** A BackendUpgrade over a temporary store with an earlier environment holding two provider packages. */
async function upgradeFlow(t, options = {}) {
  const {fake, state} = fakeVscode();
  const {BackendUpgrade, OFFERED_KEY} = load(fake, 'backend-upgrade');
  const r = release(options.release);
  const old = '1'.repeat(64);
  const root = await store(t, options.noEarlier ? [] : [old]);
  const global = new Map(Object.entries(options.global ?? {}));
  const calls = {installPinned: 0, adopted: [], installs: [], reports: [], log: []};
  const newPython = path.join(root, r.pin.sha256, 'bin/python');
  const installedNew = [];
  const setup = {
    installPinned: async (d, uv, signal) => {
      calls.installPinned++;
      assert.deepEqual(d, r.pin, 'the backend is the one this VSIX pins');
      assert.equal(uv, '/installed/uv');
      signal.throwIfAborted();
      if (options.backendFails) throw new Error('Backend wheel checksum does not match this extension.');
      await fs.mkdir(path.join(root, r.pin.sha256, 'bin'), {recursive: true});
      await fs.writeFile(newPython, '');
      await fs.writeFile(path.join(root, r.pin.sha256, '.glitnir-backend'), r.pin.sha256);
      return newPython;
    },
    adopt: async python => { calls.adopted.push(python); return options.settings ?? []; },
    requireUv: async () => options.noUv ? undefined : '/installed/uv',
  };
  const earlierPackages = options.earlier ?? [
    {name: 'glitnir-forseti', version: '0.1.0', entryPoints: ['forseti'], extras: ['z3']},
    {name: 'acme-views', version: '2.0', entryPoints: ['acme']},
  ];
  const packages = {
    packages: async (python, signal) => {
      calls.reports.push(python);
      if (python !== newPython && options.unreadable) throw new Error('Python is gone');
      return {core: python === newPython ? r.version : '0.1.0', packages: python === newPython ? installedNew : earlierPackages, commands: {}};
    },
    installVerified: async (python, fileName, bytes, extras, signal) => {
      calls.installs.push({python, fileName, bytes: bytes.toString(), extras});
      if (options.during) await options.during(signal, state);
      signal.throwIfAborted();
      if (options.installFails?.()) throw new Error('uv could not install glitnir-forseti.');
      installedNew.push({name: 'glitnir-forseti', version: r.version, entryPoints: ['forseti'], extras});
      return {info: {name: 'glitnir-forseti', version: r.version}, extras: extras.filter(e => e !== 'gone'), dropped: extras.filter(e => e === 'gone'), after: {}};
    },
  };
  const context = {
    extension: {packageJSON: {version: r.version, glitnirBackend: r.pin}},
    globalState: {get: key => global.get(key), update: async (key, value) => { global.set(key, value); }},
  };
  const upgrade = new BackendUpgrade(context, {appendLine: text => calls.log.push(text)}, setup, packages, {
    store: () => root, download: r.download,
  });
  return {upgrade, state, fake, calls, r, root, old: path.join(root, old), newPython, global, OFFERED_KEY, installedNew};
}

const snapshot = async directory => {
  const files = {};
  for (const name of await fs.readdir(directory, {recursive: true})) {
    const full = path.join(directory, name);
    if ((await fs.stat(full)).isFile()) files[name] = await fs.readFile(full, 'utf8');
  }
  return files;
};

test('the startup offer asks once per version, and nothing is downloaded without the click', async t => {
  const f = await upgradeFlow(t);
  await f.upgrade.offer();
  assert.deepEqual(f.state.infos, [['Glitnir was updated to 0.1.1. Set up its backend?', 'Set Up']]);
  assert.equal(f.r.requests.length + f.calls.installPinned + f.state.progress, 0, 'dismissing downloads and installs nothing');
  assert.equal(f.global.get(f.OFFERED_KEY), '0.1.1');
  await f.upgrade.offer();
  assert.equal(f.state.infos.length, 1, 'the same version is not offered again');
  const clicked = await upgradeFlow(t);
  clicked.state.infoAction = message => message.startsWith('Glitnir was updated') ? 'Set Up' : undefined;
  await clicked.upgrade.offer();
  assert.equal(clicked.calls.installPinned, 1);
  assert.deepEqual(clicked.calls.adopted, [clicked.newPython]);
  const off = await upgradeFlow(t);
  off.state.config.offerBackendUpdate = false;
  await off.upgrade.offer();
  assert.equal(off.state.infos.length, 0, 'glitnir.offerBackendUpdate = false turns the prompt off');
  assert.equal(off.global.get(off.OFFERED_KEY), undefined);
  const fresh = await upgradeFlow(t, {noEarlier: true});
  await fresh.upgrade.offer();
  assert.equal(fresh.state.infos.length, 0, 'a first installation is not offered an upgrade');
  const current = await upgradeFlow(t);
  await fs.mkdir(path.join(current.root, current.r.pin.sha256, 'bin'), {recursive: true});
  await fs.writeFile(path.join(current.root, current.r.pin.sha256, 'bin/python'), '');
  await fs.writeFile(path.join(current.root, current.r.pin.sha256, '.glitnir-backend'), current.r.pin.sha256);
  await current.upgrade.offer();
  assert.equal(current.state.infos.length, 0, 'a matching backend already exists');
  const remote = await upgradeFlow(t);
  remote.fake.env.remoteName = 'ssh-remote';
  await remote.upgrade.offer();
  assert.equal(remote.state.infos.length, 0);
});

test('set up installs the pinned backend, carries over release packages with extras, lists the rest and then switches', async t => {
  const f = await upgradeFlow(t);
  const before = await snapshot(f.old);
  assert.equal(await f.upgrade.available(), f.old);
  assert.equal(await f.upgrade.run(), f.newPython);
  assert.equal(f.calls.installPinned, 1);
  assert.deepEqual(f.r.requests.map(r => [r.asset, r.repository, r.tag]), [
    ['glitnir-candidate.json', 'arnovich/glitnir', 'v0.1.1'], ['SHA256SUMS', 'arnovich/glitnir', 'v0.1.1'], [f.r.pluginAsset, 'arnovich/glitnir', 'v0.1.1'],
  ], 'only this repository\'s release with the pinned tag is downloaded from');
  assert.deepEqual(f.r.requests[2].archive, {size: f.r.plugin.length});
  assert.deepEqual(f.calls.installs, [{python: f.newPython, fileName: f.r.pluginAsset, bytes: 'plugin wheel', extras: ['z3']}], 'the extra selection is kept');
  assert.deepEqual(f.calls.adopted, [f.newPython]);
  const [message] = f.state.warnings;
  assert.match(message, /Carried over: glitnir-forseti 0\.1\.1 \[z3\]\./);
  assert.match(message, /Not in this release, reinstall manually with Glitnir: Install Extension Package…: acme-views 2\.0\./);
  assert.match(message, new RegExp(`earlier environment at ${f.old} is kept`));
  assert.deepEqual(await snapshot(f.old), before, 'the earlier environment is untouched');
  assert.deepEqual(f.state.errors, []);
  assert.equal(await f.upgrade.run(), f.newPython, 'running again is idempotent');
  assert.equal(f.calls.installs.length, 1, 'a package already carried over is not installed again');
});

test('third-party packages never fail the upgrade, and an unreadable earlier environment is reported', async t => {
  const only = await upgradeFlow(t, {earlier: [{name: 'acme-views', version: '2.0', entryPoints: ['acme']}]});
  assert.equal(await only.upgrade.run(), only.newPython);
  assert.equal(only.calls.installs.length, 0);
  assert.match(only.state.warnings[0], /reinstall manually.*acme-views 2\.0/);
  const none = await upgradeFlow(t, {earlier: []});
  assert.equal(await none.upgrade.run(), none.newPython);
  assert.equal(none.r.requests.length, 0, 'without packages to carry over, only the backend is downloaded');
  assert.match(none.state.infos.at(-1)[0], /^The Glitnir 0\.1\.1 backend is ready/);
  const unreadable = await upgradeFlow(t, {unreadable: true});
  assert.equal(await unreadable.upgrade.run(), unreadable.newPython);
  assert.match(unreadable.state.warnings[0], /could not be listed/);
  const dropped = await upgradeFlow(t, {earlier: [{name: 'Glitnir_Forseti', version: '0.1.0', entryPoints: ['forseti'], extras: ['z3', 'gone']}]});
  assert.equal(await dropped.upgrade.run(), dropped.newPython);
  assert.deepEqual(dropped.calls.installs[0].extras, ['z3', 'gone'], 'names match after normalization; the installer applies the allowlist');
  assert.match(dropped.state.warnings[0], /no longer offer: glitnir-forseti\[gone\]/);
});

test('a manifest or checksum that does not match this build is refused before anything is installed', async t => {
  for (const overrides of [
    {manifest: {tag: 'v0.1.0'}}, {manifest: {version: '0.1.0'}}, {manifest: {repository: 'someone/else'}},
    {manifest: {schema: 'glitnir-thin-candidate/v1'}}, {sums: '0'.repeat(64) + '  SHA256SUMS\n'},
  ]) {
    const f = await upgradeFlow(t, {release: overrides});
    f.state.errorActions.push(undefined);
    assert.equal(await f.upgrade.run(), undefined, JSON.stringify(overrides));
    assert.equal(f.calls.installPinned, 0, 'the backend is not installed from a mismatched release');
    assert.deepEqual(f.calls.adopted, []);
    assert.match(f.state.errors[0][0], /did not finish: .*(does not match this Glitnir build|SHA256SUMS)/);
    assert.deepEqual(f.state.errors[0].slice(1), ['Retry', 'Read Setup Guide']);
  }
  const tampered = await upgradeFlow(t, {release: {servedPlugin: 'other bytes!'}});
  tampered.state.errorActions.push(undefined);
  assert.equal(await tampered.upgrade.run(), undefined);
  assert.equal(tampered.calls.installs.length, 0, 'a plugin wheel that differs from SHA256SUMS is never installed');
  assert.deepEqual(tampered.calls.adopted, []);
});

test('failure and cancellation leave the earlier environment intact and switch nothing; Retry runs again', async t => {
  let failures = 1;
  const f = await upgradeFlow(t, {installFails: () => failures-- > 0});
  const before = await snapshot(f.old);
  f.state.errorActions.push('Retry');
  assert.equal(await f.upgrade.run(), f.newPython, 'Retry finishes the setup');
  assert.match(f.state.errors[0][0], /did not finish: uv could not install glitnir-forseti\. The earlier environment is unchanged and nothing was switched\./);
  assert.equal(f.calls.installs.length, 2);
  assert.equal(f.calls.installPinned, 2, 'the retry reuses the backend environment the first attempt completed');
  assert.deepEqual(f.calls.adopted, [f.newPython], 'switched once, after the successful attempt');
  assert.deepEqual(await snapshot(f.old), before);
  const failing = await upgradeFlow(t, {backendFails: true});
  failing.state.errorActions.push(undefined);
  assert.equal(await failing.upgrade.run(), undefined);
  assert.deepEqual(failing.calls.adopted, []);
  assert.equal(failing.global.size, 0, 'nothing is selected');
  const cancelled = await upgradeFlow(t, {during: async (signal, state) => { state.cancel(); assert.ok(signal.aborted); }});
  const kept = await snapshot(cancelled.old);
  cancelled.state.errorActions.push(undefined);
  assert.equal(await cancelled.upgrade.run(), undefined);
  assert.match(cancelled.state.errors[0][0], /was cancelled/);
  assert.deepEqual(cancelled.state.errors[0].slice(1), ['Retry', 'Read Setup Guide']);
  assert.deepEqual(cancelled.calls.adopted, []);
  assert.deepEqual(await snapshot(cancelled.old), kept);
  const concurrent = await upgradeFlow(t);
  const [first, second] = [concurrent.upgrade.run(), concurrent.upgrade.run()];
  assert.equal(first, second, 'a second click joins the running setup');
  await first;
  assert.equal(concurrent.calls.installPinned, 1);
});

test('declining to install uv stops the upgrade quietly before any download', async t => {
  const f = await upgradeFlow(t, {noUv: true});
  assert.equal(await f.upgrade.run(), undefined);
  assert.equal(f.r.requests.length + f.calls.installPinned + f.state.errors.length, 0);
  assert.deepEqual(f.calls.adopted, []);
});

test('release verification binds tag, version, backend and plugin to SHA256SUMS', () => {
  const r = release();
  const verified = plain.verifyRelease(r.manifestBytes, r.sums, r.pin, r.version);
  assert.deepEqual(verified, {tag: 'v0.1.1', version: '0.1.1', plugins: [{name: 'glitnir-forseti', asset: r.pluginAsset, version: '0.1.1', size: r.plugin.length, sha256: sha(r.plugin)}]});
  const refuse = (bytes, sums, pin = r.pin, version = r.version) => assert.throws(() => plain.verifyRelease(bytes, sums, pin, version), /does not match this Glitnir build|SHA256SUMS/);
  const edited = change => Buffer.from(JSON.stringify({...r.manifest, ...change}));
  const resum = bytes => r.sums.replace(/^[a-f0-9]{64}(?=  glitnir-candidate\.json$)/m, sha(bytes));
  for (const change of [
    {tag: 'v0.1.2'}, {version: '0.1.2'}, {repository: 'arnovich/other'},
    {plugin: {...r.manifest.plugin, sha256: '0'.repeat(64)}}, {plugin: {...r.manifest.plugin, version: '0.1.0'}},
    {plugin: {...r.manifest.plugin, asset: 'glitnir_forseti-0.1.0-py3-none-any.whl'}},
    {assets: r.manifest.assets.map(a => a.name === r.pin.asset ? {...a, sha256: '0'.repeat(64)} : a)},
  ]) { const bytes = edited(change); refuse(bytes, resum(bytes)); }
  refuse(r.manifestBytes, r.sums, {...r.pin, tag: 'v0.1.0'});
  refuse(r.manifestBytes, r.sums, r.pin, '0.1.2');
  refuse(edited({}), r.sums, r.pin);
  refuse(r.manifestBytes, r.sums.replace(sha(r.plugin), '0'.repeat(64)));
  refuse(r.manifestBytes, r.sums + `${'0'.repeat(64)}  extra.txt\n`);
  assert.throws(() => plain.parseSums('nonsense\n'), /malformed/);
  assert.throws(() => plain.parseSums(`${'0'.repeat(64)}  a\n${'1'.repeat(64)}  a\n`), /malformed/);
});

test('release fetches name only this build\'s release and check the plugin digest', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-release-'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  const r = release();
  const signal = new AbortController().signal;
  const verified = await plain.fetchRelease(r.pin, r.version, directory, {signal, download: r.download});
  assert.deepEqual(r.requests.map(q => q.archive), [{maxSize: 1024 ** 2}, {maxSize: 64 * 1024}], 'text files are bounded by size before their digest is known');
  assert.ok(r.requests.every(q => /Install Extension Package/.test(q.fallback)));
  const wheel = await plain.fetchPlugin(r.pin, verified.plugins[0], directory, {signal, download: r.download});
  assert.equal(await fs.readFile(wheel, 'utf8'), 'plugin wheel');
  const bad = release({servedPlugin: 'other bytes!'});
  const other = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-release-'));
  t.after(() => fs.rm(other, {recursive: true, force: true}));
  await assert.rejects(plain.fetchPlugin(bad.pin, plain.verifyRelease(bad.manifestBytes, bad.sums, bad.pin, bad.version).plugins[0], other, {signal, download: bad.download}), /does not match the release SHA256SUMS/);
  await assert.rejects(plain.fetchRelease({...r.pin, tag: undefined}, r.version, directory, {signal, download: r.download}), /names no release/);
});

test('the carry-over plan fetches release packages with their extras and lists the rest', () => {
  const plugins = [{name: 'glitnir-forseti', asset: 'glitnir_forseti-0.1.1-py3-none-any.whl', version: '0.1.1', size: 1, sha256: 'a'.repeat(64)}];
  const previous = [
    {name: 'glitnir_forseti', version: '0.1.0', entryPoints: ['forseti'], extras: ['z3']},
    {name: 'acme-views', version: '2.0', entryPoints: ['acme']},
  ];
  const plan = plain.planCarryOver(previous, plugins);
  assert.deepEqual(plan.fetch.map(item => [item.plugin.asset, item.extras]), [['glitnir_forseti-0.1.1-py3-none-any.whl', ['z3']]]);
  assert.deepEqual(plan.manual.map(p => p.name), ['acme-views']);
  assert.deepEqual(plain.planCarryOver(previous, []).manual.length, 2, 'without a release binding everything is manual');
  const done = plain.planCarryOver(previous, plugins, [{name: 'glitnir-forseti', version: '0.1.1', entryPoints: ['forseti'], extras: ['z3']}]);
  assert.equal(done.fetch.length, 0);
  assert.equal(done.present.length, 1);
  const missingExtra = plain.planCarryOver(previous, plugins, [{name: 'glitnir-forseti', version: '0.1.1', entryPoints: ['forseti'], extras: []}]);
  assert.equal(missingExtra.fetch.length, 1, 'an extra missing from the new environment installs again');
});

test('discovery prefers this build\'s backend over a remembered earlier one, and adopting repoints only managed overrides', async t => {
  const {fake, state} = fakeVscode();
  const {EnvironmentSetup} = load(fake, 'setup');
  const home = os.homedir();
  const managed = sha => path.join(home, '.local/share/glitnir/backends', sha, 'bin/python');
  const pin = {asset: 'glitnir-0.1.1-py3-none-any.whl', size: 1, sha256: '2'.repeat(64), tag: 'v0.1.1'};
  const global = new Map(), workspace = new Map([['environment:file:///work', {python: managed('1'.repeat(64))}]]);
  const map = m => ({get: key => m.get(key), update: async (k, v) => { m.set(k, v); }});
  const setup = new EnvironmentSetup({extensionUri: file('/extension'), extension: {packageJSON: {version: '0.1.1', glitnirBackend: pin}}, workspaceState: map(workspace), globalState: map(global)}, {append() {}, appendLine() {}});
  const probed = [];
  setup.probe = async python => { probed.push(python); return {python}; };
  assert.equal((await setup.resolve(file('/work/A.lean'))).python, managed('1'.repeat(64)), 'before the upgrade the remembered environment is used');
  state.inspect.top = {pythonPath: {globalValue: managed('1'.repeat(64)), workspaceValue: '/my/own/python'}};
  await assert.rejects(setup.adopt('/elsewhere/bin/python'), /managed backend/);
  assert.deepEqual(await setup.adopt(managed(pin.sha256)), ['user settings']);
  assert.deepEqual(state.updates, [['pythonPath', managed(pin.sha256), 1]], 'only the override naming an earlier managed backend changes');
  assert.equal(global.get(`backend:${pin.sha256}`), managed(pin.sha256));
  probed.length = 0;
  assert.equal((await setup.resolve(file('/work/A.lean'))).python, managed(pin.sha256));
  assert.ok(!probed.includes(managed('1'.repeat(64))), 'the remembered earlier backend gives way');
});

test('opening a notebook without a backend offers the upgrade when an earlier one exists', async t => {
  const {fake, state} = fakeVscode();
  const {EnvironmentSetup} = load(fake, 'setup');
  const pin = {asset: 'glitnir-0.1.1-py3-none-any.whl', size: 1, sha256: '2'.repeat(64), tag: 'v0.1.1'};
  const map = () => { const m = new Map(); return {get: key => m.get(key), update: async (k, v) => { m.set(k, v); }}; };
  const setup = new EnvironmentSetup({extensionUri: file('/extension'), extension: {packageJSON: {version: '0.1.1', glitnirBackend: pin}}, workspaceState: map(), globalState: map()}, {append() {}, appendLine() {}});
  setup.probe = async () => { throw new Error('The installed Glitnir runtime is incompatible with this Glitnir build.'); };
  let runs = 0;
  setup.upgrade = {available: async () => '/store/old', run: async () => { runs++; return '/store/new/bin/python'; }};
  state.infoAction = message => message.startsWith('Glitnir was updated') ? 'Set Up' : undefined;
  assert.deepEqual(await setup.resolve(file('/work/A.lean')), {python: '/store/new/bin/python'});
  assert.equal(runs, 1);
  assert.match(state.infos[0][0], /^Glitnir was updated to 0\.1\.1\. Set up its backend to open this notebook\.$/);
  assert.match(state.infos[0][1].detail, /\/store\/old belongs to an earlier Glitnir build/);
  state.infoAction = undefined; state.infos.length = 0;
  assert.equal(await setup.resolve(file('/work/A.lean')), undefined);
  assert.equal(runs, 1, 'dismissing runs nothing');
  assert.equal(state.infos.length, 1, 'dismissing the upgrade does not stack a second prompt');
});

test('the command and setting are contributed without changing existing identifiers', () => {
  const manifest = require('../../package.json');
  const commands = manifest.contributes.commands.map(c => c.command);
  for (const id of ['glitnir.setup', 'glitnir.installExtensionPackage', 'glitnir.openPreview', 'glitnir.setupBackendForThisVersion']) assert.ok(commands.includes(id), id);
  assert.equal(manifest.contributes.commands.find(c => c.command === 'glitnir.setupBackendForThisVersion').title, 'Set Up Backend for This Version');
  const setting = manifest.contributes.configuration.properties['glitnir.offerBackendUpdate'];
  assert.deepEqual([setting.type, setting.default], ['boolean', true]);
});
