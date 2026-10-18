const assert = require('node:assert/strict');
const {test} = require('node:test');
const Module = require('node:module');
const path = require('node:path');

const file = fsPath => ({fsPath, scheme: 'file', path: fsPath, toString: () => `file://${fsPath}`});
const PIN = {asset: 'glitnir-0.1.1-py3-none-any.whl', size: 1, sha256: 'a'.repeat(64), tag: 'v0.1.1'};
const NEW_PYTHON = '/store/new/bin/python';

/** EnvironmentSetup over a VS Code double that finds no backend until one is installed. */
function harness(options = {}) {
  const state = {modals: [], notices: [], picks: [], pickItems: [], dialogs: [], dialogCalls: 0, commands: [], terminals: [], installs: [], answers: [], uvLookups: 0};
  let closeListener;
  const fake = {
    env: {}, ProgressLocation: {Notification: 15}, ConfigurationTarget: {WorkspaceFolder: 3},
    Uri: {file, joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts))},
    extensions: {getExtension: () => undefined},
    commands: {executeCommand: async (...args) => { state.commands.push(args); }},
    workspace: {isTrusted: true, workspaceFolders: [{uri: file('/work')}], getWorkspaceFolder: () => ({uri: file('/work')}), getConfiguration: () => ({get: (key, fallback) => fallback ?? '', update: async () => {}})},
    window: {
      showInformationMessage: async (message, ...rest) => {
        if (rest[0]?.modal) {
          state.modals.push([message, rest[0].detail, rest.slice(1)]);
          const answer = state.answers.shift();
          return typeof answer === 'function' ? answer() : answer;
        }
        state.notices.push([message, ...rest]);
        return rest.includes('Retry') && message.startsWith('Installing uv') ? state.terminalNotice : undefined;
      },
      showQuickPick: async items => { state.pickItems.push(items); const pick = state.picks.shift(); return typeof pick === 'function' ? pick(items) : pick; },
      showOpenDialog: async () => { state.dialogCalls++; return state.dialogs.shift(); },
      withProgress: async (o, work) => work({report() {}}, {isCancellationRequested: false, onCancellationRequested: () => ({dispose() {}})}),
      createTerminal: terminalOptions => {
        const terminal = {options: terminalOptions, shown: false, show() { this.shown = true; }};
        state.terminals.push(terminal);
        // The terminal runs the command: uv appears, then the terminal closes.
        setImmediate(() => { state.uvInstalled = true; closeListener?.(terminal); });
        return terminal;
      },
      onDidCloseTerminal: listener => { closeListener = listener; return {dispose() { closeListener = undefined; }}; },
    },
  };
  const original = Module._load;
  Module._load = function(name, ...args) { return name === 'vscode' ? fake : original.call(this, name, ...args); };
  let modules;
  try {
    for (const name of ['setup', 'backend-install']) delete require.cache[require.resolve(`../../dist/${name}`)];
    modules = {...require('../../dist/setup'), ...require('../../dist/backend-install')};
  } finally { Module._load = original; }
  const global = new Map(), workspace = new Map();
  const map = m => ({get: key => m.get(key), update: async (k, v) => { m.set(k, v); }});
  state.uvInstalled = options.uv !== false;
  const setup = new modules.EnvironmentSetup({extensionUri: file('/extension'), extension: {packageJSON: {version: '0.1.1', glitnirBackend: options.pin ?? PIN}}, workspaceState: map(workspace), globalState: map(global)},
    {append() {}, appendLine() {}}, {
      findUv: async () => { state.uvLookups++; if (!state.uvInstalled) throw new Error('Install uv first'); return '/installed/uv'; },
      findBrew: async () => options.brew,
    });
  setup.probe = async python => {
    if (python === NEW_PYTHON || (options.detected && python === '/work/.venv/bin/python')) return {python};
    throw new Error('missing');
  };
  setup.installPinned = async (d, uv, signal, report, wheel) => { state.installs.push({d, uv, wheel}); return NEW_PYTHON; };
  return {setup, state, global, modules, uri: file('/work/A.lean')};
}

test('Set Up downloads and installs the matching backend in one step, without sub-menus', async () => {
  const h = harness();
  h.state.answers.push('Set Up');
  assert.deepEqual(await h.setup.resolve(h.uri), {python: NEW_PYTHON});
  assert.equal(h.state.modals.length, 1, 'one prompt, no confirmation dialog');
  assert.deepEqual(h.state.modals[0][2], ['Set Up', 'More Options…'], 'Set Up is the first, default choice; the guide is not in the menu');
  assert.match(h.state.modals[0][1], /Set Up installs this build's verified backend with uv in one step/);
  assert.equal(h.state.pickItems.length + h.state.dialogCalls, 0, 'no source menu and no file dialog');
  assert.deepEqual(h.state.installs, [{d: PIN, uv: '/installed/uv', wheel: undefined}], 'the pinned wheel is downloaded from the release');
  assert.equal(h.global.get(`backend:${PIN.sha256}`), NEW_PYTHON, 'the installed backend is shared');
  const dismissed = harness();
  assert.equal(await dismissed.setup.resolve(dismissed.uri), undefined);
  assert.equal(dismissed.state.installs.length, 0, 'dismissing installs nothing');
});

test('Set Up Environment lists Set Up first and keeps the other choices under More Options…', async () => {
  const h = harness({detected: true});
  h.state.picks.push(items => items[0]);
  assert.deepEqual(await h.setup.resolve(h.uri, true), {python: NEW_PYTHON});
  assert.deepEqual(h.state.pickItems[0].map(item => item.label), ['Set Up (recommended)', 'Use detected environment', 'Install Extension Package…', 'More Options…']);
  assert.ok(!h.state.pickItems[0].some(item => /guide/i.test(item.label)), 'the guide is not a top-level choice');
  assert.deepEqual(h.state.installs.map(i => i.wheel), [undefined]);
  const more = harness();
  more.state.answers.push('More Options…');
  more.state.picks.push(items => items.find(item => item.source === 'wheel'));
  more.state.dialogs.push([file('/downloads/glitnir-0.1.1-py3-none-any.whl')]);
  assert.deepEqual(await more.setup.resolve(more.uri), {python: NEW_PYTHON});
  assert.deepEqual(more.state.pickItems[0].map(item => item.label), ['Choose downloaded backend wheel', 'Choose Existing Environment', 'Read setup guide']);
  assert.deepEqual(more.state.installs.map(i => i.wheel), ['/downloads/glitnir-0.1.1-py3-none-any.whl'], 'a downloaded wheel is still checked against the pin by the installer');
  const guide = harness();
  guide.state.answers.push('More Options…');
  guide.state.picks.push(items => items.find(item => item.source === 'guide'));
  assert.equal(await guide.setup.resolve(guide.uri), undefined);
  assert.equal(guide.state.commands[0][0], 'markdown.showPreview');
  assert.equal(guide.state.commands[0][1].fsPath, '/extension/dist/media/setup.md');
  const existing = harness();
  existing.state.answers.push('More Options…');
  existing.state.picks.push(items => items.find(item => item.source === 'existing'));
  existing.state.dialogs.push([file(NEW_PYTHON)]);
  assert.deepEqual(await existing.setup.resolve(existing.uri), {python: NEW_PYTHON});
  assert.equal(existing.state.installs.length, 0, 'choosing an existing environment installs nothing');
  const dev = harness({pin: {...PIN, tag: undefined}});
  dev.state.answers.push('Set Up');
  dev.state.dialogs.push([file('/dist/glitnir-0.1.1-py3-none-any.whl')]);
  assert.deepEqual(await dev.setup.resolve(dev.uri), {python: NEW_PYTHON});
  assert.deepEqual(dev.state.installs.map(i => i.wheel), ['/dist/glitnir-0.1.1-py3-none-any.whl'], 'a build without a release asks for its wheel');
});

test('without uv, Install uv is offered with its command and nothing runs without the click', async () => {
  const h = harness({uv: false});
  h.state.answers.push('Set Up', undefined);
  assert.equal(await h.setup.resolve(h.uri), undefined);
  const [message, detail, actions] = h.state.modals[1];
  assert.equal(message, 'Glitnir needs uv to install its backend.');
  assert.deepEqual(actions, ['Install uv', 'Retry', 'Read Setup Guide']);
  assert.ok(detail.includes(`/usr/bin/curl -LsSf https://astral.sh/uv/${h.modules.UV_VERSION}/install.sh | /bin/sh`), 'the command is shown before the click, with absolute paths');
  const names = ['HOME', ...(/^[A-Za-z0-9._-]{1,64}$/.test(process.env.USER ?? '') ? ['USER'] : []), ...(/^\/[\w./-]{0,1023}$/.test(process.env.TMPDIR ?? '') ? ['TMPDIR'] : []), 'LANG'];
  assert.ok(detail.includes(`in a minimal environment: PATH=/usr/bin:/bin:/usr/sbin:/sbin, ${names.join(', ')}, UV_NO_MODIFY_PATH=1 (VS Code adds its own terminal variables).`), detail);
  assert.match(detail, /leaves your shell profiles unchanged; to use uv in your own terminals, add ~\/\.local\/bin to PATH/);
  assert.equal(h.state.terminals.length + h.state.installs.length, 0, 'dismissing runs nothing');
  const clicked = harness({uv: false});
  clicked.state.answers.push('Set Up', 'Install uv');
  assert.deepEqual(await clicked.setup.resolve(clicked.uri), {python: NEW_PYTHON}, 'setup continues after the terminal closes');
  const [terminal] = clicked.state.terminals;
  const installer = clicked.modules.uvInstaller();
  assert.deepEqual(terminal.options, {name: 'Install uv', shellPath: '/bin/sh', shellArgs: ['-c', installer.command], env: installer.env, strictEnv: true},
    'the terminal gets exactly the environment shown, inheriting nothing');
  assert.ok(terminal.shown, 'the command runs where the user can see it');
  assert.equal(clicked.state.uvLookups, 2, 'uv is detected again after the terminal');
  assert.equal(clicked.state.installs[0].uv, '/installed/uv');
  const brew = harness({uv: false, brew: '/opt/homebrew/bin/brew'});
  brew.state.answers.push('Set Up', 'Install uv');
  await brew.setup.resolve(brew.uri);
  assert.match(brew.state.modals[1][1], /\/opt\/homebrew\/bin\/brew install uv/);
  assert.deepEqual(brew.state.terminals[0].options.shellArgs, ['-c', '/opt/homebrew/bin/brew install uv'], 'Homebrew is preferred when installed');
  assert.equal(brew.state.terminals[0].options.env.PATH, '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'PATH adds only Homebrew\'s own directory');
  assert.ok(!Object.keys(brew.state.terminals[0].options.env).some(name => name.startsWith('HOMEBREW_')));
  const retried = harness({uv: false});
  retried.state.answers.push('Set Up', 'Retry', () => { retried.state.uvInstalled = true; return 'Retry'; });
  assert.deepEqual(await retried.setup.resolve(retried.uri), {python: NEW_PYTHON});
  assert.equal(retried.state.terminals.length, 0, 'Retry only looks for uv again');
  assert.equal(retried.state.modals.length, 3, 'each failed lookup explains again');
  const guide = harness({uv: false});
  guide.state.answers.push('Set Up', 'Read Setup Guide');
  assert.equal(await guide.setup.resolve(guide.uri), undefined);
  assert.equal(guide.state.commands[0][0], 'markdown.showPreview');
  assert.equal(guide.state.terminals.length, 0);
});

test('the uv installer command is Homebrew or a pinned official installer, never an unversioned URL', () => {
  const {modules} = harness();
  assert.equal(modules.UV_VERSION, '0.12.23', 'the pinned uv release; bump it with the docs');
  const docs = ['../../README.md', '../media/setup.md'].map(file => require('node:fs').readFileSync(path.join(__dirname, file), 'utf8'));
  for (const text of docs) assert.ok(text.includes(`https://astral.sh/uv/${modules.UV_VERSION}/install.sh`), 'the docs show the pinned installer');
  const parent = {HOMEBREW_PREFIX: '/evil', UV_INSTALL_DIR: '/evil', USER: 'me', TMPDIR: '/var/folders/x/T/', PATH: '/evil/bin'};
  assert.deepEqual(modules.uvInstaller('/usr/local/bin/brew', '/home/me', parent), {command: '/usr/local/bin/brew install uv',
    env: {PATH: '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', HOME: '/home/me', USER: 'me', TMPDIR: '/var/folders/x/T/', LANG: 'en_US.UTF-8'}}, 'only USER and TMPDIR come from the parent environment');
  assert.deepEqual(modules.uvInstaller(undefined, '/home/me', parent), {command: `/usr/bin/curl -LsSf https://astral.sh/uv/${modules.UV_VERSION}/install.sh | /bin/sh`,
    env: {PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: '/home/me', USER: 'me', TMPDIR: '/var/folders/x/T/', LANG: 'en_US.UTF-8', UV_NO_MODIFY_PATH: '1'}});
  for (const bad of [{USER: 'me; rm -rf ~', TMPDIR: 'relative'}, {USER: '', TMPDIR: '/tmp/a b'}, {USER: 'x'.repeat(65), TMPDIR: '/tmp/$(id)'}]) {
    const env = modules.uvInstaller(undefined, '/home/me', bad).env;
    assert.ok(!('USER' in env) && !('TMPDIR' in env), `values that are not plain are dropped: ${JSON.stringify(bad)}`);
  }
  assert.doesNotMatch(modules.uvInstaller().command, /astral\.sh\/uv\/install\.sh/);
  assert.throws(() => modules.uvInstaller('brew'), /absolute path/);
  assert.equal(modules.uvInstallerTerminal(modules.uvInstaller()).strictEnv, true);
});
