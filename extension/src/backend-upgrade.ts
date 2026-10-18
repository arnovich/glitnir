import {lstat, mkdtemp, readdir, readFile, rm, stat} from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {BackendDescriptor, backendDescriptor, backendStore, findUv} from './backend-install';
import {InstalledPackage, ProviderReport, readWheel, VerifiedInstall} from './extension-packages';
import {fetchPlugin, fetchRelease, normalizedName, ReleaseFetch, ReleasePlugin, VerifiedRelease} from './release-assets';
import {githubToken} from './setup';

/** A complete managed backend environment: its directory is named by the wheel digest its marker records. */
export interface ManagedBackend {sha256: string; directory: string; python: string; created: number}

/** The complete managed backends in `store`, newest first. Incomplete or unrecognized directories are ignored. */
export async function managedBackends(store: string): Promise<ManagedBackend[]> {
  let names: string[];
  try { names = await readdir(store); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []; throw error; }
  const found: ManagedBackend[] = [];
  for (const name of names) {
    if (!/^[a-f0-9]{64}$/.test(name)) continue;
    const directory = path.join(store, name), marker = path.join(directory, '.glitnir-backend'), python = path.join(directory, 'bin/python');
    try {
      const info = await lstat(directory);
      if (!info.isDirectory() || info.isSymbolicLink() || await readFile(marker, 'utf8') !== name) continue;
      await stat(python);
      found.push({sha256: name, directory, python, created: (await stat(marker)).mtimeMs});
    } catch { /* Not a complete backend. */ }
  }
  return found.sort((a, b) => b.created - a.created);
}

/** This build's managed backend, if complete, and the newest one of another build. */
export interface UpgradeState {current?: ManagedBackend; previous?: ManagedBackend}
export async function upgradeState(store: string, sha256: string): Promise<UpgradeState> {
  const all = await managedBackends(store);
  return {current: all.find(b => b.sha256 === sha256), previous: all.find(b => b.sha256 !== sha256)};
}

export interface OfferInput {
  /** `glitnir.offerBackendUpdate`. */
  enabled: boolean;
  /** The extension version the offer was last shown for. */
  offered?: string;
  version: string;
  /** A local macOS or Linux window with a release-tagged backend pin. */
  supported: boolean;
  state: UpgradeState;
}
/** Offer once per extension version, only when an earlier build's backend exists and this build's does not. */
export function shouldOffer(input: OfferInput): boolean {
  return input.enabled && input.supported && input.offered !== input.version && !input.state.current && !!input.state.previous;
}

export interface CarryItem {plugin: ReleasePlugin; installed: InstalledPackage; extras: string[]}
export interface CarryPlan {
  /** Release assets to download and install. */
  fetch: CarryItem[];
  /** Packages that are not assets of this release; they must be reinstalled by hand. */
  manual: InstalledPackage[];
  /** Already installed in the new environment at the release version with the same extras. */
  present: InstalledPackage[];
}
/** Which of the earlier environment's provider packages come from the release, keeping their extras. */
export function planCarryOver(previous: InstalledPackage[], plugins: ReleasePlugin[], current: InstalledPackage[] = []): CarryPlan {
  const plan: CarryPlan = {fetch: [], manual: [], present: []};
  for (const installed of previous) {
    const plugin = plugins.find(p => p.name === normalizedName(installed.name));
    if (!plugin) { plan.manual.push(installed); continue; }
    const extras = installed.extras ?? [];
    const done = current.find(p => normalizedName(p.name) === plugin.name && p.version === plugin.version && extras.every(extra => p.extras?.includes(extra)));
    if (done) plan.present.push(done);
    else plan.fetch.push({plugin, installed, extras});
  }
  return plan;
}

interface Setup {
  installPinned(d: BackendDescriptor, uv: string, signal: AbortSignal, report: (message: string) => void): Promise<string>;
  adopt(python: string): Promise<string[]>;
}
interface Packages {
  packages(python: string, signal: AbortSignal): Promise<ProviderReport>;
  installVerified(python: string, fileName: string, bytes: Buffer, extras: string[], signal: AbortSignal, report: (message: string) => void): Promise<VerifiedInstall>;
}
export interface UpgradeDependencies {
  store?: () => string;
  findUv?: () => Promise<string>;
  /** Replaces release downloads, for tests. */
  download?: ReleaseFetch['download'];
}

/** The globalState key holding the extension version the update offer was last shown for. */
export const OFFERED_KEY = 'backendUpdateOffered';
export const SET_UP = 'Set Up';

interface Outcome {python: string; earlier?: string; carried: string[]; dropped: string[]; manual: InstalledPackage[]; unreadable?: string; settings: string[]}

/**
 * Set up this build's backend after an extension update: download the pinned
 * wheel into a new environment as Set Up Environment does, carry over the
 * extension packages of the earlier environment that are assets of this
 * build's release, and switch to it only once all of that succeeded. The
 * earlier environment is only read, never changed or removed.
 */
export class BackendUpgrade {
  private controller = new AbortController();
  private running?: Promise<string | undefined>;
  constructor(private context: vscode.ExtensionContext, private output: vscode.OutputChannel,
    private setup: Setup, private packages: Packages, private dependencies: UpgradeDependencies = {}) {}

  dispose(): void { this.controller.abort(); }

  private get version(): string { return this.context.extension?.packageJSON.version; }
  private store(): string { return (this.dependencies.store ?? backendStore)(); }
  private supported(): boolean { return !vscode.env.remoteName && ['darwin', 'linux'].includes(process.platform); }
  private pin(): BackendDescriptor | undefined {
    try { return backendDescriptor(this.context.extension?.packageJSON.glitnirBackend); } catch { return undefined; }
  }

  /** The earlier build's environment to upgrade from, when this build's backend has not been selected yet. */
  async available(): Promise<string | undefined> {
    const d = this.pin();
    if (!d?.tag || !this.supported() || this.context.globalState.get(`backend:${d.sha256}`)) return undefined;
    return (await upgradeState(this.store(), d.sha256)).previous?.directory;
  }

  /** At startup: offer the upgrade once per extension version. Nothing is downloaded without the click. */
  async offer(): Promise<void> {
    const d = this.pin();
    if (!d?.tag) return;
    const enabled = vscode.workspace.getConfiguration('glitnir').get<boolean>('offerBackendUpdate', true) !== false;
    const offered = this.context.globalState.get<string>(OFFERED_KEY);
    if (!enabled || offered === this.version) return;
    const state = await upgradeState(this.store(), d.sha256);
    if (!shouldOffer({enabled, offered, version: this.version, supported: this.supported() && vscode.workspace.isTrusted, state})) return;
    await this.context.globalState.update(OFFERED_KEY, this.version);
    const choice = await vscode.window.showInformationMessage(`Glitnir was updated to ${this.version}. Set up its backend?`, SET_UP);
    if (choice === SET_UP && !this.controller.signal.aborted) await this.run();
  }

  /** Run the upgrade (joining one already running); on failure offer Retry. Resolves to the new Python, if it finished. */
  run(): Promise<string | undefined> {
    this.running ??= this.attempts().finally(() => { this.running = undefined; });
    return this.running;
  }

  private async attempts(): Promise<string | undefined> {
    for (;;) {
      const progress = new AbortController();
      try { return this.announce(await this.upgrade(progress)); }
      catch (error) {
        if (this.controller.signal.aborted) return undefined;
        const failure = progress.signal.aborted ? 'was cancelled' : `did not finish: ${(error as Error).message}`;
        const message = `Setting up the backend for Glitnir ${this.version} ${failure}`;
        this.output.appendLine(message);
        const choice = await vscode.window.showErrorMessage(`${message} The earlier environment is unchanged and nothing was switched.`, 'Retry');
        if (choice !== 'Retry' || this.controller.signal.aborted) return undefined;
      }
    }
  }

  private async upgrade(controller: AbortController): Promise<Outcome> {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before setting up the Glitnir backend.');
    if (!this.supported()) throw new Error('Backend setup supports local macOS and Linux windows.');
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    if (!d.tag) throw new Error('This Glitnir build names no release to download its backend from. Run Glitnir: Set Up Environment and choose a downloaded backend wheel.');
    const uv = await (this.dependencies.findUv ?? findUv)();
    const state = await upgradeState(this.store(), d.sha256);
    return vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: `Setting up the Glitnir ${this.version} backend`, cancellable: true}, async (progress, token) => {
      const stop = (): void => controller.abort();
      this.controller.signal.addEventListener('abort', stop, {once: true});
      const subscription = token.onCancellationRequested(stop);
      if (token.isCancellationRequested || this.controller.signal.aborted) stop();
      const signal = controller.signal, report = (message: string): void => progress.report({message});
      const downloads: ReleaseFetch = {signal, githubToken, download: this.dependencies.download};
      const scratch = await mkdtemp(path.join(os.tmpdir(), 'glitnir-upgrade-'));
      try {
        let previous: InstalledPackage[] = [], unreadable: string | undefined;
        if (state.previous) {
          report('Reading the earlier environment\'s extension packages');
          try { previous = (await this.packages.packages(state.previous.python, signal)).packages; }
          catch (error) {
            signal.throwIfAborted();
            unreadable = (error as Error).message;
            this.output.appendLine(`Could not list the extension packages of ${state.previous.directory}: ${unreadable}`);
          }
        }
        // The release must match this build before anything from it is installed.
        let release: VerifiedRelease | undefined;
        if (previous.length) {
          report('Verifying this build\'s release');
          release = await fetchRelease(d, this.version, scratch, downloads);
        }
        const python = await this.setup.installPinned(d, uv, signal, report);
        signal.throwIfAborted();
        const plan = planCarryOver(previous, release?.plugins ?? [], previous.length ? (await this.packages.packages(python, signal)).packages : []);
        const carried = plan.present.map(p => describe(p.name, p.version, p.extras ?? [])), dropped: string[] = [];
        for (const item of plan.fetch) {
          report(`Downloading ${item.plugin.asset}`);
          const file = await fetchPlugin(d, item.plugin, scratch, downloads);
          const result = await this.packages.installVerified(python, item.plugin.asset, await readWheel(file), item.extras, signal, report);
          carried.push(describe(result.info.name, result.info.version, result.extras));
          dropped.push(...result.dropped.map(extra => `${result.info.name}[${extra}]`));
        }
        signal.throwIfAborted();
        // Switch only now that the environment is complete.
        const settings = await this.setup.adopt(python);
        return {python, earlier: state.previous?.directory, carried, dropped, manual: plan.manual, unreadable, settings};
      } finally {
        subscription.dispose(); this.controller.signal.removeEventListener('abort', stop);
        await rm(scratch, {recursive: true, force: true});
      }
    });
  }

  private announce(outcome: Outcome): string {
    const lines = [
      `The Glitnir ${this.version} backend is ready at ${outcome.python}.`,
      ...(outcome.carried.length ? [`Carried over: ${outcome.carried.join(', ')}.`] : []),
      ...(outcome.manual.length ? [`Not in this release, reinstall manually with Glitnir: Install Extension Package…: ${outcome.manual.map(p => `${p.name} ${p.version}`).join(', ')}.`] : []),
      ...(outcome.dropped.length ? [`Extras this release's wheels no longer offer: ${outcome.dropped.join(', ')}.`] : []),
      ...(outcome.unreadable ? [`The earlier environment's packages could not be listed (see Output → Glitnir); reinstall them with Glitnir: Install Extension Package….`] : []),
      ...(outcome.settings.length ? [`glitnir.pythonPath now names it in ${outcome.settings.join(', ')}.`] : []),
      ...(outcome.earlier ? [`The earlier environment at ${outcome.earlier} is kept; remove it yourself once no window uses it (see the README).`] : []),
      'Close existing preview tabs, then open them again to use it.',
    ];
    const message = lines.join(' ');
    this.output.appendLine(message);
    if (outcome.manual.length || outcome.dropped.length || outcome.unreadable) void vscode.window.showWarningMessage(message);
    else void vscode.window.showInformationMessage(message);
    return outcome.python;
  }
}

const describe = (name: string, version: string, extras: string[]): string => `${name} ${version}${extras.length ? ` [${extras.join(',')}]` : ''}`;
