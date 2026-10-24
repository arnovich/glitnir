import Lean
open Lean

/-- Lean 4.35.0's `Expr` constructors up to `forallE`, with the cached `data`
Lean stores after their fields; the others are placeholders that keep the
constructor indices. Used only to rebuild one node with other cached data. -/
inductive ExprMirror where
  | c0 | c1 | c2 | c3 | c4
  | app (fn arg : Expr) (data : UInt64)
  | c6
  | forallE (name : Name) (type body : Expr) (info : BinderInfo) (data : UInt64)

/-- `e` with its root node's level-parameter flag flipped (bit 43 of
`Expr.data`), its hash and structure unchanged, so `==` still holds. -/
unsafe def flipLevelParamFlag (e : Expr) : Option Expr :=
  let data : UInt64 := UInt64.xor e.data ((1 : UInt64) <<< 43)
  match e with
  | .app f a => some (unsafeCast (ExprMirror.app f a data))
  | .forallE n t b i => some (unsafeCast (ExprMirror.forallE n t b i data))
  | _ => none

/-- `e` with its root binder renamed, which `==` ignores. -/
def renameBinder : Expr → Option Expr
  | .forallE _ t b i => some (.forallE `forged t b i)
  | _ => none

def setType (info : ConstantInfo) (type : Expr) : ConstantInfo :=
  match info with
  | .thmInfo v => .thmInfo { v with type }
  | .axiomInfo v => .axiomInfo { v with type }
  | .recInfo v => .recInfo { v with type }
  | other => other

/-- Copy a compiled module, changing the type of its constant NAME as MODE says:
`identity` changes nothing (a control for the round trip), `binder` renames the
type's root binder, `data` flips its root node's cached level-parameter flag.
The changed type is `==` to the original either way. Exit 3 if that fails. -/
unsafe def main (args : List String) : IO UInt32 := do
  let [mode, name, module, input, output] := args | return 2
  let (data, _) ← readModuleData input
  let target := name.toName
  let mut constants := data.constants
  let mut found := mode == "identity"
  for info in data.constants, i in [0:data.constants.size] do
    unless info.name == target && mode != "identity" do continue
    let changed := if mode == "binder" then renameBinder info.type
      else if mode == "data" then flipLevelParamFlag info.type else none
    let some type := changed | return 3
    unless type == info.type && (mode != "data" || type.data != info.type.data) do return 3
    constants := constants.set! i (setType info type)
    found := true
  unless found do return 3
  saveModuleData output module.toName { data with constants }
  return 0
