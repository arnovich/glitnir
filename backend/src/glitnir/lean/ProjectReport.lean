/- Appended to the fixed canonical encoders from AxiomReport.lean at packaging/runtime.
Its imports compile with a toolchain-only search path; project modules are data only.

The axioms it reports are display and a cross-check only: the checker decides
statuses by the axioms the kernel replay (`ProjectReplay.lean`) collects, and
fails the check unless these are equal.

Each theorem's `uses` are display only too, and are not cross-checked: the other
theorems its type and value name directly, each with the module that declares
it, which the preview draws as the claims a claim uses. -/

/-- The fields of Lean 4.32.1's private `ExportedAxiomsState`, the state of the
extension through which `collectAxioms` takes an imported constant's axioms
from its `.olean` instead of walking its value. -/
structure ExportedAxioms where
  importedModuleEntries : Array (Array (Name × Array Name))

instance : Inhabited ExportedAxioms := ⟨⟨#[]⟩⟩

/-- Whether `collectAxioms` walks every constant of `env` itself: the one
extension it would consult instead holds no imported entries, as `importModules`
leaves every extension without `loadExts`. A toolchain that renames or drops the
extension fails this, rather than being trusted silently. -/
unsafe def axiomsAreWalked (env : Environment) : IO Bool := do
  let found := (← persistentEnvExtensionsRef.get).filter fun ext =>
    privateToUserName ext.name == `Lean.exportedAxiomsExt
  let #[ext] := found | return false
  let state : ExportedAxioms := unsafeCast (ext.getState env)
  return state.importedModuleEntries.isEmpty

/-- The theorems other than `name` that `info`'s type and value name directly,
each with its module. Of another module's, the lemmas Lean generates are left
out: internal details (`_proof_1`, `match_1`, `eq_1`, private names) and a
constructor's own lemmas (`injEq`, `sizeOf_spec`). -/
def usedTheorems (env : Environment) (module name : Name) (info : ConstantInfo) :
    Array Json := Id.run do
  let mut found : Array Json := #[]
  for used in info.getUsedConstantsAsSet do
    if used == name then continue
    let some usedInfo := env.find? used | continue
    unless usedInfo.isTheorem do continue
    let some index := env.getModuleIdxFor? used | continue
    let usedModule := env.header.moduleNames[index]!
    if usedModule != module && (used.isInternalDetail || env.isConstructor used.getPrefix) then
      continue
    found := found.push <| Json.mkObj [
      ("name", toJson used.toString), ("module", toJson usedModule.toString)]
  return found

unsafe def main (args : List String) : IO UInt32 := do
  let [moduleText, moduleRoot, artifactRoot] := args
    | IO.eprintln "usage: ProjectReport MODULE OUTPUT ARTIFACTS"; return 2
  initSearchPath (← findSysroot)
  searchPathRef.modify (⟨moduleRoot⟩ :: ⟨artifactRoot⟩ :: ·)
  let module := moduleText.toName
  let env ← importModules #[{ module }] {} (trustLevel := 0) (plugins := #[])
    (loadExts := false) (level := .private)
  let some index := env.header.moduleNames.idxOf? module | return 3
  unless ← axiomsAreWalked env do
    IO.eprintln "imported axiom summaries are loaded; refusing to report axioms"
    return 4
  let data := env.header.moduleData[index]!
  let names := data.constNames
  let declared : NameSet := names.foldl (·.insert ·) {}
  let constants := (names.zip data.constants).toList.map fun (n, c) => describe n c
  let imports := data.imports.map fun i => Json.mkObj [
    ("module", toJson i.module.toString), ("all", toJson i.importAll),
    ("exported", toJson i.isExported), ("meta", toJson i.isMeta)]
  let mut theorems : Array Json := #[]
  for name in names do
    let some info := env.find? name | continue
    unless info.isTheorem do continue
    let (axioms, _) ← (collectAxioms name : CoreM (Array Name)).toIO
      { fileName := "", fileMap := default } { env }
    let (printed, _) ← (Meta.ppExpr info.type).run'.toIO
      { fileName := "", fileMap := default } { env }
    theorems := theorems.push <| Json.mkObj [
      ("name", toJson name.toString), ("type", toJson printed.pretty),
      ("canonical", toJson (canonical env declared name info)),
      ("uses", toJson (usedTheorems env module name info)),
      ("axioms", toJson (axioms.map fun n => Json.mkObj [
        ("name", toJson n.toString),
        ("module", toJson ((env.getModuleIdxFor? n).map fun i =>
          env.header.moduleNames[i]!.toString))]))]
  -- The closure as loaded, each module with the `.olean` it was read from; the
  -- checker keys its record of fully replayed imports on these files.
  let modules ← env.header.moduleNames.mapM fun m => do
    return toJson #[m.toString, (← findOLean m).toString]
  IO.println (Json.mkObj [
    ("module", toJson moduleText), ("imports", toJson imports),
    ("constants", toJson constants), ("theorems", toJson theorems),
    ("modules", toJson modules)]).compress
  return 0
