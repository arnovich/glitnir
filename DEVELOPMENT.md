# Developing Glitnir

This guide is for contributors and maintainers. To install and use Glitnir, see
the [README](README.md). Pull requests are limited to invited collaborators; see
[CONTRIBUTING.md](CONTRIBUTING.md). [AGENTS.md](AGENTS.md) holds the repository's
invariants, its stable interfaces and the release steps.

## Repository layout

The repository has four parts:

- [`backend/`](backend/README.md): the Python backend, including the notebook
  renderer and the checking services.
- [`plugins/forseti/`](plugins/forseti/README.md): the optional `glitnir-forseti`
  provider package.
- `extension/`: the VS Code extension, with its source, webview assets,
  build scripts and tests.
- [`skills/`](skills/glitnir-notebooks/SKILL.md): the authoring skill for coding
  agents.

## Architecture

- **One renderer.** The backend's `glitnir/preview/app` is the canonical
  renderer. `npm run compile` exports it from the Python in
  `GLITNIR_BUILD_PYTHON` (`python -m glitnir.preview.editor --export`) into
  `dist/media`, with `dist/editor-runtime.json` recording its identity. At
  runtime the extension's probe refuses a backend whose renderer differs ("The
  installed Glitnir runtime is incompatible with this Glitnir build"), so build
  the extension against the wheel of the same commit. Do not maintain a second
  renderer. The workbench is five scripts, `workbench.js`, `figures.js`,
  `reading.js`, `inspector.js` and `chrome.js`, loaded in that order after
  `export.js`, `circuits.js` and `cards.js`; they share their names through
  `window.glitnirWorkbench`, which is the workbench's own namespace and no
  interface. `index.html`'s tags are the one list of scripts: the webview
  rewrites every tag and inserts its own at `<!-- host scripts -->`, the
  renderer tests load what the page lists, and a gallery must do the same
  rather than name the files.
- **Thin, universal VSIX.** The packaged extension contains no Python, Lean or
  Mathlib. Packaging pins the backend wheel in `backend/dist` (asset name, size,
  SHA-256 and release tag) as `glitnirBackend` in the packaged `package.json`;
  **Set Up Environment** downloads and verifies exactly that wheel.
- **Layers.** The backend has six packages: `glitnir.providers`, `glitnir.lean`,
  `glitnir.compute`, `glitnir.notebook`, `glitnir.preview` and `glitnir.service`.
  A module imports only from its own layer or below, and
  `backend/tests/test_layering.py` enforces it. `glitnir.preview` holds the
  renderer's view-model and assets and starts no thread or server, but it is
  not side-effect free: the editor's `--export` command writes the renderer
  bundle, and snapshot fingerprints may run git.
  `glitnir.service.preview` composes two components, `runs.DirectiveRuns`
  (evidence and decision runs, remembered Lean output, saved failures) and
  `views.ViewDrawing` (view jobs and saved drawings), which share its locks and
  cancel event through a frozen `PreviewContext` and never hold the service.
  `glitnir.notebook.records_schema` says what a well-formed
  `<Notebook>.glitnir.json` is; `check_records` reads, writes and interprets
  one.
  `glitnir.lean.project_checking` holds the two checks, `check_project` and
  `check_candidate`, and the orchestration between their Lean runs; the runs
  themselves (`observer`), where a run failed (`failures`), examples checked
  as theorems (`examples`), what the report and replay programs said
  (`reports`, `replay`) and a decision block's text and verdict (`candidate`)
  are its own modules.
  Each dictionary that crosses a layer is a declared `TypedDict`, defined where
  it is produced: the checker's result in `lean/results.py`, validated
  provider output in `providers/outcomes.py`, the records file in
  `notebook/records_schema.py` (whose validators are pinned to the shapes by
  `tests/test_records_schema.py`), the directives in `notebook/directives.py`,
  and what the renderer receives in `preview/snapshot.py` and
  `preview/cards.py`. Compute's wire protocol, the helper programs' JSON in
  `glitnir.lean` before its validators, provider output before its validator
  and request bodies stay `dict[str, Any]`.
- **Providers.** Optional features come from packages registered under the
  `glitnir.providers` entry-point group, against the interfaces in
  `backend/src/glitnir/providers/__init__.py`, with their output validated by
  `glitnir/providers/outcomes.py`. The core backend has no runtime
  dependencies; Forseti-specific code lives in `plugins/forseti`.
- **Proof authority.** Only the backend's sandboxed project check, with its
  independent kernel replay, decides a status. The user guide's
  [Checking](docs/guide.md#checking) and [Imports and caches](docs/guide.md#imports-and-caches)
  sections describe the behavior users see; AGENTS.md states the invariants
  that the replay cache and incremental replay must keep, and
  `ProjectReplay.lean` documents the argument.
- **Replay inventory.** Prepared inspection asks Lake for the full private import
  closure needed by reporting and kernel replay, including non-exported imports.
  It preserves the saved source's module flag and visibility. Fingerprint v5
  invalidates earlier saved results whose inventories could omit these inputs.
- **Named proposition display.** A project check may record one head-definition
  expansion for a named proposition, or the constructor signature of a
  single-constructor proposition. The card labels it display only and states
  its one-step/4096-character bound. Display work has separate heartbeat and
  depth limits; failure leaves the proof status intact. The expansion follows
  the saved claim's source/environment freshness. Older records have none;
  check the notebook again to populate it.
- **Saved results.** A saved result is current while the environment
  fingerprint it was saved under (`glitnir/lean/fingerprint.py`) still matches.
  The fingerprint names no location, so saved results are portable across
  checkouts and machines, macOS and Linux alike, when the sources, dependencies
  and compiled artifacts are byte-identical; a build Lake would record
  differently, a path-dependent one included, reads as out of date. Known gaps,
  unchanged by portability: a transitive module rebuilt without its importer,
  `include_str` data files, and artifacts swapped without their trace. A trace
  file that exists but is no Lake trace makes the environment unknown, so
  nothing reads as current under it. A change to what the fingerprint hashes
  bumps its `glitnir.environment-fingerprint/vN` tag, which makes every saved
  result out of date once.

## Build and test

With Python 3.12+, uv and Node 22+, run from the repository root:

```sh
uv build backend --wheel --out-dir backend/dist
uv venv --python 3.12 --no-python-downloads .venv
uv pip install --python .venv/bin/python -c backend/requirements-dev.lock \
  'backend/dist/glitnir-0.1.10-py3-none-any.whl[dev]'
(cd backend && ../.venv/bin/python -m pytest \
  && ../.venv/bin/black --check src tests scripts experiments \
  && ../.venv/bin/isort --check-only src tests scripts experiments \
  && ../.venv/bin/flake8 src tests scripts experiments && ../.venv/bin/mypy src scripts experiments)

# Optional: the Forseti view and evidence provider and its tests (as in CI). Its dependencies
# are named explicitly because glitnir is not on an index.
uv build plugins/forseti --wheel --out-dir plugins/forseti/dist
uv pip install --python .venv/bin/python -c backend/requirements-dev.lock lark z3-solver
uv pip install --python .venv/bin/python --no-deps \
  plugins/forseti/dist/glitnir_forseti-0.1.10-py3-none-any.whl
(cd plugins/forseti && ../../.venv/bin/python -m pytest -q && ../../.venv/bin/black --check src tests tools \
  && ../../.venv/bin/isort --check-only src tests tools && ../../.venv/bin/flake8 src tests tools \
  && ../../.venv/bin/mypy src)

npm ci
export GLITNIR_BUILD_PYTHON="$PWD/.venv/bin/python"
npm test
npm run package
```

Backend tests import the installed wheel, so rebuild and reinstall it after
changing `backend/src`; likewise rebuild the plugin wheel after changing
`plugins/forseti/src`. `pytest -m "not live"` leaves out the live Lean tests
(see [Live Lean tests](#live-lean-tests)).

`GLITNIR_BUILD_PYTHON` is the Python whose installed backend `npm test`,
`npm run compile` and `npm run package` export the renderer from (default
`python3`). Point it at an environment holding the wheel built from the same
commit.

### Packaging the VSIX

`npm run package` builds the universal thin `glitnir-VERSION.vsix` in the
repository root. It needs exactly one backend wheel in `backend/dist`, which it
pins, and then verifies the package: no managed runtime or native binaries, the
size limits, the version and the backend binding. `GLITNIR_PACKAGE_VERSION`
overrides the package version and `GLITNIR_BACKEND_RELEASE_TAG` (such as
`v0.1.1`) records the release the backend is downloaded from; CI sets the tag
from `package.json`.

`.vscodeignore` lists what the VSIX includes: `README.md` (the Marketplace page),
`LICENSE`, `THIRD_PARTY_NOTICES.md`, `CONTRIBUTING.md`, `SECURITY.md`, `dist/`,
the authoring skill and a few runtime dependencies. This file is not packaged.
The README must not contain SVG images other than trusted badges, which `vsce`
rejects, and its links to repository files are absolute GitHub URLs so they
work on the Marketplace.

### Host test

`npm run test:host` runs the extension in a separate VS Code profile and never
touches your own settings. It uses `GLITNIR_TEST_PYTHON` (or
`GLITNIR_BUILD_PYTHON`) as the installed backend. Set `GLITNIR_TEST_VSIX` to a
packaged VSIX to test that installation instead of the source tree, and
`VSCODE_EXECUTABLE_PATH` to use an installed VS Code rather than downloading one.
On Linux, run it under `xvfb-run -a`. If your shell sets `ELECTRON_RUN_AS_NODE`
(some editor-integrated terminals do), unset it first. CI also checks a fresh
setup from the packaged VSIX with `extension/scripts/check-thin-install.cjs`.

Run `npm test`, `npm run package` and `npm run test:host` when host behavior
changes. Run the backend's complete pytest, black, isort, flake8 and mypy checks
when changing Python or its packaging, and the plugin's when changing the
plugin.

## Live Lean tests

The backend's `test_live_` tests run Lean. The backend's conftest marks them
`live`, and a test that uses the `project_toolchain` fixture must have that
name. They skip locally unless `GLITNIR_REQUIRE_PROJECT_INSPECTION=1` and
`GLITNIR_TEST_PROOF_TRACE=1` are set with Lean 4.35.0 (the one version Glitnir
checks, `project_inspection.TOOLCHAIN`) and a working sandbox; with both set, a
live test that skips fails instead.

```sh
cd backend
GLITNIR_REQUIRE_PROJECT_INSPECTION=1 GLITNIR_TEST_PROOF_TRACE=1 \
  ../.venv/bin/python -m pytest -m live -rs
```

Replay-cache tests that only need a recorded base start from a per-test copy of
one recorded once per session (`recorded_replay_cache`); tests of the cold path
use `fresh_replay_cache`.

Each project check a backend live test runs through
`project_checking.check_project` or `check_candidate` (looked up when called,
so not those made through the editor service or the compute worker), and each
block the plugin's stepped live test checks, writes a `check stages <test>
<file>: resolve 4.2 s, capture …` line after the test's outcome, so CI logs
show where each check's time goes.
The stages are those of the result's `stage_ms` (see
`project_checking.StageTimes`), which is never saved in `.glitnir.json`.

Glitnir runs its Lean helper programs (`backend/src/glitnir/lean/*.lean`) with
`lean --run` and reads JSON from their output, so they must elaborate on the
supported Lean with no messages at all; a deprecation warning breaks a check.
`backend/scripts/check_helpers.py` composes each one as a check does, from the
installed backend, and elaborates it with the installed toolchain, failing on any
output. Run it after changing a helper or `TOOLCHAIN`:

```sh
.venv/bin/python backend/scripts/check_helpers.py
```

Two helpers mirror private Lean 4.35.0 structures with `unsafeCast`:
`ProjectReplay.lean`'s `KernelFields` (`Kernel.Environment`, through which
`rebuild` places the replayed imports in an empty environment) and
`ProjectReport.lean`'s `ExportedAxioms`. When changing `TOOLCHAIN`, compare
each with the new Lean's source field by field. `rebuild` refuses to run on any
other Lean version and checks the mirror against the public accessors before
using it, so a stale mirror fails the check rather than building anything.

The plugin's live tests need a built Forseti Lean checkout, with the modules
its view and evidence providers load built too (`lake build
Gimle.Asgard.Compile.RationalInterchange Gimle.Asgard.Model.Continuous
Gimle.Asgard.Visualization.Model`; the release's own targets do not build
them all), `lake` on `PATH`, Lean 4.35.0 installed with elan and a working
sandbox, and skip unless
`FORSETI_LEAN_TEST_PROJECT` (or `FORSETI_LEAN_PROJECT`) names that checkout;
`FORSETI_REQUIRE_LEAN_PROJECT=1` makes a missing project fail instead, and
under it a test using that checkout that skips at run time fails too; a skip it
declares with a `skipif` mark (another platform's sandbox probes) or for the
optional `simulation` input stays a skip. Plugin
tests marked `simulation` need Python Asgard (`gimle.asgard`). The stepped live
test allows each block 300 s to check (on CI's Linux runners a block takes
69–75 s); on a loaded or slower machine raise that with
`FORSETI_CHECK_BUDGET_MS` (a whole number of milliseconds). The
[plugin README](plugins/forseti/README.md) shows how to run them.

CI's `plugin-live` job runs all of the plugin's Lean tests on Linux against the
pinned release; run them locally for macOS. Its first all-green run was run
39731807284 on commit `cb59769` (2026-10-22), against forseti-lean v0.4.0 on
Lean 4.35.0. Against the current pin, forseti-lean v0.5.1 with asgard-lean
v0.4.1, every shard passed in run 40654470418 (2026-10-28). `stream` decisions and `iterate` evidence were verified on
2026-10-23, locally on macOS (Glitnir 0.1.5, Lean 4.35.0). CI covers `stream`
decisions in `tests/test_stream_decision_lean.py`; `iterate` needs Python
Asgard, which CI does not install, so it stays a local check. The plugin README
records the runs.

## Continuous integration

CI (`.github/workflows/check.yml`) runs on Linux only; verify macOS locally. Its
jobs are:

- **build** builds the backend and plugin wheels once per run, so a retry tests
  the same bytes.
- **extension** installs those wheels, runs the backend's tests with
  `-m "not live"` and its linters, the plugin's tests and linters, `npm test`,
  the minimal runtime bridge test, `npm run package`, a fresh setup from the
  packaged VSIX and the isolated host test.
- **live** runs the backend's `-m live` tests against the supported Lean
  (`TOOLCHAIN`, 4.35.0) in the bubblewrap sandbox, where a skipped live test
  fails.
- **helpers** elaborates every Lean helper program on that Lean with
  `backend/scripts/check_helpers.py`, requiring no output.
- **plugin-live** builds the Forseti Lean release the plugin pins (forseti-lean
  `v0.5.1`, checked against its commit) and runs the plugin's whole test suite
  against it with `FORSETI_REQUIRE_LEAN_PROJECT=1`, so a Lean test that cannot
  run or skips fails (a `skipif` it declares, or the optional `simulation`
  input, stays a skip). It runs as a matrix of shards, each building the release
  and given up to 180 minutes: `plugins/forseti/tools/live_shards.py` names the
  slow Lean modules of each shard, puts every other module in `rest` and gives
  the matrix its shard names, so together the shards run the suite exactly once.
  Each test's name, outcome and time are printed as it finishes. It runs on
  every pull request and `main` push; no test runs the community REPL.
- **candidate** creates a release candidate from a `main` push when the
  extension, live and helpers jobs and every plugin-live shard pass and the
  `RELEASE_CANDIDATES` repository variable is `true`
  (`.github/workflows/candidate.yml`).

Keep public CI independent of private repositories. Tests requiring an external
corpus or optional simulation must declare those prerequisites without skipping
independent tests in the same module.

## Re-pinning Forseti Lean

The plugin pins one Forseti Lean release by content: `lean-release.v1.json`,
`lean-lockfile.v1.json` and `lean-replay.v1.json` in
`plugins/forseti/src/glitnir_forseti`, the origin record
`plugins/forseti/tests/fixtures/lean-release-origin.v1.json`, `SUPPORT_VERSION`
in `plugins/forseti/tools/pin_lean_release.py` and the tag and commit
plugin-live clones in `check.yml`. Regenerate them on a Linux runner with the
manually dispatched **Regenerate Forseti Lean pins** workflow
(`.github/workflows/forseti-pins.yml`), so no local Lean build is needed:

```sh
gh workflow run forseti-pins.yml -R arnovich/glitnir \
  -f forseti_ref=v0.5.1 -f asgard_ref=v0.4.1   # -f bump_support=false to keep the version
```

It has read-only permissions and no secrets, and it never pushes or opens a pull
request. It:

1. resolves `forseti_ref` and `asgard_ref` in the public forseti-lean and
   asgard-lean repositories (a tag as `refs/tags/NAME`, a 40-hex SHA as itself,
   anything else as a branch) and fails unless the forseti-lean commit's
   `lake-manifest.json` locks exactly that asgard-lean commit from asgard-lean;
2. installs the backend and plugin built from the dispatched Glitnir commit,
   elan and the sandbox, and builds the release as plugin-live does
   (`lake exe cache get`, `lake build`, then the providers' modules);
3. runs `pin_lean_release.py` with `--forseti-tag`/`--asgard-tag` set to the
   inputs (and `--bump-support` unless `bump_support` is false, which bumps
   `SUPPORT_VERSION` only when the pinned content changed), then reinstalls the
   plugin so the replay tool and the tests read the new release pin, and runs
   `pin_lean_replay.py` with every supported platform's toolchain (below);
4. when `forseti_ref` is a tag, points plugin-live's clone at it and its commit;
5. runs the pin tests against the regenerated, reinstalled pins under
   `FORSETI_REQUIRE_LEAN_PROJECT=1`: `test_pin_lean_release.py`,
   `test_lean_release.py`, `test_the_replay_pin_matches_the_release` and
   `test_the_replay_pin_is_current`;
6. uploads the artifact `forseti-pins` (`forseti-pins.patch`, the changed files
   under `files/` and `resolved.json`) and writes a summary with the resolved
   commits, the support version, the toolchain digests per platform, the changed
   files and the commands to apply the patch.

A ref that is not a tag makes a probe, which the summary says: its origin record
names the ref as given, `check.yml` is left alone and
`test_release_records_which_checkout_satisfies_it`, which requires tags, is
deselected. Do not commit a probe's pins.

The replay pin records one toolchain digest per platform: a digest of the
toolchain release's own `lean`, `leanchecker` and shared libraries for that
platform, which change only with the toolchain. Nothing has to run on a
platform to pin it. The workflow downloads the Lean release's archives for
`Linux-x86_64`, `Linux-aarch64`, `Darwin-x86_64` and `Darwin-arm64`, checks
each against the SHA-256 GitHub reports for it, and passes the extracted
toolchains to `pin_lean_replay.py --toolchain PLATFORM=DIR`. Each computed
digest must equal the committed one when the toolchain is unchanged, and the
`Linux-x86_64` one must equal the digest of the elan install the run observed,
which shows that elan installs the archive's bytes. Closures are the same on
every platform, and replay re-observes every closure where it runs. A platform
the pin has no digest for reports the environment unknown, never a proof.

To apply a run's pins, on a branch:

```sh
gh run download RUN_ID -R arnovich/glitnir -n forseti-pins -D forseti-pins
git apply --index forseti-pins/forseti-pins.patch
```

Then update the docs that name the pinned tags (this file, `docs/guide.md`,
`plugins/forseti/README.md` and
`skills/glitnir-notebooks/references/optional-features.md`) and open a pull
request; its plugin-live job runs the whole plugin suite against the release.
Both tools still run locally on a built checkout, as their docstrings describe.

## Releases

Release instructions for maintainers are in [AGENTS.md](AGENTS.md#releases),
including [Marketplace publication](AGENTS.md#marketplace-publication): publishing
a GitHub release publishes its exact VSIX after verification and the owner's
approval. Package versions are explicit in `package.json` and `package-lock.json`;
bump `plugins/forseti/pyproject.toml` with them.

The Marketplace icon `extension/icon/glitnir-128.png` is rendered from
`extension/icon/glitnir-icon.svg` by `extension/icon/render-icon.sh` (needs
`rsvg-convert`); commit both after a change.

## Extension packages

Glitnir finds provider packages through the `glitnir.providers` entry-point
group and installs them with **Glitnir: Install Extension Package…**. Before
installing, it reads the wheel's metadata without running it and refuses:

- a file that declares no `glitnir.providers` entry point, or is the backend
  itself;
- a wheel that could reach beyond its package: one that touches the backend's
  own files, adds a startup hook (`.pth`, `sitecustomize`), names a command like
  a path or after another package's command, or needs an extra it does not
  offer;
- an `entry_points.txt` that is not plain `[group]` headers and
  `name = module:attribute` lines, without comments or indentation;
- file names or a file list (RECORD) that are not plain ASCII, a file list that
  does not give each file's SHA-256 hash and size, or an archive that is not a
  plain zip (ZIP64 records, an archive comment).

uv installs a copy that Glitnir writes from exactly the files it checked. Only
extras whose packages Glitnir knows to be published on PyPI are offered (for now
`z3-solver`).

The package picker's entries come from `extension/src/known-packages.ts`, which
is data only: an entry names a distribution, its release asset, the questions
for its extras and notes, and adds no trust. Release downloads, used by the
picker and by the backend upgrade, go through `extension/src/release-assets.ts`.
Only a plugin wheel the VSIX pins is downloaded: `npm run package` writes
`glitnirPlugins` (`[{distribution, asset, size, sha256}]`) into the VSIX's
`package.json` from the one wheel in `plugins/forseti/dist`, like
`glitnirBackend`, and pins none when there is none (such a build offers wheel
files only). The extension fetches `glitnir-candidate.json` and `SHA256SUMS`
from the release the backend pin names, refuses a manifest whose schema,
repository, tag, version, backend asset, asset digests or plugin binding differ
from the VSIX pins or from `SHA256SUMS`, and installs the downloaded bytes only
if they equal the pin. `release-candidate.cjs` refuses a candidate whose VSIX
plugin pin differs from the attached plugin wheel, at collection and
verification. A new provider asset needs a pin and a binding (today the
manifest's single `plugin` field) before the picker or the upgrade can
download it. Release requests are anonymous; only GitHub's anonymous rate limit
(`X-RateLimit-Remaining: 0`) offers a sign-in, with no scopes.
`extension/scripts/providers.py packages` reports each provider's installed
extras (those whose requirements are all installed), which the upgrade carries
over, filtered through the registry's extras. **Install uv** runs Homebrew or the
official installer of `UV_VERSION` in `extension/src/backend-install.ts`, in a
terminal created with an explicit environment and `strictEnv: true` (the host
test checks that neither the editor's environment nor `terminal.integrated.env`
reaches it); bump it, and the version shown in the README
and `extension/media/setup.md`, to a uv release at least two weeks old.

## Static export (galleries)

A gallery can publish notebooks from their committed `<Notebook>.glitnir.json`
without running any checks. `glitnir.service.static_export.SavedNotebook`
reads a saved notebook and its saved results for the canonical renderer:

- `snapshot(run_prompts=False)` returns the snapshot the renderer reads from
  `GET /api/notebook`. Each saved result is compared only with the saved source:
  one whose source changed since its check is out of date, and every other one
  is shown as recorded. It is never compared with the reader's Lean environment,
  so the snapshot sets `preview.recorded` and the renderer shows "Saved results,
  shown as recorded", never "current". By default, evidence and decision cards
  show their saved results without what running them would need, such as an
  extra to install; `run_prompts=True` keeps those prompts. A result saved
  without a known environment fingerprint is never shown as recorded, and
  `preview.project` gives the project's root relative to the notebook, so the
  snapshot holds no local absolute path. Saved failed evidence and decision
  runs are shown as recorded too: a card whose last run failed for this source
  says so ("Failed, as recorded", or a note above its chart or outcome), and a
  failure recorded for other source is not shown.
- `asset(path, sha256)` returns an image the prose shows.
- `view_imports()` lists the modules to build before drawing views.
- `draw_view(cell, line)` returns the view's saved drawing from the `views`
  section of `<Notebook>.glitnir.json` as recorded, without running Lean, when
  the source through its directive and the provider match. Otherwise it draws
  the view and waits for it, as the editor does: with Lean in the sandbox, from
  the project's own build, with the notebook's proofs skipped. That drawing is
  not saved; static export never writes records. Drawing checks nothing.
  Committing the `.glitnir.json` keeps a gallery Lean-free for views.

The module's `export_styles()` returns the CSS that **Export HTML** embeds
(`GET /api/export/styles`). `sibling_exports(directory, snapshot["references"])`
returns the referenced modules whose regular `Module.html` files already exist
beside the page being built. Pass that list to the renderer as the available
exports so notebook and imported-claim links work without emitting dead links.
For a clean multi-page gallery, use two passes: write every page without sibling
links first, then regenerate every final page after all expected HTML files are
present. Publish only the final pass. This makes links in both directions
independent of generation order while still verifying every target on disk.
Reading a snapshot or an image runs no Lean, git or provider run and writes no
file.
Nothing here checks the notebook or writes its source or results; drawing a view
writes only Glitnir's own temporary and cache files, never the project. Saved
results are matched to the saved source result by result, so a gallery that
must publish only complete results should also compare the results file's
`source_sha256` with the snapshot's `sha256`.

Render the snapshot with the packaged renderer (`glitnir/preview/app`; a gallery
finds its assets with `glitnir.preview.editor.renderer_directory()`, never the
private `_APP`), and
export it with its **Export HTML**, through the same `window.glitnirHost`
interface the editor uses (`fetch`, `asset`, `prepareHTMLExport` and
`saveHTML`). `prepareHTMLExport` returns `{saved, token, siblings}`; for a gallery,
derive `siblings` with `sibling_exports` and bind `token` to the selected output
path until `saveHTML`. The exported page contains document and exact Lean-source
views, switches between them without scripts, and links only the sibling exports
named by that response. Answer `fetch` from the prepared data and refuse anything
else:

```python
import json
from pathlib import Path

from glitnir.service.static_export import (
    SavedNotebook,
    export_styles,
    sibling_exports,
)

with SavedNotebook(Path("examples/Decay.lean")) as notebook:
    snapshot = notebook.snapshot()  # GET /api/notebook
    views = {  # GET /api/view?cell=C&line=L: a drawing, or {"error": ...}
        f"{view['cell']}:{view['line']}": notebook.draw_view(view["cell"], view["line"])
        for view in snapshot["views"]
    }
output = Path("gallery/Decay.html")
page = {
    "snapshot": snapshot,
    "views": views,
    "css": export_styles(),
    "siblings": sibling_exports(output.parent, snapshot["references"]),
}
Path("decay.json").write_text(json.dumps(page))  # read by your renderer driver
```

## Offline warm Lean experiment

`backend/experiments/warm_pool` is an advisory-only, fixed-fixture prototype,
excluded from the backend wheel and extension. See its README for the bounded
benchmark, isolation/recovery probes, evidence and production decision. It does
not install a service or add an API or editor setting.
