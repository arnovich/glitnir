/- Independent kernel replay of one compiled module, under lean's -M allocation cap.
Compile this fixed helper with toolchain-only imports before exposing project data.

    lean --run ProjectReplay.lean MODULE ARTIFACTS MODE
    MODE: full | imports-only | imports-incremental BASE | imports-cached

It reads MODULE's own data (every `.olean` part, as `importModules` reads it) and
the constants of MODULE's imports with `importModules` at the private level, which
reads every module of the transitive closure in full. The kernel never checks
anything against that imported environment: `importModules` also loads the
imports' IR and extension state, and sets `quotInit` for any import list.
Through Lean 4.34 the kernel also ran the interpreter on its environment for
`Lean.reduceBool` and `Lean.reduceNat`, so imported IR could run during a check;
Lean 4.35, which this program targets, removed both, and its kernel runs no
compiled code. Every kernel environment here still starts from
`mkEmptyEnvironment`, which holds no project IR or extension state, as in a fresh
replay.

Write X for the imported constant map (`map₁`, which `finalizeImport` merges
over the closure's modules in load order) and R(X) for `rebuild X`.

- `full` replays every imported and local constant together into an empty
  environment (`leanchecker --fresh`'s algorithm, `Lean.Kernel.Environment.replay`),
  as before caching.
- `imports-only` replays X into an empty environment, giving K, and reports
  `imports_replayed` only when R(X) and K hold equal constants and the same
  `quotInit` (`sameKernel`). Every constant in K was kernel-checked, so then
  every constant R(X) adds unchecked equals one the kernel accepted or itself
  generated, in an environment holding the same constants.
- `imports-incremental BASE` does the same with a cheaper K. BASE lists the
  modules of a closure S recorded earlier, in its recorded load order; the
  caller passes it only when this exact program reported `imports_replayed`
  and `extendable` for S, and every module of S has byte-identical files now.
  `extendable` meant that merging S's module data in that order
  (`mergeModules`, `finalizeImport`'s merge) gave exactly S's X, so merging the
  same data in the same order now gives that X again, Y; and R(Y) was found
  equal to a kernel-checked environment. Here, BASE must name modules of this
  closure, closed under their imports, and every constant of Y must be in X
  unchanged under its name, alike node by node (`sameConstantData`): a later
  module may restate a base theorem or axiom, and X then holds its copy. Then
  K = `replay (X \ Y)` over R(Y), with the kernel checking every constant of the
  other modules on top of a base whose every constant was checked before.
  Anything else replays as `imports-only`. The module order matters only
  through `mergeModules`: a different order is a different Y, which the check
  against X refuses.
- `imports-cached` is passed only when this exact program has reported
  `imports_replayed` for byte-identical module files in this load order. It
  builds R(X) again (the same deterministic function of those files) and
  replays every constant MODULE declares on top of it, so each local constant
  is kernel-checked in an environment of kernel-checked constants.

Both import modes also report `extendable`: whether `mergeModules` over this
closure's data in load order gives X exactly (alike node by node,
`sameConstantsData`), so that a later, larger closure may use this one as its
BASE. A record built on a base stands on that base's record, back to one whose
K was a fresh replay.

`rebuild` places every safe, total constant of its map, unchanged, into the
constant map of `mkEmptyEnvironment`'s kernel environment, and sets `quotInit`
exactly when they include a quotient constant. It runs no kernel check, walks no
dependencies and changes no other field. The import modes accept R(X) only when
it equals K (`sameKernel`): the same names, each with a constant equal field by
field, and the same `quotInit`, where the kernel itself checked every constant of
K and its own `init_quot` set `quotInit`. Every constant is compared field by
field and every expression in it node by node, binder names and annotations
included, with the data Lean caches in each node and the kernel trusts without
recomputing (`sameConstantData`, `sameExprData`); `==` on expressions ignores
binder names and annotations, and of that data compares only the hash. So
every constant of R(X) is one the kernel cannot tell from K's. Most are the
very objects in K, where the comparison stops at once: K's replay hands X's
own definitions, theorems, axioms and opaque constants to the kernel, which
keeps them. Those compared in full are the inductive types, constructors,
recursors and quotient, which the kernel generates itself in K while R(X)
holds the imported copies, and, in `imports-incremental`, a base constant a
later module restates, whose copy in R(X) no kernel checked (`incremental`
refuses an unlike copy first; `sameKernel` refusing it too is a second,
independent check). A forged constant under a name K lacks changes the count,
and a forged quotient constant differs from what `init_quot` created, so
neither is recorded. `Kernel.Environment`'s constructor is private: `rebuild`
writes its fields through `KernelFields`, a mirror of Lean 4.35.0's layout, and
builds nothing on another Lean or unless the mirror reads the same fields as
the public accessors, both of the empty environment and of the one it built.

Every kernel check here runs in Lean's own `Kernel.Environment.replay`, which
passes `maxRecDepth` 0 to `addDeclCore`, as `leanchecker` does: from Lean 4.35
on, 0 means unbounded, so no check is cut short by a recursion limit.

In `imports-cached`, when MODULE declares an imported name (Lean's import merges
a theorem restated under the same name and statement into one constant, which
the reporter reads), or the imports lack the kernel's own quotient from
`Init.Prelude` (`kernelQuotient`), the module is replayed as in `full`; the
output says why (`fresh`).

After checking MODULE's constants, it collects, for every theorem MODULE declares,
the axioms it depends on (`collectAxioms`) in the environment where those
constants were just kernel-checked. That environment starts from
`mkEmptyEnvironment`: it holds no imported extension state, so no axiom list
precomputed into an `.olean` is consulted, and every dependency, imported or
local, is walked from its kernel-checked value. Each axiom's module is the first
module, in load order, whose own constant data declares it, then MODULE: the
reporter's `getModuleIdxFor?`, without the names IR files add. These are the
axioms the checker decides a theorem's status by. `collectAxioms` finds each
constant in that kernel environment, where the replay added every constant (on
the cached and incremental paths, `rebuild` added the imported ones) and none
came from the environment `importModules` returned; a constant it could not find
would contribute no axioms, so the live tests check that axioms reached only
through imported theorems are reported.

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

/-- Whether two levels are alike node by node, with the same cached data
(`Level.data`) at every node. -/
unsafe def sameLevelData (a b : Level) : Bool :=
  ptrAddrUnsafe a == ptrAddrUnsafe b || a.data == b.data && match a, b with
    | .zero, .zero => true
    | .succ u, .succ v => sameLevelData u v
    | .max u v, .max u' v' | .imax u v, .imax u' v' => sameLevelData u u' && sameLevelData v v'
    | .param n, .param m => n == m
    | .mvar n, .mvar m => n == m
    | _, _ => false

/-- Whether two expressions are alike node by node, binder names and annotations
included, with the same cached data (`Expr.data`: hash, loose bound variable
range and flags) at every node and levels alike in theirs (`sameLevelData`).
`==` on `Expr` ignores binder names and annotations and, of the cached data,
which the kernel trusts without recomputing, compares only the hash. Pairs of
nodes already found alike are remembered and not compared again. -/
unsafe def sameExprData (a b : Expr) : StateM (Std.HashSet (USize × USize)) Bool := do
  if ptrAddrUnsafe a == ptrAddrUnsafe b then return true
  unless a.data == b.data do return false
  let key := (ptrAddrUnsafe a, ptrAddrUnsafe b)
  if (← get).contains key then return true
  let same ← match a, b with
    | .bvar i, .bvar j => pure (i == j)
    | .fvar x, .fvar y => pure (x == y)
    | .mvar x, .mvar y => pure (x == y)
    | .sort u, .sort v => pure (sameLevelData u v)
    | .const m us, .const n vs =>
      pure (m == n && us.length == vs.length && (us.zip vs).all fun (u, v) => sameLevelData u v)
    | .app f x, .app g y => sameExprData f g <&&> sameExprData x y
    | .lam n t e i, .lam m s f j | .forallE n t e i, .forallE m s f j =>
      pure (n == m && i == j) <&&> sameExprData t s <&&> sameExprData e f
    | .letE n t v e d, .letE m s w f c =>
      pure (n == m && d == c) <&&> sameExprData t s <&&> sameExprData v w <&&> sameExprData e f
    | .lit (.strVal l), .lit (.strVal k) => pure (l == k && l.length == k.length)
    | .lit l, .lit k => pure (l == k)
    | .mdata d e, .mdata c f => pure (d == c) <&&> sameExprData e f
    | .proj s i e, .proj t j f => pure (s == t && i == j) <&&> sameExprData e f
    | _, _ => pure false
  if same then modify (·.insert key)
  return same

/-- Every expression of a constant: its type, the value of a definition, theorem
or opaque constant, and the right-hand side of each recursor rule. -/
def constantExprs : ConstantInfo → List Expr
  | .defnInfo v => [v.type, v.value]
  | .thmInfo v => [v.type, v.value]
  | .opaqueInfo v => [v.type, v.value]
  | .recInfo v => v.type :: v.rules.map (·.rhs)
  | info => [info.type]

/-- Whether two constants are the same to the kernel: equal field by field
(`sameConstant`), and every expression alike node by node (`sameExprData`).
Nodes compared are remembered only within one constant. -/
unsafe def sameConstantData (a b : ConstantInfo) : Bool :=
  sameConstant a b && (constantExprs a).length == (constantExprs b).length &&
    (((constantExprs a).zip (constantExprs b)).allM fun (x, y) => sameExprData x y).run' {}

/-- Whether two constant maps hold the same names, each with constants the same
to the kernel (`sameConstantData`). -/
unsafe def sameConstantsData (a b : Std.HashMap Name ConstantInfo) : Bool :=
  a.size == b.size && a.toList.all fun (name, info) =>
    match b[name]? with
    | some other => sameConstantData info other
    | none => false

/-- Whether two kernel environments hold the same constants and `quotInit`: the
same names, each with constants the same to the kernel (`sameConstantData`). -/
unsafe def sameKernel (a b : Kernel.Environment) : Bool :=
  a.quotInit == b.quotInit &&
    a.constants.fold (fun n _ _ => n + 1) 0 == b.constants.fold (fun n _ _ => n + 1) 0 &&
    a.constants.fold (init := true) fun same name info =>
      same && match b.constants.find? name with
        | some other => sameConstantData info other
        | none => false


/-- `finalizeImport`'s `subsumesInfo` (private in Lean 4.35.0), unchanged: whether
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
  let .ok fresh ← (Kernel.Environment.replay subset empty.toKernelEnv).toBaseIO
    | return false
  return quotientNames.all fun name =>
    match fresh.constants.find? name, constants[name]? with
    | some (.quotInfo created), some (.quotInfo imported) => sameQuotient created imported
    | _, _ => false

/-- The fields of Lean 4.35.0's `Kernel.Environment`, in order. Its constructor
is private; `rebuild` writes an environment through this mirror. The two private
extension arrays are only carried along: `mkEmptyEnvironment` sets both to the
same object, so their relative order cannot be checked, nor does it matter. -/
structure KernelFields where
  constants : ConstMap
  quotInit : Bool
  diagnostics : Kernel.Diagnostics
  const2ModIdx : Std.HashMap Name ModuleIdx
  extensions : NonScalar
  irBaseExts : NonScalar
  header : EnvironmentHeader

/-- Whether `fields`, read through the mirror, holds what `env`'s public
accessors read: the same objects and the same `quotInit`. The flag is read last,
only where the objects already matched. -/
unsafe def readsAs (fields : KernelFields) (env : Kernel.Environment) : Bool :=
  ptrAddrUnsafe fields.constants == ptrAddrUnsafe env.constants &&
    ptrAddrUnsafe fields.diagnostics == ptrAddrUnsafe env.diagnostics &&
    ptrAddrUnsafe fields.const2ModIdx == ptrAddrUnsafe env.const2ModIdx &&
    ptrAddrUnsafe fields.header == ptrAddrUnsafe env.header &&
    fields.quotInit == env.quotInit

/-- R(`constants`): `mkEmptyEnvironment`'s kernel environment with every safe,
total constant placed unchanged in its constant map, and `quotInit` set exactly
when they include a quotient constant. Nothing is type checked; see the module
documentation for why the import modes may use it. It refuses to build on any
Lean but 4.35.0, or when `KernelFields` does not read what the public accessors
read, before writing (both values of `quotInit` included) and after. -/
unsafe def rebuild (constants : Std.HashMap Name ConstantInfo) : IO Kernel.Environment := do
  unless Lean.versionString == "4.35.0" do
    throw <| IO.userError s!"rebuilding the imports needs Lean 4.35.0, not {Lean.versionString}"
  let empty := (← mkEmptyEnvironment).toKernelEnv
  let fields : KernelFields := unsafeCast empty
  unless readsAs fields empty do
    throw <| IO.userError "unexpected kernel environment layout"
  -- `quotInit` is written where the public accessor reads it.
  let flipped : Kernel.Environment := unsafeCast { fields with quotInit := !fields.quotInit }
  unless flipped.quotInit != empty.quotInit do
    throw <| IO.userError "unexpected kernel environment layout"
  let mut map := fields.constants
  let mut quot := false
  for (name, info) in constants do
    if info.isUnsafe || info.isPartial then continue
    if info matches .quotInfo _ then quot := true
    map := map.insert name info
  let built := { fields with constants := map, quotInit := quot }
  let env : Kernel.Environment := unsafeCast built
  unless readsAs built env do
    throw <| IO.userError "unexpected kernel environment layout"
  return env

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
unsafe def incremental (env : Environment) (imported : Std.HashMap Name ConstantInfo)
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
  -- The closure's import holds exactly the base's constants under their names:
  -- a later module's copy of a base constant must be alike node by node.
  for (name, info) in baseConstants.toList do
    match imported[name]? with
    | some other => unless sameConstantData info other do return none
    | none => return none
  let rest := imported.filter fun name _ => !baseConstants.contains name
  let baseKernel ← rebuild baseConstants
  return some (← Kernel.Environment.replay rest baseKernel)

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
      -- Built first, so that a `rebuild` that refuses does so before the replay.
      let rebuilt ← rebuild imported
      let fromBase ← match base with
        | some names => incremental env imported names
        | none => pure none
      let replayed ← match fromBase with
        | some checked => pure checked
        | none => do
          Kernel.Environment.replay imported (← mkEmptyEnvironment).toKernelEnv
      importsReplayed := sameKernel replayed rebuilt
      -- A later, larger closure may rebuild this one from its module data.
      extendable := match mergeModules env.header.moduleData with
        | some merged => sameConstantsData merged imported
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
        let checked ← Kernel.Environment.replay merged (← mkEmptyEnvironment).toKernelEnv
        theorems := theoremAxioms env (Environment.ofKernelEnv checked) module own
      else
        let checked ← Kernel.Environment.replay declared (← rebuild imported)
        theorems := theoremAxioms env (Environment.ofKernelEnv checked) module own
        ran := "imports cached"
    let modules ← env.header.moduleNames.mapM fun m => do
      return toJson #[m.toString, (← findOLean m).toString]
    IO.println (Json.mkObj [
      ("replay", toJson ran), ("imports_replayed", toJson importsReplayed),
      ("fresh", toJson fresh), ("imports", toJson (imports.map importJson)),
      ("modules", toJson modules), ("theorems", toJson theorems),
      ("extendable", toJson extendable), ("base_modules", toJson baseModules)]).compress
  return 0
