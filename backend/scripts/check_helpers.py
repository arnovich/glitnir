"""Elaborate every Lean helper program on the supported toolchain; fail on output.

Glitnir runs its helper programs with `lean --run` and reads their JSON from
standard output, so any message Lean prints, a warning included, is a failure,
like an error. Each program is composed exactly as a check composes it (the
report program after the axiom encoders' prefix) from the installed `glitnir`
package, and elaborated with the toolchain `project_inspection.TOOLCHAIN` names,
located without elan and run without any search-path override.

Usage: python backend/scripts/check_helpers.py
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from glitnir.lean import prepared_packages, project_checking, proof_trace, provider_lean
from glitnir.lean.axiom_report import REPORT_PROGRAM
from glitnir.lean.project_inspection import TOOLCHAIN, _toolchain

#: Seconds one program may take to elaborate (it imports Lean or Lake).
TIMEOUT_SECONDS = 900


def compose(destination: Path) -> dict[str, Path]:
    """Write every helper program into `destination` as Glitnir runs it."""
    reader, reporter, replay = project_checking._programs(destination / "check")
    programs = {
        "ProjectRead": reader,
        "ProjectReport": reporter,
        "ProjectReplay": replay,
    }
    for name, source in (
        ("AxiomReport", REPORT_PROGRAM),
        ("ProofTrace", proof_trace.PROGRAM),
        ("ViewElab", provider_lean.PROGRAM),
        ("PreparedProject", prepared_packages.PROGRAM),
    ):
        target = destination / source.name
        target.write_bytes(source.read_bytes())
        programs[name] = target
    return programs


def elaborate(program: Path, toolchain: Path, *, lake: bool) -> tuple[int, str]:
    """Elaborate `program`; return Lean's exit status and everything it printed."""
    plugin = "libLake_shared.dylib" if sys.platform == "darwin" else "libLake_shared.so"
    command = [str(toolchain / "bin/lean"), "-M4096"]
    if lake:
        command += ["--plugin", str(toolchain / "lib/lean" / plugin)]
    completed = subprocess.run(
        [*command, program.name],
        cwd=program.parent,
        env={
            "PATH": f"{toolchain / 'bin'}:/usr/bin:/bin",
            "LEAN_SYSROOT": str(toolchain),
            "LEAN_PATH": "",
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    return completed.returncode, completed.stdout + completed.stderr


def main() -> int:
    """Elaborate each helper and report it; return 1 if any printed or failed."""
    try:
        toolchain = _toolchain()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Elaborating Glitnir's helper programs with {TOOLCHAIN} ({toolchain})")
    failed = []
    with tempfile.TemporaryDirectory(prefix="glitnir-helpers-") as directory:
        programs = compose(Path(directory).resolve())
        for name, program in programs.items():
            started = time.monotonic()
            status, output = elaborate(
                program, toolchain, lake=name == "PreparedProject"
            )
            seconds = time.monotonic() - started
            if status == 0 and not output:
                print(f"ok      {name} ({seconds:.0f} s)")
                continue
            failed.append(name)
            print(f"FAILED  {name} (exit {status}, {seconds:.0f} s)")
            print(output.rstrip())
    if failed:
        print(f"{len(failed)} helper(s) printed output or failed: {', '.join(failed)}")
        return 1
    print(f"All {len(programs)} helpers elaborate with no output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
