# The walkthrough outline

`outline.json` is a walkthrough's source of truth: the paper's statements, how
they depend on each other and which Lean declarations formalize them. The
notebook and the map are written from it, and a later agent extends the
walkthrough by reading it first. Check it with
`python3 -B scripts/walkthrough.py check OUTLINE`, run from the skill folder;
[walkthroughs.md](walkthroughs.md) says when.

The format is JSON, identified as `glitnir.walkthrough-outline/v1`. Paths in
the outline are relative to the folder that holds it, except Lean files, which
are relative to the project root. A field not listed here is not part of the
format; do not add one.

## Top level

| Key | Required | Value |
|---|---|---|
| `format` | yes | exactly `"glitnir.walkthrough-outline/v1"` |
| `title` | yes | the walkthrough's title |
| `project` | yes | the original Lean project (below) |
| `sources` | yes | every source used (below) |
| `nodes` | yes | the statements, in the paper's order (below) |
| `overview` | no | one or two sentences on what the paper proves; shown on the map page |
| `figures` | no | the figures under `assets/` (below); the helper does not read them |

## `project`

| Key | Required | Value |
|---|---|---|
| `root` | yes | the Lean project's folder, relative to the outline's folder |
| `commit` | yes | the last commit touching that folder, 7 to 40 lowercase hex characters: run `git log -1 --format=%H -- .` in the project folder |
| `toolchain` | yes | the contents of the project's `lean-toolchain` |
| `trusted` | yes | the file of top-level statements a reader must trust, relative to `root`; `null` when the project has none, which the notebook then says |
| `repository` | no | the public URL of the git repository that contains the project |
| `path` | no | the project's folder within that repository; `""` when it is the repository's root |

`check` notes, without failing, when the project has commits after `commit`
that touch its folder; commits elsewhere in the repository are ignored. The map
links to `<repository>/blob/<commit>/<path>/<file>#L<line>` when it is drawn for
publication, so set `repository` and `path` before publishing.

## `sources`

Each source is an object:

| Key | Required | Value |
|---|---|---|
| `kind` | yes | `paper`, `index` (a paper-to-Lean index), `readme`, `note` (audit or verification notes) or `other` |
| `path` | one of `path`, `url` | a local copy, relative to the outline's folder |
| `url` | one of `path`, `url` | a public address |
| `version` | no | which version was read: an arXiv version, a release, or the commit for a file in the project |
| `title` | no | a short name |

The first `paper` source is the one the map's page links open. Its local link
goes to `path`; its public link goes to `url`. A link to a PDF opens at a node's
first page.

## `nodes`

Each node is one statement of the paper, an equation the claims rest on, an
idea-level step, or an appendix or out-of-scope section summarized as one node
([walkthroughs.md](walkthroughs.md), step 2):

| Key | Required | Value |
|---|---|---|
| `id` | yes | short, unique and stable once published, such as `lem2`; never renumber |
| `label` | yes | the paper's name for it, such as `Lemma 2` or `Equation (5)`; for one of several nodes made from one paper item, that name followed by the part in parentheses, such as `Theorem 22 (3SUM)` |
| `kind` | yes | `definition`, `lemma`, `proposition`, `theorem`, `corollary`, `algorithm` or `appendix` |
| `section` | no | the paper's section, such as `§3` or `Appendix B` |
| `pages` | no | the PDF's page numbers, counted from its first page as the map's `#page=` links use them, such as `14` or `14-15`; where printed page numbers differ, give those in prose |
| `statement` | yes | the statement in the walkthrough's own words; mathematics in `$…$` |
| `idea` | yes | the idea of its proof, or what a definition is for, in a few sentences |
| `uses` | yes | for a statement, the ids of the statements its proof uses and of the definitions its statement uses; for a definition, the definitions it is built from; `[]` for none. No cycles. |
| `lean` | yes | its Lean declarations (below); `[]` when it has none, which the notebook then says |
| `claim` | no | the name of the notebook's claim that restates it (below) |

List the uses the paper's proof states. Where the Lean proof takes another
route, keep the paper's uses here and describe the Lean route in the `lean`
entries' `note` and the notebook's prose. The map draws only direct uses: a use
also reached through another listed use is not drawn, so listing it does no
harm.

## `lean` entries

| Key | Required | Value |
|---|---|---|
| `name` | yes | the declaration's full name, namespaces included, such as `OddSums.oddSum`; for an `alias`, the declaration it names |
| `file` | yes | the file that declares it, relative to `root` |
| `line` | yes | the line, counted from 1, on which its `theorem`, `def` or other keyword stands |
| `type` | yes | the declaration's header as the project writes it (below) |
| `note` | no | how the Lean form differs from the paper; prefer the project's own record of it |

`type` is the text after the name up to the top-level `:=` or `where`, binders
included: `": ETHIsFalse"` for `theorem ethIsFalse : ETHIsFalse := …`, and
`"(n : ℕ) : ℕ"` for `def oddSum (n : ℕ) : ℕ := …`. Line breaks and runs of
spaces count as one space; comments after `--` are left out. `check` compares
it with the source and reports a declaration that moved, changed or no longer
exists. The scan sees declarations that start at column 0 and name themselves
on that line, so an unnamed `instance` cannot be listed.

## `claim`

The name exactly as written after `theorem` in the notebook, without the
notebook's namespace. Each node has at most one claim, each claim belongs to
one node, and it appears in exactly one of the walkthrough's files. Give a
second statement a node of its own. The claim restates one of the node's `lean` entries;
[walkthroughs.md](walkthroughs.md) gives its shape and what can be restated.

## `figures`

Each figure is an object with `file` (under `assets/`, relative to the outline's
folder), `node` (the id it illustrates) and `caption`. Record them so a later
agent can find and revise them; the helper does not check them.

## Example

A small complete outline for a walkthrough in `odd-sums-walkthrough/`, beside
the clone `formal-proofs/` that contains the project `odd-sums`. The paper,
project and names are illustrative.

```json
{
  "format": "glitnir.walkthrough-outline/v1",
  "title": "Odd numbers add up to squares: a guided walkthrough",
  "overview": "The sum of the first n odd numbers is n squared, proved by induction and pictured by L-shaped strips.",
  "project": {
    "root": "../formal-proofs/odd-sums",
    "repository": "https://github.com/example/formal-proofs",
    "path": "odd-sums",
    "commit": "3f2a9c1d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b",
    "toolchain": "leanprover/lean4:v4.33.1",
    "trusted": "OddSums/MainResults.lean"
  },
  "sources": [
    {
      "kind": "paper",
      "title": "Odd sums",
      "path": "../formal-proofs/odd-sums/paper/odd-sums.pdf",
      "url": "https://example.org/papers/odd-sums-v2.pdf",
      "version": "v2"
    },
    {
      "kind": "index",
      "path": "../formal-proofs/odd-sums/OddSums/PaperIndex.lean",
      "version": "commit 3f2a9c1"
    },
    {
      "kind": "readme",
      "path": "../formal-proofs/odd-sums/README.md",
      "version": "commit 3f2a9c1"
    }
  ],
  "nodes": [
    {
      "id": "def1",
      "label": "Definition 1",
      "kind": "definition",
      "section": "§1",
      "pages": "2",
      "statement": "$S(n)$ is the sum of the first $n$ odd numbers, $1 + 3 + \\dots + (2n - 1)$.",
      "idea": "The object of the paper; $S(0) = 0$.",
      "uses": [],
      "lean": [
        {
          "name": "OddSums.oddSum",
          "file": "OddSums/Defs.lean",
          "line": 12,
          "type": "(n : ℕ) : ℕ",
          "note": "Sums $2i + 1$ over $i < n$, so the paper's $2n - 1$ never appears and no subtraction on ℕ is needed."
        }
      ]
    },
    {
      "id": "lem1",
      "label": "Lemma 1",
      "kind": "lemma",
      "section": "§2",
      "pages": "3",
      "statement": "$S(n + 1) = S(n) + 2n + 1$.",
      "idea": "The next odd number is added to the sum.",
      "uses": ["def1"],
      "lean": [
        {
          "name": "OddSums.oddSum_succ",
          "file": "OddSums/Defs.lean",
          "line": 18,
          "type": "(n : ℕ) : oddSum (n + 1) = oddSum n + (2 * n + 1)"
        }
      ]
    },
    {
      "id": "thm1",
      "label": "Theorem 1",
      "kind": "theorem",
      "section": "§2",
      "pages": "3-4",
      "statement": "$S(n) = n^2$ for every $n$.",
      "idea": "Induction on $n$: Lemma 1 adds $2n + 1$, and $n^2 + 2n + 1 = (n + 1)^2$.",
      "uses": ["def1", "lem1"],
      "lean": [
        {
          "name": "OddSums.MainResults.OddSumIsSquare",
          "file": "OddSums/MainResults.lean",
          "line": 15,
          "type": ": Prop",
          "note": "The trusted statement: for every n, oddSum n = n ^ 2."
        },
        {
          "name": "OddSums.MainResults.oddSumIsSquare",
          "file": "OddSums/MainResults.lean",
          "line": 21,
          "type": ": OddSumIsSquare"
        }
      ],
      "claim": "theorem1"
    },
    {
      "id": "cor2",
      "label": "Corollary 2",
      "kind": "corollary",
      "section": "§2",
      "pages": "4",
      "statement": "Every perfect square is the sum of the first few odd numbers.",
      "idea": "Write the square as $n^2$ and apply Theorem 1.",
      "uses": ["def1", "thm1"],
      "lean": [
        {
          "name": "OddSums.square_is_oddSum",
          "file": "OddSums/Squares.lean",
          "line": 9,
          "type": ": ∀ m : ℕ, IsSquare m → ∃ n, oddSum n = m",
          "note": "Lean's IsSquare m says m = r * r, not m = r ^ 2."
        }
      ],
      "claim": "corollary2"
    },
    {
      "id": "appA",
      "label": "Appendix A",
      "kind": "appendix",
      "section": "Appendix A",
      "pages": "6-7",
      "statement": "A second proof: an $n \\times n$ square splits into $n$ L-shaped strips of $1, 3, \\dots, 2n - 1$ cells.",
      "idea": "Peel the square from one corner; each strip is one odd number.",
      "uses": ["def1"],
      "lean": []
    }
  ],
  "figures": [
    {
      "file": "assets/strips.svg",
      "node": "appA",
      "caption": "A 4 × 4 square as strips of 1, 3, 5 and 7 cells."
    }
  ]
}
```

`lem1` has no claim: its explicit binder `(n : ℕ)` cannot be restated as a
claim (see [walkthroughs.md](walkthroughs.md)), so the notebook explains it in
prose and cites `OddSums.oddSum_succ` by name. The project declares
`square_is_oddSum` inside `namespace OddSums`, so the notebook's claim writes
`OddSums.oddSum` where the project writes `oddSum`:

```lean
/-- **Corollary 2**: every perfect square is a sum of the first odd numbers. -/
theorem corollary2 : ∀ m : ℕ, IsSquare m → ∃ n, OddSums.oddSum n = m := by
  exact OddSums.square_is_oddSum
```
