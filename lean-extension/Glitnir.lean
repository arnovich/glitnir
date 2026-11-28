/- Optional syntax for attaching authored prose to ordinary tactic execution. -/
import Glitnir.ExplanationInfo

open Lean Elab Tactic

namespace Glitnir

syntax (name := explain) "explain " str str " =>" tacticSeqIndentGt : tactic

elab_rules : tactic
  | `(tactic| explain $id:str $markdown:str => $body:tacticSeq) => do
    pushInfoLeaf <| .ofCustomInfo {
      stx := ← getRef
      value := .mk <| ExplanationInfo.toJson {
        authoredId := id.getString
        markdown := markdown.getString
      }
    }
    evalTactic body

end Glitnir
