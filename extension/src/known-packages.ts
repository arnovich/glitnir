/**
 * Extension packages Glitnir knows about, offered first by Install Extension
 * Package…. This is data only and adds no trust: every wheel still goes
 * through the same inspection, and one downloaded from a release is also
 * verified against that release's SHA256SUMS and glitnir-candidate.json.
 */
export interface KnownExtra {
  /** The extra's normalized name, installed only if the wheel offers it (the extras allowlist). */
  name: string;
  /** The yes/no question asked before installing it. */
  prompt: string;
}
export interface KnownPackage {
  label: string;
  description: string;
  /** The normalized distribution name; a selected wheel must have it. */
  distribution: string;
  /** The release asset of this package for a Glitnir version. */
  asset(version: string): string;
  extras: KnownExtra[];
  /** Shown before installing: what the package cannot get from here. */
  notes: string[];
}

export const KNOWN_PACKAGES: readonly KnownPackage[] = [{
  label: 'Forseti',
  description: 'circuit, model and dynamics diagrams, simulation evidence and decision procedures',
  distribution: 'glitnir-forseti',
  asset: version => `glitnir_forseti-${version}-py3-none-any.whl`,
  extras: [{name: 'z3', prompt: 'Install z3 for decision procedures? (adds the z3-solver package from PyPI)'}],
  notes: ['iterate evidence also needs Python Asgard (gimle-asgard), which is not on PyPI and cannot be installed here; install it into the backend yourself.'],
}];
