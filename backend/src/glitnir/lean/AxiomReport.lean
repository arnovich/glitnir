/-
Report what a compiled module declares, read from its `.olean` alone.

    lean --run AxiomReport.lean MODULE [DECL [NAME...]]

Loads MODULE through `importModules`, which reads the module data without
running the module's commands, macros, elaborators or initializers, and prints
one JSON document: the module's imports, every constant the module itself
declares (kind, universe parameters and `meaning`, its canonical entry), and,
when DECL is given, the full name of the module's unique constant whose last
component is DECL, the axioms that constant depends on transitively, and the
canonical proposition (`statements`) of DECL, of each axiom it depends on that
the module declares, and of each module constant whose full name is one of the
NAMEs.

A constant's canonical entry is what the kernel reads of that one declaration:
its name, kind, universe parameter names, type, a definition's value and an
inductive type's, constructor's or recursor's shape (a recursor's with its
rules). Other constants are named
in full, never expanded. Two modules declaring a constant with equal entries
declare the same thing to the kernel.

A canonical proposition is what a constant's type means, written so that two
of them are equal only when the kernel reads the same proposition: the type
itself and, recursively, every constant the module declares that it refers
to (their types, the values of definitions, the constructors of inductive
types). Constants imported from other modules are named, never expanded, so
two propositions agree only over the same imports. A constant whose name
extends the described one's (a `match` auxiliary, say) is written relative to
it, so a proposition stated under two names can still agree.

Both encodings write expressions as a DAG, each distinct node once, so a large
statement with shared subterms stays small. Both leave out exactly what the
kernel ignores: binder names (it works on de Bruijn indices, and the hygienic
names Lean invents differ between two modules compiled from the same text),
metadata, and the `nondep` flag of a `let` (Lean sets it in a theorem's type,
not in an axiom's, and the kernel neither checks nor uses it). A definition's
reducibility hints are left out too: they only order the kernel's unfolding,
never what a term means. A canonical proposition also leaves out the names of
universe parameters: each constant's are bound by that declaration alone and
instantiated by position, so it writes their count and each use as its
position in the list of the constant it occurs in. `L.{u}` and `L.{u_1}`
over the same type are then one proposition, while the order of the
parameters is kept. Everything else is kept, including binder kinds,
literals, recursor rules and, in a canonical entry, universe parameter names.

This program is fixed: the Python side runs it as it is shipped and never
builds it from checked source.
-/
import Lean
import Std.Data.HashMap
open Lean

def kindOf : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quotient"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"

def nameJson (n : Name) : Json :=
  Json.arr (n.components.toArray.map fun
    | .str _ s => toJson s
    | .num _ k => toJson k
    | .anonymous => Json.null)

/-- A universe level. With `params`, the universe parameters of the constant
being written, a parameter is written as its position in that list: the
parameters are bound by the declaration alone and instantiated by position,
so their names mean nothing to the kernel. A parameter outside the list
(which the kernel never admits) keeps its name, an array, never a number. -/
partial def levelJson (params : Option (List Name)) : Level → Json
  | .zero => Json.arr #[toJson "zero"]
  | .succ l => Json.arr #[toJson "succ", levelJson params l]
  | .max a b => Json.arr #[toJson "max", levelJson params a, levelJson params b]
  | .imax a b => Json.arr #[toJson "imax", levelJson params a, levelJson params b]
  | .param n => match params.bind (·.idxOf? n) with
    | some i => Json.arr #[toJson "param", toJson i]
    | none => Json.arr #[toJson "param", nameJson n]
  | .mvar m => Json.arr #[toJson "mvar", nameJson m.name]

def binderJson : BinderInfo → Json
  | .default => toJson "default"
  | .implicit => toJson "implicit"
  | .strictImplicit => toJson "strict"
  | .instImplicit => toJson "instance"

/-- What a canonical encoding is relative to. -/
structure Canon where
  /-- The constant being described. -/
  root : Name
  /-- The constants that may be written relative to the root: every constant
  the module declares for a proposition, none for a constant's own entry. -/
  declared : NameSet
  /-- For a proposition, the universe parameters of the constant being
  written, which are then written by position; none for a constant's own
  entry, which keeps their names. -/
  params : Option (List Name) := none

/-- A constant's universe parameters: their names in an entry, their count
(as the positions `0, 1, …`) in a proposition. A constant without any writes
`[]` either way. -/
def Canon.paramsJson (c : Canon) (ls : List Name) : Json :=
  if c.params.isSome then Json.arr ((List.range ls.length).toArray.map toJson)
  else toJson (ls.map Name.toString)

/-- A constant's name: relative to the root when the module declares it under
the root's name, as written otherwise. Distinct names stay distinct. -/
def Canon.ref (c : Canon) (n : Name) : Json :=
  if c.declared.contains n && c.root.isPrefixOf n then
    Json.arr #[toJson "self", Json.arr ((n.components.drop c.root.getNumParts).toArray.map
      fun | .str _ s => toJson s | .num _ k => toJson k | .anonymous => Json.null)]
  else
    Json.arr #[toJson "name", nameJson n]

/-- An expression as a hash key under `Expr.equal`, which compares binder
names and kinds too: finer than the canonical encoding, so a memo hit can never
merge two expressions the encoding would tell apart. -/
structure Exact where
  e : Expr

instance : BEq Exact := ⟨fun a b => a.e.equal b.e⟩
instance : Hashable Exact := ⟨fun a => a.e.hash⟩

/-- A canonical proposition is written as a DAG: each distinct node once, by
index. Nodes are identified by their own encoded text, so two DAGs have equal
text exactly when the trees they stand for are equal; `memo` only saves work. -/
structure Dag where
  index : Std.HashMap String Nat := {}
  nodes : Array Json := #[]
  memo : Std.HashMap Exact Nat := {}

abbrev DagM := StateM Dag

def Dag.node (j : Json) : DagM Nat := do
  let d ← get
  let key := j.compress
  if let some i := d.index[key]? then return i
  let i := d.nodes.size
  set { d with index := d.index.insert key i, nodes := d.nodes.push j }
  return i

partial def Canon.expr (c : Canon) (e : Expr) : DagM Nat := do
  if let some i := (← get).memo[Exact.mk e]? then return i
  let j ← match e with
    | .bvar i => pure (Json.arr #[toJson "bvar", toJson i])
    | .fvar f => pure (Json.arr #[toJson "fvar", nameJson f.name])
    | .mvar m => pure (Json.arr #[toJson "mvar", nameJson m.name])
    | .sort l => pure (Json.arr #[toJson "sort", levelJson c.params l])
    | .const n ls => pure (Json.arr #[toJson "const", c.ref n,
        Json.arr (ls.toArray.map (levelJson c.params))])
    | .app f a => do
        let x ← c.expr f; let y ← c.expr a
        pure (Json.arr #[toJson "app", toJson x, toJson y])
    | .lam _ t b bi => do
        let x ← c.expr t; let y ← c.expr b
        pure (Json.arr #[toJson "lam", binderJson bi, toJson x, toJson y])
    | .forallE _ t b bi => do
        let x ← c.expr t; let y ← c.expr b
        pure (Json.arr #[toJson "pi", binderJson bi, toJson x, toJson y])
    | .letE _ t v b _ => do
        let x ← c.expr t; let y ← c.expr v; let z ← c.expr b
        pure (Json.arr #[toJson "let", toJson x, toJson y, toJson z])
    | .lit (.natVal n) => pure (Json.arr #[toJson "nat", toJson n])
    | .lit (.strVal s) => pure (Json.arr #[toJson "str", toJson s])
    | .mdata _ b => pure (Json.arr #[toJson "same", toJson (← c.expr b)])
    | .proj s i b => do
        let x ← c.expr b
        pure (Json.arr #[toJson "proj", c.ref s, toJson i, toJson x])
  -- Metadata is dropped: a node that only wraps another stands for it.
  let i ← match j with
    | .arr #[.str "same", .num n] => pure n.mantissa.toNat
    | _ => Dag.node j
  modify fun d => { d with memo := d.memo.insert (Exact.mk e) i }
  return i

/-- The structures an expression projects from; `getUsedConstants` sees only
constants. -/
partial def projected (e : Expr) (found : Array Name := #[]) : Array Name :=
  match e with
  | .proj s _ b => projected b (found.push s)
  | .app f a => projected a (projected f found)
  | .lam _ t b _ | .forallE _ t b _ => projected b (projected t found)
  | .letE _ t v b _ => projected b (projected v (projected t found))
  | .mdata _ b => projected b found
  | _ => found

/-- Every constant an expression names, projections' structures included. -/
def refsOf (e : Expr) : Array Name :=
  e.getUsedConstants ++ projected e

/-- The constants a declared constant's meaning refers to: its type, a
definition's value (a theorem's proof is irrelevant to what it states), an
inductive type's constructors and mutual group, a constructor's type. -/
def meaningRefs : ConstantInfo → Array Name
  | .defnInfo d => refsOf d.type ++ refsOf d.value
  | .opaqueInfo d => refsOf d.type ++ refsOf d.value
  | .inductInfo d => refsOf d.type ++ d.ctors.toArray ++ d.all.toArray
  | .ctorInfo d => refsOf d.type ++ #[d.induct]
  | .recInfo d => refsOf d.type ++ d.all.toArray
  | info => refsOf info.type

/-- One declared constant as a closure entry, its expressions by DAG index. -/
def Canon.entry (c : Canon) (n : Name) (info : ConstantInfo) : DagM Json := do
  let type ← c.expr info.type
  let value ← match info with
    | .defnInfo d => pure (toJson (← c.expr d.value))
    | .opaqueInfo d => pure (toJson (← c.expr d.value))
    | _ => pure Json.null
  -- A recursor's rules are what the kernel reduces it by. Replay already
  -- rebuilds them from the inductive, which is compared too; writing them out
  -- keeps the entry complete on its own.
  let rules ← match info with
    | .recInfo d => do
        let written ← d.rules.toArray.mapM fun r => do
          pure (Json.arr #[c.ref r.ctor, toJson r.nfields, toJson (← c.expr r.rhs)])
        pure (Json.arr written)
    | _ => pure Json.null
  let shape := match info with
    | .inductInfo d => Json.arr #[toJson d.numParams, toJson d.numIndices,
        Json.arr (d.all.toArray.map c.ref), Json.arr (d.ctors.toArray.map c.ref),
        toJson d.isRec, toJson d.isUnsafe, toJson d.isReflexive, toJson d.numNested]
    | .ctorInfo d => Json.arr #[c.ref d.induct, toJson d.cidx, toJson d.numParams,
        toJson d.numFields, toJson d.isUnsafe]
    | .recInfo d => Json.arr #[Json.arr (d.all.toArray.map c.ref), toJson d.numParams,
        toJson d.numIndices, toJson d.numMotives, toJson d.numMinors, toJson d.k,
        toJson d.isUnsafe, rules]
    | .defnInfo d => toJson (match d.safety with
        | .safe => "safe" | .unsafe => "unsafe" | .partial => "partial")
    | .opaqueInfo d => toJson d.isUnsafe
    | .axiomInfo d => toJson d.isUnsafe
    | _ => Json.null
  return Json.arr #[c.ref n, toJson (kindOf info), c.paramsJson info.levelParams,
    toJson type, value, shape]

/-- The canonical proposition of `root`: its universe parameters and type,
then every declared constant its meaning reaches, ordered by name, all over
one DAG of expression nodes. The constants are found first, and the DAG is
built after sorting, so node numbering depends only on what is described.
Each constant's universe parameters are written by position in its own list
(`levelJson`), so the expression memo, keyed by the expression alone, is
emptied before each constant: one `Sort u` is a different position in each. -/
partial def canonical (env : Environment) (declared : NameSet) (root : Name)
    (info : ConstantInfo) : String := Id.run do
  let c : Canon := { root, declared }
  let mut seen : NameSet := {}
  let mut todo := (refsOf info.type).toList
  let mut reached : Array (String × Name × ConstantInfo) := #[]
  while true do
    match todo with
    | [] => break
    | n :: rest =>
      todo := rest
      if seen.contains n || !declared.contains n then continue
      seen := seen.insert n
      let some found := env.find? n | continue
      reached := reached.push ((c.ref n).compress, n, found)
      todo := (meaningRefs found).toList ++ todo
  let sorted := reached.qsort (fun a b => a.1 < b.1)
  let own : Canon := { c with params := some info.levelParams }
  let run : DagM Json := do
    let type ← own.expr info.type
    let entries ← sorted.mapM fun (_, n, found) => do
      modify fun d => { d with memo := {} }
      ({ c with params := some found.levelParams } : Canon).entry n found
    let dag ← get
    return Json.arr #[own.paramsJson info.levelParams, toJson type,
      Json.arr entries, Json.arr dag.nodes]
  return (run.run' {}).compress

/-- A constant's canonical entry, over its own DAG, with every name in full. -/
def meaningOf (name : Name) (info : ConstantInfo) : String :=
  let c : Canon := { root := .anonymous, declared := {} }
  let run : DagM Json := do
    let entry ← c.entry name info
    return Json.arr #[entry, Json.arr (← get).nodes]
  (run.run' {}).compress

def describe (name : Name) (info : ConstantInfo) : Json :=
  Json.mkObj [
    ("name", toJson name.toString),
    ("kind", toJson (kindOf info)),
    ("levels", toJson (info.levelParams.map Name.toString)),
    ("meaning", toJson (meaningOf name info))]

def main (args : List String) : IO UInt32 := do
  let (moduleText, target) ← match args with
    | [m] => pure (m, none)
    | m :: d :: _ => pure (m, some d)
    | _ => do IO.eprintln "usage: AxiomReport MODULE [DECL [NAME...]]"; return 2
  let requested := args.drop 2
  initSearchPath (← findSysroot)
  let module := moduleText.toName
  let env ← importModules #[{ module }] {} (trustLevel := 0)
  let some index := env.header.moduleNames.idxOf? module
    | IO.eprintln s!"module {module} was not loaded"; return 3
  let data := env.header.moduleData[index]!
  let names := data.constNames
  let constants := (names.zip data.constants).toList.map fun (n, c) => describe n c
  let imports := data.imports.toList.map fun i => toJson i.module.toString
  let mut fields : List (String × Json) := [
    ("module", toJson module.toString),
    ("imports", Json.arr imports.toArray),
    ("constants", Json.arr constants.toArray)]
  if let some decl := target then
    let found := names.filter fun n => n.getString! == decl
    unless found.size == 1 do
      IO.eprintln s!"expected one constant named {decl}, found {found.size}"
      return 4
    let full := found[0]!
    let (axioms, _) ← (collectAxioms full : CoreM (Array Name)).toIO
      { fileName := "", fileMap := default } { env }
    let declared : NameSet := names.foldl (·.insert ·) {}
    let described := #[full] ++ axioms.filter declared.contains
      ++ names.filter fun n => requested.contains n.toString
    let mut statements : Array (String × Json) := #[]
    for n in described do
      if statements.any (·.1 == n.toString) then continue
      let some info := env.find? n | continue
      statements := statements.push (n.toString, toJson (canonical env declared n info))
    fields := fields ++ [
      ("declaration", toJson full.toString),
      ("axioms", Json.arr (axioms.map fun a => toJson a.toString)),
      ("statements", Json.mkObj statements.toList)]
  IO.println (Json.mkObj fields).compress
  return 0
