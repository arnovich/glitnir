# Glitnir backend

The Python backend for [Glitnir](https://github.com/arnovich/glitnir), a notebook
format and VS Code extension for Lean. It renders notebooks and runs Lean checks
for the extension. The package is `glitnir`, imported as `glitnir`.
Its packages are layered from `glitnir.providers` and `glitnir.lean` up to
`glitnir.service`; see DEVELOPMENT.md.

Install it through the extension's **Glitnir: Set Up Environment** command, into a
dedicated environment. The core wheel has no runtime dependencies. Views come
from optional provider packages found through the `glitnir.providers` entry
point; `glitnir-forseti` (in `plugins/forseti` of the repository) draws Forseti
circuit, model and dynamics views, runs `-- @evidence simulate|iterate|sample`
directives and decides `-- @decide` goals when installed into the same environment.

To check a saved file from a terminal or coding agent:

```sh
python -m glitnir.notebook check FILE.lean
```

It prints one JSON result and exits 0 when every theorem is kernel checked, 1 when
some contain `sorry` or extra assumptions or when a failing command withheld the
claims after it, and 2 when checking is unavailable.
The editor's **Check saved file** additionally saves results to
`<Notebook>.glitnir.json`.

For operator preparation of a reusable Lean source module before a remote check,
see [checked-source handoffs](../docs/lemma-handoffs.md). The Linux preparation
command builds real Lake artifacts and preflights a worker copy without submitting
a compute job or changing proof authority.

See the [Glitnir README](https://github.com/arnovich/glitnir#readme) for
installation and the notebook format, and
[DEVELOPMENT.md](https://github.com/arnovich/glitnir/blob/main/DEVELOPMENT.md)
for development.

## Taskvalet research links

The preview optionally reads `<Notebook>.research.json` beside a Lean notebook:

```json
{
  "format": "glitnir.taskvalet-research-links/v1",
  "links": [{
    "claim": "Research.target",
    "project": "research-project",
    "problem": "00000000-0000-4000-8000-000000000003",
    "problem_version": 1,
    "declaration": "Research.target",
    "type": "True",
    "context_sha256": "<saved project-check prefix_sha256>",
    "source_sha256": "<sha256 of the complete saved notebook>",
    "environment": {
      "fingerprint": "<saved project-check fingerprint>",
      "check": "<saved project-check identity>",
      "toolchain": "leanprover/lean4:v4.35.0"
    },
    "axiom_policy": ["Classical.choice", "Quot.sound", "propext"]
  }]
}
```

`local_checks` may list helper claims as objects with `claim`, fully qualified
`declaration`, and exact checked `type`. Every link is re-bound to the current
saved source and `.glitnir.json` project-check records. Older records without
the exact checked-type digest remain readable, but their research link is stale
until the next explicit project check.

Configure `GLITNIR_TASKVALET_URL` and `GLITNIR_TASKVALET_TOKEN` in the backend
environment. The reader accepts HTTPS and loopback HTTP, refuses redirects,
bounds response and history sizes, and exposes no Taskvalet write operation.
