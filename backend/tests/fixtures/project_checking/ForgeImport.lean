import Lean
open Lean
unsafe def main (args : List String) : IO UInt32 := do
  let [output] := args | return 2
  initSearchPath (← findSysroot)
  let env ← importModules #[{module := `Init.Prelude}] {} (loadExts := false)
  let action : CoreM Unit := withOptions (·.setBool `debug.skipKernelTC true) do
    addDecl <| .thmDecl {name := `bad, levelParams := [], type := mkConst ``False, value := mkConst ``True.intro}
  let (_, state) ← action.toIO {fileName := "", fileMap := default}
    {env := env.setMainModule `Example.Helper}
  writeModule state.env ⟨output⟩
  return 0
