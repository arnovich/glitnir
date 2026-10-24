# Glitnir

Work on feature branches and merge through pull requests; never commit directly
to `main`. Release candidates are created only when the `RELEASE_CANDIDATES`
repository variable is `true`.

This repository owns the VS Code integration and the extracted backend in
`backend/`, including its canonical renderer and Python checking services.
The extension build consumes an installed wheel of that backend. Read
DEVELOPMENT.md before changing or building it. Default universal thin VSIXs
use a separately installed backend and existing Python and Lean.
Do not maintain a second renderer or weaken the release/sandbox checks. Keep
`gimle.glitnir` and the `glitnir.*` command/settings identifiers stable.
The Python package is `glitnir`. Forseti Lean's own modules (`Gimle.Forseti.*`), the
`gimle.forseti.*/vN` data-format identifiers (some are hashed into saved records),
`FORSETI_*` environment variables of the plugin, the core's `GLITNIR_*` environment
variables (such as `GLITNIR_ELABORATION_TIMEOUT_MS` and `GLITNIR_MEMORY_LIMIT_MB`),
the `X-Glitnir` header and shared renderer hooks (`glitnirHost`, `glitnirCircuits`,
`glitnirCards`, `glitnirExport`, `glitnir-reveal-cell`), `POST /api/evidence/run`,
the `glitnir.evidence/v1` format, the `EvidenceProvider`/`EvidenceRequest` and
`DecisionProvider`/`DecisionRequest` interfaces in `glitnir/providers.py`,
`POST /api/decide/run`, `/api/decide/stop` and `/api/decide/discard`,
`GET /api/decide/candidate`, the webview-to-extension `acceptDecision` message and
the `evidence`, `decisions`, `views` and `evidence_failures` sections of `<Notebook>.glitnir.json` are interfaces: change them only deliberately, never as renaming or branding.

Run `npm test`, `npm run package` and the isolated `npm run test:host` when host
behavior changes. Set `GLITNIR_BUILD_PYTHON` to the compatible installed runtime.
CI is Linux only; macOS is verified locally. Verify wheel hashes before installing
release artifacts. Public publication and licensing are separate approval gates.

Run the backend's complete pytest, black, isort, flake8 and mypy checks when
changing Python or its packaging. Keep public CI independent of private repositories. Tests requiring an external
corpus or optional simulation must declare those prerequisites without skipping
independent tests in the same module. Keep the independent Lean checks,
sandbox, exact quantities and source/context bindings intact. The kernel replay
always replays the checked module's own constants; the per-user replay cache
(`replay_cache.py`) only lets it skip replaying imports whose toolchain files,
replay program and every module file digest match a recorded full replay, and
only Python, outside the sandbox, reads or writes it. A module's digest covers
every file `importModules` may read for it (`replay_cache.MODULE_SUFFIXES`,
`.ir.sig` included). The kernel never checks against the environment
`importModules` returns: it carries the project's IR and extension state, and
sets `quotInit` for any import list (through Lean 4.34 the kernel's interpreter
also ran that IR for `Lean.reduceBool`/`reduceNat`; Lean 4.35 removed them, and
its kernel runs no compiled code). Every kernel environment starts from
`mkEmptyEnvironment`; the cached path rebuilds the imports there unchecked, and
only after the same program verified that rebuild equals its full replay. Kernel
checks pass `maxRecDepth` 0, which from Lean 4.35 on means unbounded, as in
Lean's own replay. An
incremental record (`imports-incremental`) kernel-replays only the modules a
closure adds to a recorded, `extendable` base with identical module digests, and
is recorded only after its own rebuild equals that replay; keep that argument
(documented in `ProjectReplay.lean`) intact when changing either side.
Statuses come only from the axioms `ProjectReplay.lean` collects in the
environment it kernel-checked; `ProjectReport.lean`'s axioms are display and a
cross-check that must equal them (a mismatch fails the check), and it refuses to
run if `collectAxioms`' imported summaries (`exportedAxiomsExt`) are loaded.
The claim uses it reports, like the reader's printed statements, are display only.
Glitnir supports exactly one Lean version, `project_inspection.TOOLCHAIN` (today
`leanprover/lean4:v4.35.0`); derive every other version value from it. The helper
programs are edited for that version directly, with no compatibility layers,
and must elaborate on it with no output. Lake's `importArts` are read only
through `glitnir/lean/artifacts.py`.
The environment fingerprint (`fingerprint.py`) decides when a saved result is
current. It holds no location, so saved results are portable across checkouts
and machines when the compiled artifacts are byte-identical: an imported module
counts only by its Lake trace's `schemaVersion`, `depHash` and `outputs`
(`trace_digest`), never by `inputs` captions, `log` or `synthetic`. Never add a
value that names a path, and bump the `glitnir.environment-fingerprint/vN` tag
whenever what it hashes changes.
Simulation is evidence, never proof. The core has one checking mode (project) and no runtime
dependencies; Forseti-specific code lives in `plugins/forseti` (`glitnir-forseti`,
loaded through the `glitnir.providers` entry point). Run its tests, black, isort,
flake8 and mypy too when changing it. The plugin has no notebook checking of
its own and reads or writes no `.forseti.json`. Its deciders' replay against
the pinned Forseti release, and their `python -m` command lines, are not
reachable from the editor; do not describe them as available. Evidence
never changes a claim status or the check exit code. A decision provider only
proposes a Lean block: the project kernel check is the proof authority, and the
backend never writes the `.lean` file. The editor-applied **Accept**
(`acceptDecision`, a `WorkspaceEdit` plus save, refused for a dirty or changed
document) is the only path that writes Lean source.

Live Lean tests skip locally unless `GLITNIR_REQUIRE_PROJECT_INSPECTION=1` and
`GLITNIR_TEST_PROOF_TRACE=1` are set with Lean 4.35.0 and a working sandbox; CI
sets both. They are the `test_live_` tests, which the backend's conftest marks
`live` (a test using `project_toolchain` must have that name). CI's `extension`
job runs `-m "not live"` with everything else; the parallel `live` job runs
`-m live`, where a skipped live test fails. Both install the wheels the `build`
job made, and a candidate needs both jobs, the `helpers` job, which
elaborates every Lean helper program with no output
(`backend/scripts/check_helpers.py`), and every `plugin-live` shard. Those run
the plugin's tests against a build of the Forseti Lean release it pins
(`plugins/forseti/tools/live_shards.py` assigns each test module to exactly one
shard), under
`FORSETI_REQUIRE_LEAN_PROJECT=1`, where a Lean test that skips fails unless the
skip is declared (`skipif`) or for `simulation`; update its tag and commit
check together with `lean-release.v1.json`. Replay-cache tests that only need a
recorded base start from a per-test copy of one recorded once per session
(`recorded_replay_cache`); tests of the cold path use `fresh_replay_cache`.
Plugin tests marked `simulation` need Python Asgard (`gimle.asgard`). The
editor passes a fresh per-process capability in `GLITNIR_SERVER_TOKEN` and the
`X-Glitnir` header; never put it in URLs, arguments, logs or rendered content.

## Releases

Package versions are explicit in `package.json` and `package-lock.json`. A green
`main` build with an unpublished version creates an immutable `vVERSION` tag and
a draft release, for example `v0.1.0`. The CI run ID stays in the source manifest
and build link. Keep the backend download tag in the VSIX bound to that same tag.
Bump the version before preparing a different candidate: a tag cannot move to
another commit, even while its release is a draft. A `main` push with an already
published version runs checks and skips candidate creation.

To validate and publish a candidate:

1. Require passing backend, independent Lean/sandbox, extension, package and
   installed-host checks. Review explicitly skipped optional integrations.
2. Download the candidate VSIX, matching backend wheel and `glitnir-forseti`
   plugin wheel. Verify `SHA256SUMS` and the source, tag, version, backend and
   plugin bindings in `glitnir-candidate.json`. The plugin is released at the
   package version (bump `plugins/forseti/pyproject.toml` with `package.json`),
   and its `glitnir` requirement must admit the released backend.
3. Install those exact assets with a fresh profile on macOS and Linux. Exercise
   uv setup, environment reuse and failure/cancellation recovery.
4. Review release notes and obtain the owner's publication approval. Publish the
   existing draft without rebuilding or changing its tag or assets. This starts
   Marketplace publication of the same VSIX (below), which waits for approval.
5. Verify anonymous discovery, matching-backend download and fresh installation.
   Confirm the private vulnerability-reporting channel advertised in `SECURITY.md`.

PRs never upload release artifacts. `main` artifacts expire after seven days;
release assets are kept independently. Retrying a build reuses its original wheels
and candidate. Conflicting bytes, moved tags, an expired original artifact or
edited release notes fail rather than overwrite. Uploads reject published
versions. Additional integration evidence must identify its tested source and
inputs and never replaces validation of the published package. Automatic
updates are a separate follow-up.

### Marketplace publication

Publishing a GitHub release triggers `.github/workflows/marketplace.yml`, which
publishes that release's VSIX to the VS Code Marketplace as `gimle.glitnir`. It
never builds: it downloads the release's assets and, with
`extension/scripts/marketplace-release.cjs`, runs the candidate verification
(`SHA256SUMS`, the manifest's source, tag, version, backend and plugin bindings)
and checks every byte against the asset digests GitHub reports. It refuses a draft,
a tag that moved off the manifest's commit and a VSIX whose channel differs from
the release. Only then does the `marketplace` environment job run
`vsce publish --packagePath` with the `vsce` locked in `package-lock.json`,
adding `--pre-release` for a GitHub pre-release. A version the Marketplace already
lists on the same channel is reported and skipped, so a retry is safe: run the
workflow manually with the release's `tag`. A version listed on the other channel
fails. A version that is not newer than the latest listed on its own channel is
refused unless a manual run sets `allow_older`. A release event runs the workflow
from the tag's commit; releases made before it existed are published, if at all,
by a manual run. The verification requires the VSIX's plugin pin (`glitnirPlugins`)
to be present and equal to the attached plugin wheel, so releases before 0.1.2
(v0.1.0, v0.1.1), which have no plugin pin, can never be published or retried: a
manual run checks out the default branch, whose verification requires the pin.
This is intended. The workflow installs dependencies with `npm ci --ignore-scripts`,
pins its actions to commit SHAs and uses no dependency cache in the job holding
the token.

The channel is fixed when the VSIX is packaged, and candidates are packaged as
releases, so a GitHub release meant for the Marketplace is published as a full
release: clear the draft's pre-release flag first. A pre-release with such a VSIX
is refused, never repackaged. The listing's `"preview": true` marks it as a preview. The Marketplace accepts each
version once, as either a pre-release or a release, and no semver pre-release
suffixes: a pre-release cannot be promoted, so a release needs a new version.

Before the first publication, the owner enables these repository settings:

1. GitHub immutable releases, so a published release's assets and tag are frozen.
2. A tag ruleset restricting who may create or update `v*` tags.
3. The `marketplace` environment, with required reviewers (Marketplace
   publication is its own approval gate), "prevent self-review" where practical,
   and deployment limited to `v*` tags and `main`.
4. The environment secret `VSCE_PAT`: an Azure DevOps token for all accessible
   organizations with Marketplace (Manage) scope for publisher `Gimle`.
5. With that token in the `VSCE_PAT` environment variable, confirm it with
   `npx @vscode/vsce@4.0.0 verify-pat gimle` (fall back to `Gimle`).

The extension ID stays `gimle.glitnir` (IDs are case-insensitive); keep
`"publisher": "gimle"`. Never pass a token on a command line or print it. Open
VSX is not published. Adding it needs `ovsx` in `devDependencies`, so it is locked
in `package-lock.json`, run with `npx --no` like `vsce`, never an unlocked
`npx --yes`.
