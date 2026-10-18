# Community REPL compatibility build

Glitnir's core no longer includes the Python proof-state adapter or its tests;
these build notes are kept for the Forseti plugin, which will own that tooling.
The adapter used
[leanprover-community/repl](https://github.com/leanprover-community/repl), at
`68a3b3a059787a7db44fb1e6281e4a657efee470` (tag `v4.32.0`), compiled with
Forseti's unchanged `leanprover/lean4:v4.32.1` toolchain.

Apply `source_positions.patch` before building. Upstream parses each tactic as
new text but runs it with the original declaration's file map. `apply?` uses
those positions for suggestions; a live test triggered a UTF-8 position panic.
The patch gives the tactic its own file map. It changes search machinery only.
The final candidate is replayed by ordinary Lean, without importing REPL.

First complete [the Python backend setup](../../../../DEVELOPMENT.md#build-and-test)
and [the Lean setup](../../../../extension/media/setup.md).
From Glitnir's `backend/` directory, choose a new durable directory for the REPL.
The example is beside the Glitnir checkout; use another new path if it exists:

```bash
repo_root="$PWD"
repl_dir="$repo_root/../../forseti-lean-repl-4.32.1"
git clone https://github.com/leanprover-community/repl.git "$repl_dir"
git -C "$repl_dir" checkout --detach 68a3b3a059787a7db44fb1e6281e4a657efee470
git -C "$repl_dir" apply --unidiff-zero "$repo_root/tools/lean_repl/source_positions.patch"
(
  cd "$repl_dir"
  elan run leanprover/lean4:v4.32.1 lake build
)
export FORSETI_LEAN_REPL="$(cd "$repl_dir" && pwd)/.lake/build/bin/repl"
test -x "$FORSETI_LEAN_REPL"
```

The adapter selects the toolchain sysroot explicitly and creates a temporary
merged import directory for the pinned Asgard/Forseti `Gimle` namespace. It
neither modifies the selected project nor calls Lake during a session. The
project must already be built and must pass `verify_project`.

The executable is explicitly supplied, untrusted search tooling; the adapter
does not authenticate arbitrary binaries. Benchmark output records its SHA-256.

The exported path lasts for this shell. Keep the checkout and build artifacts
available, and save the absolute export in local shell configuration if desired.

Rebuild the REPL
when deliberately changing the pinned Lean toolchain; its revision and patch are
part of the tested integration, rather than a request to track upstream main.
