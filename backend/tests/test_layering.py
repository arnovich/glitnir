"""Each backend package imports only from its own layer or a lower one."""

import ast
import textwrap
from collections.abc import Iterator
from pathlib import Path

import glitnir

LAYERS = ("providers", "lean", "compute", "notebook", "preview", "service")
ROOT = Path(glitnir.__file__).parent


def _layer(module: str) -> int:
    """Return the layer of a `glitnir.*` module; the bare package is below all."""
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


def _module_level_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield the statements that run at import time.

    Descends into `if`, `try`, `with` and loop blocks but not into function or
    class bodies, which run later.
    """
    for node in body:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from _module_level_statements(getattr(node, field, []))
        for handler in getattr(node, "handlers", []):
            yield from _module_level_statements(handler.body)


def _module_level_imports(path: Path, module: str) -> list[str]:
    """Return the absolute `glitnir.*` modules imported at module level.

    For `from package import name`, `name` may itself be a submodule, so
    `package.name` is reported as well.
    """
    tree = ast.parse(path.read_text())
    is_package = path.name == "__init__.py"
    found = []
    for node in _module_level_statements(tree.body):
        if isinstance(node, ast.ImportFrom):
            target = _resolve(node, module, is_package)
            if target.startswith("glitnir"):
                found.append(target)
                found += [f"{target}.{alias.name}" for alias in node.names]
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


def _imports_of(tmp_path: Path, source: str, module: str) -> list[str]:
    path = tmp_path / "sample.py"
    path.write_text(textwrap.dedent(source))
    return _module_level_imports(path, module)


def test_relative_imports_are_resolved(tmp_path: Path) -> None:
    """Relative imports resolve to absolute `glitnir.*` names."""
    source = """\
        from ..service import x
        from .environment import y
        """
    expected = [
        "glitnir.service",
        "glitnir.service.x",
        "glitnir.lean.environment",
        "glitnir.lean.environment.y",
    ]
    assert _imports_of(tmp_path, source, "glitnir.lean.foo") == expected
    assert _imports_of(tmp_path, source, "glitnir.lean.limits") == expected


def test_imported_names_may_be_submodules(tmp_path: Path) -> None:
    """`from glitnir import service` and `from .. import service` reach a layer."""
    for source, module in (
        ("from glitnir import service\n", "glitnir.lean.foo"),
        ("from .. import service\n", "glitnir.lean.foo"),
        ("from . import service\n", "glitnir.lean"),
    ):
        imported = _imports_of(tmp_path, source, module)
        assert "glitnir.service" in imported, source
        assert _layer("glitnir.service") > _layer(module)


def test_nested_module_level_blocks_are_walked(tmp_path: Path) -> None:
    """Imports under `if`, `try` and `TYPE_CHECKING` count; function bodies do not."""
    source = """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from glitnir.service import a
        try:
            import glitnir.preview as b
        except ImportError:
            from glitnir.compute import c
        else:
            from glitnir.notebook import d
        finally:
            from glitnir.lean import e
        with open("f"):
            from glitnir.providers import f
        def later():
            from glitnir.service import hidden
        class K:
            from glitnir.service import hidden_too
        """
    imported = _imports_of(tmp_path, source, "glitnir.lean.foo")
    for name in (
        "glitnir.service",
        "glitnir.preview",
        "glitnir.compute",
        "glitnir.notebook",
        "glitnir.lean",
        "glitnir.providers",
    ):
        assert name in imported, name
    assert "glitnir.service.hidden" not in imported
    assert "glitnir.service.hidden_too" not in imported
