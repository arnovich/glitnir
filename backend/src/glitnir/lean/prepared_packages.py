"""Validate historical prepared-package resolution without asserting Git provenance."""

import hashlib
from pathlib import Path
from typing import Any, Literal, TypedDict

from glitnir.lean.project import read_regular

PROGRAM = Path(__file__).with_name("PreparedProject.lean")


class ResolutionInput(TypedDict):
    """One configuration file the prepared packages were resolved from."""

    path: str
    sha256: str | None


class ResolutionPackage(TypedDict):
    """One prepared package: name, directory in the project and manifest entry."""

    name: str
    path: str
    entry: dict[str, Any]


class Resolution(TypedDict):
    """How a project's packages were resolved: prepared locally, from these inputs."""

    kind: Literal["prepared_local"]
    packages: list[ResolutionPackage]
    inputs: list[ResolutionInput]
    adapter_sha256: str


def validate_resolution(value: Any, root: Path) -> Resolution:
    """Bind the helper's captured bytes and optional absences to confined paths."""
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "packages", "inputs"}
        or value["kind"] != "prepared_local"
        or not isinstance(value["packages"], list)
        or not isinstance(value["inputs"], list)
    ):
        raise ValueError("Invalid prepared package resolution report.")
    root = root.resolve()

    def confined(name: Any) -> Path:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("Invalid prepared package input path.")
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError("Prepared package input is outside the project.")
        if path.is_symlink():
            raise ValueError("Prepared package input is a symbolic link.")
        return path.resolve()

    inputs: list[ResolutionInput] = []
    seen = set()
    for item in value["inputs"]:
        if not isinstance(item, dict) or set(item) != {"path", "contents"}:
            raise ValueError("Invalid prepared package input binding.")
        path = confined(item["path"])
        name = str(path.relative_to(root))
        if name in seen:
            raise ValueError("Duplicate prepared package input binding.")
        seen.add(name)
        contents = item["contents"]
        if contents is not None and not isinstance(contents, str):
            raise ValueError("Invalid prepared package input contents.")
        expected = contents.encode("utf-8") if contents is not None else None
        try:
            actual = read_regular(path)
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise ValueError("Prepared package inputs changed; retry inspection.")
        inputs.append(
            {
                "path": name,
                "sha256": (
                    hashlib.sha256(actual).hexdigest() if actual is not None else None
                ),
            }
        )
    packages: list[ResolutionPackage] = []
    names = set()
    for item in value["packages"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "path", "entry"}
            or not isinstance(item["name"], str)
            or not item["name"]
            or item["name"] in names
            or not isinstance(item["entry"], dict)
        ):
            raise ValueError("Invalid prepared package mapping.")
        path = confined(item["path"])
        if not path.is_dir():
            raise ValueError("Prepared package directory is missing.")
        names.add(item["name"])
        packages.append(
            {
                "name": item["name"],
                "path": str(path.relative_to(root)),
                "entry": item["entry"],
            }
        )
    return {
        "kind": "prepared_local",
        "packages": packages,
        "inputs": inputs,
        "adapter_sha256": hashlib.sha256(PROGRAM.read_bytes()).hexdigest(),
    }
