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
  configuration. The one exception is a build of the original the user has
  approved, which may write Lake's build output (`.lake/`) there; nothing else
  is ever written into the original.
- **Ask before building the original.** Large projects take many CPU-minutes
  and gigabytes of memory to build. Without the user's approval, do not build,
  and report compilation as not run.
- **Ask before any download.** If a paper is cited only by identifier, such as
  an arXiv number, ask the user for the file or for permission to download it.
- **Never present a claim as checked** unless a Glitnir check of the walkthrough
  reported it. The helper's `check` compares text; it grants no status.
- **Every Lean name, file, line and type is real.** Copy them from the source
  and let `check` confirm them. Never write a type that is not the project's
  own. `check` covers only the outline's Lean entries and the claims: confirm
  every Lean name the prose mentions by searching the project for its
  declaration.
- **Restate, do not quote.** Restate statements and definitions in notation and
  the walkthrough's own words; formulas are not quotations. Keep any prose
  copied from a paper under about 15 words per quotation, whatever the paper's
  licence.
- **Credit the original.** Name its authors and its licence in the notebook's
  opening prose, with a link to its `LICENSE` or `NOTICE` file. A relative path
  is fine in a local walkthrough; in a copy meant for publishing, link the
  repository at the recorded commit, as
  `<repository>/blob/<commit>/<path>/LICENSE`, and the same for every other
  link to the project's files. Copy those files only into a copy published for
  others.
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
Walkthrough.lean  the notebook that owns the Graph statement map
assets/           explanatory figures
lakefile.toml     a Lake project requiring the original by relative path
lean-toolchain    a copy of the original's lean-toolchain
```

## The helper

`scripts/walkthrough.py` uses only Python's standard library and needs Python
3.10 or newer. Run it from this skill's folder with Glitnir's backend Python:
the interpreter `glitnir.pythonPath` points to, or the one **Output → Glitnir**
shows as "Glitnir environment: …" (Python 3.12 or newer, which the extension
sets up with uv). Below, `python` stands for it. Pass `-B`, so that it writes no
bytecode and leaves the folder unchanged, and give it absolute paths. With no
Glitnir backend installed, run each command through uv instead, such as
`uv run --no-project --python ">=3.10" python -B scripts/walkthrough.py check OUTLINE`.
Never use the system Python: macOS ships 3.9, on which the helper only says what
to run and exits with status 2.

- `python -B scripts/walkthrough.py check OUTLINE` checks the outline's
  structure: the format, required keys, unique ids, known `uses` and no cycles.
  If those pass, it compares every `lean` entry's file, name, line and `type`
  with the project's source and reports where a declaration moved or how it
  changed. Last, it notes commits to the project folder since `commit`; that
  note is not an error. With no error it ends with one line, such as
  `ok: 21 nodes, 57 Lean entries`.
- `python -B scripts/walkthrough.py check OUTLINE --notebook FILE` also checks
  the notebook's claims (below), once the outline and its Lean entries have
  no errors. The helper still accepts several `--notebook` arguments for older
  multi-file walkthroughs. New walkthroughs keep mapped claims in one notebook:
  the Graph view and its static export show saved status and navigation for the
  notebook currently open.
  To resolve the project's names it scans every `.lean` file under the project
  root, except in `.lake` folders.
- `python -B scripts/walkthrough.py map OUTLINE` is retained for older
  walkthroughs that already publish `assets/map.svg` or `map.html`. It writes
  those files beside the outline; `--out DIR` writes them elsewhere. Its links
  open this machine's files. `--links public` links to `repository` at `commit`
  and to the sources' `url`s instead, and writes no local path. `map` refuses
  an outline with structural errors but does not compare Lean entries, so run
  `check` first. New walkthroughs use Glitnir's Graph view and static HTML
  export, both drawn directly from `outline.json` with no generated map asset.

`check` exits 0 when it reports no error, 1 when it reports any (it lists them
all), and 2 when the outline cannot be read or the command is wrong. In the map,
an arrow points from a statement to each statement that uses it directly, and a
dashed arrow leads into an appendix node.

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
`<id>`, a plain name without a dot: Lean reads `theorem A.c` inside
`namespace A`, where its statement's names can mean other declarations, so
`check` reports a dotted claim name. A node has at most one claim: give a
second statement, such as another sentence of the paper's statement or another
form of an appendix's result, a node of its own. `check --notebook` compares
the statement with the project's text:

- Write the statement as the project does, with each name written as the full
  name Lean resolves it to in the project. Whitespace may differ. Lean looks a
  name up in the innermost enclosing namespace first, then outward, then at the
  root; the namespaces of the theorem's own name count too (`theorem
  OddSums.main` is read inside `namespace OddSums`). A one-part name never
  finds a `protected` declaration that way: inside `namespace A`, `cost`
  passes over `protected def A.cost` and means the next `cost` outward. A
  name declared `protected` in one file and not in another means whichever
  the theorem's file imports, which `check` does not follow: it reports
  `declares both protected and not`, so explain the theorem in prose. A name
  part in guillemets that needs none, such as `«cost»`, is the plain name
  `cost` on both sides. So
  where the project writes `oddSum` inside `namespace OddSums.Main` and
  declares both `OddSums.oddSum` and `OddSums.Main.oddSum`, write
  `OddSums.Main.oddSum`; if it declares only `OddSums.oddSum`, write that. A
  name that resolves to a declaration inside a namespace must be written with
  that namespace, and a qualification with any other namespace is reported.
  Names the statement binds, such as `n` in `{n : ℕ}` or `∀ n, …`, and field
  names after a dot are written as the project writes them. If the statement
  binds a name that also names a project declaration where the theorem is
  written, as in `(∃ n, n = 3) ∧ n = 3` with a `def n` in scope, the text
  cannot tell its bound uses from its free ones: `check` reports
  `binds and uses n`, so explain the theorem in prose. A name bound before the
  colon, as in `{n : ℕ}`, is bound for the rest of the statement and is not
  reported, unless it is also written before its binder ends. `check` reports
  each name that differs from what Lean resolves it to, with the name it
  expects. It knows only the
  declarations that start a line (not structure fields, constructors or
  `alias`es): for such a name, find its declaration and qualify it yourself.
- A name the project reaches through `open` is not accepted: its exact text
  would not elaborate in the walkthrough, which opens nothing. To tell, read
  the `open` lines of the file that declares the theorem and find where the
  name is declared. A name declared under an enclosing namespace is qualified
  as above; one declared only under an opened namespace is reached through
  `open`. If both declare it, the text cannot tell which Lean takes, so do not
  restate it.
- Cite a `theorem` or `lemma` whose proof uses no `sorry`, `admit` or `stop`.
  A claim citing an `axiom`, a `def` or another declaration, or a theorem whose
  own proof text contains `sorry`, `admit` or `stop` (outside comments and
  strings), is an error. The proof text runs to the next command, column-0
  lines included. `check` reads only the cited proof's text: a `sorry` in a
  theorem it uses is not seen, so read the project's audit notes or axiom
  report too.
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
- In a project that uses the Lean module system (its files begin with
  `module`), cite only public declarations: those under `public section` or
  marked `public`. A private one would not elaborate in the walkthrough, and
  `check` does not see this: confirm it by reading the cited file, for
  `public section` (possibly with attributes, as in `@[expose] public section`)
  above the declaration or `public` on it.
- When a name is declared in two files, such as a statement-only file whose
  proofs are `sorry` and the file that proves it, list and import the file that
  proves it, never the `sorry` one.
- Where a theorem cannot be restated, cite another project theorem that proves
  the same proposition and can be, such as the trusted statement file's
  version, listed as another `lean` entry of the node. Otherwise explain the
  theorem in prose and cite it by its full name, without a claim. This is
  expected, sometimes for most of a project: statements about programs in a
  project that opens its machine namespace, for example, are reached through
  `open` and stay prose.

The notebook defines and opens nothing. Each line is blank, inside a `/-! -/`
prose block, a `/-- -/` docstring or a `/- -/` comment, a `--` comment, or one
of these at column 0: `import <module>`, `namespace <name>`, `section` or
`section <name>`, `end` or `end <name>`, `#print axioms <name>`, or a claim
(`theorem` or `lemma`, with any modifiers but no attributes), whose header
lines and single `exact` line are indented under it. A claim with attributes,
such as `@[simp]` or `@[instance]`, is reported: they change how the claims
after it elaborate. Only a `--` comment may follow the one module of an
`import`, the one name of a `#print axioms` or `namespace`, or the keyword of
`section` or `end` and its optional name, because Lean reads any further word
as a command of its own: `section #exit` would stop Lean before the claims. `check` reports every
other line: any other command (`open`, `variable`, `def`, `example`,
`notation`, `export`, `alias`, `set_option`, `#eval`, a lone attribute line and
so on), any other indented code, and code after a comment's closing `-/` on the
same line. Such commands
would give the restated text a meaning of the notebook's own, or add claims
`check` does not compare. Put the claims in a namespace of the walkthrough's
own, such as `namespace OddSumsWalkthrough`, so their names cannot clash with
the project's; `check` reports a walkthrough namespace in which the project
declares names, and a claim whose statement names another claim.

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
differences. An index may list `alias` entries, which `check` does not scan:
follow each alias to the declaration it names and list that. Outline a large
project from the top: the main text first, each appendix as one node, deeper
levels only when asked. Node kinds are fixed:

- An equation the claims rest on is a `lemma` node labelled with the paper's
  equation number, such as `Equation (5)`.
- A section of the main text outside the walkthrough's scope that the claims
  depend on is one node, of its key statement's kind; the prose says that it
  stands in for the section.
- A paper item split into several nodes, one per claim, is labelled with the
  paper's name and the part in parentheses, such as `Theorem 22 (3SUM)`.

*Check:* `python -B scripts/walkthrough.py check OUTLINE` exits 0.

### 3. Draw the map

Open the walkthrough notebook beside `outline.json` in Glitnir and choose
**Graph**. The map shows direct uses only: a use also reached through another
is not drawn. Edges into appendix nodes are dashed. A statement's box shows its
kind, while its dot shows the saved status of the walkthrough claim named by
the outline. The details panel links to the paper page and pinned public Lean
declarations when the outline supplies their URLs. The static HTML export uses
the same map without an external fetch.

*Check:* look at the rendered map, by whatever means you have, and fix the
outline, not the picture. If you cannot view it, say that it was not inspected.

### 4. Write the notebook

Open with the title and scope; what is proved and which file a reader must
trust; the original's authors and licence, as the rules say; tell the reader
that **Graph** shows how the paper's statements fit together; and give the
strategy in one page. If the walkthrough cannot be checked (step 6), say so
here. Then write one `{fold}` section per part of the argument, in
reading order, whose first paragraph is its summary. In each, give the
statement, the idea, how Lean states it and where that differs, and the claim.
Import only the modules the claims need, each the file that proves the cited
theorem. Say plainly where the project's final
theorem does not follow the paper's route.

*Check:* `python -B scripts/walkthrough.py check OUTLINE --notebook NOTEBOOK`
exits 0, and the preview shows the prose and claims as cards, rather than the
whole file as source. If you are maintaining an older multi-file walkthrough,
pass every file in the same helper run; its Graph view maps only claims in the
file currently open. If you cannot open the preview, say so in the report.

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
Name each check that could not run, such as the preview, a build or a Glitnir
check. Give the report to the person in your reply; write it to a file only
when asked and when you can.

## The Lake project

Untested: this template and the compile step have not yet been run on a real
project. Nor is it known whether a walkthrough file that is not a module may
import the module files of a project that uses the Lean module system. Treat
any failure as a limitation to report, not as a reason to change the original.

```toml
name = "odd-sums-walkthrough"
defaultTargets = ["Walkthrough"]

[[require]]
name = "odd-sums"                   # the package name in the original's lakefile
path = "../formal-proofs/odd-sums"  # the original, relative to this folder

[leanOptions]                       # copied from the original's lakefile
autoImplicit = false

[[lean_lib]]
name = "Walkthrough"
roots = ["Walkthrough"]             # one module per notebook file
```

For an older walkthrough in several files, list each file's module in `roots`,
such as `roots = ["Walkthrough", "Appendices"]` for `Walkthrough.lean` and
`Appendices.lean`. Copy the original's `leanOptions` exactly, so that the
restated statements elaborate as they do in the original, and leave the table
out if it sets none. Copy the original's `lean-toolchain` beside the lakefile.
Running Lake here may fetch the original's dependencies again and build the
original, writing its build output into the original's `.lake/`: ask first, as
for any build.

## Extending a walkthrough

1. Read `outline.json` first, then the notebook.
2. Keep every existing id. Add nodes, or deepen one (for an appendix node, add
   its statements as nodes it uses), and add any new sources.
3. If the project has moved, rerun `check`, fix the entries it reports, and set
   `project.commit` to the commit you verified against.
4. Rerun `check` and `map`.
5. Add or revise only the sections you were asked to; never rewrite others.
6. Validate and report as in step 6.
