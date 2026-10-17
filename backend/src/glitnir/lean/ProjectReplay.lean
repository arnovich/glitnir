/- Independent kernel replay of one compiled module, under lean's -M allocation cap.
Compile this fixed helper with toolchain-only imports before exposing project data.

    lean --run ProjectReplay.lean MODULE ARTIFACTS MODE
    MODE: full | imports-only | imports-incremental BASE | imports-cached

It reads MODULE's own data (every `.olean` part, as `importModules` reads it) and
the constants of MODULE's imports with `importModules` at the private level, which
reads every module of the transitive closure in full. The kernel never checks
anything against that imported environment: `importModules` also loads the
imports' IR and extension state, and sets `quotInit` for any import list, and
the kernel runs the interpreter on its environment for `Lean.reduceBool` and
`Lean.reduceNat`. Every kernel environment here starts from `mkEmptyEnvironment`,
which holds no project IR or extension state, as in a fresh replay.

Write X for the imported constant map (`map₁`, which `finalizeImport` merges
over the closure's modules in load order) and R(X) for `rebuild X`.

- `full` replays every imported and local constant together into an empty
  environment (Lean 4.32.1's `leanchecker --fresh` algorithm), as before caching.
- `imports-only` replays X into an empty environment, giving K, and reports
  `imports_replayed` only when R(X) and K hold equal constants and the same
  `quotInit` (`sameKernel`). Every constant in K was kernel-checked, so then
  every constant R(X) adds unchecked is one the kernel accepted, in an
  environment holding the same constants.
- `imports-incremental BASE` does the same with a cheaper K. BASE lists the
  modules of a closure S recorded earlier, in its recorded load order; the
  caller passes it only when this exact program reported `imports_replayed`
  and `extendable` for S, and every module of S has byte-identical files now.
  `extendable` meant that merging S's module data in that order
  (`mergeModules`, `finalizeImport`'s merge) gave exactly S's X, so merging the
  same data in the same order now gives that X again, Y; and R(Y) was found
  equal to a kernel-checked environment. Here, BASE must name modules of this
  closure, closed under their imports, and every constant of Y must be in X
  unchanged under its name. Then K = `replay (X \ Y)` over R(Y), with the
  kernel checking every constant of the other modules on top of a base whose
  every constant was checked before. Anything else replays as `imports-only`.
  The module order matters only through `mergeModules`: a different order is a
  different Y, which the check against X refuses.
- `imports-cached` is passed only when this exact program has reported
  `imports_replayed` for byte-identical module files in this load order. It
  builds R(X) again (the same deterministic function of those files) and
  replays every constant MODULE declares on top of it, so each local constant
  is kernel-checked in an environment of kernel-checked constants.

Both import modes also report `extendable`: whether `mergeModules` over this
closure's data in load order gives X exactly, so that a later, larger closure
may use this one as its BASE. A record built on a base stands on that base's
record, back to one whose K was a fresh replay.

`rebuild` is Lean's `Environment.replay` algorithm with one change: definitions,
theorems, axioms and opaque constants are added without type checking; their
checks are what an import mode already did. Inductive types and the quotient
are still added through the kernel's checked `addDecl` (so `quotInit` is set
only by the kernel's own `init_quot`), and generated constructors and
recursors must equal the imported ones.

In `imports-cached`, when MODULE declares an imported name (Lean's import merges
a theorem restated under the same name and statement into one constant, which
the reporter reads), or the imports lack the kernel's own quotient from
`Init.Prelude` (`kernelQuotient`), the module is replayed as in `full`; the
output says why (`fresh`).

After checking MODULE's constants, it collects, for every theorem MODULE
declares, the axioms it depends on (`collectAxioms`) in the environment where
those constants were just kernel-checked. That environment starts from
`mkEmptyEnvironment`: it holds no imported extension state, so no axiom list
precomputed into an `.olean` is consulted, and every dependency, imported or
local, is walked from its kernel-checked value. Each axiom's module is the first
module, in load order, whose own constant data declares it, then MODULE: the
reporter's `getModuleIdxFor?`, without the names `.ir` files add. These are the
axioms the checker decides a theorem's status by.

On success it prints one JSON document: what ran, whether the imports were
replayed and rebuilt equally (`imports_replayed`), whether a larger closure may
extend them (`extendable`), how many modules the base held (`base_modules`, for
`imports-incremental`), why a cached check replayed from scratch (`fresh`), the
imports, the closure as loaded (each module with the `.olean` path it was read
from) and each declared theorem's axioms (`theorems`, empty for the import
modes). -/
import Lean.Replay
import Lean.Util.CollectAxioms
open Lean

/-- Every data part of `module`, read as `importModules` reads it. -/
unsafe def moduleParts (module : Name) : IO (Array (ModuleData × CompactedRegion)) := do
  let file ← findOLean module
  unless (← file.pathExists) do
    throw <| IO.userError s!"object file '{file}' of module {module} does not exist"
  let main ← readModuleData file
  if !main.1.isModule then
    return #[main]
  let server ← CompactedRegion.read (α := ModuleData)
    (OLeanLevel.server.adjustFileName file) #[main.2]
  let priv ← CompactedRegion.read (α := ModuleData)
    (OLeanLevel.private.adjustFileName file) #[main.2, server.2]
  return #[main, server, priv]

/-- The one reuse of a name Lean's import accepts here: the same theorem. -/
def sameTheorem : ConstantInfo → ConstantInfo → Bool
  | .thmInfo a, .thmInfo b =>
    a.name == b.name && a.type == b.type && a.levelParams == b.levelParams && a.all == b.all
  | _, _ => false

def quotKindIndex : QuotKind → Nat
  | .type => 0 | .ctor => 1 | .lift => 2 | .ind => 3

def sameQuotient (a b : QuotVal) : Bool :=
  a.name == b.name && a.levelParams == b.levelParams && a.type == b.type &&
    quotKindIndex a.kind == quotKindIndex b.kind

def sameInductive (a b : InductiveVal) : Bool :=
  a.name == b.name && a.levelParams == b.levelParams && a.type == b.type &&
    a.numParams == b.numParams && a.numIndices == b.numIndices && a.all == b.all &&
    a.ctors == b.ctors && a.numNested == b.numNested && a.isRec == b.isRec &&
    a.isUnsafe == b.isUnsafe && a.isReflexive == b.isReflexive

/-- Whether two constants are the same to the kernel, field by field. -/
def sameConstant : ConstantInfo → ConstantInfo → Bool
  | .axiomInfo a, .axiomInfo b => a == b
  | .defnInfo a, .defnInfo b => a == b
  | .thmInfo a, .thmInfo b => a == b
  | .opaqueInfo a, .opaqueInfo b => a == b
  | .quotInfo a, .quotInfo b => sameQuotient a b
  | .inductInfo a, .inductInfo b => sameInductive a b
  | .ctorInfo a, .ctorInfo b => a == b
  | .recInfo a, .recInfo b => a == b
  | _, _ => false

/-- Whether two kernel environments hold equal constants and the same `quotInit`. -/
def sameKernel (a b : Kernel.Environment) : Bool :=
  a.quotInit == b.quotInit &&
    a.constants.fold (fun n _ _ => n + 1) 0 == b.constants.fold (fun n _ _ => n + 1) 0 &&
    a.constants.fold (init := true) fun same name info =>
      same && match b.constants.find? name with
        | some other => sameConstant info other
        | none => false

/-- Whether two constant maps hold the same names with equal constants. -/
def sameConstants (a b : Std.HashMap Name ConstantInfo) : Bool :=
  a.size == b.size && a.toList.all fun (name, info) =>
    match b[name]? with
    | some other => sameConstant info other
    | none => false

/-- `finalizeImport`'s `subsumesInfo` (private in Lean 4.32.1), unchanged: whether
`c₁` may replace `c₂` of the same name when both are imported. -/
def subsumes (constMap : Std.HashMap Name ConstantInfo) (c₁ c₂ : ConstantInfo) : Bool :=
  c₁.name == c₂.name && c₁.type == c₂.type && c₁.levelParams == c₂.levelParams &&
    match c₁, c₂ with
    | .thmInfo t₁, .thmInfo t₂ => t₁.all == t₂.all
    | .thmInfo t₁, .axiomInfo a₂ => t₁.all == [a₂.name] && !a₂.isUnsafe
    | .axiomInfo a₁, .axiomInfo a₂ => a₁.isUnsafe == a₂.isUnsafe && isPropCheap a₁.type
    | _, _ => false
where
  isPropCheap (ty : Expr) : Bool := Id.run do
    let mut ty := ty
    while ty.isForall do
      let .forallE (body := body) .. := ty | return false
      ty := body
    let .const n .. := ty.getAppFn | return false
    let some decl := constMap[n]? | return false
    let mut p := decl.type
    for _ in [0:ty.getAppNumArgs] do
      let .forallE (body := body) .. := p | return false
      p := body
    p.isProp

/-- The constant map `finalizeImport` builds from these modules' data, in this
order (its merge of restated theorems included), or none where it would refuse
a duplicate. -/
def mergeModules (modules : Array ModuleData) : Option (Std.HashMap Name ConstantInfo) :=
  Id.run do
    let mut map : Std.HashMap Name ConstantInfo := {}
    for data in modules do
      for name in data.constNames, info in data.constants do
        match map[name]? with
        | none => map := map.insert name info
        | some previous =>
          if subsumes map info previous then
            map := map.insert name info
          else if !subsumes map previous info then
            return none
    return some map

def quotientNames : List Name := [``Quot, ``Quot.mk, ``Quot.lift, ``Quot.ind]

/-- Whether the imports hold exactly the quotient constants the kernel itself
creates, declared by the toolchain's `Init.Prelude`: the kernel adds the
quotient again over the imported `Eq` (checking `Eq` too) in an empty
environment, and every imported quotient constant must equal what it added. -/
def kernelQuotient (env : Environment) : IO Bool := do
  let constants := env.constants.map₁
  let mut subset : Std.HashMap Name ConstantInfo := {}
  for name in [``Eq, ``Eq.refl, ``Eq.rec] ++ quotientNames do
    let some info := constants[name]? | return false
    subset := subset.insert name info
  for name in quotientNames do
    let some index := env.getModuleIdxFor? name | return false
    unless env.header.moduleNames[index]? == some `Init.Prelude do return false
  let empty ← mkEmptyEnvironment
  let .ok fresh ← (empty.replay subset).toBaseIO | return false
  return quotientNames.all fun name =>
    match fresh.constants.find? name, constants[name]? with
    | some (.quotInfo created), some (.quotInfo imported) => sameQuotient created imported
    | _, _ => false

namespace Rebuild

/-- Lean 4.32.1's `Lean.Environment.Replay` state, over a kernel environment. -/
structure State where
  env : Kernel.Environment
  remaining : NameSet := {}
  pending : NameSet := {}
  postponedConstructors : NameSet := {}
  postponedRecursors : NameSet := {}

abbrev M := ReaderT (Std.HashMap Name ConstantInfo) <| StateRefT State IO

def addDecl (d : Declaration) (check : Bool) : M Unit := do
  let env := (← get).env
  let result := if check then env.addDeclCore 0 d none else env.addDeclWithoutChecking d
  match result with
  | .ok env => modify fun s => { s with env := env }
  | .error _ => throw <| IO.userError "rebuilding the imported environment failed"

def isTodo (name : Name) : M Bool := do
  let r := (← get).remaining
  if r.contains name then
    modify fun s => { s with remaining := s.remaining.erase name, pending := s.pending.insert name }
    return true
  else
    return false

mutual
/-- `Lean.Environment.Replay.replayConstant`, adding values unchecked. -/
partial def replayConstant (name : Name) : M Unit := do
  if ← isTodo name then
    let some ci := (← read)[name]? | unreachable!
    replayConstants ci.getUsedConstantsAsSet
    if (← get).pending.contains name then
      match ci with
      | .defnInfo info => addDecl (.defnDecl info) false
      | .thmInfo info =>
        if let some (.thmInfo info') := (← get).env.find? ci.name then
          if info.name == info'.name && info.type == info'.type &&
              info.levelParams == info'.levelParams && info.all == info'.all then
            return
        addDecl (.thmDecl info) false
      | .axiomInfo info => addDecl (.axiomDecl info) false
      | .opaqueInfo info => addDecl (.opaqueDecl info) false
      | .inductInfo info =>
        let all ← info.all.mapM fun n => do pure <| ((← read)[n]!)
        for o in all do
          modify fun s =>
            { s with remaining := s.remaining.erase o.name, pending := s.pending.erase o.name }
        let ctorInfo ← all.mapM fun ci => do
          pure (ci, ← ci.inductiveVal!.ctors.mapM fun n => do pure ((← read)[n]!))
        for (_, ctors) in ctorInfo do
          for ctor in ctors do
            replayConstants ctor.getUsedConstantsAsSet
        let types : List InductiveType := ctorInfo.map fun ⟨ci, ctors⟩ =>
          { name := ci.name
            type := ci.type
            ctors := ctors.map fun ci => { name := ci.name, type := ci.type } }
        addDecl (.inductDecl info.levelParams info.numParams types false) true
      | .ctorInfo info =>
        modify fun s => { s with postponedConstructors := s.postponedConstructors.insert info.name }
      | .recInfo info =>
        modify fun s => { s with postponedRecursors := s.postponedRecursors.insert info.name }
      | .quotInfo _ =>
        replayConstant ``Eq
        addDecl .quotDecl true
      modify fun s => { s with pending := s.pending.erase name }

partial def replayConstants (names : NameSet) : M Unit := do
  for n in names do replayConstant n
end

def checkPostponed : M Unit := do
  for ctor in (← get).postponedConstructors do
    match (← get).env.find? ctor, (← read)[ctor]? with
    | some (.ctorInfo info), some (.ctorInfo info') =>
      unless info == info' do throw <| IO.userError s!"Invalid constructor {ctor}"
    | _, _ => throw <| IO.userError s!"No such constructor {ctor}"
  for rec in (← get).postponedRecursors do
    match (← get).env.find? rec, (← read)[rec]? with
    | some (.recInfo info), some (.recInfo info') =>
      unless info == info' do throw <| IO.userError s!"Invalid recursor {rec}"
    | _, _ => throw <| IO.userError s!"No such recursor {rec}"

end Rebuild

/-- The environment a fresh replay of `constants` builds, from an empty
environment, without type checking definition, theorem, axiom or opaque values. -/
def rebuild (constants : Std.HashMap Name ConstantInfo) : IO Kernel.Environment := do
  let mut remaining : NameSet := ∅
  for (n, ci) in constants.toList do
    if !ci.isUnsafe && !ci.isPartial then
      remaining := remaining.insert n
  let empty ← mkEmptyEnvironment
  let (_, s) ← StateRefT'.run (s := { env := empty.toKernelEnv, remaining }) do
    ReaderT.run (r := constants) do
      for n in remaining do
        Rebuild.replayConstant n
      Rebuild.checkPostponed
  return s.env

/-- `collectAxioms` reads nothing but this environment. -/
instance : MonadEnv (StateM Environment) where
  getEnv := get
  modifyEnv := modify

/-- Each theorem `own` declares, with the axioms it depends on in `checked`, the
environment its constants were kernel-checked in. An axiom's module is the
first in `imported`'s load order whose constant data declares it, else `module`. -/
def theoremAxioms (imported checked : Environment) (module : Name)
    (own : ModuleData) : Array Json := Id.run do
  let mut found : Array (Name × Array Name) := #[]
  let mut needed : NameSet := {}
  for name in own.constNames, info in own.constants do
    unless info.isTheorem do continue
    let axioms := (collectAxioms name : StateM Environment (Array Name)).run' checked
    found := found.push (name, axioms)
    needed := axioms.foldl (·.insert ·) needed
  let mut origin : NameMap Name := {}
  for moduleName in imported.header.moduleNames, data in imported.header.moduleData do
    for n in data.constNames do
      if needed.contains n && !origin.contains n then
        origin := origin.insert n moduleName
  for n in own.constNames do
    if needed.contains n && !origin.contains n then
      origin := origin.insert n module
  return found.map fun (name, axioms) => Json.mkObj [
    ("name", toJson name.toString),
    ("axioms", toJson (axioms.map fun a => Json.mkObj [
      ("name", toJson a.toString),
      ("module", toJson ((origin.find? a).map Name.toString))]))]

def importJson (i : Import) : Json := Json.mkObj [
  ("module", toJson i.module.toString), ("all", toJson i.importAll),
  ("exported", toJson i.isExported), ("meta", toJson i.isMeta)]

/-- Refuse a map whose key differs from the name of the constant it holds. -/
def checkNames (constants : Std.HashMap Name ConstantInfo) : IO Unit := do
  for (name, info) in constants.toList do
    unless info.name == name do
      throw <| IO.userError s!"constant {info.name} is stored under the name {name}"

def usage : String :=
  "usage: ProjectReplay MODULE ARTIFACTS \
    (full | imports-only | imports-incremental BASE | imports-cached)"

/-- The modules of a base closure, in its recorded load order, written as this
program prints module names (`Name.toString`). They are matched against the
loaded closure's names printed the same way, never parsed back into names. -/
def readBase (file : System.FilePath) : IO (Array String) := do
  let .ok json := Json.parse (← IO.FS.readFile file)
    | throw <| IO.userError "the base closure is not JSON"
  let .ok names := fromJson? (α := Array String) json
    | throw <| IO.userError "the base closure is not a list of module names"
  return names

/-- Replay only the constants of the modules outside `base` over the rebuilt
base, or none when `base` cannot be such a base for this closure. -/
def incremental (env : Environment) (imported : Std.HashMap Name ConstantInfo)
    (base : Array String) : IO (Option Kernel.Environment) := do
  let names := env.header.moduleNames
  let mut index : Std.HashMap String Nat := {}
  for name in names, i in [0:names.size] do
    if index.contains name.toString then return none
    index := index.insert name.toString i
  let mut members : NameSet := {}
  let mut data : Array ModuleData := #[]
  for text in base do
    let some i := index[text]? | return none
    let name := names[i]!
    if members.contains name then return none
    members := members.insert name
    data := data.push env.header.moduleData[i]!
  -- Closed under imports: every base module's imports are base modules.
  for module in data do
    for i in module.imports do
      unless members.contains i.module do return none
  let some baseConstants := mergeModules data | return none
  -- The closure's import holds exactly the base's constants under their names.
  for (name, info) in baseConstants.toList do
    match imported[name]? with
    | some other => unless sameConstant info other do return none
    | none => return none
  let rest := imported.filter fun name _ => !baseConstants.contains name
  let baseKernel ← rebuild baseConstants
  let checked ← (Environment.ofKernelEnv baseKernel).replay rest
  return some checked.toKernelEnv

unsafe def main (args : List String) : IO UInt32 := do
  let (moduleText, artifactRoot, mode, baseFile) ← match args with
    | [m, a, "imports-incremental", b] => pure (m, a, "imports-incremental", some b)
    | [m, a, mode] => pure (m, a, mode, none)
    | _ => IO.eprintln usage; return 2
  unless mode == "full" || mode == "imports-only" || mode == "imports-incremental" ||
      mode == "imports-cached" do
    IO.eprintln usage; return 2
  let base ← match baseFile with
    | some file => some <$> readBase file
    | none => pure none
  initSearchPath (← findSysroot)
  searchPathRef.modify (⟨artifactRoot⟩ :: ·)
  let module := moduleText.toName
  let parts ← moduleParts module
  let some (main, _) := parts[0]? | throw <| IO.userError "no module data"
  let some (own, _) := parts.back? | throw <| IO.userError "no module data"
  let imports := main.imports
  unless own.constNames.size == own.constants.size do
    throw <| IO.userError s!"module {module} lists {own.constNames.size} names for \
      {own.constants.size} constants"
  withImportModules imports {} fun env => do
    if env.header.moduleNames.contains module then
      throw <| IO.userError s!"module {module} imports itself"
    let imported := env.constants.map₁
    checkNames imported
    let mut ran := "full"
    let mut importsReplayed := false
    let mut fresh : Option String := none
    let mut theorems : Array Json := #[]
    let mut extendable := false
    let mut baseModules : Option Nat := none
    if mode == "imports-only" || mode == "imports-incremental" then
      let fromBase ← match base with
        | some names => incremental env imported names
        | none => pure none
      let replayed ← match fromBase with
        | some checked => pure checked
        | none => do
          let full ← (← mkEmptyEnvironment).replay imported
          pure full.toKernelEnv
      let rebuilt ← rebuild imported
      importsReplayed := sameKernel replayed rebuilt
      -- A later, larger closure may rebuild this one from its module data.
      extendable := match mergeModules env.header.moduleData with
        | some merged => sameConstants merged imported
        | none => false
      if fromBase.isSome then
        ran := "imports incremental"
        baseModules := base.map (·.size)
      else
        ran := "imports only"
    else
      let mut declared : Std.HashMap Name ConstantInfo := {}
      let mut restated := false
      for name in own.constNames, info in own.constants do
        if declared.contains name then
          throw <| IO.userError s!"module {module} declares {name} twice"
        if let some previous := imported[name]? then
          unless sameTheorem previous info do
            throw <| IO.userError s!"{name} in module {module} replaces an imported constant"
          restated := true
        declared := declared.insert name info
      checkNames declared
      if mode == "imports-cached" then
        if restated then
          fresh := some "restated"
        else if !(← kernelQuotient env) then
          fresh := some "quotient"
      if mode == "full" || fresh.isSome then
        -- Lean's import keeps the later, local copy of a restated theorem.
        let merged := declared.fold (init := imported) fun map name info => map.insert name info
        let checked ← (← mkEmptyEnvironment).replay merged
        theorems := theoremAxioms env checked module own
      else
        let base := Environment.ofKernelEnv (← rebuild imported)
        let checked ← base.replay declared
        theorems := theoremAxioms env checked module own
        ran := "imports cached"
    let modules ← env.header.moduleNames.mapM fun m => do
      return toJson #[m.toString, (← findOLean m).toString]
    IO.println (Json.mkObj [
      ("replay", toJson ran), ("imports_replayed", toJson importsReplayed),
      ("fresh", toJson fresh), ("imports", toJson (imports.map importJson)),
      ("modules", toJson modules), ("theorems", toJson theorems),
      ("extendable", toJson extendable), ("base_modules", toJson baseModules)]).compress
  return 0
