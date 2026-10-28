"""What `ProjectReport.lean` reported about a module, validated and compared."""

from pathlib import Path
from typing import Any

from glitnir.lean.artifacts import olean_path
from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.results import Use


def valid_statement_expansion(value: Any) -> bool:
    """Accept only a bounded, explicitly descriptive statement expansion."""
    return (
        isinstance(value, dict)
        and set(value) == {"text", "kind", "truncated"}
        and isinstance(value["text"], str)
        and len(value["text"]) <= 4096
        and value["kind"] in ("definition", "constructor", "unavailable")
        and isinstance(value["truncated"], bool)
    )


def declarations(
    value: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate independent constant and theorem reports before comparison."""
    if not isinstance(value, dict) or not isinstance(value.get("imports"), list):
        raise SnapshotError("The independent module report is malformed.")
    constants: dict[str, dict[str, Any]] = {}
    theorems: dict[str, dict[str, Any]] = {}
    for field, result in (("constants", constants), ("theorems", theorems)):
        if not isinstance(value.get(field), list):
            raise SnapshotError("The independent module report is malformed.")
        for item in value[field]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or item["name"] in result
            ):
                raise SnapshotError("The independent declaration report is malformed.")
            result[item["name"]] = item
    for item in constants.values():
        if not all(
            isinstance(item.get(key), str) for key in ("kind", "meaning")
        ) or not isinstance(item.get("levels"), list):
            raise SnapshotError("The independent constant report is malformed.")
    for item in theorems.values():
        if item.get(
            "statement_expansion"
        ) is not None and not valid_statement_expansion(item["statement_expansion"]):
            raise SnapshotError("The independent statement display is malformed.")
        if not all(
            isinstance(item.get(key), str) for key in ("canonical", "type")
        ) or not isinstance(item.get("axioms"), list):
            raise SnapshotError("The independent theorem report is malformed.")
        for axiom in item["axioms"]:
            if (
                not isinstance(axiom, dict)
                or not isinstance(axiom.get("name"), str)
                or not isinstance(axiom.get("module"), (str, type(None)))
            ):
                raise SnapshotError("The independent axiom report is malformed.")
        uses = item.get("uses", [])
        if not isinstance(uses, list) or not all(
            isinstance(use, dict)
            and isinstance(use.get("name"), str)
            and isinstance(use.get("module"), str)
            for use in uses
        ):
            raise SnapshotError("The independent theorem report is malformed.")
    return constants, theorems


def project_modules(setup: dict[str, Any], project_root: Path | None) -> set[str]:
    """Return the imported modules the checked project builds itself.

    These are the project's own `.lean` files, the other notebooks a claim may
    use, as opposed to its dependencies' and the toolchain's modules: their
    compiled files lie in the project, outside `.lake/packages`.
    """
    if project_root is None:
        return set()
    packages = project_root / ".lake" / "packages"
    found = set()
    for module, paths in setup["importArts"].items():
        olean = Path(olean_path(paths))
        if not olean.is_absolute():
            olean = project_root / olean
        try:
            resolved = olean.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_relative_to(project_root) and not resolved.is_relative_to(
            packages
        ):
            found.add(module)
    return found


def claim_uses(
    reported: list[dict[str, str]],
    module: str,
    claims: set[str],
    notebooks: set[str],
) -> list[Use]:
    """Return the claims a theorem uses, from the theorems it names directly.

    Display only: what the module report says a theorem's type and value name,
    kept when it is a claim of this notebook (`claims`, in `module`) or a
    theorem of another notebook of the project (a module in `notebooks`).
    """
    found: list[Use] = []
    seen: set[tuple[str, str]] = set()
    for use in reported:
        name, owner = use["name"], use["module"]
        entry: Use
        if owner == module:
            entry = {"module": owner, "name": name, "in_notebook": True}
            if name not in claims:
                continue
        elif owner in notebooks and not name.startswith("_private."):
            entry = {"module": owner, "name": name, "in_notebook": False}
        else:
            continue
        if (owner, name) not in seen:
            seen.add((owner, name))
            found.append(entry)
    return found


def compare_reports(
    reference: Any, candidate: Any, names: list[str]
) -> list[dict[str, Any]]:
    """Bind exact theorem names/propositions and every reference non-proof constant."""
    expected, expected_theorems = declarations(reference)
    found, found_theorems = declarations(candidate)
    if (
        reference.get("module") != candidate.get("module")
        or reference["imports"] != candidate["imports"]
    ):
        raise SnapshotError("Candidate and reference imports/module differ.")
    for name, constant in expected.items():
        other = found.get(name)
        if (
            other is None
            or constant["kind"] != other["kind"]
            or constant["levels"] != other["levels"]
        ):
            raise SnapshotError(f"The candidate changed reference declaration {name}.")
        if constant["kind"] != "theorem" and constant["meaning"] != other["meaning"]:
            raise SnapshotError(f"The candidate changed reference definition {name}.")
    for name, theorem in expected_theorems.items():
        if (
            name not in found_theorems
            or theorem["canonical"] != found_theorems[name]["canonical"]
        ):
            raise SnapshotError(
                f"The candidate changed the reference proposition {name}."
            )
    if len(names) != len(set(names)) or any(
        name not in expected_theorems for name in names
    ):
        raise SnapshotError(
            "Source declaration identities do not match the reference module."
        )
    return [found_theorems[name] for name in names]


def import_closure(report: Any, module: str) -> list[list[str]] | None:
    """Return the reporter's loaded closure without ``module``, which comes last."""
    modules = report.get("modules") if isinstance(report, dict) else None
    if (
        not isinstance(modules, list)
        or not modules
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in modules
        )
        or modules[-1][0] != module
    ):
        return None
    return modules[:-1]
