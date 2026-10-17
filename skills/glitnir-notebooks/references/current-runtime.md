# Headless validation

Use the Python interpreter configured for Glitnir (`glitnir.pythonPath`), not an
arbitrary system Python.

With that interpreter, run:

```sh
python -m glitnir.notebook check path/to/Notebook.lean
```

This emits a JSON observation without writing `<Notebook>.glitnir.json`; the
editor's **Check saved file** is what saves results. Exit 0 means the whole
snapshot passed and all reported named theorems use only the standard axiom
policy; 1 means admitted/additional-assumption results, or claims withheld after
a failing command (see `withheld`); 2 means unavailable.
Read `theorems`, their compiled `type` and `axioms` (collected by the
independent kernel replay; they decide each status), and the captured source and
environment digests. `replay` is `full`, or `imports cached` when the imported
declarations' kernel replay was reused from an earlier check of byte-identical
imports, or `imports cached (+N modules)` when only N imported modules were
replayed on top of such an earlier replay; the notebook's own declarations are
replayed either way. A historical result never establishes that dependencies
are still unchanged. Missing names or unsupported syntax are not successful
checks of those source claims. Ordinary `lake env lean` remains useful diagnostic
validation but is not a substitute for independent replay.

Use installed Lean 4.32.1 and a prepared Lake environment, or a loose file's
supported default. Sibling notebooks are ordinary Lean imports of the same Lake
project; an unbuilt import gets a hint to run `lake build`. No dependencies are fetched or built by checking.
Preserve existing toolchain/import choices and report unsupported setup.
