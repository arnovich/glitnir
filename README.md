# Glitnir: A simple notebook format for Lean

[![Visual Studio Marketplace](https://img.shields.io/badge/VS%20Code%20Marketplace-install-007ACC)](https://marketplace.visualstudio.com/items?itemName=gimle.glitnir)

Glitnir is a simple notebook format that makes it easier for a standard human mathematician to follow agent-generated proofs in Lean.

A notebook is an ordinary `.lean` file: the prose lives in a specific Markdown format inside its `/-! … -/` comment blocks, so it builds with Lean like any other file.

Glitnir is also this VS Code extension, which renders and checks notebooks beside their source, and an authoring skill that teaches coding agents such as Claude Code or Codex to write Lean in this style.

See [example notebooks](https://arnovich.github.io/forseti-examples/).

## The Origin

I built Glitnir to fit how I work with Lean and coding agents.

My usual setup is VS Code, split into three panes: one for the Lean code itself, one for the Glitnir rendering and one for the coding agents. As the agents work, the Glitnir window updates live and highlights the newest changes, so I can follow their reasoning, intervene when they go the wrong way, or, more often, ask them to explain something better in the notebook or dig deeper into something.

Here is what my setup typically looks like:

![VS Code with a Lean notebook's source on the left, its Glitnir preview in the middle and a coding agent on the right](https://raw.githubusercontent.com/arnovich/glitnir/main/docs/images/glitnir-setup.png)

Generally I am not a believer in new agent harnesses for math. I think coding agents, with the right skills and tools, are the perfect agents for mathematical research. So that is what I am trying to improve with Glitnir.

## The VS Code extension

The extension opens any `.lean` file as a rendered notebook beside your editor.
Names written **Glitnir: …** are Command Palette commands; other bold names are
buttons in the preview.

- **Reading:** prose, typeset mathematics, images and collapsible sections, with
  each claim's statement, proof and check status in its own card.
- **Live updates:** every save, from VS Code or a coding agent, refreshes the
  preview and briefly highlights the changed sections.
- **Source navigation:** **Edit in VS Code** jumps from a cell to its source, and
  **Glitnir: Reveal in Glitnir** jumps back from the cursor to the notebook.
- **Claim review:** **Next** and **Previous** step through the claims that need
  attention: out of date, not rechecked, containing `sorry`, resting on
  additional assumptions, or where checking stopped.
- **Checking:** **Check saved file** replays the saved file in Lean's kernel,
  independently of the project's compiled files, and marks each claim
  **checked**, **admitted** (it uses `sorry`) or **assumptions** (it uses extra
  axioms). Results are saved beside the notebook in `<Notebook>.glitnir.json`.
- **Proof steps:** **Explore Proof Steps** records a proof's tactic tree once,
  then lets you walk it with the goals before and after each step.
- **Evidence and decisions:** `-- @evidence` directives show simulations as cards
  that run on click; `-- @decide` directives ask a provider for a Lean block,
  which Glitnir kernel-checks before **Accept** inserts it into the notebook.
- **History and export:** recent Git commits of the notebook with their diffs,
  and **Export HTML** for an offline snapshot.

Checking, proof-step recording and evidence runs happen only when you ask. Lean
always runs in the sandbox; an evidence provider computes from its output.

Views are drawn with the notebook's proofs skipped, so they appear quickly; a
freshly drawn card says "Drawn without re-running the notebook's proofs." when
that was done. A drawing never grants or changes a claim's status. Drawings are
saved in `<Notebook>.glitnir.json` and reused without Lean until the notebook
above the directive, the environment or the provider changes. A reused drawing
does not show that note, even if it was drawn without the proofs. On macOS,
Glitnir keeps the Mac from idle sleep while its checks, proof steps, views and
evidence run Lean or Lake (decision runs are not covered).

## Install

Glitnir runs in local VS Code windows on **macOS and Linux** (see
[Limits](#limits)). You need:

- **VS Code** 1.96 or newer, with the notebook's folder open as a trusted
  workspace.
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** and
  **Python 3.12 or newer**, for the Python backend. Without uv, setup offers
  **Install uv**, which shows its command before running it: Homebrew, or the
  official installer `https://astral.sh/uv/0.12.23/install.sh`.
- **Lean 4.35.0**, the one Lean version Glitnir checks, for checking, proof
  steps, views, evidence and decisions; reading needs only the backend. Install
  it with `elan toolchain install leanprover/lean4:v4.35.0`, and have the
  notebook's project use it. A project on another Lean version still opens and
  reads, with a banner saying checking is off. Build the project yourself first
  (`lake build`, after `lake exe cache get` for Mathlib).
- **On Linux, bubblewrap** with unprivileged user namespaces, which Lean runs
  in; the setup guide has the Ubuntu commands. macOS uses its built-in sandbox.

Then:

1. Install **Glitnir — Lean Notebooks** from the
   [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=gimle.glitnir): search for it in the Extensions view, or
   run `code --install-extension gimle.glitnir`. For a specific version or an
   offline machine, download `glitnir-VERSION.vsix` from a
   [release](https://github.com/arnovich/glitnir/releases) and run
   **Extensions: Install from VSIX…**.
2. Open a Lean file in a trusted workspace and run **Glitnir: Set Up
   Environment → Set Up** (also offered when you first open a notebook). It
   downloads the backend wheel of the extension's own release, verifies its
   checksum and installs it with uv into its own environment.
3. Run **Glitnir: Open in Glitnir**.

Marketplace installs update automatically. After an update, Glitnir offers once
to set up the new version's backend and reinstall your extension packages, in
one click.

Views, evidence and decisions come from the optional **Forseti plugin**, an
extension package (a Python package installed into Glitnir's backend, not a VS
Code extension) for projects built on the Forseti and Asgard Lean libraries.
Ordinary Lean and Mathlib notebooks do not need it. To install it, run
**Glitnir: Install Extension Package… → Forseti → Download from this release**.

The [setup guide](https://github.com/arnovich/glitnir/blob/main/extension/media/setup.md)
(`INSTALL.md` in each release) covers manual installation, existing
environments, upgrading and troubleshooting.

## Try it

Save this as `Hello.lean` in a new, empty folder. A loose file needs no Lake
project, but it should not sit inside an existing Lean project.

```lean
/-! # A first notebook -/
theorem hello : True := by
  trivial
```

1. Open that folder in VS Code and run **Glitnir: Open in Glitnir**. Click
   **Edit in VS Code** on a cell to jump to its source, save an edit and watch
   the preview refresh.
2. Click **Check saved file** (needs Lean 4.35.0). The `hello` card shows
   **checked**, and the result is saved beside the notebook.
3. Run **Glitnir: Set Up Coding Agent**, then ask your agent to extend
   `Hello.lean` with the `glitnir-notebooks` skill and follow its work in the
   preview.

The [Forseti examples](https://github.com/arnovich/forseti-examples) repository
has notebooks on circuits, dynamical systems and PDEs; they are Forseti projects
and need the plugin.

## Coding agents

**Glitnir: Set Up Coding Agent** installs the bundled
[authoring skill](https://github.com/arnovich/glitnir/blob/main/skills/glitnir-notebooks/SKILL.md)
into your workspace: `.claude/skills/glitnir-notebooks` for Claude Code,
`.agents/skills/glitnir-notebooks` for Codex, or a folder you choose for another
agent. Start a new agent session and ask it to use `glitnir-notebooks`. The
skill gives the agent the notebook syntax and the checking rules, and agents
check a notebook with the command from **Glitnir: Copy Validation Command**.

## Writing a notebook

A notebook is a `.lean` file that builds with standard Lean. Glitnir reads it as
cells:

- **Prose cells** are column-zero `/-! … -/` comment blocks, written in Markdown
  with `$…$` and `$$…$$` for mathematics.
- **Lean cells** are everything between prose cells: imports, namespaces,
  definitions and claims, shown as source.
- **Claims** are named `theorem` or `lemma` declarations, rendered with the
  statement and proof shown separately and the check status alongside. A
  docstring (`/-- … -/`) right above a definition or claim becomes its
  description. `sorry` marks unfinished work and is never reported as a success.
- **Sections** come from Markdown headings of level 2 to 6 in prose cells. Add
  ` {fold}` at the end of a heading to make its section collapsible, with the
  first paragraph as the summary.
- **Images and links:** a prose line that is only `![caption](assets/figure.png)`
  shows that file from the `assets/` folder beside the notebook, and
  `[Basics](Basics.lean)` links to a sibling notebook.
- **Directives** are standalone comment lines: `-- @view circuit myCircuit`
  draws a view, `-- @evidence simulate dissipative (h := 1/100) (steps := 400)`
  shows a simulation that runs on click, and
  `-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1`
  asks a decision provider for a proof. They need an extension package such as
  the Forseti plugin.

The [authoring skill](https://github.com/arnovich/glitnir/blob/main/skills/glitnir-notebooks/SKILL.md)
has the full rules, and the
[user guide](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#the-notebook-format)
describes sections, images, links and each directive in detail.

## Using the preview

- **Contents** lists the sections; `s` switches between the rendered document
  and the Lean source.
- **Follow edits** scrolls to the sections each save changed.
- **Claims** (`c`) steps through the claims with `j` and `k`; `i` opens the
  claim inspector; `n` and `p` go to the next and previous claim that needs
  attention.
- **Check through here** (`r`) checks only through the selected claim. A result
  goes out of date when anything above its claim changes. **Glitnir: Copy
  Validation Command** gives the same check for a terminal or a coding agent.
- **Explore Proof Steps** (`t`) walks the selected proof's goals.
- **Run** on an evidence or decision card runs it, one at a time. **Accept** on
  a checked decision inserts its block after the directive and saves the file.
- `⌘K G` on macOS and `Ctrl+K G` on Linux run **Glitnir: Reveal in Glitnir**
  from the editor. Press `?` in the preview for every shortcut.

The user guide covers the
[preview](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#using-the-preview)
and
[checking](https://github.com/arnovich/glitnir/blob/main/docs/guide.md#checking)
in detail: statuses, what a check accepts, import replay and caches, linked
notebooks and the claim graph.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `glitnir.elaborationTimeoutMs` | `600000` (10 min) | Time limit for checks, proof-step recording, views, evidence and decisions, in milliseconds, capped at 30 minutes. Applies to previews opened after the change. |
| `glitnir.memoryLimitMb` | `16384` (16 GB) | Memory limit for every Lean run, in megabytes, between 1024 and 32768. Unless set, Glitnir uses 16384, or 2048 less than the machine's memory when that is smaller. Applies to previews opened after the change. |
| `glitnir.pythonPath` | empty | Python with the Glitnir backend installed. Empty finds an existing environment. Changing it closes open previews. |
| `glitnir.offerBackendUpdate` | `true` | After an extension update, offer once to set up the new version's backend. Nothing downloads until you click **Set Up**. |

## Limits

- **One supported Lean version, today 4.35.0.** Checking, proof steps, views,
  evidence and decisions run only on it. A notebook on another Lean version
  opens and reads as usual, with a banner saying those are off; saved results
  still show, marked out of date. Glitnir moves the supported version forward
  as new Lean releases are tested, so a project on a newer Lean reads until
  then.
- **Local macOS and Linux only.** Windows, remote windows (SSH, WSL, containers)
  and virtual or untrusted workspaces are unsupported. On Linux, the sandbox
  needs an x86-64 or ARM64 machine.
- **Lean always runs in the platform sandbox.** Lake configuration that needs
  subprocesses is unavailable on macOS.
- **Glitnir does not prepare projects.** It never builds or downloads
  dependencies; run `lake build` yourself. Views, evidence, decisions and
  imports of other notebooks need the notebook inside a built Lake project; a
  loose `.lean` file has no prepared imports.

## Where next

- Something wrong? Start with **Output → Glitnir**, then the
  [troubleshooting](https://github.com/arnovich/glitnir/blob/main/extension/media/setup.md#troubleshooting)
  list, then [report an issue](https://github.com/arnovich/glitnir/issues).
- [User guide](https://github.com/arnovich/glitnir/blob/main/docs/guide.md): the
  notebook format, the preview, checking, settings, keyboard shortcuts and
  extension packages in full.
- [Setup guide](https://github.com/arnovich/glitnir/blob/main/extension/media/setup.md):
  requirements, manual installation, existing environments, upgrading and
  troubleshooting.
- [Forseti plugin](https://github.com/arnovich/glitnir/blob/main/plugins/forseti/README.md):
  views, evidence and decisions, with every option and goal form.
- [DEVELOPMENT.md](https://github.com/arnovich/glitnir/blob/main/DEVELOPMENT.md):
  building, testing and releasing Glitnir, and the static export for galleries.
- [SECURITY.md](https://github.com/arnovich/glitnir/blob/main/SECURITY.md): report
  vulnerabilities privately.
- [MIT licensed](https://github.com/arnovich/glitnir/blob/main/LICENSE), with
  [third-party notices](https://github.com/arnovich/glitnir/blob/main/THIRD_PARTY_NOTICES.md).
  Pull requests are limited to
  [invited collaborators](https://github.com/arnovich/glitnir/blob/main/CONTRIBUTING.md).
