# Two-round Lean swarm evidence report

Status: **executed; trial invalidated; no admitted proof**

Task031 executed the predeclared six-call adaptive arm and six-call non-adaptive
baseline against the same Lean target. All 24 planned Lean submissions ran under
the 300-second guard and sampled 8192 MiB process-tree RSS watchdog. Twenty-three
compiled; one bounded exploratory factorization failed elaboration. Every compiled
exact target reported `[propext, Classical.choice, Quot.sound]`, so the unchanged
empty-axiom policy rejected every candidate.

The trial does not support a causal comparison between the arms. One baseline
Round 1 researcher followed the required Taskvalet problem-read workflow and was
shown adaptive findings before deriving its candidate. Both arms also exceeded the
precommitted one-hour elapsed window. These are protocol failures, so this run is
evidence about orchestration and the checker boundary but is not a valid
adaptive-versus-baseline experiment.

## Pinned inputs

- Target: `TwoRoundTrial.target`
- Prepared companion notebook: `4b9976f1650f3cfd2233acf7898a9037b2b49c337aeafb0f03a144041672574f`
- Lean: `leanprover/lean4:v4.35.0`
- Mathlib: `56a005855ea17e0b0958f73ec801ae140b0a1679`
- Taskvalet protocol commit: `3ca2d8a3862260ab3dec1f57184957af66e7a121`
- Admitted protocol SHA-256: `fc98d0da8a257e305e276c578bcea1098ffef569bb3b3f1852402da9d8c874e1`
- Glitnir preparation commit: `00cd083dcad9f6c0e11945d6592052dbc795a59c`
- Evidence archive commit: `4f02cb733bbd7fc430f3de2fc317d512c962e14a`
- Completed report/test snapshot: `4f02cb733bbd7fc430f3de2fc317d512c962e14a`
- Glitnir source: `2c6238eff6de60d63e45e10c93cdc7239de7ad69`
- Checker digest: `6e9fafb9cd397abaa610c3676e27f404ef63926486c131734fdaebe9fd1c1b40`
- Context digest: `c33461ada7554b5d422bfb549ea40e0c83355f115d1de52bd75f2e1227908c7e`
- Environment: `two-round-lean-v1`
- Derived environment descriptor: `86c226102a0eed27ffba1f7672918396d26e03086ae563d2cb55d32a0f13adf7`
- Allowed target axioms: none

The operator recorded the owner's exact admission in Taskvalet question
`b113a93ccd65e496acb1e827`, resolution note
`6e1bb8e356eefe9aa74efd2e`. Execution used an isolated loopback pilot, not the
production Taskvalet service.

## Budget result

| Measure | Adaptive | Baseline | Precommitted limit |
|---|---:|---:|---:|
| Completed model calls (operator-reported) | 6 | 6 | 6 each |
| Lean submissions | 12 | 12 | 12 each |
| Maximum concurrent model calls | 3 | 3 | 3 |
| Per-check timeout | 300 s | 300 s | 300 s |
| Per-check RSS threshold | 8192 MiB | 8192 MiB | 8192 MiB |
| Observed Lean window | 88m 30s | 100m 45s | 60m each |
| Compiling submissions | 11 | 12 | informational |
| Admitted exact targets | 0 | 0 | outcome |

The RSS guard polled the process tree and killed above the threshold. It was not a
kernel hard memory limit. No check crossed either per-job limit. A provider-capacity
failure occurred before the final baseline checker started; retrying the same
registered participant produced the sixth completed baseline call without another
Lean submission.

## Adaptive arm

Round 1 produced compiling factorization and elimination candidates. Independent
replay found that the factorization candidate declared root `target` rather than
the frozen `TwoRoundTrial.target`; the elimination candidate preserved the exact
header. Both reported the same three forbidden axioms. The adaptive handoff selected
elimination for a dependency audit as diagnostic evidence only; no lemma bundle was
eligible under the empty allowlist.

Round 2 tested a generic `CommRing` elimination helper, a Cauchy-representation
transport, a corrected factorization, and a subtraction-free semiring expansion.
The generic helpers dropped `Classical.choice` but retained `propext` and
`Quot.sound`. The proposition containing only the exact real target type still
reported all three target axioms, consistent with Lean's axiom collector traversing
both declaration type and value. The subtraction-free expansion failed and its
recovery output contained `sorryAx`; it was recorded as a dead end. The final two
independent native replays compiled and retained all three forbidden axioms.

Final adaptive records:

- selected deepening finding: `3bf0aadc-0609-422f-80bf-9ebb53f07b1c`;
- alternative finding: `81d8bce7-d05a-43e2-a6ee-95f4674d4c94`;
- final verifications: `078b90a9-31da-441a-a99a-298fee3fd9b5` and
  `7a8684c1-ca5c-4516-a6d1-761ff46bfa77`;
- final synthesis: `cc15944a-88dd-40fa-85f3-431d420f952c`.

## Baseline arm

Round 1 produced compiling factorization and elimination candidates. The
factorization researcher avoided peer evidence through a filtered read. The
elimination researcher used the ordinary full problem view, which exposed adaptive
proof text before derivation. That contamination propagates to the baseline arm and
invalidates its sealed comparison. Independent replay still provides valid evidence
that both exact candidates compile and fail the empty-axiom gate.

The preassigned Round 2 continuations refined factorization and elimination without
reading more adaptive evidence. Generic helpers again retained `propext` and
`Quot.sound`; specialization to `ℝ` restored all three target axioms. The final
factorization source also used a one-line theorem header instead of the frozen
two-line header. The final checker selected elimination diagnostically because its
header matched, while rejecting it under the axiom policy.

Final baseline records:

- factorization finding: `90197253-d67e-4d57-b983-c297bb60fbdb`;
- elimination finding: `e5624a82-5d80-4ac4-9c68-2ce1aa692441`;
- final verifications: `33d5ee47-7776-48a7-9ebf-9e29cfea85f7` and
  `b8b3c5af-2a3c-4e01-97f7-f01dbee5e0a5`;
- final synthesis: `0e98a3d4-9adf-49c4-8081-700d44f750c1`.

## Evidence and limitations

The `evidence/` directory contains sanitized publication copies of participant Lean
sources, stdout/stderr, resource receipts, publication payloads, and credential-free
Taskvalet snapshots. `evidence/sanitization.json` records each private original hash,
published hash and placeholder replacement count. The archive excludes agent
credentials, private run journals and local machine paths. The final checker calls are
independent `lake env lean` replays under separate Taskvalet identities; the pilot
did not provision Glitnir's remote compute worker, so no Glitnir compute receipt was
issued. The checker/context digests above are admission pins, not a claim that
Glitnir executed these submissions.

The archive supports an audit of the published Lean sources, outputs, resource
receipts and Taskvalet evidence. It cannot replay the model calls or restore the
private Taskvalet database and participant journals, and it contains no Glitnir
compute receipt. Model-call and concurrency counts are operator-reported rather than
independently derivable from this publication archive.

Several immutable private-pilot verification records used an incorrect
`github.com/ErikMathiesen/glitnir` provenance URL before the configured origin was
checked. The correct repository is `github.com/arnovich/glitnir`. Sanitized
publication copies correct the URI and `sanitization.json` identifies the affected
record IDs while retaining original content hashes. The private pilot remains the
immutable source record.

The orchestration used ordinary Taskvalet assignments, durable claims, findings,
verifications, and Review transitions. It did not admit formal linked-round campaign
records in the isolated pilot. Together with the elapsed-budget breach and baseline
contamination, this prevents declaring the planned comparison successful.

## Evidence audit and native replay

The admitted protocol is fixed at Taskvalet commit
`3ca2d8a3862260ab3dec1f57184957af66e7a121` and SHA-256
`fc98d0da8a257e305e276c578bcea1098ffef569bb3b3f1852402da9d8c874e1`.
The evidence bytes are fixed at Glitnir commit
`4f02cb733bbd7fc430f3de2fc317d512c962e14a`; the completed report and receipt bindings for all 24 submissions are present at
`4f02cb733bbd7fc430f3de2fc317d512c962e14a`. Verify the protocol and completed
publication snapshot before constructing the Lean environment:

```bash
git -C taskvalet checkout 3ca2d8a3862260ab3dec1f57184957af66e7a121
printf '%s  %s\n' \
  fc98d0da8a257e305e276c578bcea1098ffef569bb3b3f1852402da9d8c874e1 \
  taskvalet/examples/two-round-lean-evaluation/protocol.json | shasum -a 256 -c -
git -C glitnir checkout 4f02cb733bbd7fc430f3de2fc317d512c962e14a
cd glitnir/examples/two-round-lean-evaluation
lake update
lake build
```

Then replay the four final Round 2 submissions directly with native Lean:

```bash
lake env lean evidence/a2-check/elimination.lean
lake env lean evidence/a2-check/factorization.lean
lake env lean evidence/b2-check/evidence/elimination.lean
lake env lean evidence/b2-check/evidence/factorization.lean
```

Each command should compile and print the exact target dependencies
`[propext, Classical.choice, Quot.sound]`; the baseline factorization also emits its
recorded deprecated-import warning. Run
`python -m pytest backend/tests/test_two_round_evaluation_example.py` from the
Glitnir root to verify archive coverage, every published hash, receipt-to-source and
receipt-to-log bindings, redaction invariants, arm counts and final record IDs.
`evidence/sanitization.json` maps immutable private-pilot hashes to transformed
publication hashes, so a transformed publication file is checked against
`published_sha256`; its embedded receipt remains bound to the original source/log
hash recorded as `original_sha256`.

The useful product conclusion is narrower: Taskvalet preserved attributed negative
evidence and exact failures, while the current shared-problem read model cannot seal
comparison arms. A future trial needs separate evidence visibility domains, an
enforced investigation-wide deadline, formal linked-round admission, and a prepared
Glitnir compute worker before the first paid call. It must also replace the empty
target-axiom gate: the proposition containing only this exact target type already
reports `propext`, `Classical.choice`, and `Quot.sound` in the pinned environment.
A repeat should predeclare that unavoidable type-level baseline or compare axioms
introduced by proof values.
