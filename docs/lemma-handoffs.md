# Prepare a reusable checked-source module

An operator can prepare an imported helper module from a
`taskvalet.lean-lemma/v1` source bundle before allocating a consumer proof-check
job. Bundles are attributed worker observations. Glitnir treats their source as
untrusted; a successful build and metadata preflight do not establish proof
validity. The consumer's normal independent kernel replay remains mandatory.

This first preparation adapter runs on **Linux with bubblewrap and cgroup v2**.
Coding and tool-calling agents can export/retrieve bundles from any machine.
The operator runs preparation on the compute machine. The macOS proof-observer
sandbox remains unchanged and deliberately cannot launch a Lake build.

## Inputs and confinement

Use the same operator-prepared base, installed Glitnir checker and supported
`leanprover/lean4:v4.35.0` toolchain as the original job. The bundle's source-root
path must equal its reported module, for example `Example/Bounds.lean` for
`Example.Bounds`; projects with a `srcDir` prefix are not supported by this first
format. The module must be a valid Lake build target in that project. Supply a
new separate candidate path whose source imports the helper module. The command
does not edit the prepared base or editor sources, change a recipe/registry,
download packages, launch a worker or submit a job.

Preparation requires an existing aggregate resource boundary with `memory.max`
no greater than `--memory-mb`, `memory.swap.max=0`, and finite `pids.max <=128`.
These limits cover Lake children, private tmpfs and metadata inspection together.
Glitnir verifies the current cgroup and refuses to build outside it; it never
modifies the service limits. For example, a systemd operator can run:

```sh
systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 -p TasksMax=128 \
  python -m glitnir.compute --environment reuse-a prepare-handoff \
  /srv/prepared/base lemma.json Next.lean \
  --path Example/Next.lean --directory /srv/handoffs/reuse-a \
  --timeout-seconds 600 --memory-mb 4096 --max-bytes 20000000000
```

The user manager must support delegated memory/pid controls; otherwise use an
administrator-managed service with those limits. The private output directory
must be owned by this operator, mode700, outside the base and without symlink
ancestors. Its disk budget covers the derived project and a second worker-style
copy. Copying and build growth are monitored against that budget; use an external
filesystem quota as well if the host requires a strict instantaneous disk cap.
The cgroup bounds actual aggregate memory, including private tmpfs. Preparation
also caps build threads and individual artifact file sizes; captured logs have a
separate bounded size. Virtual-address-space limits are deliberately avoided:
Lean's runtime reserves address space independently of its actual memory use.

Lake runs with required bubblewrap confinement, no network, a clean environment,
the pinned toolchain/base readable and only the private `.lake` writable. It uses
the real `lake --keep-toolchain --no-cache build PATH:leanArts`, retaining Lake's
trace/hash/C/setup artifacts along with the importable outputs. A supervised build
group retains the preparation lock until descendants terminate, including after
the parent disappears. The existing read-only project inspector then resolves a
worker-style staged candidate copy and requires the helper import to be present.

## Receipt and recovery

The command prints one `glitnir.lean-handoff/v1` JSON object containing `prepared`,
`binding`, the original problem/author/job/source provenance and a resolved
preflight description. Save the full object. The `binding` contains exactly the
environment, checker/context digests, candidate path, seconds and memory accepted
by Taskvalet's existing `ComputeBinding`. Provision a normal compute worker with
the returned `prepared` directory, and give only `binding` to the participant's
operator compute configuration. Consumer check submission remains asynchronous.

Reuse the same command, bundle, candidate bytes, paths, checker and resource
limits to recover. `state.json` saves frozen inputs and an absolute deadline;
recovery does not extend it. An interrupted build is recreated from the original
base inside that deadline. A completed inventoried build is reused for an
interrupted metadata preflight. Changed inputs, corrupt artifacts, invalid saved
bindings and missing/expired resource boundaries are refused. A completed receipt
is reconstructed and checked against the original inputs before return.

Preparation never changes a participant journal, replaces an identity, resubmits
a compute job, increments model calls or grants owner acceptance. Its internal
staging request is never sent to Taskvalet. Keep actual agent/job/model budgets in
the existing campaign/host. An expired preparation requires an explicit new
operator attempt with a distinct directory and budget; it cannot silently reset
this journal or the consumer's existing proof-check allowance.

## Prepare and serve with one operator command

`handoff-worker` composes preparation, a real worker-style snapshot capture,
registration and serial job execution. It accepts a bundle already fetched by a
participant through Taskvalet's `lemma_fetch` tool or `valet participate
lemma_fetch` command. Save its returned `bundle` member as `lemma.json`, rather
than the entire fetch response. Transfer that validated bundle and the proposed
consumer source to the compute host. Keep the participant's journal and credential on its
own machine. The operator supplies a separate project-scoped compute-worker key,
an installed checker matching the producer, a prepared base and all resource
policy. Neither source nor bundle may select those settings. Use a distinct environment
identifier for each concurrently served derived context; reusing the original
worker's environment is refused. Scheduling selects environment/checker, so
reusing another active context's name can waste a check on the wrong worker.

Run the command as the same unprivileged OS user that will execute checks, inside
an administrator-provided service with cgroup v2 limits, a hard service lifetime
and a filesystem quota. For example, inside that service:

```sh
python -m glitnir.compute \
  --server https://tasks.example.test --project lab --environment reuse-a \
  --credential-file /etc/lab/compute-worker.json \
  handoff-worker /srv/prepared/base lemma.json Next.lean \
  --path Example/Next.lean --directory /srv/handoffs/reuse-a \
  --timeout-seconds 180 --memory-mb 4096 --max-bytes 20000000000 \
  --lifetime-seconds 1800
```

The key must have no group/other permissions (0400 or 0600); its parent must not
be writable by untrusted processes. Use the inherited `ELAN_HOME` or the
operator's original `HOME/.elan` for the preinstalled toolchain. The command
redirects its process's `HOME` and temporary directory into the private session,
so the worker's artifact/replay caches and scratch stay there too. This entry
point changes process environment and must run as its own process, never as a
model tool or in a threaded agent host. It does not install software, obtain
credentials, create system services or change the permanent worker registry.

The cgroup must have `MemoryMax <= --memory-mb`, swap disabled and finite
`TasksMax <=128`, just as for preparation. Configure a hard `RuntimeMaxSec` and
kill the whole service cgroup on stop; automatic restarts must not grant a fresh
host budget. `--lifetime-seconds` is an additional cooperative session deadline,
saved before preparation and never renewed by a retry. Network calls and Lake
preparation can take time to unwind after cancellation; the host service is the
hard wall-clock boundary. `--max-bytes` now covers the entire session's regular
files, including preparation, capture, worker journals and caches. Monitoring is
periodic, so an external filesystem quota supplies the hard instantaneous cap.
Preparation uses its own deadline/disk monitor. Capture is checked against the
session budget before and after; periodic session monitoring starts only after
preparation and metadata inspection finish, to keep their process launches single
threaded. The host quota and service lifetime cover these startup stages too.
Reserve enough space for the full trusted toolchain snapshot, even for an
Init-only project (several GiB on Linux). No kernel replay or proof-check
submission is spent on capture preflight, and its temporary store is discarded.

Before any registration, capture opens the staged source, toolchain and imported
artifacts using the same descriptors as the native checker. Every directory
ancestor must therefore be readable and traversable by the worker, including
root-owned parents (0755 is suitable where disclosure of their names is allowed).
A metadata-only `resolved` result does not establish this. The session itself
must remain private, owned by the worker (0700), with no symlink ancestors.

After capture and successful registration, the command writes private atomic
`compute-profile.json` and `ready.json` files and prints one flushed
`glitnir.handoff-worker/v1` receipt. The receipt includes the full original
handoff provenance, profile, worker ID and absolute deadline. Transfer only the
credential-free `compute-profile.json` to the consumer. It uses
`taskvalet.compute-profile/v1`, with the exact server/project and six-field
binding accepted by Taskvalet CLI `participate --compute-profile FILE` and
`Participant.connect(..., compute_profile=Path(...))`. The profile selects
execution policy; it grants no authority and requires the consumer's existing
matching run and credential. Consumer submission remains asynchronous.

The ready files are removed on normal stop or failure. After a crash they can be
stale: verify the process/service and deadline before distributing a profile;
a copied profile is not a liveness guarantee or an extra job allowance. Readiness
means environment/capture and registration succeeded, never that a proof passed.
Each consumer check still elaborates and independently kernel-replays its source
and imports. Compute results remain evidence until explicitly published and
reviewed under the existing shared-knowledge workflow.

Restart only with the identical bundle, source, base, destination, paths and
limits, using the same session directory. The session lock prevents overlapping
operator processes; `session.json` retains its original deadline. Completed
preparation is revalidated, capture runs again and a new worker registers for the
remaining time. Changed inputs or an expired/corrupt journal fail without a new
registration. The command preserves prior worker lease/completion journals under
`worker/` and never resubmits or reruns an uncertain check. Use the existing
`retry-completion` command with the original lease directory to recover a saved
completion. The supervisor/operator remains responsible for credential expiry or
revocation, assignment/job/model budgets and scoped final cleanup.
