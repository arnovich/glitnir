/- Observe original saved tactics. This program never grants proof authority. -/
import Lean.Elab.Command
import Lean.Elab.Import
import Lean.Meta.PPGoal
open Lean Elab

-- The annotation package stores a tagged Json in CustomInfo. The helper can
-- decode this toolchain-owned type without importing project modules here.
deriving instance TypeName for Lean.Json

private def goals (ctx : ContextInfo) (mctx : MetavarContext)
    (ids : List MVarId) : IO (Array String) := do
  let ctx := { ctx with mctx }
  ids.toArray.mapM fun id => do
    return (← ctx.runMetaM {} (Meta.ppGoal id)).pretty

private def original (stx : Syntax) : Bool :=
  match stx.getHeadInfo with
  | .original .. => true
  | _ => false

private def container (kind : Name) : Bool :=
  kind == .anonymous || kind == `null || kind == `by || kind == `Lean.cdotTk ||
  kind.toString.startsWith "Lean.Parser.Tactic.tacticSeq" ||
  kind == ``Lean.Parser.Term.byTactic

-- Report only the narrow one-goal case: exactly one newly scoped proposition.
-- LocalContext identities are used within this observation, never serialized.
private def introducedLocal (ctx : ContextInfo) (tactic : TacticInfo) : IO (Option Json) := do
  let [beforeGoal] := tactic.goalsBefore | return none
  let [afterGoal] := tactic.goalsAfter | return none
  let some beforeDecl := tactic.mctxBefore.findDecl? beforeGoal | return none
  let some afterDecl := tactic.mctxAfter.findDecl? afterGoal | return none
  let added := afterDecl.lctx.getFVarIds.filter fun id => (beforeDecl.lctx.find? id).isNone
  if added.size != 1 then return none
  let some localDecl := afterDecl.lctx.find? added[0]! | return none
  if localDecl.userName.isAnonymous || localDecl.isAuxDecl || localDecl.isImplementationDetail then return none
  let ctx := { ctx with mctx := tactic.mctxAfter }
  let some type ← ctx.runMetaM afterDecl.lctx do
    if !(← Meta.isProp localDecl.type) then return none
    return some (← Meta.ppExpr (← instantiateMVars localDecl.type)).pretty
    | return none
  return some <| Json.mkObj [("name", toJson localDecl.userName.toString), ("type", toJson type)]

private partial def visit (tree : InfoTree) (ctx? : Option ContextInfo)
    (parent : Option Nat) (parentSpan : Option (Nat × Nat))
    (nodes : Array Json) (explanations : Array Json) : IO (Array Json × Array Json) := do
  if nodes.size >= 4096 then throw <| IO.userError "Proof trace exceeds 4096 tactic observations"
  if explanations.size >= 4096 then throw <| IO.userError "Proof trace exceeds 4096 explanation observations"
  match tree with
  | .hole _ => return (nodes, explanations)
  | .context info child => visit child (info.mergeIntoOuter? ctx?) parent parentSpan nodes explanations
  | .node info children =>
    let mut nodes := nodes
    let mut explanations := explanations
    let mut parent := parent
    let mut parentSpan := parentSpan
    if let some ctx := ctx? then
      if let .ofCustomInfo custom := info then
        if original custom.stx && custom.stx.getKind.toString == "Glitnir.explain" &&
            parent.any (fun index =>
              (nodes[index]?.bind (·.getObjValAs? String "kind" |>.toOption)) == some "Glitnir.explain") then
          if let some payload := custom.value.get? Json then
            if (payload.getObjValAs? String "kind").toOption == some "glitnir.explanation/v1" then
              if let (some start, some stop) := (custom.stx.getPos?, custom.stx.getTailPos?) then
                let span := (start.byteIdx, stop.byteIdx)
                if span.1 < span.2 then
                  let authoredId ← IO.ofExcept <| payload.getObjValAs? String "authored_id"
                  let markdown ← IO.ofExcept <| payload.getObjValAs? String "markdown"
                  explanations := explanations.push <| Json.mkObj [
                    ("start", toJson span.1), ("end", toJson span.2),
                    ("parent", toJson parent), ("authored_id", toJson authoredId),
                    ("markdown", toJson markdown),
                    ("declaration", toJson (ctx.parentDecl?.map Name.toString |>.getD ""))]
      if let .ofTacticInfo tactic := info then
        if original tactic.stx && !container tactic.stx.getKind then
          if let (some start, some stop) := (tactic.stx.getPos?, tactic.stx.getTailPos?) then
            let span := (start.byteIdx, stop.byteIdx)
            if span.1 < span.2 && parentSpan != some span then
              let before ← goals ctx tactic.mctxBefore tactic.goalsBefore
              let after ← goals ctx tactic.mctxAfter tactic.goalsAfter
              let introduced ← if tactic.stx.getKind.toString == "Glitnir.explain" then
                introducedLocal ctx tactic else pure none
              let node := Json.mkObj [
                ("start", toJson span.1), ("end", toJson span.2),
                ("parent", toJson parent), ("before", toJson before), ("after", toJson after),
                ("before_ids", toJson (tactic.goalsBefore.map (·.name.toString))),
                ("after_ids", toJson (tactic.goalsAfter.map (·.name.toString))),
                ("kind", toJson tactic.stx.getKind.toString),
                ("introduced_local", toJson introduced),
                ("declaration", toJson (ctx.parentDecl?.map Name.toString |>.getD ""))]
              parent := some nodes.size
              parentSpan := some span
              nodes := nodes.push node
    for child in children do
      let (newNodes, newExplanations) ←
        visit child (info.updateContext? ctx?) parent parentSpan nodes explanations
      nodes := newNodes
      explanations := newExplanations
    return (nodes, explanations)

/-- Elaborate the saved source as written and report scoped tactic observations. -/
def main (args : List String) : IO UInt32 := do
  let [source, setupFile, libraryRoots, report] := args
    | IO.eprintln "usage: ProofTrace SOURCE SETUP LIBRARIES_JSON REPORT"; return 2
  initSearchPath (← findSysroot)
  let libraries ← IO.ofExcept <| (Json.parse libraryRoots).bind Json.getArr?
  let libraries ← libraries.toList.mapM fun item => do
    return System.FilePath.mk (← IO.ofExcept item.getStr?)
  searchPathRef.modify (libraries ++ ·)
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
  state := { state with infoState := { enabled := true } }
  let mut parser := parserState
  let mut nodes : Array Json := #[]
  let mut explanations : Array Json := #[]
  let mut errors : Array String := #[]
  let mut warnings : Array String := #[]
  let mut completed := 0
  repeat
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
      | .error _ => errors := errors.push "Command elaboration did not complete."
    let mut trees := state.infoState.trees.toArray
    let mut messages := pending ++ state.messages
    pending := {}
    for task in state.snapshotTasks do
      for snapshot in task.get.getAll do
        messages := messages ++ snapshot.diagnostics.msgLog
        if let some tree := snapshot.infoTree? then trees := trees.push tree
    for tree in trees do
      let (newNodes, newExplanations) ← visit tree none none none nodes explanations
      nodes := newNodes
      explanations := newExplanations
    for message in messages.toList do
      if message.severity == .error then errors := errors.push (← message.data.toString)
      if message.severity == .warning then warnings := warnings.push (← message.data.toString)
    state := { state with messages := {}, snapshotTasks := #[], infoState := { enabled := true } }
    completed := max completed (if atEnd then input.utf8ByteSize else
      stx.getTailPos?.map (·.byteIdx) |>.getD completed)
    let progress := Json.mkObj [
      ("source_bytes", toJson input.utf8ByteSize),
      ("completed_bytes", toJson completed), ("steps", toJson nodes.size)]
    IO.FS.writeFile (report ++ ".progress.tmp") progress.compress
    IO.FS.rename (report ++ ".progress.tmp") (report ++ ".progress.json")
    if atEnd then break
  IO.FS.writeFile report (Json.mkObj [
    ("nodes", toJson nodes), ("explanations", toJson explanations),
    ("errors", toJson errors), ("warnings", toJson warnings)]).compress
  return 0
