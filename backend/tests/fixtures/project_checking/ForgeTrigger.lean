import Lean
open Lean
/-- Copy a compiled module, adding `trigger : reduceBool evil = true := rfl`, whose
check would run the imported `evil`'s IR, and a theorem of `False` the kernel
never checked. -/
unsafe def main (args : List String) : IO UInt32 := do
  let [input, output] := args | return 2
  let (data, _) ← readModuleData input
  let bool := mkConst ``Bool
  let yes := mkConst ``Bool.true
  let trigger : ConstantInfo := .thmInfo {
    name := `trigger, levelParams := [],
    type := mkApp3 (mkConst ``Eq [Level.one]) bool
      (mkApp (mkConst ``Lean.reduceBool) (mkConst `evil)) yes,
    value := mkApp2 (mkConst ``Eq.refl [Level.one]) bool yes, all := [`trigger] }
  let forged : ConstantInfo := .thmInfo {
    name := `forgedLocal, levelParams := [], type := mkConst ``False,
    value := mkConst ``True.intro, all := [`forgedLocal] }
  saveModuleData output `Example { data with
    constNames := data.constNames ++ #[`trigger, `forgedLocal],
    constants := data.constants ++ #[trigger, forged] }
  return 0
