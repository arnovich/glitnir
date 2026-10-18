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

- **One renderer.** The backend's `glitnir/notebook/app` is the canonical
  renderer. `npm run compile` exports it from the Python in
  `GLITNIR_BUILD_PYTHON` (`python -m glitnir.notebook.editor --export`) into
  `dist/media`, with `dist/editor-runtime.json` recording its identity. At
  runtime the extension's probe refuses a backend whose renderer differs ("The
  installed Glitnir runtime is incompatible with this Glitnir build"), so build
  the extension against the wheel of the same commit. Do not maintain a second
  renderer.
- **Thin, universal VSIX.** The packaged extension contains no Python, Lean or
  Mathlib. Packaging pins the backend wheel in `backend/dist` (asset name, size,
  SHA-256 and release tag) as `glitnirBackend` in the packaged `package.json`;
  **Set Up Environment** downloads and verifies exactly that wheel.
- **Providers.** Optional features come from packages registered under the
  `glitnir.providers` entry-point group, against the interfaces in
  `backend/src/glitnir/providers.py`. The core backend has no runtime
  dependencies; Forseti-specific code lives in `plugins/forseti`.
- **Proof authority.** Only the backend's sandboxed project check, with its
  independent kernel replay, decides a status. The README's
  [Checking](README.md#checking) and [Imports and caches](README.md#imports-and-caches)
  sections describe the behavior users see; AGENTS.md states the invariants
  that the replay cache and incremental replay must keep, and
  `ProjectReplay.lean` documents the argument.

## Build and test

With Python 3.12+, uv and Node 22+, run from the repository root:

```sh
uv build backend --wheel --out-dir backend/dist
uv venv --python 3.12 --no-python-downloads .venv
uv pip install --python .venv/bin/python -c backend/requirements-dev.lock \
  'backend/dist/glitnir-0.1.1-py3-none-any.whl[dev]'
(cd backend && ../.venv/bin/python -m pytest && ../.venv/bin/black --check src tests \
  && ../.venv/bin/isort --check-only src tests && ../.venv/bin/flake8 src tests \
  && ../.venv/bin/mypy src)

# Optional: the Forseti view and evidence provider and its tests (as in CI). Its dependencies
# are named explicitly because glitnir is not on an index.
uv build plugins/forseti --wheel --out-dir plugins/forseti/dist
uv pip install --python .venv/bin/python -c backend/requirements-dev.lock lark z3-solver
uv pip install --python .venv/bin/python --no-deps \
  plugins/forseti/dist/glitnir_forseti-0.1.1-py3-none-any.whl
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
`GLITNIR_TEST_PROOF_TRACE=1` are set with Lean 4.32.1 and a working sandbox;
with both set, a live test that skips fails instead.

```sh
cd backend
GLITNIR_REQUIRE_PROJECT_INSPECTION=1 GLITNIR_TEST_PROOF_TRACE=1 \
  ../.venv/bin/python -m pytest -m live -rs
```

Replay-cache tests that only need a recorded base start from a per-test copy of
one recorded once per session (`recorded_replay_cache`); tests of the cold path
use `fresh_replay_cache`.

The plugin's live tests need a built Forseti Lean checkout, `lake` on `PATH`,
Lean 4.32.1 installed with elan and a working sandbox, and skip unless
`FORSETI_LEAN_TEST_PROJECT` (or `FORSETI_LEAN_PROJECT`) names that checkout;
`FORSETI_REQUIRE_LEAN_PROJECT=1` makes a missing project fail instead. Plugin
tests marked `simulation` need Python Asgard (`gimle.asgard`). The
[plugin README](plugins/forseti/README.md) shows how to run them.

Verified on 2026-10-15 against forseti-lean `c65dada` (Lean 4.32.1): the opt-in
live tests passed for proven and refuted `entailment` and `trajectory` decisions
and for `simulate` evidence. `stream` decisions and `iterate` evidence have not
been run through Glitnir's checker yet. CI does not run these tests
(`plugins/forseti/tests/test_decision_provider_lean.py` and
`test_evidence_provider_lean.py`, with `FORSETI_LEAN_TEST_PROJECT` set), so
re-run them after Forseti or Glitnir changes. The plugin README records later
runs.

## Continuous integration

CI (`.github/workflows/check.yml`) runs on Linux only; verify macOS locally. Its
jobs are:

- **build** builds the backend and plugin wheels once per run, so a retry tests
  the same bytes.
- **extension** installs those wheels, runs the backend's tests with
  `-m "not live"` and its linters, the plugin's tests and linters, `npm test`,
  the minimal runtime bridge test, `npm run package`, a fresh setup from the
  packaged VSIX and the isolated host test.
- **live** runs the backend's `-m live` tests against Lean 4.32.1 in the
  bubblewrap sandbox, where a skipped live test fails.
- **candidate** creates a release candidate from a `main` push when both test
  jobs pass and the `RELEASE_CANDIDATES` repository variable is `true`
  (`.github/workflows/candidate.yml`).

Keep public CI independent of private repositories. Tests requiring an external
corpus or optional simulation must declare those prerequisites without skipping
independent tests in the same module.

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

## Static export (galleries)

A gallery can publish notebooks from their committed `<Notebook>.glitnir.json`
without running any checks. `glitnir.notebook.static_export.SavedNotebook`
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
  snapshot holds no local absolute path.
- `asset(path, sha256)` returns an image the prose shows.
- `view_imports()` lists the modules to build before drawing views.
- `draw_view(cell, line)` draws one listed view and waits for it, as the editor
  does: with Lean in the sandbox, from the project's own build. Drawing checks
  nothing.

The module's `export_styles()` returns the CSS that **Export HTML** embeds
(`GET /api/export/styles`). Reading a snapshot or an image runs no Lean, git or
provider run and writes no file.
Nothing here checks the notebook or writes its source or results; drawing a view
writes only Glitnir's own temporary and cache files, never the project. Saved
results are matched to the saved source result by result, so a gallery that
must publish only complete results should also compare the results file's
`source_sha256` with the snapshot's `sha256`.

Render the snapshot with the packaged renderer (`glitnir/notebook/app`), and
export it with its **Export HTML**, through the same `window.glitnirHost`
interface the editor uses (`fetch`, `asset` and `saveHTML`). Answer `fetch` from
the prepared data and refuse anything else:

```python
import json
from pathlib import Path

from glitnir.notebook.static_export import SavedNotebook, export_styles

with SavedNotebook(Path("examples/Decay.lean")) as notebook:
    snapshot = notebook.snapshot()  # GET /api/notebook
    views = {  # GET /api/view?cell=C&line=L: a drawing, or {"error": ...}
        f"{view['cell']}:{view['line']}": notebook.draw_view(view["cell"], view["line"])
        for view in snapshot["views"]
    }
page = {"snapshot": snapshot, "views": views, "css": export_styles()}
Path("decay.json").write_text(json.dumps(page))  # read by your renderer driver
```
