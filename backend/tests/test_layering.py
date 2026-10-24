"""Each backend package imports only from its own layer or a lower one."""

import ast
from pathlib import Path

import glitnir

LAYERS = ("providers", "lean", "compute", "notebook", "preview", "service")
ROOT = Path(glitnir.__file__).parent


def _layer(module: str) -> int:
    """Return the layer of a `glitnir.*` module; the bare package is below all."""
    parts = module.split(".")
    return LAYERS.index(parts[1]) if len(parts) > 1 and parts[1] in LAYERS else -1


def _module_level_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = []
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("glitnir")
        ):
            found.append(node.module)
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
        module = "glitnir." + ".".join(path.relative_to(ROOT).with_suffix("").parts)
        module = module.removesuffix(".__init__")
        for imported in _module_level_imports(path):
            if _layer(imported) > _layer(module):
                upward.append(f"{module} -> {imported}")
    assert upward == [], "\n".join(upward)
