# Warm Lean experiment: measured result

**Decision: defer shared warm elaboration across independent agents.** A fresh command state does not reset imported mutable tactic state. Continue using the existing prepared-build, immutable-artifact and native replay caches; this change enables no production service.

## Timing matrix

macOS/Darwin 27, ARM64, eight logical CPUs, 24 GiB host RAM, Python 3.12.11 and Lean 4.35.0. Two identical valid candidates per row, alternating two logical clients. Values are seconds; each pair is request 1/request 2. These small samples are observations, not latency percentiles or a production speedup estimate.

| Import profile | Mode | Preparation total | Advisory response 1 / 2 | Native validation 1 / 2 | Scenario elapsed |
| --- | --- | ---: | ---: | ---: | ---: |
| Basic | cold | 42.431 | 3.476 / 5.044 | 76.064 / 39.151 | 167.859 |
| Basic | prepared | 20.922 | 3.508 / 3.352 | 80.759 / 40.924 | 150.885 |
| Basic | warm | 23.077 | 5.133 / 0.201 | 81.458 / 40.356 | 151.625 |
| Standard | warm | 23.657 | 5.216 / 0.192 | 104.295 / 47.680 | 182.396 |
| Standard | prepared | 24.490 | 6.648 / 4.108 | 99.443 / 49.411 | 185.603 |
| Standard | cold | 47.478 | 11.612 / 4.212 | 98.414 / 95.639 | 259.029 |

Common artifact-store initialization took **36.610s**, separately from every row. All 12 timing candidates passed independent native validation. Each scenario began with an empty replay cache; subsequent checks used the same cache policy across modes. The second Standard/cold check spent 46.3 s capturing inputs and 33.3 s in the reader, despite reusing replayed imports. Its slower total must not be attributed solely to execution mode.

The repeated warm diagnostic returned in 0.192–0.201 s. Prepared/frozen requests still started a fresh diagnostic process. Basic prepared and warm scenario totals were 150.9 s and 151.6 s respectively: the faster second preliminary answer did not eliminate final replay/preparation costs. OS caches were not flushed, host load was not isolated or recorded, and both fixed proof bodies are trivial. No Mathlib/physical-system workload or remote Taskvalet transport is measured.

Pool submission occurs after fixture preparation; its response/queue times exclude that preparation. Preparation and scenario elapsed retain the cost. The warm pool closes before serial native validation; submission-to-final fields include that batch barrier and later validation queue. They are not production editor end-to-end latencies.

## Isolation and recovery

- Within one resident process, repeated declaration names and namespace/options reset worked. The identical stateful proof elaborated once, then failed with `imported tactic state leaked`; a fresh process elaborated it again. The source SHA-256 is `d97a88fc1e69019dfdcb9b02575a52f551c883e37c9b78abc97838836ee83db8`.
- Independent native validation accepted that exact stateful source twice: 136.6 s including 85.3 s importing/replaying its closure, then 49.7 s using cached imports. This validates the proof while exposing unstable preliminary elaboration.
- `by sorry` elaborated but native admission rejected it with `sorryAx`. The invalid proof produced diagnostic errors and was rejected natively. There are 16 independent native checks in total: 12 timing candidates plus these four controls.
- Process loss stayed `interrupted`; cancellation after a request-bound Lean start ACK stayed `cancelled`. Later work used new IDs/processes. Request-count recycling, changed environment selection and idempotent retrieval are covered by the unit/native controls; no unknown request is silently retried.
- Real source, configuration and compiled-artifact mutations were refused. The separate native test covers changed imports and missing previous declarations. Memory-triggered recycling has a targeted observer-injection unit test; this run did not force a native memory exhaustion.

## Queue and resource observations

Four-job bursts alternated two clients and started with cold processes. One slot had observed queue ages 0.000–6.093 s and finished its final request about 6.268 s after submission; two slots had queue ages 0.000–6.797 s and finished about 7.030 s after submission. This does not support increasing capacity: startup, contention and a tiny burst dominate. Steady-state capacity and server queue timing remain unmeasured.

Peak sampled owned regular-file logical bytes were **2.305GiB**, below the 3 GiB guard. Maximum sampled advisory process-tree RSS was 3345.9 MiB, below the 4096 MB ceiling. RSS may double-count shared pages and is not a true peak. Disk excludes metadata/symlinks, and sampling is not an OS quota. The run completed inside its 1800 s budget and removed its owned scratch; only evidence/source files remain.

## Reproducibility and review

[JSONL evidence](macos-arm64.jsonl) includes all six completed scenarios and a `finished` event. Development runs that stopped for invalid probe syntax/comparison flaws are excluded. [Exact measured sources](macos-arm64-source.zip) hash to the manifest’s `experiment_sha256`. Lean helper bytes match the final source tree. After measurement, the final source added a shared budget-aware wait to the cancellation and negative probes, plus formatting fixes. These guard paths did not hit a resource limit in the accepted run; the archive preserves the exact measured version, and the new forwarding behavior has two targeted regressions. The manifest also binds the installed checker and Lean helper. Core checker source baseline: `de528fd4e5bb1282ac37ad5dfd2846e0df56f821`.

Independent proof/isolation, reliability and measurement judges reviewed the prototype. Their preparation-cleanup, observer-failure, cancellation-synchronization, setup-cost and reporting findings were fixed. The result supports deferring shared mutable process reuse; a production proposal needs a demonstrated isolation boundary, aggregate OS limits, durable server timing/cache telemetry and representative target-host measurements. Portable checkpoints, autoscaling and rollout remain out of scope.
