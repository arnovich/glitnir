import Lean
import Gimle.Asgard.Visualization.Model
import Gimle.Forseti

/-! Notebook presentation adapter over Asgard's typed visualization graph.
No proof is checked here: selected declarations provide labels, never authority.
The notebook checker owns their status. Keep this out of replay imports. -/
namespace ForsetiView
open Lean Gimle.Asgard Visualization

private def endpoint (e : Endpoint) (offset : Nat := 0) : Json :=
  toJson [e.node + offset, e.port]

private def operation : Operation → String
  | .identity => "identity" | .constant _ => "constant" | .scalar _ => "scalar"
  | .add => "add" | .multiply => "multiply" | .split => "split" | .swap => "swap"
  | .terminal => "terminal" | .integrate _ _ => "integrate"

private def graph (g : Diagram) : Json := Json.mkObj [
  ("nodes", toJson (g.nodes.map fun n => Json.mkObj [
    ("kind", toJson (operation n.operation)), ("label", toJson n.operation.label),
    ("inputs", toJson n.inputs), ("outputs", toJson n.outputs),
    ("x", toJson n.x), ("y", toJson n.y)])),
  ("edges", toJson (g.edges.map fun e => Json.mkObj [
    ("source", endpoint e.source), ("target", endpoint e.target),
    ("feedback", toJson e.feedback), ("lane", toJson e.lane)])),
  ("inputs", toJson (g.inputs.map (endpoint ·))),
  ("outputs", toJson (g.outputs.map (endpoint ·)))]

def circuitData {n m : Nat} (c : Circuit n m) : Except String Json := do
  let g ← ofCircuit c
  return Json.mkObj [("svg", toJson g.scene.xml), ("graph", graph g)]

def dynamicsData {n m : Nat} (c : Dynamics.Circuit n m) : Except String Json := do
  let g ← ofDynamics c
  return Json.mkObj [("svg", toJson g.scene.xml), ("graph", graph g)]

def modelData {b : Model.Body} {e : Model.Evolution}
    (c : Model.ContinuousModel b e) : Except String Json := do
  let model ← overview c
  let detail := match ofDynamics c.feedback with
    | .ok g => if g.nodes.length ≤ 512 then graph g else Json.null
    | .error _ => Json.null
  return Json.mkObj [("detail", detail), ("svg", toJson model.scene.xml), ("model", Json.mkObj [
    ("axis", toJson model.axis), ("start", toJson model.start),
    ("states", toJson (model.states.map fun row => Json.mkObj [
      ("name", toJson row.name), ("id", toJson row.id),
      ("equation", toJson row.equation), ("initial", toJson row.initial)])),
    ("observations", toJson model.observations)])]

def boundary {n m : Nat} (c : Circuit n m) (offset : Nat) : Except String Json := do
  let g ← ofCircuit c
  return Json.mkObj [("inputs", toJson (g.inputs.map (endpoint · offset))),
    ("outputs", toJson (g.outputs.map (endpoint · offset))),
    ("nodeStart", toJson offset), ("nodeCount", toJson g.nodes.length)]

def circuitSize {n m : Nat} (c : Circuit n m) : Except String Nat :=
  (ofCircuit c).map (·.nodes.length)

def dynamicsSize {n m : Nat} (c : Dynamics.Circuit n m) : Except String Nat :=
  (ofDynamics c).map (·.nodes.length)

open Meta Elab Term Command

/-- Preserve constructor order, which is also Asgard's graph node order. -/
private unsafe def components (value : Expr) (offset : Nat := 0) (fuel : Nat := 256)
    : MetaM (Array (Expr × Nat) × Nat) := do
  if fuel == 0 then throwError "Circuit annotation nesting limit exceeded"
  let expanded ← whnf value
  let name := expanded.getAppFn.constName!
  let args := expanded.getAppArgs
  if [``Circuit.compose, ``Circuit.parallel, ``Dynamics.Circuit.compose,
      ``Dynamics.Circuit.parallel].contains name then
    let (left, count) ← components args[args.size-2]! offset (fuel-1)
    let (right, count') ← components args[args.size-1]! (offset+count) (fuel-1)
    return (#[(value, offset)] ++ left ++ right, count + count')
  else if [``Dynamics.Circuit.lift, ``Dynamics.Circuit.trace].contains name then
    let (children, count) ← components args[args.size-1]! offset (fuel-1)
    return (#[(value, offset)] ++ children, count)
  else
    -- Opaque definitions can contain many nodes even when Meta.whnf cannot
    -- expose their syntax. Count the actual graph before placing siblings.
    let type ← inferType value
    let counter := if type.getAppFn.constName! == ``Dynamics.Circuit then ``dynamicsSize else ``circuitSize
    let expression ← mkAppM counter #[value]
    let count ← evalExpr (Except String Nat) (← inferType expression) expression
    let .ok count := count | throwError "Cannot locate the circuit component"
    return (#[(value, offset)], count)

private def printed (e : Expr) : MetaM String := do
  return (← ppExpr e).pretty

/-- Only closed, explicitly named Hoare declarations are accepted. Matching is
Lean definitional equality, never equality guessed from printed names. -/
private unsafe def annotations (target : Expr) (proofs : Array Syntax) : TermElabM Json := do
  if proofs.size > 16 then throwError "At most 16 Hoare declarations may label a view"
  let (parts, _) ← components target
  let mut result := #[]
  for proofSyntax in proofs do
    let proof ← elabTerm proofSyntax none
    synthesizeSyntheticMVarsNoPostponing
    let proof ← instantiateMVars proof
    unless proof.isConst do throwError "Select a closed Hoare declaration by name"
    let type ← inferType proof
    let name := type.getAppFn.constName!
    let args := type.getAppArgs
    unless name == ``Gimle.Forseti.ExactHoare || name == ``Gimle.Forseti.QuantitativeHoare do
      throwError "{proofSyntax}: expected a closed ExactHoare or QuantitativeHoare declaration"
    let pre := args[2]!
    let circuit := args[3]!
    let post := args[4]!
    let epsilon ← if args.size == 6 then pure (toJson (← printed args[5]!)) else pure Json.null
    let mut matchedCount := 0
    for (part, offset) in parts do
      if ← withoutModifyingState (isDefEq part circuit) then
        let expr ← mkAppM ``boundary #[part, mkNatLit offset]
        let bounds ← evalExpr (Except String Json) (← inferType expr) expr
        let .ok bounds := bounds | throwError "Cannot draw selected Hoare boundary"
        let .ok fields := bounds.getObj? | throwError "Invalid boundary"
        if result.size ≥ 256 then throwError "Too many matching components; select a smaller circuit"
        result := result.push (Json.mkObj (fields.toList ++ [
          ("name", toJson proof.constName!.toString),
          ("pre", toJson (← printed pre)), ("post", toJson (← printed post)),
          ("preDetail", toJson (← printed (← whnf (← mkAppM ``Gimle.Forseti.Predicate.holds #[pre])))),
          ("postDetail", toJson (← printed (← whnf (← mkAppM ``Gimle.Forseti.Predicate.holds #[post])))),
          ("preKey", toJson (reprStr pre)), ("postKey", toJson (reprStr post)),
          ("epsilonKey", if args.size == 6 then toJson (reprStr args[5]!) else Json.null),
          ("epsilon", epsilon)]))
        matchedCount := matchedCount + 1
    if matchedCount == 0 then throwError "{proofSyntax}: its circuit is not a component of this view"
  return Json.arr result

syntax (name := viewCommand) "#forseti_view" ident ident "using" "[" ident,* "]" : command

@[command_elab viewCommand] unsafe def elaborateView : CommandElab := fun stx => do
  let `(command| #forseti_view $kind:ident $target:ident using [$proofs:ident,*]) := stx
    | throwUnsupportedSyntax
  liftTermElabM do
    let kind := kind.getId.toString
    let term ← elabTerm target none
    synthesizeSyntheticMVarsNoPostponing
    let term ← instantiateMVars term
    let function := match kind with
      | "circuit" => ``circuitData | "dynamics" => ``dynamicsData | _ => ``modelData
    let expr ← mkAppM function #[term]
    let data ← evalExpr (Except String Json)
      (← inferType expr) expr
    let .ok data := data | throwError "Could not draw the circuit"
    let .ok fields := data.getObj? | throwError "Invalid view report"
    let labels ← if proofs.getElems.isEmpty then pure (Json.arr #[]) else do
      if kind == "model" then throwError "Hoare labels require a circuit or dynamics view"
      annotations term (proofs.getElems.map (·.raw))
    logInfo ("forsetiView:" ++ (Json.mkObj (fields.toList ++ [("contracts", labels)])).compress)
end ForsetiView
