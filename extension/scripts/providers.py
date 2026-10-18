"""Report installed Glitnir provider packages without starting a notebook server.

``packages`` lists distributions that declare ``glitnir.providers`` entry points
without importing them, with the extras whose requirements are all installed
(installers record no extras, so this is how a backend upgrade keeps them), and
the files in the environment's scripts directory with the distributions whose
RECORD lists each one. ``providers`` also loads
them as a preview would and reports the view kinds, evidence verbs and
decision kinds they serve, with any load errors, and why an evidence verb or
decision kind cannot run yet (its provider's ``unavailable()``).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import sysconfig
from importlib import metadata
from typing import Any

GROUP = "glitnir.providers"
MODES = ("packages", "providers")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_EXTRA_MARKER = re.compile(
    r"""\bextra\s*==\s*["']([^"']+)["']|["']([^"']+)["']\s*==\s*extra\b"""
)


def _installed(requirement: str) -> bool:
    """Return whether the distribution a requirement names is installed."""
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if not match:
        return False
    try:
        metadata.distribution(match.group(0))
    except metadata.PackageNotFoundError:
        return False
    return True


def _extras(dist: metadata.Distribution) -> list[str]:
    """Return the declared extras whose requirements are all installed.

    An extra with no requirements adds nothing and is never reported.
    """
    declared = dist.metadata.get_all("Provides-Extra") or []
    needs: dict[str, list[str]] = {_normalized(extra): [] for extra in declared}
    for line in dist.requires or []:
        requirement, _, marker = line.partition(";")
        match = _EXTRA_MARKER.search(marker)
        if match:
            extra = _normalized(match.group(1) or match.group(2))
            if extra in needs:
                needs[extra].append(requirement.strip())
    return sorted(
        extra
        for extra, requirements in needs.items()
        if requirements and all(_installed(r) for r in requirements)
    )


def _packages() -> list[dict[str, Any]]:
    """Return provider distributions other than the Glitnir core, by name."""
    found: dict[str, dict[str, Any]] = {}
    for entry in metadata.entry_points(group=GROUP):
        dist = entry.dist
        if dist is None:
            continue
        name = dist.metadata["Name"] or ""
        if not name or _normalized(name) == "glitnir":
            continue
        item = found.setdefault(
            _normalized(name),
            {
                "name": name,
                "version": dist.version,
                "entryPoints": [],
                "extras": _extras(dist),
            },
        )
        if entry.name not in item["entryPoints"]:
            item["entryPoints"].append(entry.name)
    return [found[key] for key in sorted(found)]


def _commands() -> dict[str, list[str]]:
    """Return each file in the scripts directory with its owners' names.

    An owner is a distribution whose RECORD lists the file; installers write
    console and GUI scripts there.
    """
    directory = sysconfig.get_path("scripts")
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return {}
    owners: dict[str, list[str]] = {name: [] for name in names}
    real = os.path.realpath(directory)
    for dist in metadata.distributions():
        name = _normalized(dist.metadata["Name"] or "")
        try:
            files = dist.files or []
        except Exception:  # an unreadable RECORD owns nothing
            continue
        for file in files:
            if file.name not in owners or name in owners[file.name]:
                continue
            located = str(dist.locate_file(file))
            if name and os.path.realpath(os.path.dirname(located)) == real:
                owners[file.name].append(name)
    return owners


def _core() -> str | None:
    try:
        return metadata.version("glitnir")
    except metadata.PackageNotFoundError:
        return None


def _unavailable(table: dict[str, Any]) -> dict[str, str]:
    """Return why each word's provider cannot run yet, for words that cannot."""
    reasons: dict[str, str] = {}
    for word in sorted(table):
        try:
            with contextlib.redirect_stdout(sys.stderr):
                reason = table[word].unavailable()
        except Exception as error:  # a provider must never break the report
            reason = f"{type(error).__name__}: {error}"
        if reason:
            reasons[word] = str(reason)
    return reasons


def main() -> None:
    """Print one JSON report, or an error and exit status 1."""
    try:
        if sys.version_info < (3, 12):
            raise ValueError("Glitnir requires Python 3.12 or newer")
        if len(sys.argv) != 2 or sys.argv[1] not in MODES:
            raise ValueError("Usage: providers.py packages|providers")
        result: dict[str, Any] = {"core": _core(), "packages": _packages()}
        if sys.argv[1] == "packages":
            result["commands"] = _commands()
        if sys.argv[1] == "providers":
            from glitnir.providers import load_providers

            # Provider code must not corrupt the report on standard output.
            with contextlib.redirect_stdout(sys.stderr):
                loaded = load_providers()
            for key, table in (
                ("views", loaded.views),
                ("evidence", loaded.evidence),
                ("decisions", loaded.decisions),
            ):
                result[key] = {word: table[word].name for word in sorted(table)}
            result["errors"] = dict(sorted(loaded.errors.items()))
            result["unavailable"] = {
                "evidence": _unavailable(loaded.evidence),
                "decisions": _unavailable(loaded.decisions),
            }
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
