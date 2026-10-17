# Security reporting

Report suspected vulnerabilities privately through
[GitHub vulnerability reporting](https://github.com/arnovich/glitnir/security/advisories/new).
Do not put exploit details, credentials, private notebooks or private dependency
source in public issues or pull requests.

Include the Glitnir release version, operating system, a minimal reproduction and
the expected versus observed behavior. We review reports on a best-effort basis;
there is no guaranteed response time or bug bounty.

Use the newest published release for fixes. Older previews and unpublished draft
candidates do not receive a separate maintenance commitment.

Glitnir requires trusted local workspaces. Lean checking also requires the
platform sandbox; do not disable confinement to work around a setup failure.
Saved check results, simulation output and source preview are not independent
proof verification. See the installation guide for the supported environment.

The independent kernel replay always replays a notebook's own declarations. It
skips replaying the imported declarations only when the per-user cache directory
(`~/Library/Caches/glitnir/replay` on macOS, `$XDG_CACHE_HOME/glitnir/replay` or
`~/.cache/glitnir/replay` elsewhere) records a successful full replay with the same
toolchain files, replay program and digests of every imported module file. A
larger closure may build on such a record when every module it lists has the same
file digests: its other modules' declarations are then replayed in the kernel on
top of the recorded ones, and the result is recorded only if that succeeds, so a
record always stands on a chain of kernel replays back to one from an empty
environment. Only
the checker reads or writes that directory, outside the sandbox. A local process
that can write it can forge a cached success; that is the same trust boundary as
being able to modify your files, the toolchain or Glitnir itself. Delete the
directory to force full replays.

Each check links its snapshot to the files it hashed in the per-user artifact
store beside that directory (`glitnir/artifacts`): private files named by their
SHA-256, written once, verified against their name, and never opened for
writing again. Before a check uses one it requires a read-only regular file of
the user with the size it hashed. Like the replay cache, a local process that
can write the store can change what a later check reads; that is the same trust
boundary. It includes Lean runs started with `GLITNIR_UNSANDBOXED=1` and any
plugin or tool that runs Lean outside the sandbox: they run as you and can
write the store, so a file they plant is trusted by later checks.

The store keeps toolchain files in a namespace every check shares and each
project's compiled dependencies in a namespace of its own, keyed by the
project directory's path, device and inode. The sandbox of a check can read,
read-only, its own snapshot, the toolchain namespace and its own project's
namespace, which includes files from that project's earlier checks; it cannot
read other projects' files. A project created later at the same path gets
another namespace, unless the file system reuses the old directory's inode
number, in which case it can read the files its predecessor stored.
Project sources and configuration files are hashed for a check's identity and
never stored.

A theorem's status (`checked`, `admitted` or `assumptions`) is decided only by
the axioms that same replay collects in the environment it kernel-checked, which
holds no compiled code or precomputed axiom summaries from the project or its
dependencies. The module report's own axiom list must be identical, or the check
fails.
