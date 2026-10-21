# Taskvalet computation workers

Glitnir can submit saved-file Lean checks to an authenticated Taskvalet server.
The server queues each frozen request for a compatible worker. The editor stays
responsive, and coding agents can submit a job and continue working before
retrieving its result. Swarm membership is not required.

Workers perform the existing sandboxed elaboration and independent kernel replay.
A completed job can contain admitted, conditional or withheld claims, or an
unavailable check. Completion alone does not establish a theorem. Taskvalet
attributes reports; it does not certify mathematics.

## VS Code

Open **Glitnir: Open Settings**, or search Settings for @ext:gimle.glitnir.

| Setting | Meaning |
| --- | --- |
| glitnir.executionBackend | local (default) or taskvalet |
| glitnir.taskvaletServer | HTTPS origin without a path |
| glitnir.taskvaletProject | Project authorized by the credential |
| glitnir.taskvaletEnvironment | Versioned prepared environment |
| glitnir.elaborationTimeoutMs | Existing time limit; remote queue time counts |
| glitnir.memoryLimitMb | Existing Lean memory ceiling, also used remotely |

**Set Taskvalet Credential** stores a dedicated compute-client token in VS Code
SecretStorage, scoped to server origin and project. **Clear Taskvalet Credential**
removes it. Credentials are never workspace settings. A trusted workspace and
matching local backend are still required. Settings/credential changes close
existing previews and cancel their work; reopen them to use the new configuration.

Check and Check Through Line use Taskvalet. Proof exploration, views, evidence and
decisions remain local in this first version. Progress shows the job state.
Saved results retain worker/job, checker, dependency-context and actual
checked-environment identities. Dependency byte changes and destination changes
invalidate remote freshness.

## Server administration

Use a Taskvalet build supporting glitnir.check/v1 with authenticated hosting.
Add this table to its server configuration:

    [compute]
    enabled = true
    max_pending = 100

On the server, issue separate project-scoped credentials:

    valet access --database /srv/taskvalet/credentials.sqlite3 issue \
      --role compute_client --project lab --server-url https://tasks.example.com \
      --output /secure/compute-client.json
    valet access --database /srv/taskvalet/credentials.sqlite3 issue \
      --role compute_worker --project lab --server-url https://tasks.example.com \
      --output /secure/compute-worker.json

Use the actual configured database and owner. Keep the generated JSON files
private (mode 600). Existing credentials retain their issued capabilities;
installation does not upgrade them. Compute-only credentials cannot access the
ordinary task board or mutate human task claims.

## Prepared worker

Install the **same Glitnir backend wheel** on client and worker. Prepare and
build the project with its pinned Lean toolchain and dependencies first.
The worker never fetches packages or edits the prepared project.

    python -m glitnir.compute \
      --server https://tasks.example.com --project lab --environment example-v1 \
      --credential-file /secure/compute-worker.json \
      worker --prepared /srv/lean/example-v1 --journal /srv/lean/job-journal \
      --max-seconds 1800 --max-memory-mb 16384

Run this as a supervised service under a dedicated operating-system user.
It polls outbound and needs no inbound port. Each process runs one job at a time.
The default worker ceiling is 16384 MB, matching the maximum default editor
request. Provision that capacity or explicitly lower both the worker ceiling and
the editor memory setting. Jobs need a worker whose ceiling covers their request.
Add workers on larger machines to add capacity. Glitnir serializes Lean runs in
each user's temporary area, so workers intended to run concurrently need
separate users or private TMPDIR directories with their own resource allocation.

Use immutable, versioned environment names. Client and worker must have matching
saved project/dependency sources and Lake configuration; only the selected
candidate is transported. Other uncommitted edits must already exist on the
worker. Operators provide compiled artifacts, whose actual identity is captured
by native checking and replay. The source-context hash does not assert that an
artifact was reproduced from those sources.

Supported dependencies live under .lake/packages. Overrides, dependencies
elsewhere, custom manifest/config filenames and symbolic links in build trees
are refused. Each process makes one private materialized copy of the prepared
build, then reuses it with candidate overlays. The first job includes that disk
copy and needs sufficient space. Existing artifact and kernel-replay caches
remain available. This is not a shared mutable Lean REPL.

## Coding agents

The CLI works independently of the editor and swarm launcher. Common options
precede the subcommand:

    python -m glitnir.compute \
      --server https://tasks.example.com --project lab --environment example-v1 \
      --credential-file /secure/compute-client.json \
      submit Example.lean --receipt /private/jobs/check-1.json

Submission returns a UUID and state immediately. The receipt is written
atomically before the request is sent. Continue working, then use status RECEIPT,
result RECEIPT, or cancel RECEIPT with the same common options. Result returns the
frozen snapshot's report even after edits: compare source/context hashes before
applying it. Submit also accepts --through-line, --timeout-seconds and --memory-mb.

If the response is lost, use retry RECEIPT to resend the same UUID and exact bytes.
A new submit is a new job. Editor submission errors include the recovery path.
The CLI accepts Taskvalet's issued JSON credential files and verifies their
origin/project binding.

Environment alternatives are GLITNIR_TASKVALET_URL, GLITNIR_TASKVALET_PROJECT,
GLITNIR_TASKVALET_ENVIRONMENT and GLITNIR_TASKVALET_TOKEN.
GLITNIR_TASKVALET_CA_FILE supplies a private CA bundle; verification is never
disabled. The blocking notebook CLI also supports these variables when
GLITNIR_EXECUTION_BACKEND=taskvalet.

## Failure and recovery

Queue time counts toward deadlines. Workers heartbeat every ten seconds;
expired leases become interrupted. Cancellation is terminal immediately.
Workers stop when they observe cancellation, lose transport, reach their
deadline or receive termination. Interrupted jobs are never silently rerun;
late success after cancellation or expiry is refused.

If completion publication fails, lease.json and completion.json remain in the
per-job journal directory. Use retry-completion JOB_DIRECTORY with the original
credential and common options. This republishes the saved report without Lean.
It requires the original live lease or repeats an identical accepted completion;
expired/interrupted leases cannot be revived.

Receipts contain source/results and worker journals contain lease secrets.
Keep them private. Retention is operator-managed: archive/remove terminal
journals and stopped workers' staging copies. Pending-job limits do not bound
historical storage. Deployment and environment provisioning are separate
operator steps; building this branch changes no running server.

## Review scope

Independent reviewers covered queue recovery, credential isolation and the proof
boundary. Fixed findings included sandbox-contained staging, dependency override
rejection, exact dependency freshness, invalid-job handling and durable recovery.
No blockers remain for the supported prepared-project layout. Restart backends
and workers when installing a new checker; do not replace their installed files
in place during an active check.

## Local validation

The full backend suite passed: 1,399 tests, with 62 optional/live cases skipped.
Two additional editor integration cases passed, including rejection of a context
change before submission. The native worker test separately passed real Lean
checks for a valid theorem and an admitted theorem using the same staged build.
All 293 extension tests, VSIX packaging verification and the isolated packaged
VS Code host test passed. Black, isort, flake8 and mypy passed.

An isolated HTTPS Taskvalet server, real issued credentials, installed CLI and
native worker completed an end-to-end check. Submission returned in 0.7 seconds;
the agent then edited its source while the worker checked the frozen version.
The checked result referred to the original theorem and the edit remained intact.
A prior cold run correctly reached its ten-minute deadline; the successful run
used the supported thirty-minute budget. These are correctness checks, not a
performance benchmark or a production deployment.

## Release candidate 0.1.4

The release branch integrates upstream main through `330c497` and pairs with
Taskvalet 0.14.0. The integration review found and fixed three issues: worker
memory ceilings now cover default editor requests; remote submission applies the
same supported-Lean guard as local checking, including inherited toolchains; and
local Z3 search does not inherit the remote computation credential. Regression
tests cover each boundary. The review panel found no remaining blockers after
these fixes.

The release remains subject to Linux CI (including every required live Lean
test), exact candidate asset verification and fresh-profile installation checks.
The earlier end-to-end evidence above describes the initial implementation;
release artifacts must be validated separately before publication or rollout.

After integration, the installed 0.1.4 wheels passed 1,538 non-live backend tests
(2 optional skips; 65 live cases delegated to CI), 1,515 plugin tests (295
optional integration skips), and all 303 extension tests. Backend and plugin
format/lint/type checks passed. The universal VSIX and isolated packaged macOS
host check passed. These checks used the integrated release worktree; CI binds
its own built assets to the eventual candidate commit.
