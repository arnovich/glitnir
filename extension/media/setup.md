# Glitnir setup guide

Glitnir runs in local VS Code windows on **macOS and Linux**; see
[Limits](https://github.com/arnovich/glitnir#limits) in the README. This guide
covers requirements, setup, the Forseti plugin, upgrading and troubleshooting.
The [user guide](https://github.com/arnovich/glitnir/blob/main/docs/guide.md)
describes the notebook format, the preview and checking.

## Requirements

- **VS Code** 1.96 or newer, with the notebook's folder open as a trusted
  workspace.
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** and
  **Python 3.12 or newer**, for the Python backend. VS Code may not inherit your
  shell's `PATH`, so setup looks for `uv` in `~/.local/bin`, `/opt/homebrew/bin`,
  `/usr/local/bin`, `/usr/bin` and `/bin`. Without it, setup offers **Install
  uv** (see [Set Up Environment](#set-up-environment)).
- **Lean 4.35.0**, the one Lean version Glitnir checks, installed with
  [elan](https://github.com/leanprover/elan):
  `elan toolchain install leanprover/lean4:v4.35.0`. Reading a notebook needs only
  the backend; checking, proof steps, views, evidence and decisions need Lean.
  A notebook on another Lean version still opens and reads, with a banner saying
  those are off. Glitnir moves the supported version forward as new Lean
  releases are tested.
  Glitnir never builds or downloads a project's dependencies: build the
  notebook's Lake project first (`lake build`, after `lake exe cache get` for
  Mathlib).
- **On Linux, bubblewrap** with unprivileged user namespaces, which Lean runs in.
  On Ubuntu 24.04, install `bubblewrap apparmor apparmor-profiles` and run
  `sudo apparmor_parser -r /usr/share/apparmor/extra-profiles/bwrap-userns-restrict`.
  macOS uses its built-in sandbox.

## Install the extension

Install **Glitnir — Lean Notebooks** from the
[Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=gimle.glitnir): search for it in the Extensions view, or run
`code --install-extension gimle.glitnir`.

For a specific version or an offline machine, install it from a GitHub release
instead:

1. Download `glitnir-VERSION.vsix` from
   [Releases](https://github.com/arnovich/glitnir/releases).
2. In VS Code, run **Extensions: Install from VSIX…** and select it.

## Set Up Environment

1. Open a Lean file in a trusted workspace.
2. Run **Glitnir: Set Up Environment → Set Up** (also offered when you first open
   a notebook). In one step it downloads the backend wheel of the extension's own
   release, verifies its checksum against the one the extension records, and
   installs it with uv into a new environment under
   `~/.local/share/glitnir/backends`.
3. Run **Glitnir: Open in Glitnir**.

**More Options…** holds the other setup choices:

- **Choose downloaded backend wheel** installs a matching backend `.whl` you
  downloaded or built yourself, for development builds and offline use. It must
  still match the checksum the extension records.
- **Choose Existing Environment** selects a Python where the backend is already
  installed.
- **Read setup guide** opens this guide, which setup errors also link to.

If uv is not installed, setup says so and offers **Install uv**, showing the
command first: Homebrew's `install uv`, by Homebrew's full path, when Homebrew
is installed, otherwise the official installer of the uv version this release
pins, `/usr/bin/curl -LsSf https://astral.sh/uv/0.12.23/install.sh | /bin/sh`.
Nothing runs until you click **Install uv**; the command then runs in a VS Code
terminal named "Install uv", where you can watch it. When that terminal closes,
or when you choose **Retry** (for example after installing uv yourself), Glitnir
looks for uv again and continues the setup.

The command runs in a minimal environment, not your shell's:
`PATH=/usr/bin:/bin:/usr/sbin:/sbin` (plus Homebrew's own directory), `HOME`,
`USER`, `TMPDIR` and `LANG`, and the terminal variables VS Code adds. Nothing
else comes from VS Code, your shell profiles or `terminal.integrated.env`
settings.

The official installer puts uv in `~/.local/bin`, where Glitnir looks, and is
told (`UV_NO_MODIFY_PATH=1`) to leave your shell profiles alone; add
`~/.local/bin` to `PATH` yourself to use uv in your own terminals.

Without any setup, Glitnir also finds a backend in the environment you chose
before, a `.venv` in the workspace or in a folder above the notebook, or
`python3`. The `glitnir.pythonPath` setting overrides all of these. The backend
must match the extension's release.

To install by hand, download the VSIX, backend wheel and `SHA256SUMS` from one
release, then run (`--ignore-missing` skips the assets you did not download):

```sh
shasum -a 256 -c --ignore-missing SHA256SUMS   # Linux: sha256sum -c --ignore-missing SHA256SUMS
uv venv --python 3.12 --no-python-downloads "$HOME/.local/share/glitnir/venv"
uv pip install --python "$HOME/.local/share/glitnir/venv/bin/python" --reinstall \
  ./glitnir-*-py3-none-any.whl
```

Then set `glitnir.pythonPath` to that environment's `bin/python`.

## The Forseti plugin

The optional `glitnir-forseti` package adds Forseti views, evidence and decisions
(see [Extensions](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#extension-packages)). It is not on PyPI; each release has its wheel,
built from the same commit and tested with that release's backend.

Run **Glitnir: Install Extension Package… → Forseti → Download from this
release** (also offered in **Glitnir: Set Up Environment**), and answer Yes to
"Install z3 for decision procedures?" for decisions. Glitnir downloads the
wheel from the same release as the backend and installs it only if its SHA-256
equals the one the extension records for it (and the release's `SHA256SUMS`
and `glitnir-candidate.json` bind the same bytes).

Alternatively, from a file: download `glitnir_forseti-VERSION-py3-none-any.whl`
and `SHA256SUMS` from the same release as the VSIX, verify it with
`shasum -a 256 -c --ignore-missing SHA256SUMS` (Linux:
`sha256sum -c --ignore-missing SHA256SUMS`), and choose **Choose downloaded
wheel** instead. To install it by hand into the backend's Python:

```sh
uv pip install --python <backend python> \
  "./glitnir_forseti-VERSION-py3-none-any.whl[z3]"
```

For `iterate` evidence, also install Python Asgard (`gimle-asgard`, not on
PyPI) into the backend yourself, for example
`uv pip install --python <backend python> <gimle-asgard wheel>`.

From a checkout of the repository, build the wheel first with
`uv build plugins/forseti --wheel --out-dir plugins/forseti/dist` and select or
install `plugins/forseti/dist/glitnir_forseti-VERSION-py3-none-any.whl`.

The backend's Python is shown in **Output → Glitnir** ("Glitnir environment: …")
or set by `glitnir.pythonPath`. After a manual install, close and reopen the
preview. Views need a notebook in a Lake project whose packages build
`Gimle.Forseti` and `Gimle.Asgard.Visualization.Model`. The user guide's
[Extensions](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#extension-packages)
section describes the package picker, the checks every wheel goes through and
what each Forseti feature needs.

## Upgrading

An extension installed from the Marketplace updates automatically, like any
other. One installed from a VSIX does not: download the new release's VSIX and
install it the same way. Either way, the backend then follows as described
below.

Each release pins its own backend wheel, and each backend lives in its own
environment, `~/.local/share/glitnir/backends/<wheel SHA-256>`. When the
extension updates (from the Marketplace, or a new VSIX) and finds an environment
from an earlier Glitnir build but none for its own backend, it shows one
notification: "Glitnir was updated to X. Set up its backend?". **Set Up** first
lists what it will do: the backend and package wheels it downloads, the extras
and the PyPI packages they add, the packages you must reinstall yourself, and
any settings file whose `glitnir.pythonPath` it changes. Nothing is downloaded
or installed until you confirm, after which it:

1. downloads this release's backend wheel, checks it against the size and
   SHA-256 the extension records, and installs it with uv into a new
   environment, exactly as **Glitnir: Set Up Environment → Set Up** does;
2. for each package of the earlier environment whose wheel the extension pins
   (like its backend, the extension records the release's `glitnir-forseti`
   wheel by size and SHA-256), downloads the release's `glitnir-candidate.json`
   and `SHA256SUMS`, refuses a release that does not bind exactly the pinned
   wheel and backend, downloads the wheel, installs it only if its SHA-256
   equals the recorded one, with the same checks as **Install Extension
   Package…**, and keeps only the installed extras Glitnir knows (for Forseti,
   `z3`);
3. switches to the new environment only once all of that has succeeded. Each
   `glitnir.pythonPath` the confirmation listed (one naming the earlier managed
   environment) is changed to the new one; one edited since the confirmation is
   left alone and reported, and other overrides are never touched.

Packages whose wheel the extension does not pin (third-party or local wheels,
packages you installed yourself such as `gimle-asgard`, and every package in a
build packaged without a plugin wheel) are listed to
reinstall with **Glitnir: Install Extension Package…**; they never fail the
upgrade. If anything fails or you cancel, the earlier environment is unchanged,
nothing is switched, and the message offers **Retry**; opening a notebook offers
the same setup again. **Glitnir: Set Up Backend for This Version** does all of
this at any time, and running it again is safe. The notification appears once
per extension version; set `glitnir.offerBackendUpdate` to `false` to turn it
off. Downloads go only to this repository's GitHub release for the pinned tag.

The earlier environment is never deleted. Once no window uses it (**Output →
Glitnir** shows the environment in use), remove it yourself, but not while a
`.lock` directory beside it exists:

```sh
ls ~/.local/share/glitnir/backends/          # one directory per backend build
rm -rf ~/.local/share/glitnir/backends/<earlier wheel SHA-256>
```

A release that changes the replay program shows saved results as out of date
once, and its first check replays the imports again (see [Imports and
caches](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#imports-and-caches)).
A release that changes how the environment is identified shows them as out of
date once, too.

## Troubleshooting

Start with **Output → Glitnir**, which logs the environment Glitnir found, setup
and package installs, and each preview's backend output.

- **"No Python environment with the Glitnir backend was found"**: run **Glitnir:
  Set Up Environment** (see [Set Up Environment](#set-up-environment)).
- **"Glitnir needs uv to install its backend"**: choose **Install uv**, or
  install uv into one of the locations under [Requirements](#requirements) and
  choose **Retry**.
- **"The installed Glitnir runtime is incompatible with this Glitnir build"**: the
  backend is from another release; run **Glitnir: Set Up Backend for This
  Version** (see [Upgrading](#upgrading)).
- **"Install leanprover/lean4:v4.35.0 with elan"**: install the toolchain with
  `elan toolchain install leanprover/lean4:v4.35.0`.
- **"This project uses Lean X. Glitnir checks Lean 4.35.0, so checking, proof
  steps, views, evidence and decisions are off"** (or "declares no Lean
  version", or "lean-toolchain could not be read"): Glitnir runs Lean only for a
  project whose `lean-toolchain` declares Lean 4.35.0, as
  `leanprover/lean4:v4.35.0` (a loose file may also declare none). Installing
  4.35.0 does not help; the project itself must declare it. Saved results still
  show, out of date, and the notebook reads as usual. Change `lean-toolchain`
  only if the project builds with 4.35.0, then rebuild it with `lake build` and
  reopen the preview.
- **"no working sandbox"** on Linux: install bubblewrap and allow unprivileged
  user namespaces (see [Requirements](#requirements)). Do not disable confinement
  to work around it.
- **"Views need a Lake project"** (or evidence, decisions, or imports of other
  notebooks): these need the notebook inside a built Lake project; a loose
  `.lean` file has no prepared imports.
- **Large proofs run out of memory.** Every Lean run is limited to
  `glitnir.memoryLimitMb` (16 GB by default, less on a smaller machine). A large
  proof, a big decision block, or an incremental replay on top of a recorded
  Mathlib replay (about 4.7 GB) can exceed a lower limit and fail. Raise the setting (up to 32768) and reopen the
  preview.
- **Checks time out.** Raise `glitnir.elaborationTimeoutMs` (up to 30 minutes)
  and reopen the preview. The first replay of a new set of imports has its own
  60-minute limit and can take long for Mathlib; later checks reuse it.
- **Results show as out of date after an upgrade.** The replay program, or how
  the environment is identified, changed; check again (see [Imports and caches](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#imports-and-caches)).
- **The cache uses a lot of disk.** Delete `glitnir/replay` and
  `glitnir/artifacts` in your user cache directory at any time; the next check
  rebuilds what it needs.
- **"Backend setup is already in progress"**: wait for the other setup to finish.
  If it was interrupted, remove the `.lock` directory the message names once no
  setup is running.

## Help

If that does not help, open an
[issue](https://github.com/arnovich/glitnir/issues) with your OS, Glitnir version
and a minimal example. The
[README](https://github.com/arnovich/glitnir#readme) and the
[user guide](https://github.com/arnovich/glitnir/blob/main/docs/guide.md) have more.
