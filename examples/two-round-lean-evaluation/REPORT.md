# Two-round Lean swarm evidence report

Status: **prepared; execution not admitted**

This companion is the human-readable destination for Taskvalet task 031. The fixed
protocol is maintained in the Taskvalet repository. Preparation and this notebook
contain no model result and authorize no model or Lean execution.

## Pinned inputs

- Target: `TwoRoundTrial.target`
- Prepared companion notebook: `4b9976f1650f3cfd2233acf7898a9037b2b49c337aeafb0f03a144041672574f`
- Lean: `leanprover/lean4:v4.35.0`
- Mathlib: `56a005855ea17e0b0958f73ec801ae140b0a1679`
- Glitnir source: `2c6238eff6de60d63e45e10c93cdc7239de7ad69`
- Glitnir checker: `6e9fafb9cd397abaa610c3676e27f404ef63926486c131734fdaebe9fd1c1b40`
- Prepared context: `c33461ada7554b5d422bfb549ea40e0c83355f115d1de52bd75f2e1227908c7e`
- Allowed axioms: none

## Predeclared comparison

Each arm has six `gpt-6-astra` calls at high reasoning effort, at most three
concurrent participants, one hour elapsed, twelve compute submissions, and per-job
limits of 300 seconds and 8192 MiB. Both use `codex-desktop/subagent-v1`. The
adaptive arm selects checked round-one evidence for its second-round consumer. The
baseline follows a fixed second-round allocation and receives no selected evidence.

## Results

To be completed from the admitted run's immutable Taskvalet records and Glitnir
receipts. The final report will give separate adaptive and baseline tables for:

- checked target and helper declarations;
- actual selected dependencies;
- elapsed time, model calls, compute submissions and observed resources;
- repeated, abandoned, failed and inconclusive work;
- human interventions; and
- exact revisions and replay commands.

## Decision and limitations

To be completed after both arms stop. Target completion is distinct from checked
advancement using a selected helper. A negative result remains valid. This single
trial cannot establish general mathematical superiority.
