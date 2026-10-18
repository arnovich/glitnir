# Glitnir

Until this repository is public, commit directly to `main`; the history is squashed
into one commit before the first public release. Once it is public, work on feature
branches and merge through pull requests. Release candidates are created only when
the `RELEASE_CANDIDATES` repository variable is `true`.

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
the `evidence` and `decisions` sections of `<Notebook>.glitnir.json` are interfaces: change them only deliberately, never as renaming or branding.

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
only Python, outside the sandbox, reads or writes it. The kernel never checks
against the environment `importModules` returns: it carries the project's IR,
which the kernel's interpreter runs for `Lean.reduceBool`/`reduceNat`, and sets
`quotInit` for any import list. Every kernel environment starts from
`mkEmptyEnvironment`; the cached path rebuilds the imports there unchecked, and
only after the same program verified that rebuild equals its full replay. An
incremental record (`imports-incremental`) kernel-replays only the modules a
closure adds to a recorded, `extendable` base with identical module digests, and
is recorded only after its own rebuild equals that replay; keep that argument
(documented in `ProjectReplay.lean`) intact when changing either side.
Statuses come only from the axioms `ProjectReplay.lean` collects in the
environment it kernel-checked; `ProjectReport.lean`'s axioms are display and a
cross-check that must equal them (a mismatch fails the check), and it refuses to
run if `collectAxioms`' imported summaries (`exportedAxiomsExt`) are loaded.
The claim uses it reports, like the reader's printed statements, are display only.
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
`GLITNIR_TEST_PROOF_TRACE=1` are set with Lean 4.32.1 and a working sandbox; CI
sets both. They are the `test_live_` tests, which the backend's conftest marks
`live` (a test using `project_toolchain` must have that name). CI's `extension`
job runs `-m "not live"` with everything else; the parallel `live` job runs
`-m live`, where a skipped live test fails. Both install the wheels the `build`
job made, and a candidate needs both jobs. Replay-cache tests that only need a
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
   existing draft without rebuilding or changing its tag or assets.
5. Verify anonymous discovery, matching-backend download and fresh installation.
   Confirm the private vulnerability-reporting channel advertised in `SECURITY.md`.

PRs never upload release artifacts. `main` artifacts expire after seven days;
release assets are kept independently. Retrying a build reuses its original wheels
and candidate. Conflicting bytes, moved tags, an expired original artifact or
edited release notes fail rather than overwrite. Uploads reject published
versions. Additional integration evidence must identify its tested source and
inputs and never replaces validation of the published package. Marketplace
publication and automatic updates are separate follow-ups.
