# A complete source-only walkthrough

**Use when:** learning the expected explanatory depth or validating the sibling
Lake setup before writing a larger walkthrough.
**Don't use:** as a claim that an unrelated project builds or that its prose has
been verified. Adapt the argument to the reader; do not copy generic paragraphs.

The files in [assets/counter-example](../assets/counter-example/) form a complete,
small example using only Lean's standard library:

- `counter/Counter.lean`: a public module defining a recursive counter and proving
  that it equals `n+n`, with its Lake configuration, toolchain and MIT licence.
- `counter-walkthrough/Walkthrough.lean`: the explanation and three restated claims.
- `counter-walkthrough/outline.json`: exact declaration headers and source lines.
- `counter-walkthrough/atlas.json`: the result, its induction step and two supports.

The original source revision is recorded in the outline. Public source links
point to that revision in Glitnir; the local source files must match it. The
bundled source is a teaching formalization, not a purported external paper.
There is no paper route or separate trusted statement file.

## What to learn from it

The reader knows induction but is new to Lean. The notebook leads with the
result, explains the model and zero case, then deepens the successor step.
Its central inference is a calculation, not an inventory of theorem names:

$$T(n+1)=T(n)+2=(n+n)+2=(n+1)+(n+1).$$

Compare these two descriptions:

> Weak: The induction step follows from `ticks_step` and `double_step`.

> Useful: The recurrence adds two to the previous count. Substitute the induction
> hypothesis $T(n)=n+n$ inside $k\mapsto k+2$. The arithmetic lemma then identifies
> $(n+n)+2$ with the doubled successor; its equality is used backwards.

The useful paragraph explains the inputs, operation and direction of each
inference. The atlas exposes the same calculation as a subargument inside the
main theorem, explicitly not a separately proved lemma. Its two-edge depth is
appropriate for this small proof; depth is not a quality score.

`Counter.ticks_step (n : Nat)` has a claim card that forwards its round
argument unchanged: `exact Counter.ticks_step (n := n)`. All three cards
restate their original theorem headers. Implicit and instance binders can still
be inferred; unsupported section-variable context remains prose-only.

## Reproduce the validation

Use a disposable directory outside this repository, preserving the two sibling
folders. With `SKILL` set to the absolute skill folder and `DEMO` to a new,
empty destination, copy the example:

```sh
mkdir -p "$DEMO"
cp -R "$SKILL/assets/counter-example/counter" "$DEMO/counter"
cp -R "$SKILL/assets/counter-example/counter-walkthrough" "$DEMO/counter-walkthrough"
```

This bundled example explicitly permits building those disposable copies. It
requires the pinned Lean toolchain already installed; do not download or build
an unrelated formalization. In the commands below, `python` means the matching
Glitnir backend interpreter. Set `REPO` to the local Glitnir clone containing
the pinned source commit. First validate the source mapping and provenance:

```sh
python -B "$SKILL/scripts/walkthrough.py" check "$DEMO/counter-walkthrough/outline.json" --notebook "$DEMO/counter-walkthrough/Walkthrough.lean" --require-provenance --source-repository "$REPO"
python -B "$SKILL/scripts/proof_atlas.py" --report "$DEMO/counter-walkthrough/outline.json"
```

The source check compares the exported source bytes with the recorded commit
in `REPO`, using `project.path`. It must report no provenance, source, type or
claim errors. Atlas coverage should be four arguments, depth two, and two
terminal boundaries. Neither helper grants proof status.

The sibling dependency builds in Lean, but Glitnir's prepared-project inspection
refuses paths outside the notebook project. Prepare a verified local dependency
inside the disposable walkthrough, then point Lake to it:

```sh
python -B "$SKILL/scripts/walkthrough.py" vendor "$DEMO/counter-walkthrough/outline.json" --source-repository "$REPO" --out "$DEMO/counter-walkthrough/vendor-counter"
python -c 'from pathlib import Path; import sys; p = Path(sys.argv[1]); p.write_text(p.read_text().replace("../counter", "vendor-counter"))' "$DEMO/counter-walkthrough/lakefile.toml"
```

Run preparation before building the sibling source: a generated, unpinned
`lake-manifest.json` is refused as an additional source input. The outline still
cites the original sibling source. Vendoring copies verified Git bytes and never
changes mathematics, configurations, or the original source commit. For a real
project, prepared transitive dependencies must also stay inside the inspection
root; this tiny example does not establish that arbitrary projects can relocate.

From `$DEMO/counter-walkthrough`, run:

```sh
lake build
lake env lean Walkthrough.lean
python -m glitnir.notebook check Walkthrough.lean
```

The first command builds the tiny local dependency and the notebook. This tests
an ordinary notebook importing a public `module` file. Inspect the final JSON:
`CounterWalkthrough.recurrence`, `CounterWalkthrough.arithmetic_bridge` and
`CounterWalkthrough.result`
should have `checked` observations under Glitnir's standard axiom policy. Do not
infer an empty axiom list from a checked status. The headless command does not
save `.glitnir.json`; no records are bundled or fabricated.

Open the copied notebook in the Glitnir editor. The `serve` command is an
API started by a client with a session capability, not a standalone browser preview. Inspect the collapsed summaries and expanded induction
calculation, then Explore's Read, Route map and Statement graph. Follow the
induction step to each support and inspect its source qualification. Export HTML
and verify that the same explanations, mathematics and links remain readable.
Use **Check saved file** if this copy should carry saved observations.

The backend tests in `test_walkthrough_example.py` validate exact mappings and
the canonical preview snapshot without Lean; its live tests build the source and
sibling project, verify the external-path refusal, then independently check
claims using the verified dependency prepared by `vendor` inside the walkthrough
project. Live tests require the
project's supported toolchain and sandbox. A future toolchain change must update
both example pins from `project_inspection.TOOLCHAIN` and rerun the live tests.

For an actual walkthrough's delivery, report the commands and observed outcomes
for that copy, including any unavailable preview or check. A passing example is
not evidence that the new walkthrough has been checked.
