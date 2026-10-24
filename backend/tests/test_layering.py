"""Each backend package imports only from its own layer or a lower one."""

import ast
import textwrap
from pathlib import Path

import glitnir

LAYERS = ("providers", "lean", "compute", "notebook", "preview", "service")
ROOT = Path(glitnir.__file__).parent
#: Service code still in `notebook` until it moves to `service`; each may
#: import `preview`. Delete this set when `glitnir.service` exists.
SERVICE_UNTIL_MOVED = {
    "glitnir.notebook.server",
    "glitnir.notebook.source_preview",
    "glitnir.notebook.static_export",
}


def _layer(module: str) -> int:
    """Return the layer of a `glitnir.*` module; the bare package is below all."""
    if module in SERVICE_UNTIL_MOVED:
        return LAYERS.index("service")
    parts = module.split(".")
    return LAYERS.index(parts[1]) if len(parts) > 1 and parts[1] in LAYERS else -1


def _resolve(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """Return the absolute dotted name a `from ... import` node refers to.

    `module` is the importing module's dotted name; for an `__init__.py` the
    package is the module itself, otherwise its parent.
    """
    if node.level == 0:
        return node.module or ""
    package = module if is_package else module.rpartition(".")[0]
    base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _module_level_imports(path: Path, module: str) -> list[str]:
    """Return the absolute `glitnir.*` modules imported at module level."""
    tree = ast.parse(path.read_text())
    is_package = path.name == "__init__.py"
    found = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            target = _resolve(node, module, is_package)
            if target.startswith("glitnir"):
                found.append(target)
        if isinstance(node, ast.Import):
            found += [
                alias.name for alias in node.names if alias.name.startswith("glitnir")
            ]
    return found


def test_modules_import_only_downward() -> None:
    """`__main__` modules are entry points and may import anything.

    `lean/fingerprint.py` imports `glitnir.compute` inside a function to record
    the remote execution identity; that is the one known upward edge, and it is
    not module level, so this test does not see it.
    """
    upward = []
    for path in ROOT.rglob("*.py"):
        if path.name == "__main__.py":
            continue
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(["glitnir", *parts])
        for imported in _module_level_imports(path, module):
            if _layer(imported) > _layer(module):
                upward.append(f"{module} -> {imported}")
    assert upward == [], "\n".join(upward)


def test_relative_imports_are_resolved(tmp_path: Path) -> None:
    """Relative imports resolve to absolute `glitnir.*` names."""
    source = textwrap.dedent("""\
        from ..service import x
        from .environment import y
        """)
    path = tmp_path / "sample.py"
    path.write_text(source)
    assert _module_level_imports(path, "glitnir.lean.foo") == [
        "glitnir.service",
        "glitnir.lean.environment",
    ]
    assert _module_level_imports(path, "glitnir.lean.limits") == [
        "glitnir.service",
        "glitnir.lean.environment",
    ]
