/- Read supported ordinary declarations independently of their proof values.
The selected project's frontend interprets statements; this is not proof authority.
This fixed helper compiles using only the trusted toolchain search path.

An `example` whose type is a proposition is read as the theorem the checker
compiles in its place: `private theorem TAGn`, where `n` is the byte offset of
its `example` keyword and TAG a prefix that occurs nowhere in the source. The
checker rewrites exactly that keyword in the text it compiles. Any other
`example` (no type, or a type that is not a proposition) claims nothing, is
read as Lean reads it and is listed under `examples`.

`#print axioms c` is the one `#`-command read: it only prints, so it is
elaborated like any other command and accepted when it declares nothing. Lean
realizes reserved names such as `f.eq_1` on demand, adding them to the module,
so one that does is refused like an unsupported command. Its output is an
information message, never an error, and decides nothing: statuses come from the
axioms the kernel replay collects.

Each theorem's `statement` is its type as Lean pretty-prints it right after the
command, in that command's namespace, open declarations and options: what a
reader of the notebook would see in the editor. It is display only, like
everything else read from this report besides which commands are claims; the
checker shows it beside the kernel form the independent reporter prints.

With REST, the source is the start of a longer saved file whose remaining text
REST holds, cut at a line start for a check through a chosen claim. The source's
last command is then parsed again, in the same parser state, over the source
followed by REST; if Lean would read that command further into REST (a proof
continuing below the cut, a `termination_by` clause), the cut splits it, and the
check stops at that command as at a failing one, so the command is never
checked shortened. REST is only parsed, never elaborated. This is a second look
only: the checker cuts a prefix only where its own trusted lexer finds a command
end, and never relies on this untrusted report for that.

This program elaborates the source's own code, so its report is untrusted: the
checker finds every `example` keyword itself with its own lexer, requires this
report to account for exactly those, and takes from it only which are claims. -/
import Lean
open Lean Elab

private def theoremCommand (stx : Syntax) : Bool :=
  (stx.getKind == ``Lean.Parser.Command.declaration &&
    stx[1].getKind == ``Lean.Parser.Command.theorem) || stx.getKind == `lemma

/-- Peel only scoped options around a named theorem. Other `in` commands stay
unsupported; in particular, examples keep their existing keyword-only rewrite. -/
private def scopedTheorem (stx : Syntax) : Option (Array Syntax × Syntax) := Id.run do
  let mut wrappers := #[]
  let mut command := stx
  while command.getKind == ``Lean.Parser.Command.in do
    unless command.getNumArgs == 3 &&
        command[0].getKind == ``Lean.Parser.Command.set_option do return none
    wrappers := wrappers.push command
    command := command[2]
  if !wrappers.isEmpty && !theoremCommand command then return none
  return some (wrappers, command)

/-- Restore the exact scoped syntax around a theorem whose proof was masked. -/
private def wrapOptions (wrappers : Array Syntax) (command : Syntax) : Syntax :=
  wrappers.foldr (fun wrapper body => wrapper.setArg 2 body) command

/-- Re-enter the options for display only, after Lean closed the theorem's scope. -/
private def inOptionScope (wrappers : Array Syntax) (act : Command.CommandElabM α) :
    Command.CommandElabM α := Command.withScope id do
  for wrapper in wrappers do Command.elabCommandTopLevel wrapper[0]
  act

private def exampleCommand (stx : Syntax) : Bool :=
  stx.getKind == ``Lean.Parser.Command.declaration &&
    stx[1].getKind == ``Lean.Parser.Command.example

private def noProof (stx : Syntax) : Command.CommandElabM Syntax := do
  let value ← `(Lean.Parser.Command.declValSimple| := sorryAx _ true)
  return stx.setArg 1 (stx[1].setArg 3 value)

private def allowed (kind : Name) : Bool :=
  [``Lean.Parser.Command.declaration, ``Lean.Parser.Command.namespace,
   ``Lean.Parser.Command.section,
   ``Lean.Parser.Command.end, ``Lean.Parser.Command.open, ``Lean.Parser.Command.export,
   ``Lean.Parser.Command.variable, ``Lean.Parser.Command.set_option,
   ``Lean.Parser.Command.attribute, ``Lean.Parser.Command.moduleDoc, `lemma].contains kind

/-- `#print axioms c`; every other `#print` form is a different syntax kind. -/
private def printAxioms (kind : Name) : Bool :=
  kind == ``Lean.Parser.Command.printAxioms

private def errors (messages : MessageLog) : IO Bool := do
  let mut failed := false
  for message in messages.toList do
    if message.severity == .error then
      IO.eprintln (← message.data.toString)
      failed := true
  return failed

/-- Record where checking stopped, so the checker can retry the earlier source. -/
private def writeFailure (report : String) (failedAt : Nat)
    (declarations : Array Json) : IO Unit :=
  IO.FS.writeFile report (Json.mkObj [
    ("failed_at", toJson failedAt), ("declarations", toJson declarations)]).compress

/-- Elaborate `act` on `state`: none if it raised, else its result, the state
after it and every message it produced. -/
private def runCommand (context : Command.Context) (state : Command.State)
    (act : Command.CommandElabM α) : IO (Option (α × Command.State × MessageLog)) := do
  match (← (act.run context |>.run state).toIO') with
  | .error _ => return none
  | .ok (result, after) =>
    let mut messages := after.messages
    for task in after.snapshotTasks do
      for node in task.get.getAll do messages := messages ++ node.diagnostics.msgLog
    return some (result, { after with snapshotTasks := #[], messages := {} }, messages)

/-- Declaration modifiers holding `doc` (an optional docstring node) and each
of `extra` at its index, and nothing else. -/
private def modifiers (doc : Syntax) (extra : List (Nat × Syntax)) : Syntax :=
  mkNode ``Lean.Parser.Command.declModifiers <| (List.range 7).toArray.map fun i =>
    if i == 0 then doc else
    match extra.lookup i with
    | some modifier => mkNullNode #[modifier]
    | none => mkNullNode

/-- The example command `stx` as the checker compiles it: its `example` keyword
replaced by `private theorem name`, everything else (docstring, signature and
value) unchanged. -/
private def exampleTheorem (stx : Syntax) (name : Name) : Syntax :=
  let ex := stx[1]
  let keyword := ex[0]
  let declId := mkNode ``Lean.Parser.Command.declId #[mkIdentFrom keyword name, mkNullNode]
  let sig := mkNode ``Lean.Parser.Command.declSig #[ex[1][0], ex[1][1][0]]
  let visibility := mkNode ``Lean.Parser.Command.private #[mkAtomFrom keyword "private"]
  mkNode ``Lean.Parser.Command.declaration #[modifiers stx[0][0] [(2, visibility)],
    mkNode ``Lean.Parser.Command.theorem #[mkAtomFrom keyword "theorem", declId, sig, ex[2]]]

/-- Whether the type of the example `stx` is a proposition, read from a
`private noncomputable def` with its signature and no value: the header Lean
elaborates for an example, private like an example. None when that header does
not elaborate alone. -/
private def exampleIsProp (stx : Syntax) (name : Name) :
    Command.CommandElabM (Option Bool) := do
  let ex := stx[1]
  let keyword := ex[0]
  let declId := mkNode ``Lean.Parser.Command.declId #[mkIdentFrom keyword name, mkNullNode]
  let value ← `(Lean.Parser.Command.declValSimple| := sorryAx _ true)
  let visibility := mkNode ``Lean.Parser.Command.private #[mkAtomFrom keyword "private"]
  let kind := mkNode ``Lean.Parser.Command.noncomputable #[mkAtomFrom keyword "noncomputable"]
  Command.elabCommandTopLevel <| mkNode ``Lean.Parser.Command.declaration #[
    modifiers mkNullNode [(2, visibility), (4, kind)],
    mkNode ``Lean.Parser.Command.definition #[
      mkAtomFrom keyword "def", declId, ex[1], value, mkNullNode]]
  let env ← getEnv
  let some info := env.find? (mkPrivateName env ((← getCurrNamespace) ++ name))
    | return none
  return some (← Command.liftTermElabM (Meta.isProp info.type))

/-- The type of `name` as Lean pretty-prints it in the current scope: its
namespace, open declarations and options. -/
private def statementText (name : Name) : Command.CommandElabM (Option String) := do
  let some info := (← getEnv).find? name | return none
  return some (← Command.liftTermElabM (Meta.ppExpr info.type)).pretty

/-- Read a frozen source and emit reference module plus source declaration ranges. -/
def main (args : List String) : IO UInt32 := do
  let (source, setupFile, artifactRoot, tag, output, report, rest?) ← match args with
    | [s, c, a, t, o, r] => pure (s, c, a, t, o, r, none)
    | [s, c, a, t, o, r, rest] => pure (s, c, a, t, o, r, some rest)
    | _ => do
      IO.eprintln "usage: ProjectRead SOURCE SETUP ARTIFACTS TAG OUTPUT REPORT [REST]"
      return 2
  initSearchPath (← findSysroot)
  searchPathRef.modify (⟨artifactRoot⟩ :: ·)
  let setup ← ModuleSetup.load ⟨setupFile⟩
  let input ← IO.FS.readFile source
  let inputCtx := Parser.mkInputContext input source
  let (header, parserState, headerLog) ← Parser.parseHeader inputCtx
  if ← errors headerLog then return 3
  let isModule := setup.isModule || HeaderSyntax.isModule header
  let opts := Elab.async.set (maxHeartbeats.set setup.options.toOptions 1000000) false
  unsafe enableInitializersExecution
  setup.dynlibs.forM Lean.loadDynlib
  let (env, messages) ← processHeaderCore 0 (HeaderSyntax.imports header)
    isModule opts headerLog inputCtx (mainModule := setup.name)
    (package? := setup.package?) (arts := setup.importArts) (plugins := setup.plugins)
  if ← errors messages then return 3
  let mut state := Command.mkState env {} opts
  let mut parser := parserState
  let mut declarations : Array Json := #[]
  -- Examples that claim nothing, so the checker can account for every one.
  let mut unclaimed : Array Json := #[]
  let mut commandStart := parserState.pos
  -- The last command read: where it starts, and how it was parsed.
  let mut lastCommand : Option (Nat × Parser.ParserModuleContext × Parser.ModuleParserState) :=
    none
  repeat
    commandStart := parser.pos
    let scope := state.scopes.head!
    let context : Parser.ParserModuleContext := {
      env := state.env, options := scope.opts,
      currNamespace := scope.currNamespace, openDecls := scope.openDecls }
    let parsedFrom := parser
    let (stx, next, log) := Parser.parseCommand inputCtx context parser {}
    parser := next
    if stx.getKind != ``Lean.Parser.Command.eoi then
      lastCommand := some (commandStart.byteIdx, context, parsedFrom)
    if ← errors log then
      writeFailure report commandStart.byteIdx declarations
      return 3
    if stx.getKind == ``Lean.Parser.Command.eoi then break
    let some (wrappers, declaration) := scopedTheorem stx | do
      IO.eprintln "unsupported scoped reference command; only set_option around named theorems is supported"
      writeFailure report commandStart.byteIdx declarations
      return 4
    unless allowed declaration.getKind || printAxioms declaration.getKind do
      IO.eprintln s!"unsupported reference command: {stx.getKind}"
      writeFailure report commandStart.byteIdx declarations
      return 4
    let some start := stx.getPos? | do
      writeFailure report commandStart.byteIdx declarations
      return 4
    let some stop := stx.getTailPos? | do
      writeFailure report commandStart.byteIdx declarations
      return 4
    let context : Command.Context := {
      cmdPos := start, fileName := source, fileMap := inputCtx.fileMap,
      snap? := none, cancelTk? := none }
    let mut command := declaration
    let mut isTheorem := theoremCommand declaration
    let mut keyword : Option (Nat × Nat) := none
    let mut plainExample : Option (Nat × Nat) := none
    if exampleCommand stx then
      let ex := stx[1]
      let some keywordStart := ex[0].getPos? | do
        writeFailure report commandStart.byteIdx declarations
        return 4
      let some keywordStop := ex[0].getTailPos? | do
        writeFailure report commandStart.byteIdx declarations
        return 4
      let name := Name.mkSimple (tag ++ toString keywordStart.byteIdx)
      -- Without a type an example states nothing. Otherwise a header that does
      -- not elaborate alone is read as a claim, so its error is reported.
      let isProp ← if ex[1][1].getNumArgs == 0 then pure (some false) else
        match ← runCommand context state (exampleIsProp stx name) with
        | some (result, _, probed) => pure (if probed.hasErrors then none else result)
        | none => pure none
      if isProp != some false then
        unless (stx[0].getArgs.extract 1 7).all (·.isNone) do
          IO.eprintln ("an example checked as a claim may have a docstring, " ++
            "but no attributes or other modifiers")
          writeFailure report commandStart.byteIdx declarations
          return 4
        unless ex[2].getKind == ``Lean.Parser.Command.declValSimple do
          IO.eprintln "unsupported example proof syntax"
          writeFailure report commandStart.byteIdx declarations
          return 4
        command := exampleTheorem stx name
        isTheorem := true
        keyword := some (keywordStart.byteIdx, keywordStop.byteIdx)
      else
        plainExample := some (keywordStart.byteIdx, keywordStop.byteIdx)
    if isTheorem && command[1][3].getKind != ``Lean.Parser.Command.declValSimple then
      IO.eprintln "unsupported theorem proof syntax"
      writeFailure report commandStart.byteIdx declarations
      return 4
    let before := state.env.constants.map₂.toList.map (·.1)
    let act : Command.CommandElabM Unit := do
      let reference ← if isTheorem then noProof command else pure command
      Command.elabCommandTopLevel (wrapOptions wrappers reference)
    match ← runCommand context state act with
    | none =>
      IO.eprintln "reference elaboration raised an exception"
      writeFailure report commandStart.byteIdx declarations
      return 3
    | some ((), after, messages) =>
      if ← errors messages then
        writeFailure report commandStart.byteIdx declarations
        return 3
      if printAxioms stx.getKind &&
          after.env.constants.map₂.toList.length != before.length then
        IO.eprintln ("#print axioms would add a declaration Lean generates on " ++
          "demand (such as an equation lemma); print a declared constant's axioms")
        writeFailure report commandStart.byteIdx declarations
        return 4
      state := after
      if let some (first, last) := plainExample then
        unclaimed := unclaimed.push <| Json.mkObj [
          ("start", toJson start.byteIdx), ("end", toJson stop.byteIdx),
          ("keyword", toJson #[first, last])]
      if isTheorem then
        let names := state.env.constants.map₂.toList.filterMap fun (n, info) =>
          if before.contains n || !info.isTheorem then none else some n
        unless names.length == 1 do
          IO.eprintln "the theorem does not declare exactly one reference constant"
          writeFailure report commandStart.byteIdx declarations
          return 4
        let span := match keyword with
          | some (first, last) => [("keyword", toJson #[first, last])]
          | none => []
        -- Printed in this command's scope; a failure only leaves it out.
        let statement := match ← runCommand context state
            (inOptionScope wrappers (statementText names[0]!)) with
          | some (some text, _, _) => [("statement", toJson text)]
          | _ => []
        declarations := declarations.push <| Json.mkObj <| [
          ("name", toJson names[0]!.toString), ("start", toJson start.byteIdx),
          ("end", toJson stop.byteIdx)] ++ span ++ statement
  if let (some restFile, some (start, context, parsedFrom)) := (rest?, lastCommand) then
    let whole := Parser.mkInputContext (input ++ (← IO.FS.readFile restFile)) source
    let (stx, _, log) := Parser.parseCommand whole context parsedFrom {}
    let ends : Bool := match stx.getTailPos? with
      | some tail => tail.byteIdx ≤ input.utf8ByteSize
      | none => false
    unless !log.hasErrors && ends do
      IO.eprintln ("the requested check would end inside this command; check through " ++
        "the last line of a claim")
      writeFailure report start declarations
      return 4
  writeModule state.env ⟨output⟩
  IO.FS.writeFile report (Json.mkObj [
    ("declarations", toJson declarations), ("examples", toJson unclaimed)]).compress
  return 0
