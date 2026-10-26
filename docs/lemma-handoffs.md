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
