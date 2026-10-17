"""Check one candidate unit the way a replay does, for the live attack tests.

The live tests in ``test_lean_independent.py`` and ``test_lean_sandbox_attacks.py``
send a hostile script through the elaborating session and then the
out-of-process check, against the release ``FORSETI_LEAN_PROJECT`` names.
What the elaborating process prints is only a first filter: a unit is
accepted only when `glitnir_forseti.lean.independent` confirms it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from glitnir.lean.environment import LEAN_AXIOM_ALLOWLIST

from glitnir_forseti.lean import SessionLimits, SubprocessLeanSession, render_unit
from glitnir_forseti.lean.independent import IndependentStatus, check_independently

#: Environment variable naming a built standalone Lean release checkout.
PROJECT_VARIABLE = "FORSETI_LEAN_PROJECT"
#: The prelude a unit is checked under unless a test names another.
DEFAULT_PRELUDE = "import Gimle.Forseti"


def _project() -> Path | None:
    configured = os.environ.get(PROJECT_VARIABLE)
    return Path(configured).resolve() if configured else None


def check_unit(
    statement: str,
    script: tuple[str, ...] | list[str],
    prelude: str = DEFAULT_PRELUDE,
    timeout_ms: int = 300_000,
) -> dict[str, Any]:
    """Elaborate one declaration, then confirm it out of process.

    Returns:
        A mapping with `accepted`, `status`, `errors`, `axioms`,
        `forbidden_axioms` and `independent`, or `accepted` and `error` when
        the unit cannot be rendered or elaborated. `accepted` is true only
        when Lean raised no error and admitted no `sorry`, and the
        out-of-process check confirmed the unit with no axiom outside the
        allowlist.
    """
    tactics = tuple(script)
    try:
        unit = render_unit(prelude, statement, tactics)
    except (TypeError, ValueError) as failure:
        return {"accepted": False, "error": f"cannot render the unit: {failure}"}
    limits = SessionLimits(timeout_ms=timeout_ms)
    project = _project()
    try:
        elaboration = SubprocessLeanSession(project).elaborate(unit, limits)
    except Exception as failure:  # the transport is untrusted search machinery
        return {"accepted": False, "error": f"elaboration failed: {failure}"}
    permitted = set(LEAN_AXIOM_ALLOWLIST)
    axioms = list(elaboration.axioms)
    forbidden = [a for a in axioms if a not in permitted]
    accepted = elaboration.succeeded() and not forbidden
    independent: dict[str, Any] = {"status": "not_run", "reason": ""}
    if accepted:
        check = check_independently(prelude, statement, tactics, limits, project)
        independent = {"status": check.status.value, "reason": check.reason}
        if check.status is IndependentStatus.CONFIRMED:
            axioms = list(check.axioms)
            forbidden = [a for a in axioms if a not in permitted]
            accepted = not forbidden
        else:
            accepted = False
    return {
        "accepted": accepted,
        "status": elaboration.status.value,
        "errors": [message.text for message in elaboration.stream.errors()],
        "axioms": axioms,
        "forbidden_axioms": forbidden,
        "independent": independent,
    }
