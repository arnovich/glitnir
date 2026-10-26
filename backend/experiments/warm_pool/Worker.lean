/- Offline imported-environment experiment. Reports are advisory, never proof authority. -/
import Lean
open Lean Elab

private def errorsAndWarnings (messages : MessageLog) : IO (Array String × Array String) := do
  let mut errors := #[]
  let mut warnings := #[]
  for message in messages.toList do
    if message.severity == .error then
      errors := errors.push ((← message.data.toString).take 4000 |>.toString)
    if message.severity == .warning then
      warnings := warnings.push ((← message.data.toString).take 4000 |>.toString)
  return (errors, warnings)

private def skipTrailing (inputCtx : Parser.InputContext)
    (context : Parser.ParserModuleContext) (parser : Parser.ModuleParserState) :
    Parser.ModuleParserState :=
  let s := Parser.whitespace.run inputCtx context (Parser.getTokenTable context.env)
    { cache := Parser.initCacheForInput inputCtx.inputString, pos := parser.pos }
  if s.errorMsg.isNone && inputCtx.atEnd s.pos then { parser with pos := s.pos } else parser

private def elaborate (input : String) (base : Environment) (options : Options)
    (imports : Array Import) (isModule : Bool) : IO (Array String × Array String) := do
  let context := Parser.mkInputContext input "Candidate.lean"
  let (header, parser, log) ← Parser.parseHeader context
  if HeaderSyntax.imports header != imports || HeaderSyntax.isModule header != isModule then
    return (#["Imports or module mode changed; select a new frozen environment."], #[])
  let (headerErrors, headerWarnings) ← errorsAndWarnings log
  let mut errors := headerErrors
  let mut warnings := headerWarnings
  let mut parser := parser
  -- This resets declarations, scopes and options, but cannot reset arbitrary IO state.
  let mut state := Command.mkState base {} options
  repeat
    let scope := state.scopes.head!
    let parserContext := {
      env := state.env, options := scope.opts,
      currNamespace := scope.currNamespace, openDecls := scope.openDecls }
    let (stx, next, parseLog) := Parser.parseCommand context parserContext (skipTrailing context parserContext parser) {}
    parser := next
    let atEnd := stx.getKind == ``Lean.Parser.Command.eoi
    unless atEnd do
      let commandContext : Command.Context := {
        cmdPos := stx.getPos?.getD 0, fileName := context.fileName,
        fileMap := context.fileMap, snap? := none, cancelTk? := none }
      match (← ((Command.elabCommandTopLevel stx).run commandContext |>.run state).toIO') with
      | .ok ((), after) => state := after
      | .error _ => errors := errors.push "Command elaboration did not complete."
    let mut messages := parseLog ++ state.messages
    for task in state.snapshotTasks do
      for snapshot in task.get.getAll do
        messages := messages ++ snapshot.diagnostics.msgLog
    let (newErrors, newWarnings) ← errorsAndWarnings messages
    errors := (errors ++ newErrors).extract 0 100
    warnings := (warnings ++ newWarnings).extract 0 100
    state := { state with messages := {}, snapshotTasks := #[] }
    if atEnd then break
  return (errors, warnings)

def main (args : List String) : IO UInt32 := do
  let [setupFile, headerFile] := args
    | IO.eprintln "usage: Worker SETUP HEADER"; return 2
  initSearchPath (← findSysroot)
  let setup ← ModuleSetup.load ⟨setupFile⟩
  let original ← IO.FS.readFile headerFile
  let context := Parser.mkInputContext original "Candidate.lean"
  let (header, _, log) ← Parser.parseHeader context
  let options := Elab.async.set (maxHeartbeats.set setup.options.toOptions 1000000) false
  unsafe enableInitializersExecution
  setup.dynlibs.forM Lean.loadDynlib
  let imports := HeaderSyntax.imports header
  let (base, messages) ← processHeaderCore 0 imports
    (setup.isModule || HeaderSyntax.isModule header) options log context
    (mainModule := setup.name) (package? := setup.package?)
    (arts := setup.importArts) (plugins := setup.plugins)
  let (errors, _) ← errorsAndWarnings messages
  unless errors.isEmpty do
    IO.eprintln (Json.compress (toJson errors)); return 2
  let output ← IO.getStdout
  let input ← IO.getStdin
  output.putStrLn (Json.mkObj [("ready", toJson true)]).compress
  output.flush
  repeat
    let line ← input.getLine
    if line.isEmpty then break
    let .ok request := Json.parse line | return 2
    let .ok identity := request.getObjValAs? String "id" | return 2
    let .ok source := request.getObjValAs? String "source" | return 2
    if source.utf8ByteSize > 1000000 then return 2
    output.putStrLn (Json.mkObj [("started", toJson identity)]).compress
    output.flush
    let started ← IO.monoMsNow
    let (errors, warnings) ← elaborate source base options imports (HeaderSyntax.isModule header)
    let elapsed := (← IO.monoMsNow) - started
    let response := Json.mkObj [
      ("id", toJson identity), ("status", toJson (if errors.isEmpty then "elaborated" else "errors")),
      ("errors", toJson errors), ("warnings", toJson warnings), ("elapsed_ms", toJson elapsed)]
    output.putStrLn response.compress
    output.flush
  return 0
