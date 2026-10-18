import * as path from 'node:path';
import {downloadRuntime, RELEASE_REPOSITORY} from './runtime-download';
import {BackendDescriptor, backendDescriptor, backendStore, findBrew, findUv, installBackend, UvInstaller, uvInstaller, uvInstallerTerminal} from './backend-install';
import * as vscode from 'vscode';
import {discover, DiscoveryError, Environment, ProbeResult, probePython} from './discovery';

/**
 * Release downloads are anonymous: the repository is public. Only when GitHub
 * reports its anonymous rate limit is a sign-in offered, asking for no scopes.
 */
export async function githubToken(): Promise<string> {
  const session = await vscode.authentication.getSession('github', [], {createIfNone: true});
  return session.accessToken;
}

/** The managed environment directory holding `python`, if it is one of the store's backends. */
export function managedBackendSha(python: string | undefined, store = backendStore()): string | undefined {
  if (!python) return undefined;
  const directory = path.dirname(path.dirname(path.resolve(python)));
  return path.dirname(directory) === store && /^[a-f0-9]{64}$/.test(path.basename(directory)) && path.basename(python) === 'python'
    ? path.basename(directory) : undefined;
}

const UPGRADE_ACTION = 'Set Up';
/** The one-step setup: download this build's verified backend and install it with uv. */
export const SET_UP = 'Set Up';
export const MORE_OPTIONS = 'More Options…';
export const INSTALL_UV = 'Install uv';
type Source = 'download' | 'wheel' | 'existing';

export interface SetupDependencies {
  findUv?: () => Promise<string>;
  findBrew?: () => Promise<string | undefined>;
}

/** Open the setup guide shipped with the extension. */
export async function openSetupGuide(context: vscode.ExtensionContext): Promise<void> {
  await vscode.commands.executeCommand('markdown.showPreview', vscode.Uri.joinPath(context.extensionUri, 'dist', 'media', 'setup.md'));
}

/** A `glitnir.pythonPath` value that switching to this build's backend rewrites, and the file it lives in. */
export interface Override {target: vscode.ConfigurationTarget; folder?: vscode.Uri; file: string}

/** Offered when no backend is found but an earlier build's managed backend exists. */
export interface UpgradeOffer {
  /**
   * The upgrade from an earlier build's environment, worked out without
   * downloading or installing anything, or undefined when there is none.
   */
  prepare(): Promise<PreparedUpgrade | undefined>;
  /** Run a prepared upgrade the user confirmed; resolves to the new Python, or undefined when it did not finish. */
  run(prepared: PreparedUpgrade): Promise<string | undefined>;
}
/** What Set Up will install, download and change, shown before the click. */
export interface PreparedUpgrade {earlier?: string; detail: string}

interface PythonApi {
  ready?: Promise<void>;
  environments: {
    getActiveEnvironmentPath(uri: vscode.Uri): {path: string};
    resolveEnvironment(environment: {path: string}): Promise<{executable?: {uri?: vscode.Uri}} | undefined>;
  };
}

/** Bound optional Python-extension discovery so a slow extension cannot hang setup. */
async function selectedPython(uri: vscode.Uri): Promise<string | undefined> {
  const extension = vscode.extensions.getExtension<PythonApi>('ms-python.python');
  if (!extension) return undefined;
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      (async () => {
        const api = await extension.activate();
        await api.ready;
        const env = await api.environments.resolveEnvironment(api.environments.getActiveEnvironmentPath(uri));
        return env?.executable?.uri?.fsPath;
      })(),
      new Promise<undefined>(resolve => { timer = setTimeout(() => resolve(undefined), 3000); }),
    ]);
  } catch { return undefined; }
  finally { clearTimeout(timer); }
}

/** Workspace-local remembered choices; every use is checked against the installed pin. */
export class EnvironmentSetup {
  private controller = new AbortController();
  /** Set by the extension once the backend upgrade exists; offered when opening a notebook finds no backend. */
  upgrade?: UpgradeOffer;
  constructor(private context: vscode.ExtensionContext, private output: vscode.OutputChannel, private dependencies: SetupDependencies = {}) {}

  dispose(): void { this.controller.abort(); }

  private check(): void {
    if (this.controller.signal.aborted) throw new Error('Environment discovery cancelled');
    if (!vscode.workspace.isTrusted) throw new Error('Trust the workspace before selecting an environment.');
  }

  private async probe(python: string): Promise<ProbeResult> {
    this.check();
    const script = vscode.Uri.joinPath(this.context.extensionUri, 'dist/probe.py').fsPath;
    return probePython(python, script, this.controller.signal);
  }

  /**
   * An explicit setup action installs only the wheel pinned by this VSIX: in one
   * step from its release, or from a wheel the user selects (always for a
   * build that names no release).
   */
  private async installWithUv(chooseWheel: boolean): Promise<string | undefined> {
    this.check();
    if (vscode.env.remoteName || !['darwin', 'linux'].includes(process.platform)) throw new Error('Backend setup supports local macOS and Linux windows.');
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    const uv = await this.requireUv();
    if (!uv) return undefined;
    this.check();
    let wheel: string | undefined;
    if (chooseWheel || !d.tag) {
      const files = await vscode.window.showOpenDialog({title: 'Select matching Glitnir backend wheel', canSelectMany: false, canSelectFiles: true, canSelectFolders: false, filters: {'Python wheel': ['whl']}});
      if (!files?.[0]) return undefined;
      if (files[0].scheme !== 'file') throw new Error('Select a local backend wheel.');
      wheel = files[0].fsPath;
    }
    this.check();
    this.output.appendLine(`Installing the Glitnir backend in ${path.join(backendStore(), d.sha256)}`);
    return vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: 'Installing Glitnir backend', cancellable: true}, async (progress, token) => {
      const controller = new AbortController();
      const stop = (): void => controller.abort();
      this.controller.signal.addEventListener('abort', stop, {once: true});
      const subscription = token.onCancellationRequested(stop);
      if (token.isCancellationRequested || this.controller.signal.aborted) stop();
      try {
        return await this.installPinned(d, uv, controller.signal, message => progress.report({message}), wheel);
      } finally { subscription.dispose(); this.controller.signal.removeEventListener('abort', stop); }
    });
  }

  /**
   * The installed uv, or, when there is none, an explanation with Install uv:
   * only that click opens a terminal running the shown command. After the
   * terminal closes, or on Retry, uv is looked up again. Undefined when dismissed.
   */
  async requireUv(): Promise<string | undefined> {
    for (;;) {
      this.check();
      try { return await (this.dependencies.findUv ?? findUv)(); } catch { /* Offer to install it. */ }
      const installer = uvInstaller(await (this.dependencies.findBrew ?? findBrew)());
      const variables = Object.entries(installer.env).map(([name, value]) => `${name}=${value}`).join(' ');
      const profiles = installer.env.UV_NO_MODIFY_PATH ? ' It leaves your shell profiles unchanged; to use uv in your own terminals, add ~/.local/bin to PATH.' : '';
      const choice = await vscode.window.showInformationMessage('Glitnir needs uv to install its backend.', {
        modal: true,
        detail: `uv creates the backend environment from your installed Python. Install uv runs this command in a VS Code terminal, where you can watch it:\n\n${installer.command}\n\nwith only these environment variables: ${variables}.${profiles}\n\nSetup continues when the terminal closes. Choose Retry once uv is installed some other way.`,
      }, INSTALL_UV, 'Retry', 'Read Setup Guide');
      this.check();
      if (choice === 'Read Setup Guide') { await openSetupGuide(this.context); return undefined; }
      if (choice === INSTALL_UV) await this.runInTerminal(installer);
      else if (choice !== 'Retry') return undefined;
    }
  }

  /** Run `command` visibly in a new terminal; resolves when the terminal closes or the user chooses Retry. */
  private async runInTerminal(installer: UvInstaller): Promise<void> {
    const command = installer.command;
    const terminal = vscode.window.createTerminal(uvInstallerTerminal(installer));
    terminal.show();
    await new Promise<void>(resolve => {
      const closed = vscode.window.onDidCloseTerminal(t => { if (t === terminal) finish(); });
      const finish = (): void => { closed.dispose(); resolve(); };
      void vscode.window.showInformationMessage(`Installing uv in the terminal: ${command}. Glitnir continues setup when the terminal closes, or choose Retry.`, 'Retry').then(choice => { if (choice === 'Retry') finish(); }, finish);
    });
  }

  /**
   * Install, or reuse, the backend this VSIX pins in its own managed
   * environment: from a selected wheel, or downloaded from this build's
   * release. Either way the wheel must match the pinned size and SHA-256.
   */
  installPinned(d: BackendDescriptor, uv: string, signal: AbortSignal, report: (message: string) => void, wheel?: string): Promise<string> {
    return installBackend(backendStore(), d, {
      uv, wheel, signal, progress: report,
      probe: python => probePython(python, vscode.Uri.joinPath(this.context.extensionUri, 'dist/probe.py').fsPath, signal),
      acquire: d.tag ? (destination, signal) => downloadRuntime(destination, {
        archive: {size: d.size}, source: {repository: RELEASE_REPOSITORY, tag: d.tag!, asset: d.asset},
      }, {signal, progress: () => report('Downloading the matching backend'), githubToken}) : undefined,
    });
  }

  /**
   * The `glitnir.pythonPath` values that name an earlier build's managed
   * backend, which switching to this build's backend repoints, each with the
   * settings file it is written to. Other overrides are the user's own choice
   * and are kept.
   */
  pendingOverrides(): Override[] {
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    const earlier = (value: string | undefined): boolean => { const sha = managedBackendSha(value); return !!sha && sha !== d.sha256; };
    const overrides: Override[] = [];
    const top = vscode.workspace.getConfiguration('glitnir').inspect<string>('pythonPath');
    if (earlier(top?.globalValue)) overrides.push({target: vscode.ConfigurationTarget.Global, file: 'your user settings.json'});
    if (earlier(top?.workspaceValue)) {
      const file = vscode.workspace.workspaceFile?.scheme === 'file' ? vscode.workspace.workspaceFile.fsPath
        : vscode.workspace.workspaceFolders?.[0] ? path.join(vscode.workspace.workspaceFolders[0].uri.fsPath, '.vscode', 'settings.json') : 'the workspace settings';
      overrides.push({target: vscode.ConfigurationTarget.Workspace, file});
    }
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
      if (earlier(vscode.workspace.getConfiguration('glitnir', folder.uri).inspect<string>('pythonPath')?.workspaceFolderValue)) {
        overrides.push({target: vscode.ConfigurationTarget.WorkspaceFolder, folder: folder.uri, file: path.join(folder.uri.fsPath, '.vscode', 'settings.json')});
      }
    }
    return overrides;
  }

  /**
   * Switch every window to this build's complete managed backend at `python`:
   * share it, and repoint the overrides `pendingOverrides` names, which the
   * confirmation listed. Returns the settings files that changed.
   */
  async adopt(python: string): Promise<string[]> {
    this.check();
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    if (managedBackendSha(python) !== d.sha256) throw new Error('Only this build\'s managed backend can be adopted.');
    await this.context.globalState.update(`backend:${d.sha256}`, python);
    const changed: string[] = [];
    for (const override of this.pendingOverrides()) {
      await vscode.workspace.getConfiguration('glitnir', override.folder).update('pythonPath', python, override.target);
      changed.push(override.file);
    }
    this.output.appendLine(`Glitnir backend for this build selected: ${python}`);
    return changed;
  }

  private async discovery(uri: vscode.Uri) {
    const folder = vscode.workspace.getWorkspaceFolder(uri);
    const cwd = folder?.uri.fsPath ?? path.dirname(uri.fsPath);
    const key = `environment:${folder?.uri.toString() ?? cwd}`;
    const config = vscode.workspace.getConfiguration('glitnir', uri);
    const pinned = this.context.extension?.packageJSON.glitnirBackend?.sha256;
    const sharedPython = this.context.globalState?.get<string>(`backend:${pinned}`);
    let remembered = this.context.workspaceState.get<Environment>(key);
    // Once this build's own backend is set up, a remembered backend of an earlier build gives way to it.
    const rememberedSha = managedBackendSha(remembered?.python);
    if (sharedPython && rememberedSha && rememberedSha !== pinned) remembered = undefined;
    const input = {
      cwd, notebook: uri.fsPath,
      roots: (vscode.workspace.workspaceFolders ?? []).map(f => f.uri.fsPath),
      pythonOverride: config.get<string>('pythonPath', ''),
      remembered,
      selectedPython: await selectedPython(uri),
      sharedPython,
      cancelled: () => this.controller.signal.aborted,
      log: (text: string) => this.output.append(text),
    };
    return {folder, key, config, input};
  }

  /** The environment previews would use, without offering setup when there is none. */
  async discovered(uri: vscode.Uri): Promise<Environment | undefined> {
    this.check();
    const {input} = await this.discovery(uri);
    this.check();
    try { return await discover(input, python => this.probe(python)); }
    catch (error) {
      this.check();
      if (error instanceof DiscoveryError) return undefined;
      throw error;
    }
  }

  async resolve(uri: vscode.Uri, configure = false): Promise<Environment | undefined> {
    this.check();
    const {folder, key, config, input} = await this.discovery(uri);
    this.check();
    let installRequested = false, source: Source | undefined;
    const canInstall = !!this.context.extension?.packageJSON.glitnirBackend;
    const setUpDetail = 'Download this build\'s verified backend and install it with uv, using installed Python 3.12+. Python and Lean are not downloaded; existing environments are kept.';
    const remember = async (env: Environment, replaceOverride: boolean): Promise<Environment> => {
      this.check();
      // Only user-invoked setup may replace an explicit override, and only here.
      if (replaceOverride && input.pythonOverride) {
        if (!folder) throw new Error('Open a workspace folder to replace explicit environment settings.');
        await config.update('pythonPath', env.python, vscode.ConfigurationTarget.WorkspaceFolder);
      }
      this.check();
      if (installRequested) await this.context.globalState.update(`backend:${backendDescriptor(this.context.extension?.packageJSON.glitnirBackend).sha256}`, env.python);
      await this.context.workspaceState.update(key, env);
      this.output.appendLine(`Glitnir environment selected: ${env.python}`);
      void vscode.window.showInformationMessage('Glitnir environment is ready. Close existing preview tabs, then open them again to use it.');
      return env;
    };
    let detected: Environment | undefined;
    try {
      detected = await discover(input, python => this.probe(python));
      this.output.appendLine(`Glitnir environment: ${detected.python} (check saved files explicitly)`);
    } catch (error) {
      this.check();
      if (!(error instanceof DiscoveryError)) throw error;
      const prepared = canInstall ? await this.upgrade?.prepare() : undefined;
      this.check();
      if (prepared) {
        const version = this.context.extension?.packageJSON.version;
        const choice = await vscode.window.showInformationMessage(`Glitnir was updated to ${version}. Set up its backend${configure ? '' : ' to open this notebook'}.`, {
          modal: true,
          detail: `${error.message}\n\nThe backend at ${prepared.earlier} belongs to an earlier Glitnir build.\n\n${prepared.detail}`,
        }, UPGRADE_ACTION, 'Other Setup Options');
        this.check();
        if (choice === UPGRADE_ACTION) {
          const python = await this.upgrade!.run(prepared);
          return python ? {python} : undefined;
        }
        if (choice !== 'Other Setup Options') return undefined;
      }
      const action = await vscode.window.showInformationMessage(
        'Set up Glitnir to open this notebook',
        {modal: true, detail: `${error.message}\n\n${canInstall ? `${SET_UP} installs this build's verified backend with uv in one step. ` : ''}Source preview needs an installed Python backend only. Explicit project checks also need the supported Lean toolchain and prepared dependencies. ${MORE_OPTIONS} offers a downloaded wheel, an existing environment and the setup guide.`},
        ...(canInstall ? [SET_UP] : ['Choose Existing Environment']), MORE_OPTIONS,
      );
      this.check();
      if (action === SET_UP) source = 'download';
      else if (action === 'Choose Existing Environment') source = 'existing';
      else if (action === MORE_OPTIONS) source = await this.moreOptions(canInstall);
      if (!source) return undefined;
    }
    this.check();
    if (detected) {
      if (!configure) return detected;
      const choice = await vscode.window.showQuickPick([
        ...(canInstall ? [{label: `${SET_UP} (recommended)`, detail: setUpDetail, action: 'download' as const}] : []),
        {label: 'Use detected environment', detail: `Python: ${detected.python}`, action: 'detected' as const},
        {label: 'Install Extension Package…', detail: 'Add an optional provider package, such as glitnir-forseti, to this backend.', action: 'package' as const},
        {label: MORE_OPTIONS, detail: 'Choose a downloaded backend wheel or another installation, or read the setup guide.', action: 'more' as const},
      ], {title: 'Set Up Glitnir', placeHolder: 'A compatible environment is already installed.'});
      this.check();
      if (!choice) return undefined;
      if (choice.action === 'detected') return remember(detected, false);
      if (choice.action === 'package') {
        await vscode.commands.executeCommand('glitnir.installExtensionPackage', uri);
        return undefined;
      }
      source = choice.action === 'more' ? await this.moreOptions(canInstall) : choice.action;
      if (!source) return undefined;
    }
    this.check();
    installRequested = source !== 'existing';
    let executable: string | undefined;
    if (installRequested) executable = await this.installWithUv(source === 'wheel');
    else {
      const pythonFile = await vscode.window.showOpenDialog({title: 'Select Python with the Glitnir backend installed', canSelectMany: false, canSelectFiles: true, canSelectFolders: false, openLabel: 'Use Python'});
      executable = pythonFile?.[0].fsPath;
    }
    if (!executable) return undefined;
    this.check();
    const python = await this.probe(executable);
    return remember({python: python.python}, true);
  }

  /** The less common choices, and the setup guide; undefined when dismissed or the guide was opened. */
  private async moreOptions(canInstall: boolean): Promise<Source | undefined> {
    const choice = await vscode.window.showQuickPick([
      ...(canInstall ? [{label: 'Choose downloaded backend wheel', detail: 'Select the matching .whl file, for development builds or offline use. It must match this build\'s pinned checksum.', source: 'wheel' as const}] : []),
      {label: 'Choose Existing Environment', detail: 'Select a Python that already has the matching Glitnir backend installed.', source: 'existing' as const},
      {label: 'Read setup guide', detail: 'Prerequisites, manual installation and troubleshooting.', source: 'guide' as const},
    ], {title: 'Set Up Glitnir: more options'});
    this.check();
    if (choice?.source === 'guide') { await openSetupGuide(this.context); return undefined; }
    return choice?.source;
  }
}
