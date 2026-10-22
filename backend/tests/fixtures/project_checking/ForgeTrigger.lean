import Lean
open Lean
/-- Copy a compiled module, adding `trigger : reduceBool evil = true := rfl`, whose
check would have run the imported `evil`'s IR through Lean 4.34, and a theorem of
`False` the kernel never checked. Lean 4.35 removed `Lean.reduceBool` and runs no
compiled code in the kernel, so the trigger names a constant no import declares. -/
unsafe def main (args : List String) : IO UInt32 := do
  let [input, output] := args | return 2
  let (data, _) ← readModuleData input
  let bool := mkConst ``Bool
  let yes := mkConst ``Bool.true
  let trigger : ConstantInfo := .thmInfo {
    name := `trigger, levelParams := [],
    type := mkApp3 (mkConst ``Eq [Level.one]) bool
      (mkApp (mkConst `Lean.reduceBool) (mkConst `evil)) yes,
    value := mkApp2 (mkConst ``Eq.refl [Level.one]) bool yes, all := [`trigger] }
  let forged : ConstantInfo := .thmInfo {
    name := `forgedLocal, levelParams := [], type := mkConst ``False,
    value := mkConst ``True.intro, all := [`forgedLocal] }
  saveModuleData output `Example { data with
    constNames := data.constNames ++ #[`trigger, `forgedLocal],
    constants := data.constants ++ #[trigger, forged] }
  return 0
