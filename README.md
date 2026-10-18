# Glitnir: A simple notebook format for Lean

Glitnir is a simple notebook format that makes it easier for a standard human mathematician to follow agent generated proofs in Lean.

The format is pretty simple, as a notebook is just an ordinary `.lean` file with extra information provided in a specific Markdown format inside the `/-! … -/` comment blocks.

In this repo you will find a VS Code extension for rendering notebooks and a skill for instructing coding agents, like Claude Code or Codex, to produce Lean code in the style of Glitnir notebooks.

You can see [examples of what notebooks](https://arnovich.github.io/forseti-examples/) look like here.

## The Origin

I built Glitnir to fit how I work with Lean and coding agents.

My usual setup is VS Code, split into three panes: one for the Lean code itself, one for the Glitnir rendering and one for the coding agents.

<!-- Here is what my setup typically looks like:
(INSERT SCREENSHOT) -->

As the coding agents are working, I use the Glitnir window to see what they are doing, keep up with their reasoning etc. I can then intervene in the coding agent pane, if I think it is going the wrong way, or, more often, ask it to explain something better in the notebook or dig deeper into something.

The Glitnir notebook updates live and highlights the newest updates, making it easy for you to follow along the work of the coding agents.

Generally I am not a believer in new agent harnesses for math. I think coding agents, with the right skills and tools, are the perfect agents for mathematical research. So that is what I am trying to improve with this Glitnir.

## The notebook format

A Glitnir notebook is a `.lean` file that works perfectly well with standard Lean but extends the syntax with *cells*, rendered as such by Glitnir:

- **Prose cells** are column-zero `/-! … -/` comment blocks, written in Markdown
  with `$…$` and `$$…$$` for mathematics.
- **Lean cells** are everything between prose cells: imports, namespaces,
  definitions and claims, shown as source.
- **Claims** are named `theorem` or `lemma` declarations. Each one is rendered
  with its statement and proof shown separately and its check status alongside.
- **Docstrings** (`/-- … -/`) placed immediately above a definition or claim
  become its description.

Definitions that appear before a claim form its context. A claim should not
depend on declarations that come later in the file. `sorry` marks unfinished
work and is never reported as a success.

### Sections

Markdown headings in prose cells (levels 2–6) structure the notebook. A section
runs until the next heading of the same or a higher level, or the end of the
file, and includes the Lean cells in between. Add ` {fold}` at the end of a
heading to make that section collapsible. The first paragraph after the heading
stays visible as the summary:

```lean
/-!
## Establish the bound {fold}

Damping gives the upper bound; a square gives the lower bound.

The detailed argument follows, with its supporting lemmas below.
-/
```

### Images and links

A prose line that is only an image, such as `![caption](assets/figure.png)`,
shows that file from the `assets/` folder beside the notebook. Images must be
PNG, JPEG, GIF, WebP or SVG files of at most 10 MB, named by a relative path
inside `assets/`; the preview says why any other reference, or a missing,
changed or oversized file, is not shown. SVG files are shown only as images, so
any scripts in them do not run, and **Export HTML** embeds the images so the
snapshot works offline. Images are illustrations: they do not affect checking
or saved results. `[Basics](Basics.lean)` links to a sibling notebook: the
preview reads it in the same panel, read-only and with its own saved results,
with **Back**, **Forward** and a trail of the notebooks read; **Open in its own
preview** opens it where it can be checked. To use another notebook's results,
import it like any Lean module (`import Basics`); it must be a module of the
same Lake project and built with `lake build`.

### Optional directives

Standalone comment lines can request extra views of objects defined earlier in
the notebook, such as `-- @view circuit myCircuit`. Views are drawn by optional
provider packages (see [Extensions](#extensions)); without one, the directive
reports an error instead of drawing.

`-- @evidence <verb> <target> (options)` shows a simulation of an object defined
earlier as a card with a chart, provenance and a **Run** button, for example
`-- @evidence simulate dissipative (h := 1/100) (steps := 400)`. It runs only
when you click **Run**, one run at a time, and the result is saved in
`<Notebook>.glitnir.json`. A card is Never run, Running, Current or Out of date
(anything above the directive, or the environment, changed). Evidence is never
proof: it changes no claim's status and not the check exit code. Without a
provider the line shows why, and saved results stay viewable.

`-- @decide <name> : <goal>` asks an installed decision provider for a Lean proof
(or refutation) of a goal; continuation lines start with `--` and at least two
spaces:

```lean
-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1
-- @decide energy_band : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V >= 0 and V <= 6
```

It becomes a card, and nothing runs until you click **Run** (one provider run at
a time, evidence included). The provider proposes a Lean block; Glitnir checks
the saved text up to the directive plus that block with its own sandboxed
kernel check, which is the only proof authority. The card shows its status
(Never run, Running, Unknown, Rejected, Candidate, Accepted or Out of date), the
stages, the statement in Lean's kernel form (for example
`Eq (instHAdd.hAdd 2 2) 4`), the axioms and the full block. A proven block
declares `theorem <name>`; a refutation declares `theorem <name>_refuted : ¬ (…)`.
The provider is trusted only for matching the theorem to your goal: the kernel
guarantees the theorem, so read the statement before accepting.

- **Accept** (VS Code preview only, only for a checked candidate) inserts the
  block right after the directive through the editor and saves the file. It is
  refused while the notebook has unsaved edits, or when the editor's text is not
  the saved file the backend reads. Saved edits below the directive since the
  run keep the candidate acceptable; an edit above it (or an environment change)
  makes it Out of date, and it must be run again. Accept is the only path that
  writes Lean source; the backend never does.
- **Discard** removes the saved result (refused while that decision runs).
- **Stop** cancels the run, also with unsaved edits. A provider whose `decide`
  ignores Stop blocks other provider runs until the preview closes.

Results are saved in `<Notebook>.glitnir.json` under `decisions`, with the same
16 MiB and unreadable-file rules as evidence. Without a provider the line shows
why.

### Checking

A notebook is ordinary Lean that builds with its Lake project (or, for a loose
file, the standard library). Checks are explicit and cover the whole saved file,
or, with **Check through here** on a claim (also in the inspector, or `r` on the
selected claim), only its lines 1 through that claim's last line: the same
sandboxed compile and kernel replay of that prefix. Every checked prefix, also
the one before a failing command, ends where Glitnir's own lexer finds a command
end: the next code is a word that never occurs inside a command (a declaration
keyword, `namespace`, `section`, `end`, `variable`, `private`, … at any column),
or at column 0 `open`/`set_option` (not their `… in` forms), `#print`, a module
doc, or a doc comment or `@[…]` followed by a command word; or the file ends. A
`mutual … end` block is one command. A cut inside a
command moves back to the end of the command before it, so no command is ever
checked shortened. Claims after it are not checked: each keeps its earlier
saved result, marked "Not rechecked", current only while the environment it was
checked in and the source through its last line still match. The next check of
the whole file replaces every result.
Results are saved beside the notebook in `<Notebook>.glitnir.json`; a result goes
out of date when anything above the claim changes (any edit above it, not only
to declarations it uses), when an edit below it continues its command (another
proof step, `where`, `termination_by`), or the toolchain, `lake-manifest.json`, a rebuilt
import or a dependency checkout changes. If a command fails, only the claims
from that command onward are withheld. See the
[authoring skill](skills/glitnir-notebooks/SKILL.md) for the full rules.

Besides declarations, `namespace`/`section`/`end`, `open`, `export`, `variable`,
`set_option`, `attribute` and module docs, a notebook may use `#print axioms c`.
It only prints and decides nothing: statuses come from the kernel replay's own
axiom lists, described below. A `#print axioms` of a missing name fails like any
command, and one naming a lemma Lean generates on demand (such as `f.eq_1`)
withholds the check from that line, because Lean would add that lemma to the
module. Every other command, including other `#print` forms, `#check` and
`#eval`, withholds the check from that line.

An `example` whose type is a proposition is a claim, shown as "example (line N)"
with its **Checked statement**. Lean discards examples, so the checker compiles
each as a private theorem with a reserved name, which stays in the checked module
like any other declaration. Its status is about that theorem's statement, which
the card shows as Lean prints it where the example stands (its namespace, `open`s
and this project's notation, which is project code), with the kernel form, every
name in full and without notation, one fold away; both are display only. Results saved before this (format `v1`) still
load and show only the kernel form. The checker finds every `example` keyword
with its own lexer, replaces only those keywords (the rest of the file is compared
byte for byte, and lines keep their numbers), and the theorem is then bound,
replayed and given its status like any other. The helper that reads the file runs
its code and only decides which examples are claims; it cannot choose what is
compiled. Section variables are included as for a theorem (the ones the statement
mentions, plus instances), not as Lean includes them for an example (also the
ones its proof uses), so state any hypothesis the proof uses. An example without a
type, or whose type is not a proposition, claims nothing and is marked "not a
claim". Examples that use `decl_name%`, and examples in a file the lexer cannot
read exactly (for example, a string containing `{`), withhold the check from that
example. A file with a carriage return left after CRLF normalisation is refused.

Each result also records the claims it uses: the theorems its statement and proof
name directly, from this notebook or from another notebook of the project (a
module the project builds itself, not a dependency). Cards show them as chips,
the sidebar draws them as the claim graph, and a chip of another notebook opens
it in the same panel at that claim. Uses are display only and never change a
status. Lean leaves out lemmas a tactic uses only by unfolding (`rfl` lemmas in
`simp`), so a claim may use more than its chips show.

Every check replays the notebook's own declarations in Lean's kernel on top of
its compiled imports. The first check of a set of imports also replays every
imported declaration (all of Mathlib, for a Mathlib notebook) into an empty
environment, as a separate step with its own 60-minute limit ("Replaying imported
modules once; later checks reuse it"); the check's own time limit starts after it.
Glitnir records each successful replay of the imports in your user cache
(`~/Library/Caches/glitnir/replay` on macOS, `$XDG_CACHE_HOME/glitnir/replay` or
`~/.cache/glitnir/replay` elsewhere), keyed by the digests of the toolchain, the
replay program and every module file of the import closure. A larger closure
that contains a recorded one with identical module files (a notebook importing
Mathlib and one more module, after one importing Mathlib) replays only its other
modules' declarations, on top of the recorded ones, and is recorded in turn
("imports cached (+N modules)"). When one of those other modules restates a
recorded theorem and its copy wins Lean's merge of the two (it is loaded later
and is not identical), all of its imports are replayed instead; an identical
or earlier restatement keeps the incremental replay. Building on a recorded
replay holds both rebuilt environments in memory at once: for Mathlib about
4.7 GB, within the 8 GB default of `glitnir.memoryLimitMb`.
A result's `replay` field is `full`, `imports
cached`, or `imports cached (+N modules)`. The notebook's own declarations are replayed
either way, in an environment rebuilt from nothing but the replayed imports'
declarations (no compiled code from the project or its dependencies), which the
first replay verified equals its own. When the imports do not hold the kernel's
own quotient (`init_quot`), or a notebook restates an imported theorem, the whole
closure is replayed from an empty environment instead. Delete the cache directory
to force full replays. Anyone who can write to it can forge a record, which is the
same trust as being able to change your files.

Each check reads the files it depends on (the toolchain, compiled imports and
project sources) once, hashes them, and links its private snapshot to a copy of
the toolchain's and compiled imports' exact bytes in a store beside the replay
cache (`glitnir/artifacts`),
named by their SHA-256 and never changed once written. Checks with the same
dependencies share these files instead of copying gigabytes each time. Lean sees
only the toolchain's files and its own project's, read-only; project sources
are hashed but never stored. The least recently used files are removed when the
store exceeds 40 GiB or would leave less free space than 10 GiB (or a tenth of
the disk, if that is less), never while a check uses them. Delete it at any time to free the space.

A theorem's status comes from the axioms it depends on: `checked` when they are
all among Lean's standard axioms and declared by the toolchain's own modules,
`admitted` when they include `sorryAx`, `assumptions` otherwise. The kernel
replay collects these axioms itself, in the environment where it has just
checked the notebook's declarations, walking every dependency (imported lemmas
included) from its checked value rather than from summaries stored in compiled
files. The separate module report lists them too, for display; when the two
lists differ the check is unavailable rather than trusting either. Upgrading to
a version that changes the replay program invalidates saved results once (they
show as out of date) and the recorded import replays, so the first check after
an upgrade replays the imports again.

## The VS Code extension

The extension opens any `.lean` file as a rendered notebook beside your editor. Main advantages and features of the extension:

- **Reading:** prose, typeset mathematics, images and collapsible sections,
  with each claim's statement, proof and check status in its own card.
- **Source navigation:** **Edit in VS Code** jumps from a cell to its source, and
  **Glitnir: Reveal in Glitnir** jumps back from the cursor to the notebook.
- **Live updates:** the preview refreshes whenever the file is saved, from VS Code
  or a coding agent, and briefly highlights changed sections. **Follow edits**
  scrolls to them.
- **Claim review:** **Next** and **Previous** step through the claims that need
  attention, in source order: out of date, not rechecked, containing `sorry`,
  resting on additional assumptions, or where checking stopped.
- **Proof steps:** **Explore Proof Steps** records a proof's tactic tree once, then
  lets you walk it with the goals before and after each step.
  **Inspect proof goals** in the claim inspector shows the goals after each tactic
  step of the selected claim from the same recording. Both are historical
  observations and never change a status.
- **Checking:** **Check saved file** checks the whole file and saves each claim's
  result to `<Notebook>.glitnir.json`. Claims show as cards marked current or out
  of date. If a command fails, claims before it are still checked; later claims
  show as plain source under a "Checking stopped at line N" banner.
  **Copy Validation Command** gives the equivalent command-line check.
- **Evidence:** `-- @evidence` directives show simulations as cards with a
  chart and provenance, run on click and saved with the check results.
- **Decisions:** `-- @decide` directives show as cards. **Run** asks a provider
  for a Lean block, Glitnir kernel-checks it, and **Accept** inserts a checked
  block into the notebook.
- **History:** recent Git commits for the notebook, with each commit's diff.
- **Export HTML:** saves an offline snapshot of the rendered notebook.
- **Coding agents:** **Set Up Coding Agent** installs the authoring skill for
  Claude Code or Codex.

Checking, proof-step recording and evidence runs happen only when you ask. Lean
runs in the sandbox; an evidence provider computes from its output.

### Keyboard shortcuts

Single keys work while the preview has focus, outside text fields; press **?** in
the preview for this list. Moving never starts a check.

<!-- shortcuts:start (a test keeps this table equal to the preview's list) -->
| Keys | Action |
|---|---|
| `j` `k` | Next / previous claim |
| `n` `p` | Next / previous claim that needs attention: out of date, not rechecked, contains sorry, additional assumptions, or where checking stopped. In Proof steps: next / previous step |
| `c` `t` | Claims / Proof steps |
| `s` | Switch between document and Lean source |
| `i` | Show or hide the claim inspector |
| `r` | Check the saved file through the selected claim (in Proof steps: all of it) |
| `e` | Edit the selected claim or proof step in VS Code |
| `?` | Show these shortcuts |
| `Esc` | Close the dialog; in Proof steps, return to Claims |
| `⌘K` then `G` (macOS), `Ctrl+K` then `G` (Linux) | In the Lean editor: show the cursor’s place in Glitnir. Release the first keys before G |
<!-- shortcuts:end -->

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `glitnir.elaborationTimeoutMs` | `600000` (10 min) | Time limit for checks and proof-step recording, in milliseconds, capped at 30 minutes. It also bounds each Lake run of the project inspection that resolves a notebook's Lake setup before a check, a view or evidence run (`import Mathlib` makes that take minutes), each Lean run of a view, and a view's wait for another Lean run. Inspections and views run in the background and the preview polls them, so none is cut short by a request timeout. Applies to previews opened after the change. |
| `glitnir.memoryLimitMb` | `8192` (8 GB) | Memory limit for Lean checks, proof-step recording, views, evidence and decisions, in megabytes, between 1024 and 32768. Applies to previews opened after the change. |
| `glitnir.pythonPath` | empty | Python with the Glitnir backend installed. Empty finds an existing environment. |

Changing `glitnir.pythonPath` closes open previews; reopen them to apply the
change.

## Install

Requires VS Code, **Python 3.12+** and **uv** on local **macOS or Linux**.

1. Download `glitnir-VERSION.vsix` from [Releases](https://github.com/arnovich/glitnir/releases).
2. In VS Code, run **Extensions: Install from VSIX…**.
3. Open a Lean file in a trusted workspace, then run **Glitnir: Set Up Environment → Install Backend with uv → Download matching backend**.
4. Run **Glitnir: Open in Glitnir**.

Python, Lean and Mathlib are installed separately. Preview needs only the Python
backend; proof checking also needs **Lean 4.32.1**, prepared project dependencies
and a working platform sandbox. Windows and remote VS Code environments are
unsupported.

For the optional Forseti views, evidence and decisions, download
`glitnir_forseti-VERSION-py3-none-any.whl` and `SHA256SUMS` from the same release,
verify the wheel with `shasum -a 256 -c --ignore-missing SHA256SUMS` (Linux:
`sha256sum -c --ignore-missing SHA256SUMS`), then run **Glitnir: Install
Extension Package…** and select it (see [Extensions](#extensions)).

See the [setup guide](extension/media/setup.md) for prerequisites, manual installation,
existing environments and troubleshooting. Use the VSIX, backend wheel and plugin
wheel from the same release.

## Try it

Save this as `Hello.lean`, then run **Glitnir: Open in Glitnir**:

```lean
/-! # A first notebook -/
theorem hello : True := by
  trivial
```

- **Edit in VS Code** jumps to source; **Glitnir: Reveal in Glitnir** returns to the notebook.
- Save edits to refresh the preview. Enable **Follow edits** to follow changed sections.
- Use **Claims** and **Proof steps** to review proofs; press **?** for keyboard shortcuts.
- Use **Check saved file** to check the saved file.
- **Export HTML** saves an offline snapshot.

For more advanced examples explore [Forseti examples](https://github.com/arnovich/forseti-examples) for notebooks
on circuits, dynamical systems and PDEs.

## Extensions

Optional Python packages can add notebook features. They are found through the
`glitnir.providers` entry-point group, so install them into the same Python
environment as the Glitnir backend.

To install one, run **Glitnir: Install Extension Package…** (also offered in
**Glitnir: Set Up Environment**) in a trusted workspace with the backend set up,
and select the package's `.whl` file. Glitnir reads the wheel's metadata without
running it and refuses a file that declares no `glitnir.providers` entry point or
is the backend itself. Pick any optional extras (none are preselected), confirm,
and uv installs the wheel into the backend's Python. Its dependencies, and those
of the chosen extras, come from PyPI as prebuilt wheels; the installed backend
keeps its version. Only extras whose packages Glitnir knows to be published on
PyPI are offered (for now `z3-solver`); others are listed with what to install
yourself. A message then lists the view kinds, evidence verbs and decision kinds
now available (with what is still missing, such as an extra), or the providers
that failed to load (details in **Output → Glitnir**).
Glitnir refuses a wheel that could reach beyond its package: one that touches
the backend's own files, adds a startup hook (`.pth`, `sitecustomize`), names a
command like a path or after another package's command, or needs an extra it
does not offer. Its `entry_points.txt` must be plain `[group]` headers and
`name = module:attribute` lines, without comments or indentation.
It also refuses a wheel whose file names or file list (RECORD) are not plain
ASCII, whose file list does not give each file's SHA-256 hash and size, or
that is not a plain zip archive (ZIP64 records, an archive comment). uv
installs a copy that Glitnir writes from exactly the files it checked. The Set
Up Environment item appears only once a backend is detected.

Glitnir does not remove packages for you: uninstalling deletes whatever the
installed package's file list names, which Glitnir cannot vouch for. To remove
one, check its files, then run `uv pip uninstall --python <backend python>
<name>` and reopen the previews.

The backend installed by Set Up Environment is shared by every window and
workspace using it, so a package change affects them all, while only this
window's previews close; reopen previews in other windows too. If uv fails or is
cancelled, the environment may be partly changed: run the install
again. After an extension update that pins a new backend, Set Up Environment
creates a new environment; install the extension packages into it again.

`glitnir-forseti` (in [`plugins/forseti`](plugins/forseti)) draws Forseti
circuit, model and dynamics views for `-- @view circuit|model|dynamics <name>`
directives. The notebook must sit in a Lake project whose packages build
`Gimle.Forseti` and `Gimle.Asgard.Visualization.Model`; the notebook need not
import the visualization module, which the view loads from the project's build. If that build is missing or out of date,
the card names the `lake build` command to run. A loose `.lean` file has no
prepared imports to draw from. A view is drawn in the background, one at a time:
its card says what it is doing, including waiting for another Lean run, and
**Stop** cancels it.

It also runs `-- @evidence simulate|iterate <target>` directives, loading
`Gimle.Asgard.Compile.RationalInterchange` and `Gimle.Asgard.Model.Continuous`
from the project's build in the same way. `simulate` needs nothing more; `iterate`
needs Python Asgard (`gimle-asgard`), which is not on PyPI: install it into the
backend's environment from its own wheel or checkout. Without it, `iterate` cards
say so and cannot run, and the provider list shows `iterate` as unavailable.
Numbers in options are plain decimals or fractions such as `0.01` or `1/100`.

It also decides `-- @decide <name> : <goal>` directives of kind `entailment`,
`trajectory` or `stream` (the `z3` extra, `glitnir-forseti[z3]`, is needed). Its
blocks need the Forseti and Asgard modules the goal uses imported in the
notebook; a missing one shows as the check's unknown-identifier rejection. See
the [plugin README](plugins/forseti/README.md).

Verified on 2026-10-15 against forseti-lean `c65dada` (Lean 4.32.1): the opt-in
live tests passed for proven and refuted `entailment` and `trajectory` decisions
and for `simulate` evidence. `stream` decisions and `iterate` evidence have not
been run through Glitnir's checker yet. CI does not run these tests
(`plugins/forseti/tests/test_decision_provider_lean.py` and
`test_evidence_provider_lean.py`, with `FORSETI_LEAN_TEST_PROJECT` set), so
re-run them after Forseti or Glitnir changes. The first check of a Forseti
project replays Mathlib and Forseti once (about 18 minutes on a laptop); later
checks take a few minutes.

`glitnir-forseti` is not on PyPI. Each release has its wheel, built from the same
commit and tested with that release's backend:

1. Download `glitnir_forseti-VERSION-py3-none-any.whl` and `SHA256SUMS` from the
   same release as the VSIX, and verify it with
   `shasum -a 256 -c --ignore-missing SHA256SUMS` (Linux:
   `sha256sum -c --ignore-missing SHA256SUMS`). From a checkout, build it instead:
   `uv build plugins/forseti --wheel --out-dir plugins/forseti/dist`.
2. Run **Glitnir: Install Extension Package…**, select the wheel, and choose the
   `z3` extra for decisions.
3. For `iterate` evidence, install `gimle-asgard` into the backend yourself, for
   example `uv pip install --python <backend python> <gimle-asgard wheel>`.

To install by hand instead, find the backend's Python (shown in **Output →
Glitnir** as "Glitnir environment: …", or set by the `glitnir.pythonPath`
setting) and install the wheel into it, with the extras you need. Its `lark`
dependency comes from the package index; the installed backend satisfies its
`glitnir` dependency. Then close and reopen the notebook preview:

```sh
uv pip install --python <backend python> \
  "./glitnir_forseti-VERSION-py3-none-any.whl[z3]"
```

The core backend has no runtime dependencies.

## Coding agents

Run **Glitnir: Set Up Coding Agent**, choose your workspace and agent, then start
a new agent session and ask it to use `glitnir-notebooks`.
See the bundled [authoring skill](skills/glitnir-notebooks/SKILL.md).

## Development

The repository has four parts:

- [`backend/`](backend/README.md): the Python backend, including the notebook
  renderer and the checking services.
- [`plugins/forseti/`](plugins/forseti/README.md): the optional `glitnir-forseti`
  provider package.
- `extension/`: the VS Code extension, with its source, webview assets,
  build scripts and tests.
- [`skills/`](skills/glitnir-notebooks/SKILL.md): the authoring skill for coding
  agents.

With Python 3.12+, uv and Node 22+, run from the repository root:

```sh
uv build backend --wheel --out-dir backend/dist
uv venv --python 3.12 --no-python-downloads .venv
uv pip install --python .venv/bin/python -c backend/requirements-dev.lock \
  'backend/dist/glitnir-0.1.0-py3-none-any.whl[dev]'
(cd backend && ../.venv/bin/python -m pytest && ../.venv/bin/black --check src tests \
  && ../.venv/bin/isort --check-only src tests && ../.venv/bin/flake8 src tests \
  && ../.venv/bin/mypy src)

# Optional: the Forseti view and evidence provider and its tests (as in CI). Its dependencies
# are named explicitly because glitnir is not on an index.
uv build plugins/forseti --wheel --out-dir plugins/forseti/dist
uv pip install --python .venv/bin/python -c backend/requirements-dev.lock lark z3-solver
uv pip install --python .venv/bin/python --no-deps \
  plugins/forseti/dist/glitnir_forseti-0.1.0-py3-none-any.whl
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
`plugins/forseti/src`. Live Lean tests skip locally unless the environment
variables in [AGENTS.md](AGENTS.md) are set; CI runs them in a separate job.
`pytest -m "not live"` leaves them out.

Release instructions for maintainers are in [AGENTS.md](AGENTS.md#releases).

## Help and license

Check **Output → Glitnir**, then [report an issue](https://github.com/arnovich/glitnir/issues)
with your OS, Glitnir version and a minimal example. Report vulnerabilities
[privately](SECURITY.md).

[MIT licensed](LICENSE), with [third-party notices](THIRD_PARTY_NOTICES.md).
Pull requests are limited to [invited collaborators](CONTRIBUTING.md).
