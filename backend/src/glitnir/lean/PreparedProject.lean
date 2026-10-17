/- Inspect existing package contents without invoking Git or changing the workspace.
The dependency traversal and module setup are Lake's; Git provenance is not checked. -/
module
import all Lake.Load.Resolve
import Lake.Load.Workspace
import Lake.Build.Module
import Lake.Build.Run
import Lake.Config.InstallPath

open Lean Lake System

private def inside (root path : FilePath) : IO FilePath := do
  let actual ← IO.FS.realPath path
  unless root.components.isPrefixOf actual.components do
    throw <| IO.userError s!"prepared package path is outside the project: {path}"
  return actual

private def recorded (root : FilePath) (inputs : IO.Ref (Array Json))
    (path : FilePath) : IO (Option String) := do
  -- Python also resolves and validates absent paths after the helper exits.
  unless root.components.isPrefixOf path.normalize.components do
    throw <| IO.userError s!"prepared input is outside the project: {path}"
  let text ← try
    let _ ← inside root path
    some <$> IO.FS.readFile path
  catch e =>
    if e matches .noFileOrDirectory .. then pure none else throw e
  let entry := Json.mkObj [("path", toJson path.toString), ("contents", toJson text)]
  let previous ← inputs.get
  if let some old := previous.find? (fun j =>
      (j.getObjValAs? String "path").toOption == some path.toString) then
    unless old == entry do
      throw <| IO.userError "prepared package input changed during inspection"
  else
    inputs.modify (·.push entry)
  return text

private def parseManifest (path : FilePath) (text : Option String) : Except IO.Error Manifest :=
  match text with
  | none => .error (.noFileOrDirectory path.toString 0 "")
  | some text => (Manifest.parse text).mapError (IO.userError ∘ (s!"{path}: " ++ ·))

private def inspect (source : FilePath) : LoggerIO Json := do
  let root ← IO.FS.realPath (← IO.currentDir)
  let (_, some lean, some lake) ← findInstall?
    | error "supported Lean/Lake installation is missing"
  let environment ← match (← (Env.compute lake lean none (some true)).toBaseIO) with
    | .ok env => pure env
    | .error e => error e
  let config : LoadConfig := {lakeEnv := environment, wsDir := root, updateToolchain := false}
  let ws ← loadWorkspaceRoot config
  let inputs ← IO.mkRef #[]
  let manifestText ← recorded root inputs ws.manifestFile
  let manifest ← match parseManifest ws.manifestFile manifestText with
    | .ok manifest => pure manifest
    | .error e => error e.toString
  let overridesText ← recorded root inputs ws.packageOverridesFile
  let overrides ← match overridesText with
    | none => pure #[]
    | some text => match Manifest.parseEntries text with
      | .ok entries => pure entries
      | .error e => error e
  let entries := (manifest.packages ++ overrides).foldl
    (fun map entry => map.insert entry.name entry) (mkNameMap PackageEntry)
  let relPkgsDir := manifest.packagesDir?.getD ws.relPkgsDir
  let packages ← IO.mkRef #[]
  let resolve (_pkg : Package) (dep : Dependency) (current : Workspace) : LoggerIO MaterializedDep := do
    let some entry := entries.find? dep.name
      | error s!"dependency {dep.name} is not in the captured prepared package map"
    let (relPkgDir, remoteUrl) := match entry.src with
      | .path dir => (dir, "")
      | .git url _ _ subDir? =>
        let dir := relPkgsDir / entry.dirName
        (subDir?.map (dir / ·) |>.getD dir, Git.filterUrl? url |>.getD "")
    let pkgDir ← inside root (current.dir / relPkgDir)
    -- The ordinary resolver selects .lean/.toml for extensionless config names.
    let cfg ← realConfigFile (pkgDir / entry.configFile)
    if cfg.toString.isEmpty then error s!"missing prepared package configuration: {pkgDir}"
    let _ ← recorded root inputs cfg
    let manifest? ← match entry.manifestFile? with
      | none => pure <| .error (.noFileOrDirectory "" 0 "")
      | some file => do
        let path := pkgDir / file
        pure <| parseManifest path (← recorded root inputs path)
    packages.modify (·.push <| Json.mkObj [
      ("name", toJson entry.name.toString), ("path", toJson pkgDir.toString),
      ("entry", toJson entry)])
    return {pkgDir, relPkgDir, remoteUrl, manifest?, manifestEntry := entry}
  let ws ← ws.resolveDepsCore resolve ws.root.wsIdx ws.wsIdx_root_lt
    ws.packages.size ws.wsIdx_root_lt {} false
  let build : BuildConfig := {noBuild := true}
  let setup ← ws.runBuild (cfg := build) do
    setupServerModule source.toString (← IO.FS.realPath source) none
  return Json.mkObj [
    ("setup", toJson setup),
    ("resolution", Json.mkObj [
      ("kind", toJson "prepared_local"),
      ("packages", toJson (← packages.get)), ("inputs", toJson (← inputs.get))])]

public def main (args : List String) : IO UInt32 := do
  let ["setup-file", source] := args | IO.eprintln "usage: PreparedProject SOURCE"; return 2
  initSearchPath (← findSysroot)
  let some result ← (inspect ⟨source⟩).toBaseIO {}
    | IO.eprintln "Glitnir: inspection failed"; return 1
  IO.println result.compress
  return 0
