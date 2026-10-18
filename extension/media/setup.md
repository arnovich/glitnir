# Install Glitnir

Glitnir runs on local macOS and Linux. It needs VS Code 1.96+, Python 3.12+ and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Checking also needs
Lean 4.32.1 through elan, with the project's dependencies already built. On Linux,
checking needs bubblewrap with unprivileged user namespaces; on Ubuntu 24.04,
install `bubblewrap apparmor apparmor-profiles` and run
`sudo apparmor_parser -r /usr/share/apparmor/extra-profiles/bwrap-userns-restrict`.

1. Install the VSIX from a [release](https://github.com/arnovich/glitnir/releases)
   with **Extensions: Install from VSIX…**.
2. Open a Lean file and run **Glitnir: Set Up Environment → Install Backend with
   uv → Download matching backend**. Setup verifies the wheel's checksum against
   the VSIX. To use a wheel you already downloaded, choose **Choose downloaded
   backend wheel**; to reuse an environment, choose **Choose Existing Environment**.
3. Run **Glitnir: Open in Glitnir**.

To install by hand, download the VSIX, backend wheel and `SHA256SUMS` from one
release, then run (`--ignore-missing` skips the assets you did not download):

```sh
shasum -a 256 -c --ignore-missing SHA256SUMS   # Linux: sha256sum -c --ignore-missing SHA256SUMS
uv venv --python 3.12 --no-python-downloads "$HOME/.local/share/glitnir/venv"
uv pip install --python "$HOME/.local/share/glitnir/venv/bin/python" --reinstall \
  ./glitnir-*-py3-none-any.whl
```

Then set `glitnir.pythonPath` to that environment's `bin/python`.

Optional views, evidence and decisions come from provider packages such as
`glitnir-forseti`, installed into the same environment as the backend. Run
**Glitnir: Install Extension Package…** (also offered in **Glitnir: Set Up
Environment**) and select the package's `.whl` file. Glitnir checks that the
wheel declares a `glitnir.providers` entry point, offers the optional extras
whose packages it knows are on PyPI (for `glitnir-forseti`, `z3` for decisions),
installs it with uv while keeping the installed backend, closes this window's
previews that use the backend and lists the providers now available.
It refuses a wheel whose files, file list (RECORD), commands or requirements
could reach beyond the package, such as the backend's own files, a command
named like a path or replacing another package's command, or a withheld extra, one
whose file names or file list are not plain ASCII, one whose file list does not
give each file's SHA-256 hash and size, and one that is not a plain zip archive
(ZIP64 records, an archive comment). uv installs a copy of the wheel that
Glitnir writes from exactly the files it checked.
The Set Up Environment item appears only once a backend is detected. The managed
backend is shared by all windows and workspaces, so a change affects them all;
reopen their previews. If uv fails or is cancelled, run the install again.

Glitnir does not remove packages for you: uninstalling deletes whatever the
installed package's file list names, which Glitnir cannot vouch for. To remove
one, check its files, then run
`uv pip uninstall --python <backend python> <name>` and reopen the previews.
`iterate` evidence also needs Python Asgard (`gimle-asgard`), which is not on
PyPI; install it into the backend yourself.

Each release also has the `glitnir-forseti` wheel,
`glitnir_forseti-VERSION-py3-none-any.whl`, built from the same commit and tested
with that release's backend; it is not on PyPI. Download it and `SHA256SUMS` from
the same release as the VSIX and backend, verify it with
`shasum -a 256 -c --ignore-missing SHA256SUMS` (Linux:
`sha256sum -c --ignore-missing SHA256SUMS`), then run **Glitnir: Install Extension
Package…**, select the wheel and choose the `z3` extra for decisions. To install
it by hand instead:

```sh
uv pip install --python <backend python> \
  "./glitnir_forseti-VERSION-py3-none-any.whl[z3]"
```

From a checkout of the repository, build the wheel first with
`uv build plugins/forseti --wheel --out-dir plugins/forseti/dist` and select or
install `plugins/forseti/dist/glitnir_forseti-0.1.1-py3-none-any.whl`.

The backend's Python is shown in **Output → Glitnir** ("Glitnir environment: …")
or set by `glitnir.pythonPath`. After a manual install, close and reopen the
preview. Its views need a notebook in a Lake project whose packages build
`Gimle.Forseti` and `Gimle.Asgard.Visualization.Model`.

## Upgrading

Each VSIX pins its own backend, installed in its own environment under
`~/.local/share/glitnir/backends/`. After an extension update, Glitnir shows
"Glitnir was updated to X. Set up its backend?" once if it finds an environment
from an earlier build but none for the new one. Nothing downloads until you
click **Set Up**. It then downloads and verifies the new backend as **Download
matching backend** does, into a new environment, and carries over the earlier
environment's extension packages that the same release publishes (such as
`glitnir-forseti`, with extras such as `z3`): each is checked against the
release's `SHA256SUMS` and `glitnir-candidate.json`, which must match this
VSIX's tag, version and backend, and installed with the same checks as
**Install Extension Package…**. Other packages are listed to reinstall with
**Install Extension Package…**. Glitnir switches to the new environment only
when everything succeeded; if something fails or you cancel, the earlier
environment is unchanged and **Retry** runs it again. **Glitnir: Set Up Backend
for This Version** does the same at any time. Set `glitnir.offerBackendUpdate`
to `false` to turn the notification off.

The earlier environment is kept. Once no window uses it, remove it with
`rm -rf ~/.local/share/glitnir/backends/<earlier wheel SHA-256>` (not while a
`.lock` directory beside it exists).

## Help

If something fails, check **Output → Glitnir**, then open an
[issue](https://github.com/arnovich/glitnir/issues). See the
[Glitnir README](https://github.com/arnovich/glitnir#readme) for more.
