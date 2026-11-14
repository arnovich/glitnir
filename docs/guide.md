# Glitnir user guide

Glitnir lets you read, inspect and check Lean notebooks in VS Code. A notebook
is an ordinary `.lean` file, with explanations alongside the definitions and proofs.
For installation, upgrades or a backend that will not start, use the
[setup guide](../extension/media/setup.md).

## Using the preview

Right-click a `.lean` file in Explorer and choose **Open in Glitnir**. This opens
its notebook without starting the Lean source editor. You can also use the
**Glitnir: Open in Glitnir** command from an open file.

- **Notebook** shows the mathematical narrative and claim cards. Use Contents
  to find a section or declaration; select a claim to open its Inspector.
- **Source** shows the saved Lean file. Line links connect it to the notebook;
  **Edit in VS Code** opens the source for editing.
- **Explore** shows recorded claim relationships or a guided proof atlas.

Saving the file refreshes the preview, including saves made by a coding agent.
**More → Follow live edits** controls whether the notebook scrolls to changes.
It does not start a proof check.

**Needs review** lists claims that are out of date, contain `sorry`, use extra
assumptions, or were not rechecked. Use **Previous** and **Next** to visit them.
An empty review list does not certify the file. The status beside the filename
separates the backend connection from check results; click it for details.

## Checking

Save the file, then choose **Check saved file**. Glitnir elaborates it in a
sandbox and independently replays its declarations in Lean's kernel. Checking
needs **Lean 4.35.0**. For a Lake project, build its dependencies first
(`lake build`; for Mathlib, first run `lake exe cache get`). A loose `.lean`
file can use the standard library without a Lake project. Files using another Lean version remain readable, but
Glitnir cannot check them.

| Claim status | Meaning |
| --- | --- |
| **checked** | The proof uses only Lean's standard axioms. |
| **admitted** | The proof depends on `sorry`. |
| **assumptions** | The proof depends on additional axioms; inspect the list. |

A failed, stopped or unavailable check is not a proof. Drawings and simulations
never change a claim's status. `native_decide` introduces an additional axiom
and therefore gives **assumptions**.

To check only the work so far, select a claim and use its **Check lines 1–…** action.
Later claims keep their earlier results, marked **Not rechecked**. Unsupported
commands such as `#eval` and `#check` stop checking from that point; the preview
explains where and why.

Results are saved in `<Notebook>.glitnir.json` beside the source. Keep both files
in Git. A result becomes out of date when its relevant source or checked
environment changes. Results can remain current in another checkout or on
another machine when the compiled artifacts are byte-identical.

### Imports and caches

The first check replays the imported libraries too, which can take many minutes
for Mathlib. Later checks reuse that replay while its inputs match; the notebook's
own declarations are always replayed. Some upgrades invalidate the cache and
saved results, so the next check takes longer. See
[development notes](../DEVELOPMENT.md#architecture) for the checking design and
[troubleshooting](../extension/media/setup.md#troubleshooting) for setup problems.

## Inspecting a proof

Select a claim, then choose **Show proof steps** in its Inspector. Glitnir records
the saved proof with Lean and shows the starting goals, each tactic, and the
goals before and after it. Inspection is a separate action from checking.

**Explore** shows the relationships recorded by a check. Select a statement to
see its uses and users, then **Show in Notebook** to return to the explanation.
The graph is a reading aid: it can omit dependencies used only by unfolding.

A walkthrough may also provide a proof atlas: **Read** explains an argument,
**Route map** follows a curated route, and **Statement graph** shows its mapped
statements. Use **Search proof**, route choices and navigation history to find
your way. These routes are explanations, not extracted proof dependencies.
Walkthroughs for incompatible Lean versions stay **Unchecked**.

## The notebook format

Put Markdown prose in column-zero `/-! … -/` comments. Everything between those
comments is Lean code. Named theorems and lemmas become claim cards; `/-- … -/`
docstrings describe the declaration immediately below them.

```lean
/-!
# A first notebook

Explain the idea, then state and prove it.
-/

theorem hello : True := by
  trivial
```

Use `$…$` for inline mathematics and `$$…$$` for displayed equations. Headings
structure the notebook. Add ` {fold}` to a heading to make its section
collapsible, with the first paragraph as its visible summary.

An image on its own line, `![Caption](assets/figure.png)`, embeds a local image
from the notebook's `assets/` folder. `[Next](Next.lean)` links to a sibling
notebook; Glitnir reads it in the same panel, with Back and Forward navigation.
To check that notebook, choose **Open in its own preview**.

For the full authoring conventions and directive syntax, use the
[notebook authoring skill](../skills/glitnir-notebooks/SKILL.md).

## Extension packages

Ordinary Lean and Mathlib notebooks need no plugin. For Forseti notebooks, run
**Glitnir: Install Extension Package… → Forseti → Download from this release**.
Choose the `z3` extra if you want decision procedures.

- **Views** (`-- @view`) draw supported circuits and models.
- **Evidence** (`-- @evidence`) runs simulations and displays their results.
- **Decisions** (`-- @decide`) propose a proof or refutation as a Lean block.
  Glitnir checks that block before **Accept** inserts it into the saved notebook.
  An unresolved decision establishes neither the claim nor its negation.

The notebook must import the Lean libraries its directives use. The
[Forseti reference](../plugins/forseti/README.md) lists supported goals, options
and dependencies. The [setup guide](../extension/media/setup.md#the-forseti-plugin)
covers installation. Packages share the backend environment, so changes affect
other windows using it too; reopen their previews after installing or upgrading.

## Export and history

Choose **More → Export HTML** for a self-contained, light-mode page with the
rendered notebook and its complete saved Lean source. It works offline without
scripts. Export linked notebooks into the same folder to retain links between
them. Export the linked notebooks first, then re-export the referring notebook.
The snapshot records results as they stood when exported; it does not
check them or keep them up to date.

**More → History** shows recent Git commits of the notebook and their diffs.

## Coding agents

Run **Glitnir: Set Up Coding Agent**, choose the workspace and agent, then start
a new agent session and ask it to use `glitnir-notebooks`. The command supports
Codex and Claude Code, or exports the skill for another agent. Use
**Glitnir: Copy Validation Command** to give an agent the command for checking
the notebook.

For remote Lean checks through an existing Taskvalet service, see
[Taskvalet computation workers](taskvalet-compute.md). Checks can run remotely;
proof inspection, views, evidence and decisions run locally. The service needs
its own configured workers and credentials.

## Settings

Use **Glitnir: Open Settings** to choose the backend Python, adjust Lean's time
and memory limits, or configure Taskvalet. Reopen previews after changing their
runtime settings. The defaults allow ten minutes and up to 16 GB for Lean;
the memory default is lower on smaller machines. Import replay has its own
longer time limit.

## Keyboard shortcuts

Single keys work when the preview has focus, outside text fields. Press **?**
to see the shortcuts. Navigation never starts a check.

<!-- shortcuts:start (a test keeps this table equal to the preview's list) -->
| Keys | Action |
|---|---|
| `j` `k` | Next / previous claim |
| `n` `p` | Next / previous claim that needs attention: out of date, not rechecked, contains sorry, additional assumptions, or where checking stopped |
| `s` | Switch between notebook and Lean source |
| `g` | Show the recorded proof graph |
| `i` | Show or hide the claim inspector in Notebook |
| `r` | Check the saved file through the selected claim |
| `e` | Edit the selected claim in VS Code |
| `?` | Show these shortcuts |
| `Esc` | Close this dialog or the History panel |
| `⌘K` then `G` (macOS), `Ctrl+K` then `G` (Linux) | In the Lean editor: show the cursor’s place in Glitnir. Release the first keys before G |
<!-- shortcuts:end -->
