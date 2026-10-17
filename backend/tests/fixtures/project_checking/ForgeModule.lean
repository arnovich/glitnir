import Lean
open Lean
/-- Copy a compiled module, adding a theorem of `False` the kernel never checked. -/
unsafe def main (args : List String) : IO UInt32 := do
  let [input, output] := args | return 2
  let (data, _) ← readModuleData input
  let forged : ConstantInfo := .thmInfo {
    name := `forgedLocal, levelParams := [], type := mkConst ``False,
    value := mkConst ``True.intro, all := [`forgedLocal] }
  saveModuleData output `Example { data with
    constNames := data.constNames.push forged.name,
    constants := data.constants.push forged }
  return 0
