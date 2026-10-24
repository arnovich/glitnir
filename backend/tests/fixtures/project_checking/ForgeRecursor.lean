import Lean
open Lean
/-- Copy a compiled module, renaming the first binder of `Two.rec`'s type: the
same recursor up to binder names, which `==` on expressions ignores. -/
unsafe def main (args : List String) : IO UInt32 := do
  let [input, output] := args | return 2
  let (data, _) ← readModuleData input
  let rename : ConstantInfo → ConstantInfo
    | .recInfo v =>
      match v.name == `Two.rec, v.type with
      | true, .forallE _ t b i => .recInfo { v with type := .forallE `forged t b i }
      | _, _ => .recInfo v
    | info => info
  saveModuleData output `Example.Helper { data with constants := data.constants.map rename }
  return 0
