# Glitnir user guide

This guide describes the notebook format, the preview, checking, settings,
keyboard shortcuts and extension packages in full. The [README](../README.md) is the
short version. Installation, upgrading and troubleshooting are in the
[setup guide](../extension/media/setup.md).

## The notebook format

A Glitnir notebook is a `.lean` file that builds with standard Lean. Glitnir
reads it as *cells*:

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
provider packages (see [Extensions](#extension-packages)); without one, the directive
reports an error instead of drawing.

`-- @evidence <verb> <target> (options)` shows a simulation of an object defined
earlier as a card with a chart, provenance and a **Run** button, for example
`-- @evidence simulate dissipative (h := 1/100) (steps := 400)`, or sampled
paths of a stepped model under random shocks (`-- @evidence sample`). It runs
only when you click **Run**, one run at a time, and the result is saved in
`<Notebook>.glitnir.json`. A card is Never run, Running, Current, Failed or Out
of date (anything above the directive, or the environment, changed). A failed
run is saved too when it failed in Lean, in the provider, with output that is
not evidence, or at its time limit: the card then reads Failed, or keeps its
last chart with a note that the last run failed. A provider crash is saved as
its exception type only. Failures of the setup, such as an unbuilt module or a
missing Python dependency, are shown but not saved. While the preview is open,
running a directive again after editing only its options (or the lines below it)
reuses what Lean printed for it before, as long as the source above it and the
environment are unchanged: the run takes seconds, so a corrected option, or one
the provider still refuses, such as a state count that does not match the
circuit, is reported at once. Lean's first run of a directive cannot be skipped:
only it shows the target's shape. Evidence is never proof: it changes no claim's status
and not the check exit code. Without a
provider the line shows why, and saved results stay viewable.

Views are drawn with the notebook's proofs skipped: theorem and lemma proofs are
replaced by `sorry` and examples are skipped, so a drawing does not re-run them.
If that draws nothing, Glitnir draws with the full notebook. A freshly drawn
card that skipped the proofs says "Drawn without re-running the notebook's
proofs." Evidence runs are elaborated the same way. A drawing never grants or
changes a claim's status. Drawings are saved in `<Notebook>.glitnir.json` (the
`views` section, at most 4 MiB) and reused without running Lean while the source
through the directive, the environment and the provider are unchanged. A reused
drawing does not show the note, even if it was drawn without the proofs. If
saving a check, evidence or a decision would make the file larger than 16 MiB,
the saved drawings are removed from it first and drawn again when needed.
Commit that file to keep a gallery of the notebook free of Lean for views.

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
(Never run, Running, Unknown, Rejected, Candidate, Accepted, Failed or Out of
date), the stages, the statement in Lean's kernel form (for example
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
- **Discard** removes the saved result and any saved failure (refused while
  that decision runs).
- **Stop** cancels the run, also with unsaved edits. A provider whose `decide`
  ignores Stop blocks other provider runs until the preview closes.

Results are saved in `<Notebook>.glitnir.json` under `decisions`, with the same
16 MiB and unreadable-file rules as evidence. A block the kernel check rejects
is such a result (Rejected), and so is Unknown. A run that fails before there
is a result is saved too, as for evidence, when the provider crashed or refused
the goal, returned an outcome that is not a decision of the directive, or the
candidate check reached its time limit: the card then reads Failed, or keeps
its saved result with a note that the last run failed. A provider crash is
saved as its exception type only; setup failures (a missing import, Lake, the
sandbox, a missing Python dependency such as Z3), a busy checker and Stop are
shown but not saved. Without a provider the line shows why.

## Using the preview

### Reading

Run **Glitnir: Open in Glitnir** from the Command Palette, or **Open in Glitnir**
on a `.lean` file in the Explorer's context menu. The preview opens beside the
editor and shows the saved file, not unsaved edits. **Contents** lists the
sections, and `s` switches between the rendered document and the Lean source.

- **Edit in VS Code** on a cell jumps to its source. **Glitnir: Reveal in
  Glitnir** (the editor title button, the editor context menu, or `⌘K G` on
  macOS and `Ctrl+K G` on Linux) shows the cursor's place in the preview.
- Every save, from VS Code or a coding agent, refreshes the preview and briefly
  highlights the changed sections. **Follow edits** scrolls to them.
- **Claims** (`c`) steps through the claims with `j` and `k`; `i` shows the claim
  inspector for the selected claim.
- `n` and `p`, or **Glitnir: Next Review Item** and **Glitnir: Previous Review
  Item**, step through the claims that need attention: out of date, not
  rechecked, containing `sorry`, resting on additional assumptions, or where
  checking stopped.

### Proof goals and steps

**Glitnir: Explore Proof Steps** (or `t` in the preview) records the selected
proof's tactic tree once with Lean, then lets you walk it with the goals before
and after each step: `n` and `p` move between steps, `r` checks the whole saved
file, and `Esc` returns to Claims. **Inspect proof goals** in the claim inspector
shows the goals after each tactic step of the selected claim from the same
recording. Recording runs Lean in the sandbox within `glitnir.elaborationTimeoutMs`.
Both are historical observations of the saved file and never change a status.

### Linked notebooks

A Markdown link to a sibling notebook, such as `[Basics](Basics.lean)`, opens it
in the same panel, read-only and with its own saved results; see [Images and
links](#images-and-links). A claim chip of another notebook opens it at that
claim. Use **Open in its own preview** to check it.

### Export and history

- **Export HTML** saves an offline snapshot of the rendered notebook, with its
  images embedded.
- **History** lists recent Git commits of the notebook, each with its diff.

A gallery can also publish notebooks from their committed saved results without
running any checks; see [Static export](https://github.com/arnovich/glitnir/blob/main/DEVELOPMENT.md#static-export-galleries)
in DEVELOPMENT.md.

## Checking

A notebook is ordinary Lean that builds with its Lake project (or, for a loose
file, the standard library). Checks are explicit: **Check saved file** checks the
whole saved file, and nothing is checked when you only move around.
Glitnir checks one Lean version, today 4.35.0. A notebook whose project is on
another version still opens and reads, with a banner at the top saying that
checking, proof steps, views, evidence and decisions are off; its saved results
show, marked out of date, and none of those controls is offered. A loose file with
no declared version counts as 4.35.0. Glitnir adopts new Lean versions by moving
that one version forward.
Results are saved beside the notebook in `<Notebook>.glitnir.json`; a result goes
out of date when anything above the claim changes (any edit above it, not only
to declarations it uses), when an edit below it continues its command (another
proof step, `where`, `termination_by`), or the toolchain, `lake-manifest.json`, a rebuilt
import or a dependency checkout changes. Where the project lives does not count:
a committed `.glitnir.json` stays current in another clone or on another machine
whose sources, dependencies and compiled imports are byte-identical, and reads
as out of date otherwise. If a command fails, only the claims
from that command onward are withheld, and they show as plain source under a
"Checking stopped at line N" banner. See the
[authoring skill](https://github.com/arnovich/glitnir/blob/main/skills/glitnir-notebooks/SKILL.md)
for the full rules.

**Glitnir: Copy Validation Command** copies the equivalent command-line check of
the saved file, for a terminal or a coding agent. It runs
`python -m glitnir.notebook check FILE.lean` with the backend's Python, prints
one JSON result and writes no notebook metadata. It exits 0 when every theorem is
kernel checked, 1 when some contain `sorry` or extra assumptions or when a
failing command withheld the claims after it, and 2 when checking is unavailable.

### Checking through a claim

**Check through here** on a claim (also in the inspector, or `r` on the selected
claim) checks only lines 1 through that claim's last line: the same sandboxed
compile and kernel replay of that prefix. Every checked prefix, also the one
before a failing command, ends where Glitnir's own lexer finds a command end: the
next code is a word that never occurs inside a command (a declaration keyword,
`namespace`, `section`, `end`, `variable`, `private`, … at any column), or at
column 0 `open`/`set_option` (not their `… in` forms), `#print`, a module doc,
or a doc comment or `@[…]` followed by a command word; or the file ends. A
`mutual … end` block is one command. A cut inside a command moves back to the
end of the command before it, so no command is ever checked shortened. Claims
after it are not checked: each keeps its earlier saved result, marked "Not
rechecked", current only while the environment it was checked in and the source
through its last line still match. The next check of the whole file replaces
every result.

### What a check accepts

Named theorems may use `set_option ... in`, including nested options. The option
applies only to that theorem, and its checked source range includes the whole
wrapper. A partial check cannot split the wrapper from the theorem or shorten its
proof. Other scoped commands, including `open ... in` and scoped anonymous
`example` declarations, remain unsupported and withhold the check from there.

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
name in full and without notation, one fold away; both are display only. Results
saved before this (format `v1`) still load and show only the kernel form. The
checker finds every `example` keyword with its own lexer, replaces only those
keywords (the rest of the file is compared byte for byte, and lines keep their
numbers), and the theorem is then bound, replayed and given its status like any
other. The helper that reads the file runs its code and only decides which
examples are claims; it cannot choose what is compiled. Section variables are
included as for a theorem (the ones the statement mentions, plus instances), not
as Lean includes them for an example (also the ones its proof uses), so state any
hypothesis the proof uses. An example without a type, or whose type is not a
proposition, claims nothing and is marked "not a claim". Examples that use
`decl_name%`, and examples in a file the lexer cannot read exactly (for example,
a string containing `{`), withhold the check from that example. A file with a
carriage return left after CRLF normalisation is refused.

### Statuses

A theorem's status comes from the axioms it depends on: `checked` when they are
all among Lean's standard axioms and declared by the toolchain's own modules,
`admitted` when they include `sorryAx`, `assumptions` otherwise. The kernel
replay collects these axioms itself, in the environment where it has just
checked the notebook's declarations, walking every dependency (imported lemmas
included) from its checked value rather than from summaries stored in compiled
files. The separate module report lists them too, for display; when the two
lists differ the check is unavailable rather than trusting either. A proof by
`native_decide` rests on an axiom Lean adds to the module that uses
`native_decide`, so its status is `assumptions`.

### Imports and caches

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
4.7 GB, within the 16 GB default of `glitnir.memoryLimitMb`.
A result's `replay` field is `full`, `imports cached`, or `imports cached (+N
modules)`. The notebook's own declarations are replayed either way, in an
environment rebuilt from nothing but the replayed imports' declarations (no
compiled code from the project or its dependencies), which the first replay
verified equals its own. When the imports do not hold the kernel's own quotient
(`init_quot`), or a notebook restates an imported theorem, the whole closure is
replayed from an empty environment instead. Delete the cache directory to force
full replays. Anyone who can write to it can forge a record, which is the same
trust as being able to change your files.

Each check reads the files it depends on (the toolchain, compiled imports and
project sources) once, hashes them, and links its private snapshot to a copy of
the toolchain's and compiled imports' exact bytes in a store beside the replay
cache (`glitnir/artifacts`), named by their SHA-256 and never changed once
written. Checks with the same dependencies share these files instead of copying
gigabytes each time. Lean sees only the toolchain's files and its own project's,
read-only; project sources are hashed but never stored. The least recently used
files are removed when the store exceeds 40 GiB or would leave less free space
than 10 GiB (or a tenth of the disk, if that is less), never while a check uses
them. Delete it at any time to free the space.

Upgrading to a version that changes the replay program invalidates saved results
once (they show as out of date) and the recorded import replays, so the first
check after an upgrade replays the imports again. A version that changes how the
environment is identified shows saved results as out of date once, too.

### Uses and the claim graph

Each result also records the claims it uses: the theorems its statement and proof
name directly, from this notebook or from another notebook of the project (a
module the project builds itself, not a dependency). Cards show them as chips,
the sidebar draws them as the claim graph, and a chip of another notebook opens
it in the same panel at that claim. Uses are display only and never change a
status. Lean leaves out lemmas a tactic uses only by unfolding (`rfl` lemmas in
`simp`), so a claim may use more than its chips show.
## Settings

| Setting | Default | Purpose |
|---|---|---|
| `glitnir.elaborationTimeoutMs` | `600000` (10 min) | Time limit for checks and proof-step recording, in milliseconds, capped at 30 minutes. It also bounds the project inspection that resolves a notebook's Lake setup before a check, a view or evidence run (`import Mathlib` makes that take minutes), each Lean run of a view, and the wait of a check, view or evidence run for another Lean run to finish: one runs at a time, and the others wait their turn rather than fail. Inspections and views run in the background and the preview polls them, so none is cut short by a request timeout; **Stop** cancels either. Applies to previews opened after the change. |
| `glitnir.memoryLimitMb` | `16384` (16 GB) | Memory limit for Lean checks, proof-step recording, views, evidence and decisions, in megabytes, between 1024 and 32768. Unless set, Glitnir uses 16384, or 2048 less than the machine's memory when that is smaller. Applies to previews opened after the change. |
| `glitnir.pythonPath` | empty | Python with the Glitnir backend installed. Empty finds an existing environment. |
| `glitnir.offerBackendUpdate` | `true` | After an extension update, offer once to set up the new version's backend and carry over extension packages (see [Upgrading](../extension/media/setup.md#upgrading)). Nothing downloads until you click **Set Up**. |

Changing `glitnir.pythonPath` closes open previews; reopen them to apply the
change.

## Keyboard shortcuts

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

The Command Palette also has **Glitnir: Review Claims**, **Glitnir: Explore Proof
Steps**, **Glitnir: Next Review Item**, **Glitnir: Previous Review Item** and
**Glitnir: Keyboard Shortcuts**.

## Extension packages

Optional Python packages can add notebook features. They are found through the
`glitnir.providers` entry-point group, so install them into the same Python
environment as the Glitnir backend.

### Installing extension packages

To install one, run **Glitnir: Install Extension Package…** (also offered in
**Glitnir: Set Up Environment**) in a trusted workspace with the backend set up:

1. **Pick a package.** The list shows the packages Glitnir knows about (for now
   **Forseti**: circuit, model and dynamics diagrams, simulation evidence and
   decision procedures), marked "installed (version)" when they are, and last
   **Other wheel file…** for third-party or development wheels.
2. **Choose the source** of a known package. **Download from this release
   (recommended)** fetches the wheel the extension pins from the GitHub release
   its backend comes from and installs it only if its SHA-256 equals the one the
   extension records; the release's `SHA256SUMS` and the plugin binding in
   `glitnir-candidate.json` must bind the same bytes, or it is refused. A build
   packaged without the plugin wheel does not offer it. **Choose downloaded
   wheel** selects a file, which must be that package's wheel; the confirmation
   says whether it is identical to the release's or a local wheel. For an
   installed package this reinstalls or updates it.
3. **Answer its questions**, such as "Install z3 for decision procedures? (adds
   the z3-solver package from PyPI)", asked only for extras the wheel offers.
   Notes say what cannot be installed from here.
4. **Confirm**, and Glitnir installs it and lists the providers now available.

The list of known packages is data only and grants no trust: every wheel goes
through the same checks, and a downloaded one must be the wheel the extension
pins. With **Other wheel file…**, select the package's `.whl` file. Glitnir reads the wheel's metadata without
running it and refuses a file that declares no `glitnir.providers` entry point or
is the backend itself. Pick any optional extras (none are preselected), confirm,
and uv installs the wheel into the backend's Python. Its dependencies, and those
of the chosen extras, come from PyPI as prebuilt wheels; the installed backend
keeps its version. Only extras whose packages Glitnir knows to be published on
PyPI are offered (for now `z3-solver`); others are listed with what to install
yourself. A message then lists the view kinds, evidence verbs and decision kinds
now available (with what is still missing, such as an extra), or the providers
that failed to load (details in **Output → Glitnir**). Glitnir refuses a wheel
that could reach beyond its package, such as one that touches the backend's own
files or adds a startup hook; [DEVELOPMENT.md](https://github.com/arnovich/glitnir/blob/main/DEVELOPMENT.md#extension-packages)
lists the exact rules. The Set Up Environment item appears only once a backend
is detected.

Glitnir does not remove packages for you: uninstalling deletes whatever the
installed package's file list names, which Glitnir cannot vouch for. To remove
one, check its files, then run `uv pip uninstall --python <backend python>
<name>` and reopen the previews.

The backend installed by Set Up Environment is shared by every window and
workspace using it, so a package change affects them all, while only this
window's previews close; reopen previews in other windows too. If uv fails or is
cancelled, the environment may be partly changed: run the install
again. After an extension update that pins a new backend, **Set Up** in the
update notification (or **Glitnir: Set Up Backend for This Version**) creates a
new environment and carries over the packages published with the release; see
[Upgrading](../extension/media/setup.md#upgrading).

To install by hand instead, find the backend's Python (shown in **Output →
Glitnir** as "Glitnir environment: …", or set by the `glitnir.pythonPath`
setting) and install the wheel into it, with the extras you need. Then close and
reopen the notebook preview:

```sh
uv pip install --python <backend python> \
  "./glitnir_forseti-VERSION-py3-none-any.whl[z3]"
```

### Forseti: views, evidence and decisions

`glitnir-forseti` ([install it](../extension/media/setup.md#the-forseti-plugin); source in
[`plugins/forseti`](https://github.com/arnovich/glitnir/tree/main/plugins/forseti))
provides three features.

**Views.** It draws Forseti circuit, model and dynamics views for
`-- @view circuit|model|dynamics <name>` directives. The notebook must sit in a
Lake project whose packages build `Gimle.Forseti` and
`Gimle.Asgard.Visualization.Model`; the notebook need not import the
visualization module, which the view loads from the project's build. If that
build is missing or out of date, the card names the `lake build` command to run.
A loose `.lean` file has no prepared imports to draw from. A view is drawn in the
background: a notebook's views of this provider are drawn together in one Lean
run, and a card says what it is doing, including waiting for another Lean run.
If that run times out, each of its views is drawn in a run of its own when asked
for again. **Stop** on a card drops only that view. Views and evidence reuse the
prepared Lake setup while the environment is unchanged (checks never do). On
macOS the Mac is kept awake while Glitnir's checks, proof steps, views and
evidence run Lean or Lake; decision runs are not covered.

**Evidence.** It runs `-- @evidence simulate|iterate|sample <target>`
directives, loading `Gimle.Asgard.Compile.RationalInterchange` and
`Gimle.Asgard.Model.Continuous` from the project's build in the same way.
`simulate` needs nothing more; `iterate` needs Python Asgard (`gimle-asgard`),
which is not on PyPI: install it into the backend's environment from its own
wheel or checkout. Without it, `iterate` cards say so and cannot run, and the
provider list shows `iterate` as unavailable. `sample` draws seeded shock paths
in float64 through a stepped model the plugin knows: its target is
`Gimle.Forseti.Examples.HedgedSpread.compiled`, and the notebook must import
`Gimle.Forseti.Examples.HedgedSpreadStepped`, which defines its step. It needs
nothing more, requires `(law := gaussian|twopoint|threepoint)`,
`(variance := …)` and `(observe := …)`, and takes `paths` (4000, at most
100000), `steps` (256), `seed` (7), `show` (8, or `paths` if fewer) and
`barrier` (1) as options. Its card is sampled evidence beside a proved bound,
never the bound.
Numbers in options are plain decimals or fractions such as `0.01` or `1/100`.

**Decisions.** It decides `-- @decide <name> : <goal>` directives of kind
`entailment`, `trajectory`, `stream` or `stepped` (the `z3` extra,
`glitnir-forseti[z3]`, is needed). Its blocks need the Forseti and Asgard modules
the goal uses imported in the notebook; a missing one shows as the check's
unknown-identifier rejection. `stepped` decisions and `sample` evidence need a
Forseti Lean build that contains `Gimle.Forseti.Stepped` and
`Gimle.Forseti.Examples.HedgedSpreadStepped`, as the pinned release, Forseti Lean
v0.4.1, does; an older build reports the missing module.

`entailment`, `trajectory` and `stepped` decisions and `simulate` and `sample`
evidence pass Glitnir's check in the plugin's live tests against Forseti Lean
v0.4.1 on Lean 4.35.0 (Linux CI, 2026-10-25, run 40217643283). `stream`
decisions and `iterate` evidence were run through Glitnir's checker locally on
macOS on 2026-10-23 (Glitnir 0.1.5, Lean 4.35.0); CI covers `stream` decisions
only, as `iterate` needs Python Asgard. The first check of a Forseti project replays Mathlib and
Forseti once (about 18 minutes on a laptop); later checks take a few minutes. The
[plugin README](https://github.com/arnovich/glitnir/blob/main/plugins/forseti/README.md)
lists every option and goal form.

## Coding agents

Run **Glitnir: Set Up Coding Agent**, choose your workspace and agent, then start
a new agent session and ask it to use `glitnir-notebooks`.
See the bundled [authoring skill](https://github.com/arnovich/glitnir/blob/main/skills/glitnir-notebooks/SKILL.md).

The command installs the skill into the workspace, keeping any edits you made to
an earlier copy: **Codex** uses `.agents/skills/glitnir-notebooks` and **Claude
Code** uses `.claude/skills/glitnir-notebooks`. **Export for another agent**
writes it to a folder you choose; give its `SKILL.md` to your agent. Agents can
check a notebook with the command from **Glitnir: Copy Validation Command**.

