# Walkthroughs of existing formalizations

A walkthrough explains a finished Lean project through its paper's argument
rather than its files. It has two parts: a map of the paper's numbered
statements and which ones each proof uses, and a notebook read from the top. In
the notebook each key statement is explained, shown as the project states it in
Lean, and restated as a claim proved by the project's own theorem. Write it for
the user's own understanding first, and well enough that others can read it.

This reference covers writing a walkthrough and extending one. The notebook
rules in [SKILL.md](../SKILL.md) still apply; the outline format is in
[walkthrough-outline.md](walkthrough-outline.md).

## Rules

- **The original stays untouched.** Never write into the original project or
  the repository that contains it: no notebooks, no annotations, no build
  configuration.
- **Ask before building the original.** Large projects take many CPU-minutes
  and gigabytes of memory to build. Without the user's approval, do not build,
  and report compilation as not run.
- **Ask before any download.** If a paper is cited only by identifier, such as
  an arXiv number, ask the user for the file or for permission to download it.
- **Never present a claim as checked** unless a Glitnir check of the walkthrough
  reported it. The helper's `check` compares text; it grants no status.
- **Every Lean name, file, line and type is real.** Copy them from the source
  and let `check` confirm them. Never write a type that is not the project's
  own.
- **Restate, do not quote.** Restate statements and definitions in notation and
  the walkthrough's own words; formulas are not quotations. Keep any prose
  copied from a paper under about 15 words per quotation, whatever the paper's
  licence. Keep the original's attribution and licence in the walkthrough.
- **Record every difference.** Where the Lean form differs from the paper, say
  so in the node's `note` and in the notebook, above all when the project's own
  index records the difference.

## Where a walkthrough lives

Unless the user names another place, a walkthrough has a folder of its own named
`<project>-walkthrough/`, beside the git repository that contains the original
and never inside it. For a project `odd-sums` inside the clone `formal-proofs/`,
that is `odd-sums-walkthrough/` next to `formal-proofs/`, never
`formal-proofs/odd-sums-walkthrough/`. The folder holds:

```text
outline.json      the outline, the source of truth
Walkthrough.lean  the notebook; large projects may use one file per part
assets/           map.svg and the figures
map.html          the clickable map
lakefile.toml     a Lake project requiring the original by relative path
lean-toolchain    a copy of the original's lean-toolchain
```

## The helper

`scripts/walkthrough.py` uses only Python's standard library. Run it from this
skill's folder with `python3 -B`, so that it leaves the folder unchanged, and
give it absolute paths:

- `python3 -B scripts/walkthrough.py check OUTLINE` checks the outline's
  structure: the format, required keys, unique ids, known `uses` and no cycles.
  If those pass, it compares every `lean` entry's file, name, line and `type`
  with the project's source and reports where a declaration moved or how it
  changed. Last, it notes commits to the project folder since `commit`; that
  note is not an error.
- `python3 -B scripts/walkthrough.py check OUTLINE --notebook FILE` also checks
  the notebook's claims (below), once the outline and its Lean entries have
  no errors. For a walkthrough in several files, repeat `--notebook` once per
  file in one run: together the files must hold each node's claim exactly once.
- `python3 -B scripts/walkthrough.py map OUTLINE` writes `assets/map.svg` and
  `map.html` beside the outline; `--out DIR` writes them elsewhere. Its links
  open this machine's files. `--links public` links to `repository` at `commit`
  and to the sources' `url`s instead, and writes no local path: use it for a
  copy meant for others. `map` refuses an outline with structural errors but
  does not compare Lean entries, so run `check` first.

`check` exits 0 when it reports no error, 1 when it reports any (it lists them
all), and 2 when the outline cannot be read or the command is wrong.

## Claims

A claim restates one of a node's `lean` entries and is proved by it:

```lean
/-- **Theorem 1**: the sum of the first $n$ odd numbers is $n^2$. -/
theorem theorem1 : OddSums.MainResults.OddSumIsSquare := by
  exact OddSums.MainResults.oddSumIsSquare
```

The shape is `theorem <id> : <the project's statement> := by`, then
`exact <project theorem>` indented on the next line, and nothing else. Use `by`
so that the preview shows the claim as a claim card. The node's `claim` is
`<id>`, and `check --notebook` compares the statement with the project's text:

- Write the statement exactly as the project does. Whitespace may differ. A
  name may be qualified only with a namespace that encloses the project's
  declaration: where the project writes `oddSum` inside `namespace OddSums`,
  write `OddSums.oddSum`.
- A name the project reaches through `open` is not accepted. Its exact text
  would not elaborate in the walkthrough, which opens nothing, so explain that
  theorem in prose and cite it by name.
- Writing an `abbrev` alias in place of what the project wrote, or unfolding
  one, differs textually and is reported as a mismatch. Restate the theorem's
  own declared type.
- A theorem that depends on section `variable`s cannot be compared, and a claim
  citing it is an error. Explain it in prose and cite it by name.
- A theorem whose header binds explicit `( )` or strict-implicit `⦃ ⦄`
  arguments before the colon that starts its type cannot be restated: `exact`
  does not supply those arguments, so the claim would not compile, and a claim
  citing it is an error. Explain it in prose and cite it by name. Implicit
  `{ }` and instance `[ ]` binders are fine, and so is a `∀ (n : ℕ), …` after
  the colon.

The notebook defines and opens nothing. It contains only imports, prose,
comments and docstrings, `namespace`, `section` and `end`, `#print axioms`, and
claims. `open`, `variable`, `def`, `notation`, `set_option` and other commands
would give the restated text a meaning of the notebook's own, so `check` reports
them. Put the claims in a namespace of the walkthrough's own, such as
`namespace OddSumsWalkthrough`, so their names cannot clash with the project's.

The text check does not see either side's wider context, such as an `open` or
notation elsewhere in the project's file or what the notebook's imports bring
into scope. It is the main automatic guard, because originals are usually
unbuilt and on another Lean version, but it is not a proof. Only a Glitnir check
of the walkthrough grants a claim a status.

## The workflow

### 1. Gather context

Start with what the user supplied. Then read the project: its READMEs; the
file of top-level statements a reader must trust (often a short file such as
`MainResults.lean`, `Challenge.lean` or `EndStatement.lean`) and how the proofs
connect to it; any index from the paper to Lean (a `PaperIndex.lean`, a
`docs/INDEX.md`, a JSON list of paper items); audit and verification notes such
as `AUDIT.md`; and papers in the repository (`*.pdf`, `*.tex`). For a paper
cited only by identifier, ask as the rules say. Record each source with its
version in `sources`, the trusted file in `project.trusted`, and the project's
`commit` and `toolchain`.

*Check:* the outline names the trusted statement file and every source used.

### 2. Extract the outline

List the paper's statements in its order, each with its label, section and
pages, kind, statement in your own words, the idea of its proof, the statements
it uses, and its Lean declarations with how each differs from the paper. Prefer
the project's own index for the paper-to-Lean mapping and its recorded
differences. Outline a large project from the top: the main text first, each
appendix as one node, deeper levels only when asked.

*Check:* `python3 -B scripts/walkthrough.py check OUTLINE` exits 0.

### 3. Draw the map

Run `python3 -B scripts/walkthrough.py map OUTLINE`. The map shows direct uses
only: a use also reached through another is not drawn.

*Check:* look at the rendered map, by whatever means you have, and fix the
outline, not the picture. If you cannot view it, say that it was not inspected.

### 4. Write the notebook

Open with the title and scope; what is proved and which file a reader must
trust; the original's attribution and licence; the map as
`![How the proof fits together](assets/map.svg)`, with a pointer to `map.html`;
and the strategy in one page. If the walkthrough cannot be checked (step 6),
say so here. Then write one `{fold}` section per part of the argument, in
reading order, whose first paragraph is its summary. In each, give the
statement, the idea, how Lean states it and where that differs, and the claim.
Import only the modules the claims need. Say plainly where the project's final
theorem does not follow the paper's route.

*Check:* `python3 -B scripts/walkthrough.py check OUTLINE --notebook NOTEBOOK`
exits 0, with one `--notebook` for each file of the walkthrough in the same run,
and the preview shows the prose, and the claims as cards, rather than the whole
file as source.

### 5. Add figures

Draw SVG figures under `assets/`, each illustrating one statement's idea,
referenced from prose as SKILL.md describes, and recorded in `figures`. Figures
explain; they never carry an argument.

*Check:* look at every figure rendered, and check that each number in a caption
matches the statement it illustrates.

### 6. Validate and report

1. Run `check --notebook` as in step 4.
2. Compile each file with `lake env lean <file>` from the walkthrough's folder
   only when the original is built and the user has agreed (see below).
   Otherwise report compilation as not run.
3. Run a Glitnir check, as SKILL.md describes, when the walkthrough's toolchain
   is the Lean version Glitnir supports. Otherwise state in the notebook and the
   report that the walkthrough is unchecked and why, for example: the original
   pins another Lean version.

Report the sources used and their versions, the project commit, what is
unchecked, every recorded paper-to-Lean difference, and any note from `check`.

## The Lake project

Untested: this template and the compile step have not yet been run on a real
project. Treat any failure as a limitation to report, not as a reason to change
the original.

```toml
name = "odd-sums-walkthrough"
defaultTargets = ["Walkthrough"]

[[require]]
name = "odd-sums"                   # the package name in the original's lakefile
path = "../formal-proofs/odd-sums"  # the original, relative to this folder

[[lean_lib]]
name = "Walkthrough"
roots = ["Walkthrough"]             # one module per notebook file
```

For a walkthrough in several files, list each file's module in `roots`, such
as `roots = ["Walkthrough", "Appendices"]` for `Walkthrough.lean` and
`Appendices.lean`. Copy the original's `lean-toolchain` beside it. Running Lake here may fetch the
original's dependencies again and build the original: ask first, as for any
build.

## Extending a walkthrough

1. Read `outline.json` first, then the notebook.
2. Keep every existing id. Add nodes, or deepen one (for an appendix node, add
   its statements as nodes it uses), and add any new sources.
3. If the project has moved, rerun `check`, fix the entries it reports, and set
   `project.commit` to the commit you verified against.
4. Rerun `check` and `map`.
5. Add or revise only the sections you were asked to; never rewrite others.
6. Validate and report as in step 6.
