/- Authored explanation metadata for a Glitnir tactic observation. -/
import Lean

-- The toolchain-owned Json type is the payload ABI shared with ProofTrace.lean.
deriving instance TypeName for Lean.Json

namespace Glitnir

/-- A small payload attached to Lean's information tree while an explanation runs. -/
structure ExplanationInfo where
  authoredId : String
  markdown : String

/-- The narrow wire value read by Glitnir's toolchain-only trace helper. -/
def ExplanationInfo.toJson (info : ExplanationInfo) : Lean.Json :=
  Lean.Json.mkObj [
    ("kind", Lean.toJson ("glitnir.explanation/v1" : String)),
    ("authored_id", Lean.toJson info.authoredId),
    ("markdown", Lean.toJson info.markdown)
  ]

end Glitnir
