/- Elaborate a composed view source. This program never grants proof authority. -/
import Lean.Elab.Command
import Lean.Elab.Import
open Lean Elab

/-- Elaborate the composed source and report its information and error messages.
Each error is `{"line": n, "text": t}`, `line` being its 1-based line in SOURCE.

SOURCE is the notebook before the directive, the provider's preamble from byte
PREAMBLE and its command from byte COMMAND. The preamble is the provider's own
code, so it is elaborated at the top level, outside the notebook's namespaces,
`open`s, options and variables, which could otherwise make its names ambiguous.
Its declarations and syntax remain; the command is elaborated back in the
notebook's scope, where the directive's names are written. -/
def main (args : List String) : IO UInt32 := do
  let [source, setupFile, preambleArg, commandArg, report] := args
    | IO.eprintln "usage: ViewElab SOURCE SETUP PREAMBLE COMMAND REPORT"; return 2
  let (some preambleStart, some commandStart) := (preambleArg.toNat?, commandArg.toNat?)
    | IO.eprintln "ViewElab: PREAMBLE and COMMAND must be byte offsets"; return 2
  initSearchPath (← findSysroot)
  let setup ← ModuleSetup.load ⟨setupFile⟩
  let input ← IO.FS.readFile source
  let inputCtx := Parser.mkInputContext input source
  let (header, parserState, headerLog) ← Parser.parseHeader inputCtx
  let opts := Elab.async.set (maxHeartbeats.set setup.options.toOptions 1000000) false
  unsafe enableInitializersExecution
  setup.dynlibs.forM Lean.loadDynlib
  let (env, messages) ← processHeaderCore 0 (HeaderSyntax.imports header)
    (setup.isModule || HeaderSyntax.isModule header) opts headerLog inputCtx
    (mainModule := setup.name) (package? := setup.package?)
    (arts := setup.importArts) (plugins := setup.plugins)
  -- Each top-level command starts with an empty message log, so header and
  -- parser diagnostics are carried here until the command's are collected.
  let mut pending := messages
  let mut state := Command.mkState env {} opts
  let mut parser := parserState
  let mut information : Array String := #[]
  let mut errors : Array Json := #[]
  -- The notebook's scopes while the preamble is elaborated at the top level.
  let mut notebookScopes : Option (List Command.Scope) := none
  let mut commandReached := false
  repeat
    -- Switch scopes before parsing: scoped syntax depends on what is open.
    let position := parser.pos.byteIdx
    if !commandReached && position ≥ commandStart then
      commandReached := true
      if let some scopes := notebookScopes then
        state := { state with scopes := scopes }
        notebookScopes := none
    else if !commandReached && notebookScopes.isNone && position ≥ preambleStart then
      notebookScopes := some state.scopes
      state := { state with scopes := [{ header := "", opts := opts }] }
    let scope := state.scopes.head!
    let context := {
      env := state.env, options := scope.opts,
      currNamespace := scope.currNamespace, openDecls := scope.openDecls }
    let (stx, next, log) := Parser.parseCommand inputCtx context parser {}
    parser := next
    pending := pending ++ log
    let atEnd := stx.getKind == ``Lean.Parser.Command.eoi
    unless atEnd do
      let context : Command.Context := {
        cmdPos := stx.getPos?.getD 0,
        fileName := source, fileMap := inputCtx.fileMap, snap? := none, cancelTk? := none }
      let act := Command.elabCommandTopLevel stx
      match (← (act.run context |>.run state).toIO') with
      | .ok ((), after) => state := after
      | .error _ =>
        errors := errors.push (Json.mkObj [("text", "Command elaboration did not complete.")])
    let mut messages := pending ++ state.messages
    pending := {}
    for task in state.snapshotTasks do
      for snapshot in task.get.getAll do
        messages := messages ++ snapshot.diagnostics.msgLog
    for message in messages.toList do
      if message.severity == .error then
        errors := errors.push (Json.mkObj [
          ("line", toJson message.pos.line), ("text", toJson (← message.data.toString))])
      if message.severity == .information then
        information := information.push (← message.data.toString)
    state := { state with messages := {}, snapshotTasks := #[] }
    if atEnd then break
  IO.FS.writeFile report (Json.mkObj [
    ("information", toJson information), ("errors", toJson errors)]).compress
  return 0
