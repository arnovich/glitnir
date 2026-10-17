"""The core backend imports nothing from an installed extension package."""

import subprocess
import sys


def test_core_modules_import_without_the_forseti_extension() -> None:
    """A fresh interpreter that refuses ``glitnir_forseti`` imports every module.

    A subprocess keeps the blocked package out of this session's module cache.
    """
    program = "\n".join(
        [
            "import importlib, pkgutil, sys",
            "sys.modules['glitnir_forseti'] = None",
            "import glitnir",
            "found = pkgutil.walk_packages(glitnir.__path__, 'glitnir.')",
            "names = [module.name for module in found]",
            "assert 'glitnir.lean.project_checking' in names, names",
            "for name in names:",
            "    if not name.endswith('__main__'):",
            "        importlib.import_module(name)",
            "assert sys.modules['glitnir_forseti'] is None",
            "assert not [n for n in sys.modules if n.startswith('glitnir_forseti.')]",
        ]
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
