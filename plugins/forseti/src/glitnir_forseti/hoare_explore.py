"""Recognize a small, explicit source form of Forseti's exact Hoare rules."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from glitnir.notebook.document import NotebookError, parse_notebook

if TYPE_CHECKING:
    from glitnir.providers import HoareProof

_NAME = r"(?:[^\W\d]|_)[\w']*(?:\.(?:[^\W\d]|_)[\w']*)*"
_TRIPLE = re.compile(
    rf":\s*(?:Gimle\.Forseti\.)?ExactHoare\s+({_NAME})\s+({_NAME})\s+({_NAME})"
)
_SEQUENCE = re.compile(
    rf"exact\s+(?:Gimle\.Forseti\.)?exactHoareSequential\s+({_NAME})\s+({_NAME})"
)


def hoare_proofs(sources: Mapping[str, str]) -> dict[str, HoareProof]:
    """Describe named exact triples and recognizable sequential applications."""
    proofs: dict[str, HoareProof] = {}
    steps: dict[str, tuple[str, str]] = {}
    for name, source in sources.items():
        try:
            parsed = parse_notebook(source)
        except NotebookError:
            continue
        declarations = [cell.claim for cell in parsed.cells if cell.claim]
        if len(declarations) != 1:
            continue
        declaration = declarations[0]
        if declaration.name.rsplit(".", 1)[-1] != name.rsplit(".", 1)[-1]:
            continue
        match = _TRIPLE.fullmatch(declaration.statement.strip())
        if not match:
            continue
        pre, command, post = match.groups()
        proofs[name] = {
            "pre": pre,
            "command": command,
            "post": post,
            "rule": None,
            "premises": [],
        }
        step = _SEQUENCE.fullmatch(declaration.proof.strip())
        if step:
            steps[name] = (step[1], step[2])

    for name, (left, right) in steps.items():
        scope, _, _ = name.rpartition(".")
        local_left = f"{scope}.{left}" if scope else left
        local_right = f"{scope}.{right}" if scope else right
        left = local_left if local_left in proofs else left
        right = local_right if local_right in proofs else right
        if left not in proofs or right not in proofs or name in (left, right):
            continue
        first, second, whole = proofs[left], proofs[right], proofs[name]
        if (
            first["pre"] == whole["pre"]
            and first["post"] == second["pre"]
            and second["post"] == whole["post"]
        ):
            whole["rule"] = "Sequential composition"
            whole["premises"] = [left, right]
    return proofs
