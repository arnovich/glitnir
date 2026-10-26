# Offline warm Lean worker experiment

This is a fixed-fixture, advisory-only prototype for Taskvalet task006. It is
excluded from the backend wheel, extension and production compute worker. It
starts no listener, enrolls no agents, calls no model provider and changes no
live server or project. Only Glitnir's independent native checker can accept a
candidate.

## Run

Install the backend wheel and locked development dependencies as described in
[DEVELOPMENT.md](../../../DEVELOPMENT.md), with its supported Lean toolchain and
working sandbox. From `backend/`:

```sh
../.venv/bin/python -m pytest tests/test_warm_pool_experiment.py -m 'not live'
GLITNIR_REQUIRE_PROJECT_INSPECTION=1 GLITNIR_TEST_PROOF_TRACE=1 \
  ../.venv/bin/python -m pytest tests/test_warm_pool_experiment.py -m live
../.venv/bin/python -m experiments.warm_pool.benchmark --output new-results.jsonl
```

The output file must not already exist. Scratch is private and removed on exit;
only the JSONL evidence remains. Cancellation or a failed assertion leaves a
partial journal without a `finished` event. Such a journal is not a completed
comparison. Never merge partial and completed runs into one sample.

The native test defaults to a 3 GiB scratch allowance. CI explicitly calculates
a test-only allowance from its installed toolchain, capped at 16 GiB and checked
against free space. This does not increase the standalone experiment's 3 GiB
allowance. CI checks this experiment's Python and compiles its Lean helper;
normal test collection never starts a benchmark.

## What is compared

The timing matrix uses public `Init` and `Std.Data.HashMap` fixtures, two identical
valid requests per scenario, and alternating logical clients. These are small
standard-library workloads, not Mathlib or physical-system projects. Modes are:

| Mode | Project/build and snapshot | Diagnostic process |
| --- | --- | --- |
| Cold | New fixed project, build and snapshot per request | New process per request |
| Prepared/frozen | One build and frozen snapshot per scenario | New process per request |
| Warm | One build and frozen snapshot per scenario | Reuse imported environment; fresh command state per request |

One-time shared artifact-store initialization is measured separately before the
matrix. It is a common setup cost, not charged solely to the first cold scenario.
The OS page cache is never flushed. Profile order reverses the mode order, but
there are too few samples for a statistical speedup, percentile or capacity claim.

Every matrix request is independently validated by `check_candidate` with its
exact source bytes, saved-header hash and original context. The warm pool closes
before native validation. Each scenario starts with an empty owned replay cache;
subsequent native requests may reuse that cache under the same policy in every
mode. Native validation still captures its inputs independently. The advisory
prepared/frozen baseline is therefore different from today's native compute job.

Pool submission occurs after fixture preparation. Its queue/response times
exclude preparation, which remains in the preparation and scenario totals.
No remote API transport is timed. Receipts separate queue age, service/response time, startup/import and Lean's
own command time. Preparation has its own wall time, including capture. Native
receipts include stage times and an actual submission-to-final elapsed time.
That elapsed time includes the advisory-batch barrier and serial native queue;
it is not a production editor-latency estimate. One/two-worker four-job bursts
are separately labelled as initially cold processes, not steady-state capacity.

## Isolation and recovery probes

A separate fixture imports `Lean` and initializes an `IO.Ref`. Fixed probes test
scope/option reset, repeated declarations, changed imports, process loss,
request-count recycling, request-bound cancellation after Lean's start ACK,
changed saved source/configuration/compiled inputs, and mutable imported tactic
state. Two independent native checks of the stateful candidate establish the
final-admission boundary. Admitted and invalid proofs are checked separately:
`by sorry` may elaborate but must fail native admission.

Command-state reset does not reset process-global IO. The prototype makes no
cross-tenant isolation promise. Its diagnostic filename is `Candidate.lean`, so
file-sensitive macros/tactics can differ from a normal project check. Warm
responses always carry `proof_authority: false`; stdout or an imported tactic
cannot make an advisory response a checked theorem.

Snapshots use the native full content identity, including the frozen toolchain,
compiled inputs, setup, source/configuration and checker, plus the experimental
helper hash. Original-input stat guards detect ordinary operator edits. They do
not defend against a malicious host rewriting its own immutable store. No
portable proof-state checkpoint is created or trusted.

## Resource and outcome limits

The standalone run admits at most two diagnostic workers, 128 retained jobs,
16 pending jobs, eight requests per process, one million source/reply bytes,
4096 MB per Lean process and 1800 seconds per run. The process recycles after a
complete reply at a sampled 90% RSS high-water mark; exceeding the memory ceiling
interrupts it. RSS includes ordinary descendants and may count shared pages
more than once. Sampling cannot establish a true memory peak or replace an
aggregate OS resource boundary.

A preparation preflight reserves the first toolchain capture and scratch
headroom. A half-second monitor counts unique regular-file logical bytes in the
owned scratch tree, deduplicates hard links, and cancels on 3 GiB, low free space
or the wall deadline. This is a sampled guard, not a filesystem quota; it excludes
metadata/symlinks and may overshoot between samples. Native scratch/output limits
remain in force. Fixed Lake builds run in owned process groups, bounded by time,
output and shared cancellation, and are stopped with their ordinary descendants.

Queued cancellation never executes the job. Process death, observer failure,
protocol ambiguity and unknown outcomes are explicit interruptions. Identical
request IDs retrieve their original future; changed bytes under an ID are
refused. Process-generation numbers are local to one Pool; do not join receipts
from different pools by generation alone. An interrupted request is never silently executed again. A later
request uses a new UUID and, when required, a new process generation.

## Evidence and decision

Defer shared warm elaboration across agents. The native control demonstrates
persistent imported IO state despite a fresh command state. The identical proof
elaborates first, fails on its second warm request, and elaborates in a fresh
process. Two independent native checks accept those same source bytes. The
proof remains valid; the warm diagnostic behavior is not reproducible across
independent candidates. Preserve prepared/build/artifact/replay reuse while
requiring a new isolation design before considering shared warm elaboration.

The [completed measurement report](results/REPORT.md) records all six scenarios,
independent checks and limitations. The
[measurement source archive](results/macos-arm64-source.zip) preserves the exact
Python/Lean bytes identified by `experiment_sha256`; the source tree may include
subsequent formatting or review fixes. Verify that digest by concatenating each
archived filename, a NUL byte and its contents in sorted filename order, then
hashing the result with SHA-256. The
JSONL manifest binds code, helper, checker, supported Lean, host architecture and
limits. Incomplete development runs are excluded from accepted evidence.

Production integration requires a separate proposal: Taskvalet owns durable
scheduling and leases; Glitnir owns execution and final replay. Keep the existing
`glitnir.check/v1` meaning unchanged. A diagnostic job kind, trusted-state reset
mechanism, aggregate worker limits or service rollout is not provided here.
Portable checkpoints, autoscaling and capacity increases remain deferred.
