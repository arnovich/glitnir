# Verified worker process exit (task410)

An operator may enable `worker --cgroup-root PATH` inside a delegated Linux
cgroup-v2 service. PATH must contain the current worker process. Each leased job
gets a fresh child cgroup; the trusted build supervisor joins before it starts any
command. Existing sandbox restrictions remain necessary for untrusted Lean input.

The worker durably saves the original server, project, worker, lease, source and
host/boot intent before dispatch. Cleanup kills the entire job subtree and reads
`cgroup.events` until `populated 0`. Only then does it seal `worker-exit.json` and
POST the original lease and receipt to Taskvalet's `/jobs/{id}/stopped` endpoint.
Deploy that endpoint before enabling exit reporting. This is an authenticated
worker attestation; the server does not inspect the worker's kernel itself.

Use `retry-exit JOURNAL` to retry a sealed receipt after a lost response. It does
not rerun computation. `recover-exit JOURNAL` can construct a missing receipt only
after a kernel boot change on the same protected machine identity. It refuses
same-boot intent recovery because an empty group does not fence a live launcher.
Recovery retains the original credential, worker, lease and destination; it never
claims a new job. A cancelled or interrupted job is never promoted to success.

Workers without this option retain existing behavior and cannot satisfy a host's
verified native-exit gate. Cgroup checks and reboot recovery do not themselves
qualify the complete notebook participant host or enable direct IDE launch.
