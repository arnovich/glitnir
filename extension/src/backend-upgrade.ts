import {lstat, mkdtemp, readdir, readFile, rm, stat} from 'node:fs/promises';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import {BackendDescriptor, backendDescriptor, backendStore} from './backend-install';
import {InstalledPackage, ProviderReport, VerifiedInstall} from './extension-packages';
import {knownPackage} from './known-packages';
import {fetchPlugin, fetchRelease, normalizedName, PluginPin, pluginPins, ReleaseFetch, ReleasePlugin} from './release-assets';
import {githubToken, openSetupGuide, Override, PreparedUpgrade} from './setup';

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

export interface CarryItem {
  plugin: ReleasePlugin;
  installed: InstalledPackage;
  /** Installed extras the registry allows for this package; only these are installed. */
  extras: string[];
  /** Installed extras the registry does not allow, which are not carried over. */
  dropped: string[];
}
export interface CarryPlan {
  /** Plugin wheels this VSIX pins, to download from the release and install. */
  fetch: CarryItem[];
  /** Packages this VSIX pins no wheel for; they must be reinstalled by hand. */
  manual: InstalledPackage[];
  /** Already installed in the new environment at the pinned version with the same extras. */
  present: InstalledPackage[];
}
/**
 * Which of the earlier environment's provider packages this VSIX pins a wheel
 * for, keeping only the installed extras the known-package registry allows.
 */
export function planCarryOver(previous: InstalledPackage[], plugins: ReleasePlugin[], current: InstalledPackage[] = []): CarryPlan {
  const plan: CarryPlan = {fetch: [], manual: [], present: []};
  for (const installed of previous) {
    const plugin = plugins.find(p => p.name === normalizedName(installed.name));
    if (!plugin) { plan.manual.push(installed); continue; }
    const allowed = new Set(knownPackage(plugin.name)?.extras.map(extra => extra.name) ?? []);
    const extras = (installed.extras ?? []).filter(extra => allowed.has(extra));
    const dropped = (installed.extras ?? []).filter(extra => !allowed.has(extra));
    const done = current.find(p => normalizedName(p.name) === plugin.name && p.version === plugin.version && extras.every(extra => p.extras?.includes(extra)));
    if (done) plan.present.push(done);
    else plan.fetch.push({plugin, installed, extras, dropped});
  }
  return plan;
}

interface Setup {
  installPinned(d: BackendDescriptor, uv: string, signal: AbortSignal, report: (message: string) => void): Promise<string>;
  /** The settings files whose glitnir.pythonPath switching would rewrite. */
  pendingOverrides(): Override[];
  adopt(python: string, planned: Override[]): Promise<{changed: string[]; skipped: string[]}>;
  /** The installed uv, after offering to install it; undefined when the user declined. */
  requireUv(): Promise<string | undefined>;
}
interface Packages {
  packages(python: string, signal: AbortSignal): Promise<ProviderReport>;
  installVerified(python: string, fileName: string, bytes: Buffer, extras: string[], signal: AbortSignal, report: (message: string) => void): Promise<VerifiedInstall>;
}
export interface UpgradeDependencies {
  store?: () => string;
  /** Replaces release downloads, for tests. */
  download?: ReleaseFetch['download'];
}

/** The globalState key holding the extension version the update offer was last shown for. */
export const OFFERED_KEY = 'backendUpdateOffered';
export const SET_UP = 'Set Up';

/** The user dismissed a prerequisite prompt; nothing was started. */
class Dismissed extends Error {}

/** What Set Up will do, worked out before the click from local state and this VSIX's pins. */
interface Plan extends PreparedUpgrade {
  previous?: ManagedBackend;
  /** The earlier environment's provider packages, or undefined when they could not be listed. */
  packages: InstalledPackage[];
  unreadable?: string;
  carry: CarryPlan;
  /** The glitnir.pythonPath settings shown in the confirmation; only these are rewritten. */
  overrides: Override[];
}

interface Outcome {python: string; earlier?: string; carried: string[]; dropped: string[]; manual: InstalledPackage[]; unreadable?: string; settings: string[]; skipped: string[]}

/**
 * Set up this build's backend after an extension update: download the pinned
 * wheel into a new environment as Set Up Environment does, carry over the
 * earlier environment's extension packages whose wheels this VSIX pins, and
 * switch to it only once all of that succeeded. Before anything runs, a
 * confirmation lists what will be downloaded, installed and changed. The
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
  private plugins(): PluginPin[] { return pluginPins(this.context.extension?.packageJSON.glitnirPlugins); }

  /** The upgrade from an earlier build's environment, when this build's backend has not been selected yet. */
  async prepare(): Promise<Plan | undefined> {
    const d = this.pin();
    if (!d?.tag || !this.supported() || this.context.globalState.get(`backend:${d.sha256}`)) return undefined;
    const plan = await this.plan(d);
    return plan.previous ? plan : undefined;
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

  /**
   * Run the upgrade (joining one already running). Without a `prepared` plan
   * the user already confirmed, it first shows what it will do and runs only
   * on Set Up. On failure it offers Retry. Resolves to the new Python, if it finished.
   */
  run(prepared?: PreparedUpgrade): Promise<string | undefined> {
    this.running ??= this.attempts(prepared as Plan | undefined).finally(() => { this.running = undefined; });
    return this.running;
  }

  /** Work out the plan: list the earlier environment's packages (a local, read-only report) against this VSIX's pins. */
  private async plan(d: BackendDescriptor): Promise<Plan> {
    const state = await upgradeState(this.store(), d.sha256);
    const plugins = this.plugins().map(p => ({name: p.distribution, asset: p.asset, version: this.version, size: p.size, sha256: p.sha256}));
    let packages: InstalledPackage[] = [], unreadable: string | undefined;
    if (state.previous) {
      try { packages = (await this.packages.packages(state.previous.python, this.controller.signal)).packages; }
      catch (error) {
        this.controller.signal.throwIfAborted();
        unreadable = (error as Error).message;
        this.output.appendLine(`Could not list the extension packages of ${state.previous.directory}: ${unreadable}`);
      }
    }
    const carry = planCarryOver(packages, plugins);
    const directory = path.join(this.store(), d.sha256);
    const overrides = this.setup.pendingOverrides();
    const pypi = (item: CarryItem): string => {
      const extras = item.extras.flatMap(name => knownPackage(item.plugin.name)?.extras.find(extra => extra.name === name)?.packages ?? []);
      return `${extras.length ? `${extras.join(', ')} and its` : 'Its'} own dependencies come from PyPI as prebuilt wheels`;
    };
    const lines = [
      `Downloads ${d.asset} from GitHub release ${d.tag}, checks it against the SHA-256 this extension records, and installs it with uv into this version's environment (created if needed), ${directory}.`,
      ...carry.fetch.map(item => `Installs ${describe(item.plugin.name, item.plugin.version, item.extras)} from ${item.plugin.asset} in the same release, checked against the SHA-256 this extension records for it. ${pypi(item)}.`),
      ...(carry.fetch.some(item => item.dropped.length) ? [`Extras not carried over (Glitnir does not install them): ${carry.fetch.flatMap(item => item.dropped.map(extra => `${item.plugin.name}[${extra}]`)).join(', ')}.`] : []),
      ...(carry.manual.length ? [`Not in this release, reinstall yourself with Glitnir: Install Extension Package…: ${carry.manual.map(p => `${p.name} ${p.version}`).join(', ')}.`] : []),
      ...(unreadable ? ['The earlier environment\'s extension packages could not be listed; reinstall them yourself afterwards.'] : []),
      ...overrides.map(override => `Changes glitnir.pythonPath in ${override.file} to the new environment.`),
      ...(state.previous ? [`The earlier environment at ${state.previous.directory} is kept.`] : []),
    ];
    return {earlier: state.previous?.directory, previous: state.previous, packages, unreadable, carry, overrides, detail: lines.join('\n')};
  }

  private async attempts(prepared?: Plan): Promise<string | undefined> {
    let plan = prepared;
    if (!plan) {
      if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before setting up the Glitnir backend.');
      const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
      if (!d.tag) throw new Error('This Glitnir build names no release to download its backend from. Run Glitnir: Set Up Environment and choose a downloaded backend wheel.');
      plan = await this.plan(d);
      const choice = await vscode.window.showInformationMessage(`Set up the Glitnir ${this.version} backend?`, {modal: true, detail: plan.detail}, SET_UP);
      if (choice !== SET_UP || this.controller.signal.aborted) return undefined;
    }
    for (;;) {
      const progress = new AbortController();
      try { return this.announce(await this.upgrade(plan, progress)); }
      catch (error) {
        if (this.controller.signal.aborted || error instanceof Dismissed) return undefined;
        const failure = progress.signal.aborted ? 'was cancelled' : `did not finish: ${(error as Error).message}`;
        const message = `Setting up the backend for Glitnir ${this.version} ${failure}`;
        this.output.appendLine(message);
        const choice = await vscode.window.showErrorMessage(`${message} The earlier environment is unchanged and nothing was switched.`, 'Retry', 'Read Setup Guide');
        if (choice === 'Read Setup Guide') await openSetupGuide(this.context);
        if (choice !== 'Retry' || this.controller.signal.aborted) return undefined;
      }
    }
  }

  private async upgrade(plan: Plan, controller: AbortController): Promise<Outcome> {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before setting up the Glitnir backend.');
    if (!this.supported()) throw new Error('Backend setup supports local macOS and Linux windows.');
    const d = backendDescriptor(this.context.extension?.packageJSON.glitnirBackend);
    if (!d.tag) throw new Error('This Glitnir build names no release to download its backend from. Run Glitnir: Set Up Environment and choose a downloaded backend wheel.');
    const uv = await this.setup.requireUv();
    if (!uv) throw new Dismissed('uv is not installed.');
    return vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: `Setting up the Glitnir ${this.version} backend`, cancellable: true}, async (progress, token) => {
      const stop = (): void => controller.abort();
      this.controller.signal.addEventListener('abort', stop, {once: true});
      const subscription = token.onCancellationRequested(stop);
      if (token.isCancellationRequested || this.controller.signal.aborted) stop();
      const signal = controller.signal, report = (message: string): void => progress.report({message});
      const downloads: ReleaseFetch = {signal, githubToken, download: this.dependencies.download};
      const scratch = await mkdtemp(path.join(os.tmpdir(), 'glitnir-upgrade-'));
      try {
        // The release must bind exactly the pinned wheels before anything from it is installed.
        if (plan.carry.fetch.length) {
          report('Verifying this build\'s release');
          await fetchRelease(d, this.version, this.plugins(), scratch, downloads);
        }
        const python = await this.setup.installPinned(d, uv, signal, report);
        signal.throwIfAborted();
        // Recomputed against the new environment, so a retry skips what an earlier attempt installed.
        const carry = planCarryOver(plan.packages, plan.carry.fetch.map(item => item.plugin),
          plan.carry.fetch.length ? (await this.packages.packages(python, signal)).packages : []);
        const carried = carry.present.map(p => describe(p.name, p.version, p.extras ?? []));
        const dropped = plan.carry.fetch.flatMap(item => item.dropped.map(extra => `${item.plugin.name}[${extra}]`));
        for (const item of carry.fetch) {
          report(`Downloading ${item.plugin.asset}`);
          const bytes = await fetchPlugin(d, item.plugin, scratch, downloads);
          const result = await this.packages.installVerified(python, item.plugin.asset, bytes, item.extras, signal, report);
          carried.push(describe(result.info.name, result.info.version, result.extras));
          dropped.push(...result.dropped.map(extra => `${result.info.name}[${extra}]`));
        }
        signal.throwIfAborted();
        // Switch only now that the environment is complete.
        const {changed: settings, skipped} = await this.setup.adopt(python, plan.overrides);
        return {python, earlier: plan.previous?.directory, carried, dropped, manual: plan.carry.manual, unreadable: plan.unreadable, settings, skipped};
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
      ...(outcome.dropped.length ? [`Extras not carried over: ${outcome.dropped.join(', ')}.`] : []),
      ...(outcome.unreadable ? [`The earlier environment's packages could not be listed (see Output → Glitnir); reinstall them with Glitnir: Install Extension Package….`] : []),
      ...(outcome.settings.length ? [`glitnir.pythonPath now names it in ${outcome.settings.join(', ')}.`] : []),
      ...(outcome.skipped.length ? [`glitnir.pythonPath in ${outcome.skipped.join(', ')} changed after the confirmation and was left alone.`] : []),
      ...(outcome.earlier ? [`The earlier environment at ${outcome.earlier} is kept; remove it yourself once no window uses it (see the setup guide).`] : []),
      'Close existing preview tabs, then open them again to use it.',
    ];
    const message = lines.join(' ');
    this.output.appendLine(message);
    if (outcome.manual.length || outcome.dropped.length || outcome.unreadable || outcome.skipped.length) void vscode.window.showWarningMessage(message);
    else void vscode.window.showInformationMessage(message);
    return outcome.python;
  }
}

const describe = (name: string, version: string, extras: string[]): string => `${name} ${version}${extras.length ? ` [${extras.join(',')}]` : ''}`;
