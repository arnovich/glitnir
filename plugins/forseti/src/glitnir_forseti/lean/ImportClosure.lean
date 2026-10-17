/-
List the transitive import closure of a module, read from `.olean` headers.

    lean --run ImportClosure.lean MODULE...

Loads the MODULEs together through `importModules` without its environment extensions, which
reads module headers and data without running any module's commands, macros,
elaborators or initializers. Prints one line per module in the closure, in
import order: its name, a tab, and the `.olean` file the search path resolves
it to.

This program is fixed: the Python side runs it as it is shipped and never
builds it from checked source.
-/
import Lean
open Lean

def main (arguments : List String) : IO UInt32 := do
  if arguments.isEmpty then
    IO.eprintln "usage: ImportClosure MODULE..."
    return 2
  initSearchPath (← findSysroot)
  let imports := arguments.toArray.map fun module => ({ module := module.toName } : Import)
  let env ← importModules imports {} (loadExts := false)
  for name in env.header.moduleNames do
    IO.println s!"{name}\t{← findOLean name}"
  return 0
