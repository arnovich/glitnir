// Run in a separate VS Code profile; never touch the owner's editor settings.
const {downloadAndUnzipVSCode, resolveCliArgsFromVSCodeExecutablePath, runTests} = require('@vscode/test-electron');
const {spawnSync} = require('node:child_process');
const {mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync, existsSync, readFileSync, readdirSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const python = process.env.GLITNIR_TEST_PYTHON || process.env.GLITNIR_BUILD_PYTHON;
// The editor inherits this; Install uv's terminal must not (see host.cjs).
process.env.HOMEBREW_GLITNIR_PARENT = 'inherited';
if (!python) throw new Error('Set GLITNIR_TEST_PYTHON to the installed compatible runtime.');
const root = mkdtempSync(path.join(tmpdir(), 'glitnir-vscode-host-'));
const workspace = path.join(root, 'workspace');
mkdirSync(path.join(workspace, '.vscode'), {recursive: true});
// No Glitnir settings: exercise discovery of a nearby installation.
symlinkSync(path.dirname(path.dirname(python)), path.join(workspace, '.venv'), 'dir');
writeFileSync(path.join(workspace, 'lakefile.lean'), 'This must never execute');
// The supported toolchain, so a check is accepted and stops on its stale digest, not on the Lean version.
writeFileSync(path.join(workspace, 'lean-toolchain'), 'leanprover/lean4:v4.32.1\n');
for (const name of ['First', 'Second']) writeFileSync(path.join(workspace, `${name}.lean`), `/-! # ${name} notebook -/\n\ntheorem simple : True := by\n  trivial\n`);
async function main() {
  let extension = path.resolve(__dirname, '../..');
  let executable = process.env.VSCODE_EXECUTABLE_PATH;
  const installArgs = [];
  if (process.env.GLITNIR_TEST_VSIX) {
    executable ||= await downloadAndUnzipVSCode('stable');
    const [cli, ...args] = resolveCliArgsFromVSCodeExecutablePath(executable);
    const extensions = path.join(root, 'extensions');
    installArgs.push('--extensions-dir', extensions);
    const installed = spawnSync(cli, [...args, '--user-data-dir', path.join(root, 'profile'), ...installArgs, '--install-extension', path.resolve(process.env.GLITNIR_TEST_VSIX)], {stdio: 'inherit', env: {...process.env, ELECTRON_RUN_AS_NODE: '1'}});
    if (installed.error || installed.status) throw installed.error || new Error('Thin VSIX installation failed');
    const names = readdirSync(extensions).filter(name => name.startsWith('gimle.glitnir-'));
    if (names.length !== 1) throw new Error('Expected one installed Glitnir extension');
    extension = path.join(extensions, names[0]);
  }
  await runTests({
    extensionDevelopmentPath: extension,
    extensionTestsPath: path.resolve(__dirname, 'host.cjs'),
    extensionTestsEnv: {PATH: root, GLITNIR_HOST_PYTHON: path.resolve(python), GLITNIR_HOST_RESULT: path.join(root, 'failure.txt'), GLITNIR_HOST_SUCCESS: path.join(root, 'success.txt')},
    ...(executable ? {vscodeExecutablePath: executable} : {}),
    launchArgs: [workspace, ...installArgs, '--disable-extensions', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes', '--user-data-dir', path.join(root, 'profile')],
  });
  if (!existsSync(path.join(root, 'success.txt'))) throw new Error('VS Code exited without running the extension host tests. Use the editor executable, not its CLI wrapper.');
  rmSync(root, {recursive: true, force: true});
}
main().catch(error => {
  if (existsSync(path.join(root, 'failure.txt'))) console.error(readFileSync(path.join(root, 'failure.txt'), 'utf8'));
  console.error(error); process.exitCode = 1;
});
