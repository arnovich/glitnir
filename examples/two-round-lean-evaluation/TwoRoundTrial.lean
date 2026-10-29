import Mathlib

/-!
# Two-round checked-evidence evaluation

This notebook fixes one small Lean question for an adaptive two-round Taskvalet
swarm and an equal-budget non-adaptive baseline. The initial researchers see the
target and assumptions below, without a completed proof. The final version records
the selected round-one helper, the allocation decision and the independently
replayed outcome. Taskvalet campaign state and peer agreement never determine this
claim's Glitnir status.

Exact target: `TwoRoundTrial.target`

Exact assumptions: `a b c : ℝ` and `h : a + b + c = 0`, with no additional
hypotheses and an empty allowed-axiom policy.

Round-one directions:

* factor the difference between the two sides;
* eliminate `c` from the hypothesis and normalize.

The adaptive consumer may use one selected, independently reviewed checked module.
The baseline consumer receives its preassigned direction without selected evidence.
The report beside this notebook preserves failures, repeated work, resources and
human interventions. A remaining `sorry` is an explicit negative outcome, never a
completed target.
-/

namespace TwoRoundTrial

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
/- BEGIN PROOF target -/
  sorry
/- END PROOF target -/

end TwoRoundTrial
