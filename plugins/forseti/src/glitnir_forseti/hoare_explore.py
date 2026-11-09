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
_EQUIVALENCE = re.compile(
    rf":\s*(?:Gimle\.Forseti\.)?GlobalCircuitEquivalence\s+({_NAME})\s+({_NAME})"
)
_APPLICATION = re.compile(
    rf"exact\s+(?:Gimle\.Forseti\.)?"
    rf"(exactHoareSequential|exactCircuitSubstitution)\s+({_NAME})\s+({_NAME})"
)


def hoare_proofs(sources: Mapping[str, str]) -> dict[str, HoareProof]:
    """Describe exact triples and two supported rules from saved source."""
    proofs: dict[str, HoareProof] = {}
    steps: dict[str, tuple[str, str, str]] = {}
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
            equivalence = _EQUIVALENCE.fullmatch(declaration.statement.strip())
            if equivalence:
                proofs[name] = {
                    "equivalence": (equivalence[1], equivalence[2]),
                    "rule": None,
                    "premises": [],
                }
            continue
        pre, command, post = match.groups()
        proofs[name] = {
            "pre": pre,
            "command": command,
            "post": post,
            "rule": None,
            "premises": [],
        }
        step = _APPLICATION.fullmatch(declaration.proof.strip())
        if step:
            steps[name] = (step[1], step[2], step[3])

    for name, (rule, left, right) in steps.items():
        scope, _, _ = name.rpartition(".")
        local_left = f"{scope}.{left}" if scope else left
        local_right = f"{scope}.{right}" if scope else right
        left = local_left if local_left in proofs else left
        right = local_right if local_right in proofs else right
        if left not in proofs or right not in proofs or name in (left, right):
            continue
        first, second, whole = proofs[left], proofs[right], proofs[name]
        if "equivalence" in first or first["pre"] != whole["pre"]:
            continue
        if rule == "exactHoareSequential":
            if "equivalence" in second or first["post"] != second["pre"]:
                continue
            if second["post"] != whole["post"]:
                continue
            label = "Sequential composition"
        else:
            if second.get("equivalence") != (first["command"], whole["command"]):
                continue
            if first["post"] != whole["post"]:
                continue
            label = "Circuit substitution"
        whole["rule"] = label
        whole["premises"] = [left, right]
    return proofs
