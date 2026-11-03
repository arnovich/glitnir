# Author a proof atlas

**Use when:** a walkthrough needs exploration at several levels, separate paper and
formalization reading routes, or explanations that are awkward in one linear notebook.
**Don't use:** for a small notebook already clear with folded sections, or automatic
Lean dependency extraction. The atlas is curated exposition, not checking evidence.

Keep `outline.json` as the paper-to-Lean source map. Add `atlas.json` beside it;
Glitnir offers **Read**, **Route map**, and **Statement graph** in Explore.
Read shows an explanation; Route map shows a route's curated support links;
Statement graph shows notebook statements and their recorded links. The sidebar
owns routes, search, step navigation and named Back/Forward history. Selection
stays synchronized without changing the active view. **Sources and details** below
the explanation holds pinned references, qualifications, mapped statement status,
source revision and argument links; it leaves the explanation visible.
A route switch explicitly starts at that route's result. Static export includes
linked, expandable argument explanations and all declaration qualification notes.

Start at a headline result. Explain a useful route two or more steps deep before
expanding coverage. Each argument states its goal, scope/assumptions, contribution
to the enclosing argument, proof idea, and any paper-versus-Lean difference. Use
short mathematical labels, not file names. A supporting step may be a curated
subargument rather than a declaration, but must retain a source mapping and say
when that mapping is only a related counterpart. Keep statement and proof
qualifications in the outline's declaration `note`; the atlas displays them.

## Deepen a difficult branch

Choose a bottleneck whose proof is still hard to understand after the overview.
Read its pinned source and follow several layers to actual intermediate lemmas,
explaining what each establishes and how its parent uses it. Stop at an explicit,
useful boundary; expanding all branches uniformly is usually less helpful.
For example, the 3SAT atlas follows improved pieces through reindexing, the
padded-to-counted conversion and the padding-machine execution lemma. Keep the
cost model and state-size assumptions visible at each step.

Reuse a node when two arguments use the same step, so the route map and sidebar
identify the shared structure. Static HTML retains supporting links and backlinks. A node may belong to only one route; connect every
published node to at least one route root. The validator diagnoses disconnected
nodes. Add precise intermediate declaration mappings to the appropriate outline
statement when needed, keeping its existing ID and recording qualification notes.
Do not create notebook claims merely to make these steps navigable.

## Argument links

**Link to this argument** and **Copy argument link** identify the route and stable
argument ID. In VS Code the copied link is a notebook-relative fragment, such as
`#atlas?route=lean&node=half_bridge`; use it in a Markdown link in that notebook
or append it to the URL of its exported HTML. A browser preview copies its full
URL. Do not share a temporary VS Code webview URL or a local preview server as a
public notebook address. The same fragment opens the route and argument live
and targets the argument in static export. Keep IDs stable when revising prose.

## Sidecar format

The format is `glitnir.proof-atlas/v1`. All listed keys are required. The root
contains `format`, `commit` (exactly the outline project's pinned commit), `nodes`
and `routes`. No checking statuses belong here. IDs are stable once published;
source revisions are provenance and must not become bookmark IDs.

Each node contains:

- `id`: unique stable text, at most 128 characters.
- `label`: readable argument name.
- `statement`: an existing outline node ID, binding the argument to the paper/source map.
- `declarations`: full names selected from that outline node's `lean` entries;
  `[]` displays all of its mappings. Do not invent declaration names or source spans.
- `goal`, `scope`, `role`, `idea`: nonempty prose, with Markdown and `$…$` mathematics.
- `difference`: paper/formalization qualification; `""` when none is needed.

Each route contains `id`, `label`, `root` (an atlas node ID), and `supports`:
objects mapping atlas node IDs to arrays of supporting atlas node IDs. All targets
must exist, relationships must be unique, and each route must be acyclic. Label
routes as curated paper or Lean arguments; never imply that hand-authored links
are dependencies extracted from elaborated Lean terms. Missing deeper exposition
is an exploration boundary, not evidence that a result has no dependencies.

Example shape (use real outline IDs and commit instead of these illustrative values):

```json
{
  "format": "glitnir.proof-atlas/v1",
  "commit": "abc1234",
  "nodes": [
    {"id": "result", "label": "The main bound", "statement": "thm1",
     "declarations": [], "goal": "Establish the bound.",
     "scope": "Under the theorem's hypotheses.", "role": "The headline result.",
     "idea": "Combine two estimates.", "difference": ""}
  ],
  "routes": [
    {"id": "lean", "label": "Lean argument (curated)", "root": "result", "supports": {}}
  ]
}
```

The atlas accepts at most 2,000 nodes, 16 routes and a 1 MiB regular sidecar;
route maps and sidebar flows page arguments rather than discarding the rest of the index.
Keep source bindings within the outline's supported bounds. Do not manufacture
extra notebook claims just to navigate: use arguments bound to existing statements.

## Validate

Use the matching Glitnir runtime Python (atlas support is required):

```sh
python -B scripts/proof_atlas.py /path/to/walkthrough/outline.json
```

This uses the preview's complete outline loading and normalization path. It checks
revision, renderable declaration and argument targets, bounds and route cycles.
It exits unsuccessfully for outline diagnostics or arguments unreachable from
every route root; the preview retains usable routes alongside those warnings.
A successful validation grants no proof status. Also run the
walkthrough helper's existing `check` against the original sources and notebook.
Textual checks, elaboration and independent Glitnir checking remain distinct.
An incompatible toolchain stays explicitly unchecked across the exploration views.

Inspect all three views: start at a result, follow the deepened branch using the
sidebar, open Sources and details, switch views, and use Back/Forward. In the
route map, select a shared step and check the sidebar highlight. Open a copied argument link
in the live preview and its static export, and check any paged map boundaries. Check keyboard focus,
small-screen reflow, and exported links. Verify the mathematical scope and
qualifications after changing a route or source binding; a structurally valid
atlas alone does not establish that its exposition is correct.

Use **Search proof** in the sidebar to locate a late step by its mathematical label, a
phrase in the explanation, or a mapped declaration. Check that each result
names the intended routes and opens the expected argument without changing the
active view.

Reopening a preview restores its reading route, argument and Read/Route map
layout for the same notebook and atlas revision; Statement graph is not persisted. VS Code keeps the position in workspace storage.
The browser preview uses local storage on its origin. Direct argument links
take precedence. Select the first sidebar step or select the current route again to return to its result.

At a shared step, **View this argument in** switches to another reachable
route while keeping the argument selected. Compare each route’s map and explanation; static exports also retain supporting
links and backlinks. In HTML exports these links jump to the corresponding
route section. Keep a shared ID only when both routes discuss the same step.

Run `proof_atlas.py --report path/to/outline.json` with the matching runtime.
For each route, read the reachable argument count, longest depth in support
edges, arguments with multiple reachable parents, and boundaries where the
route stops. Inspect named boundaries for missing explanation and repair any
unreachable argument or broken source binding reported by the validator.

In Explore, notebook-only sidebars are hidden. **Sources and details** consolidates
references, source revision, mapped status and copyable argument links. There is
no Actions menu, separate source-status fold or argument/source pane switch.

The exploration sidebar shows the selected route and current argument. Use its
**Lean proof / Paper proof** choices to change reading routes. Back/Forward name
the arguments previously visited. Route map owns the overview, with no separate
overview toggle. Check paging and the current-step highlight after selecting a
sidebar step, map node or search result. Read remains a bounded prose column,
with assumptions and paper/formalization differences visible. Supporting-link
and backlink navigation in static exports remains useful without the live sidebar.
