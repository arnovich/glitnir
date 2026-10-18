import * as path from 'node:path';
import {downloadRuntime, RELEASE_REPOSITORY} from './runtime-download';
import {BackendDescriptor, backendDescriptor, backendStore, findUv, installBackend} from './backend-install';
import * as vscode from 'vscode';
import {discover, DiscoveryError, Environment, ProbeResult, probePython} from './discovery';

/** Release downloads are anonymous; GitHub sign-in is asked for only if GitHub refuses them. */
export async function githubToken(): Promise<string> {
  const session = await vscode.authentication.getSession('github', ['repo'], {createIfNone: true});
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

/** Offered when no backend is found but an earlier build's managed backend exists. */
export interface UpgradeOffer {
  /** The earlier build's environment directory, or undefined when there is nothing to upgrade from. */
  available(): Promise<string | undefined>;
  /** Run the upgrade; resolves to the new Python, or undefined when it did not finish. */
  run(): Promise<string | undefined>;
}

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
  constructor(private context: vscode.ExtensionContext, private output: vscode.OutputChannel) {}

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

  /** An explicit setup action installs only the wheel pinned by this VSIX. */
  private async installWithUv(): Promise<string | undefined> {
    this.check();
    if (vscode.env.remoteName || !['darwin', 'linux'].includes(process.platform)) throw new Error('Backend setup supports local macOS and Linux windows.');
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    const uv = await findUv();
    const source = await vscode.window.showQuickPick([
      ...(d.tag ? [{label: 'Download matching backend', download: true, detail: 'Download the verified backend from this Glitnir release.'}] : []),
      {label: 'Choose downloaded backend wheel', download: false, detail: 'Select the matching .whl file. The wheel has no runtime dependencies.'},
    ], {title: 'Install Backend with uv'});
    if (!source) return undefined;
    let wheel: string | undefined;
    if (!source.download) {
      const files = await vscode.window.showOpenDialog({title: 'Select matching Glitnir backend wheel', canSelectMany: false, canSelectFiles: true, canSelectFolders: false, filters: {'Python wheel': ['whl']}});
      if (!files?.[0]) return undefined;
      if (files[0].scheme !== 'file') throw new Error('Select a local backend wheel.');
      wheel = files[0].fsPath;
    }
    const accepted = await vscode.window.showInformationMessage('Install Glitnir backend with uv?', {
      modal: true, detail: `Use installed Python 3.12+ and install the backend in ${path.join(backendStore(), d.sha256)}. Python and Lean will not be downloaded. Existing environments are kept.`,
    }, 'Install Backend');
    if (accepted !== 'Install Backend') return undefined;
    this.check();
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
   * Switch every window to this build's complete managed backend at `python`:
   * share it, and repoint `glitnir.pythonPath` values that name an earlier
   * build's managed backend. Other overrides are the user's own choice and are
   * kept. Returns the settings scopes that changed.
   */
  async adopt(python: string): Promise<string[]> {
    this.check();
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    if (managedBackendSha(python) !== d.sha256) throw new Error('Only this build\'s managed backend can be adopted.');
    await this.context.globalState.update(`backend:${d.sha256}`, python);
    const changed: string[] = [];
    const earlier = (value: string | undefined): boolean => { const sha = managedBackendSha(value); return !!sha && sha !== d.sha256; };
    const top = vscode.workspace.getConfiguration('glitnir').inspect<string>('pythonPath');
    for (const [value, target, label] of [[top?.globalValue, vscode.ConfigurationTarget.Global, 'user settings'], [top?.workspaceValue, vscode.ConfigurationTarget.Workspace, 'workspace settings']] as const) {
      if (!earlier(value)) continue;
      await vscode.workspace.getConfiguration('glitnir').update('pythonPath', python, target);
      changed.push(label);
    }
    for (const folder of vscode.workspace.workspaceFolders ?? []) {
      const config = vscode.workspace.getConfiguration('glitnir', folder.uri);
      if (!earlier(config.inspect<string>('pythonPath')?.workspaceFolderValue)) continue;
      await config.update('pythonPath', python, vscode.ConfigurationTarget.WorkspaceFolder);
      changed.push(`${folder.name} folder settings`);
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
    let installRequested = false;
    const canInstall = !!this.context.extension?.packageJSON.glitnirBackend;
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
      const earlier = canInstall && !configure ? await this.upgrade?.available() : undefined;
      this.check();
      if (earlier) {
        const version = this.context.extension?.packageJSON.version;
        const choice = await vscode.window.showInformationMessage(`Glitnir was updated to ${version}. Set up its backend to open this notebook.`, {
          modal: true,
          detail: `${error.message}\n\nThe backend at ${earlier} belongs to an earlier Glitnir build. Set Up downloads this build's verified backend into a new environment and carries over the extension packages from this release; the earlier environment is kept.`,
        }, UPGRADE_ACTION, 'Other Setup Options');
        this.check();
        if (choice === UPGRADE_ACTION) {
          const python = await this.upgrade!.run();
          return python ? {python} : undefined;
        }
        if (choice !== 'Other Setup Options') return undefined;
      }
      const action = await vscode.window.showInformationMessage(
        'Set up Glitnir to open this notebook',
        {modal: true, detail: `${error.message}\n\nSource preview needs an installed Python backend only. Explicit project checks also need the supported Lean toolchain and prepared dependencies. You can choose existing installations or follow the installation guide.`},
        ...(canInstall ? ['Install Backend with uv'] : []), 'Choose Existing Environment', 'Read Installation Guide',
      );
      this.check();
      if (action === 'Read Installation Guide') {
        await vscode.commands.executeCommand('markdown.showPreview', vscode.Uri.joinPath(this.context.extensionUri, 'dist', 'media', 'setup.md'));
        return undefined;
      }
      installRequested = action === 'Install Backend with uv';
      if (!installRequested && action !== 'Choose Existing Environment') return undefined;
    }
    this.check();
    if (detected) {
      if (!configure) return detected;
      const choice = await vscode.window.showQuickPick([
        {label: 'Use detected environment', detail: `Python: ${detected.python}`, detected: true},
        ...(canInstall ? [{label: 'Install Backend with uv', detail: 'Set up the matching backend using installed Python.', detected: false, install: true}] : []),
        {label: 'Choose another installation', detail: 'Select an installed Python backend.', detected: false},
        {label: 'Install Extension Package…', detail: 'Add an optional provider package, such as glitnir-forseti, to this backend.', detected: false, extensionPackage: true},
      ], {title: 'Set Up Glitnir', placeHolder: 'A compatible environment is already installed.'});
      this.check();
      if (!choice) return undefined;
      if (choice.detected) return remember(detected, false);
      if ('extensionPackage' in choice && choice.extensionPackage) {
        await vscode.commands.executeCommand('glitnir.installExtensionPackage', uri);
        return undefined;
      }
      installRequested = 'install' in choice && choice.install === true;
    }
    this.check();
    let executable: string | undefined;
    if (installRequested) executable = await this.installWithUv();
    else {
      const pythonFile = await vscode.window.showOpenDialog({title: 'Select Python with the Glitnir backend installed', canSelectMany: false, canSelectFiles: true, canSelectFolders: false, openLabel: 'Use Python'});
      executable = pythonFile?.[0].fsPath;
    }
    if (!executable) return undefined;
    this.check();
    const python = await this.probe(executable);
    return remember({python: python.python}, true);
  }
}
