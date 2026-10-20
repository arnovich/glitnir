/- Elaborate a composed view source. This program never grants proof authority. -/
import Lean.Elab.Command
import Lean.Elab.Import
open Lean Elab

/-! Usage: `ViewElab REQUEST REPORT`.

REQUEST is `{"source": path, "setup": path, "marker": m, "views": [{"at": n,
"preamble": p, "command": c}]}`. SOURCE is the provider's imports and the
notebook before its last view's directive. Each view is spliced in at byte `at`
of SOURCE, the start of its directive line: the notebook before it is
elaborated, then the view's preamble and command. The `at` values are
non-decreasing, each at most the size of SOURCE and never inside a character.

REPORT is `{"views": [{"information": [t], "errors": [e], "proofs_skipped": b}]}`
in request order. A view's information is what its preamble and command
printed. Its errors are SOURCE's before its `at`, each `{"line": n, "text": t}`
with `n` its 1-based line in SOURCE, then its preamble's and command's, which
carry no line.

The preamble is the provider's own code, so it starts in a fresh top-level
scope: none of the notebook's namespaces, `open`s, options (it has the setup's)
or variables, which could otherwise make its names ambiguous. Only that scope
is swapped. Extensions the notebook activated in the environment, such as its
local and scoped notation, instances and attributes, still apply to the
preamble.

The command is elaborated back in the notebook's scopes, where the directive's
names are written, with the `open`s of the preamble's final scope added: a
command may use what its preamble opened. Names in the notebook's namespaces
still resolve first; one that both the notebook's and the preamble's `open`s
resolve is ambiguous, as Lean reports. Neither the preamble nor the command
changes what the notebook after the directive sees: the notebook continues from
its own state at the splice.

The imports are processed once. The notebook is then elaborated twice at most,
each pass starting from the imported environment. The first pass skips its
proofs: a `theorem` or `lemma` whose value is `:= term` gets the value
`sorryAx _ true` (as `ProjectRead.lean` reads it) and an `example` is parsed but
not elaborated; every other command, the preambles and the commands are
elaborated as written. A view whose information then has no message starting
with the marker is drawn again in a second pass, which elaborates the notebook
as written; its report replaces the first pass's, with `proofs_skipped` false.

Lean's parser attaches the whitespace and comments after a command to it, so
each part of SOURCE is parsed over SOURCE cut at the next view's `at`: a
command never reads past a splice, and the parser resumes there, at the start
of a line, where the next part begins. -/

private def theoremCommand (stx : Syntax) : Bool :=
  (stx.getKind == ``Lean.Parser.Command.declaration &&
    stx[1].getKind == ``Lean.Parser.Command.theorem) || stx.getKind == `lemma

private def exampleCommand (stx : Syntax) : Bool :=
  stx.getKind == ``Lean.Parser.Command.declaration &&
    stx[1].getKind == ``Lean.Parser.Command.example

private def noProof (stx : Syntax) : Command.CommandElabM Syntax := do
  let value ← `(Lean.Parser.Command.declValSimple| := sorryAx _ true)
  return stx.setArg 1 (stx[1].setArg 3 value)

/-- One view of the request. -/
private structure View where
  start : Nat
  preamble : String
  command : String
  deriving Inhabited

/-- What one view reported, and whether the notebook's proofs were skipped. -/
private structure ViewReport where
  information : Array String := #[]
  errors : Array Json := #[]
  proofsSkipped : Bool
  deriving Inhabited

private def ViewReport.json (view : ViewReport) : Json :=
  Json.mkObj [("information", toJson view.information), ("errors", toJson view.errors),
    ("proofs_skipped", toJson view.proofsSkipped)]

private structure Request where
  source : String
  setup : String
  marker : String
  views : Array View

private def readRequest (text : String) : Except String Request := do
  let json ← Json.parse text
  let views ← (← json.getObjValAs? (Array Json) "views").mapM fun view => do
    return { start := ← view.getObjValAs? Nat "at",
             preamble := ← view.getObjValAs? String "preamble",
             command := ← view.getObjValAs? String "command" }
  return { source := ← json.getObjValAs? String "source",
           setup := ← json.getObjValAs? String "setup",
           marker := ← json.getObjValAs? String "marker", views }

/-- An error entry; `line` only for a message in SOURCE. -/
private def errorEntry (message : Message) (line : Bool) : IO Json := do
  let text := ("text", toJson (← message.data.toString))
  return Json.mkObj <| if line then [("line", toJson message.pos.line), text] else [text]

/-- `parser` moved past the whitespace and comments at its position when nothing
else is left before the end of `inputCtx`. Lean's parser reads them only before
a command, so a part of SOURCE holding nothing else, such as the line between
two adjacent directives, would otherwise end in an "unexpected end of input". -/
private def skipTrailing (inputCtx : Parser.InputContext)
    (context : Parser.ParserModuleContext) (parser : Parser.ModuleParserState) :
    Parser.ModuleParserState :=
  let s := Parser.whitespace.run inputCtx context (Parser.getTokenTable context.env)
    { cache := Parser.initCacheForInput inputCtx.inputString, pos := parser.pos }
  if s.errorMsg.isNone && inputCtx.atEnd s.pos then { parser with pos := s.pos } else parser

/-- Parse and elaborate the commands of `inputCtx` from `parser` to its end.
Returns the state and parser position after them, the information they printed
and their errors, which carry their line when `line` is set. With `skip`, proofs
are skipped as the first pass does. -/
private def elaborate (inputCtx : Parser.InputContext) (skip line : Bool)
    (state : Command.State) (parser : Parser.ModuleParserState) :
    IO (Command.State × Parser.ModuleParserState × Array String × Array Json) := do
  let mut state := state
  let mut parser := parser
  let mut information : Array String := #[]
  let mut errors : Array Json := #[]
  repeat
    let scope := state.scopes.head!
    let context := {
      env := state.env, options := scope.opts,
      currNamespace := scope.currNamespace, openDecls := scope.openDecls }
    let (stx, next, log) :=
      Parser.parseCommand inputCtx context (skipTrailing inputCtx context parser) {}
    parser := next
    let atEnd := stx.getKind == ``Lean.Parser.Command.eoi
    unless atEnd || (skip && exampleCommand stx) do
      let context : Command.Context := {
        cmdPos := stx.getPos?.getD 0, fileName := inputCtx.fileName,
        fileMap := inputCtx.fileMap, snap? := none, cancelTk? := none }
      let act : Command.CommandElabM Unit := do
        if skip && theoremCommand stx &&
            stx[1][3].getKind == ``Lean.Parser.Command.declValSimple then
          Command.elabCommandTopLevel (← noProof stx)
        else
          Command.elabCommandTopLevel stx
      match (← (act.run context |>.run state).toIO') with
      | .ok ((), after) => state := after
      | .error _ =>
        errors := errors.push (Json.mkObj [("text", "Command elaboration did not complete.")])
    -- Each top-level command starts with an empty message log.
    let mut messages := log ++ state.messages
    for task in state.snapshotTasks do
      for snapshot in task.get.getAll do
        messages := messages ++ snapshot.diagnostics.msgLog
    for message in messages.toList do
      if message.severity == .error then
        errors := errors.push (← errorEntry message line)
      if message.severity == .information then
        information := information.push (← message.data.toString)
    state := { state with messages := {}, snapshotTasks := #[] }
    if atEnd then break
  return (state, parser, information, errors)

/-- Elaborate the notebook once, from the imported environment `env`, splicing
in the views whose indices `chosen` lists in ascending order; report each. -/
private def pass (input fileName : String) (env : Environment) (opts : Options)
    (parser : Parser.ModuleParserState) (headerErrors : Array Json)
    (views : Array View) (chosen : Array Nat) (skip : Bool) :
    IO (Array ViewReport) := do
  let mut state := Command.mkState env {} opts
  let mut parser := parser
  let mut sourceErrors := headerErrors
  let mut reports : Array ViewReport := #[]
  for index in chosen do
    let view := views[index]!
    -- The offsets were checked to fall between characters.
    let part := (String.fromUTF8? (input.toUTF8.extract 0 view.start)).getD ""
    let (after, next, _, errors) ←
      elaborate (Parser.mkInputContext part fileName) skip true state parser
    parser := next
    sourceErrors := sourceErrors ++ errors
    -- The notebook's state at the splice, which the next part continues.
    state := after
    let fresh := { after with scopes := [{ header := "", opts := opts }] }
    let (preambled, _, preambleInformation, preambleErrors) ←
      elaborate (Parser.mkInputContext view.preamble fileName) false false fresh {}
    let opened := preambled.scopes.head!.openDecls
    let inScope := { preambled with scopes := match after.scopes with
      | top :: rest => { top with openDecls := opened ++ top.openDecls } :: rest
      | [] => [] }
    let (_, _, commandInformation, commandErrors) ←
      elaborate (Parser.mkInputContext view.command fileName) false false inScope {}
    reports := reports.push {
      information := preambleInformation ++ commandInformation,
      errors := sourceErrors ++ preambleErrors ++ commandErrors,
      proofsSkipped := skip }
  return reports

/-- Elaborate the request's views and report their information and errors. -/
def main (args : List String) : IO UInt32 := do
  let [requestFile, report] := args
    | IO.eprintln "usage: ViewElab REQUEST REPORT"; return 2
  let request := readRequest (← IO.FS.readFile requestFile)
  if let .error error := request then
    IO.eprintln s!"ViewElab: malformed REQUEST: {error}"; return 2
  let .ok { source, setup := setupFile, marker, views } := request | return 2
  let input ← IO.FS.readFile source
  let size := input.utf8ByteSize
  let mut previous := 0
  for view in views do
    unless previous ≤ view.start && view.start ≤ size do
      IO.eprintln s!"ViewElab: offsets must be non-decreasing and at most {size}, \
        the size of SOURCE; got {view.start} after {previous}"
      return 2
    if (String.fromUTF8? (input.toUTF8.extract 0 view.start)).isNone then
      IO.eprintln s!"ViewElab: offset {view.start} is inside a character"
      return 2
    previous := view.start
  initSearchPath (← findSysroot)
  let setup ← ModuleSetup.load ⟨setupFile⟩
  let inputCtx := Parser.mkInputContext input source
  let (header, parserState, headerLog) ← Parser.parseHeader inputCtx
  let opts := Elab.async.set (maxHeartbeats.set setup.options.toOptions 1000000) false
  unsafe enableInitializersExecution
  setup.dynlibs.forM Lean.loadDynlib
  let (env, messages) ← processHeaderCore 0 (HeaderSyntax.imports header)
    (setup.isModule || HeaderSyntax.isModule header) opts headerLog inputCtx
    (mainModule := setup.name) (package? := setup.package?)
    (arts := setup.importArts) (plugins := setup.plugins)
  let mut headerErrors : Array Json := #[]
  for message in messages.toList do
    if message.severity == .error then
      headerErrors := headerErrors.push (← errorEntry message true)
  let all := (List.range views.size).toArray
  let mut reports ← pass input source env opts parserState headerErrors views all true
  let missed := all.filter fun index =>
    !reports[index]!.information.any (·.startsWith marker)
  unless missed.isEmpty do
    let again ← pass input source env opts parserState headerErrors views missed false
    for (index, redrawn) in missed.zip again do
      reports := reports.set! index redrawn
  IO.FS.writeFile report
    (Json.mkObj [("views", toJson (reports.map ViewReport.json))]).compress
  return 0
