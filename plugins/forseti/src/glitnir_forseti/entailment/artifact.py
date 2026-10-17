"""The versioned entailment artifact: everything a fresh replay needs.

``gimle.forseti.entailment-artifact/v2`` retains the complete goal, the exact
evidence, the identity of the theorem that was checked, the Lean source that
was checked (as a record; replay regenerates it and compares), the environment
it was checked in and the effective limits. Everything in it is a claim:
`replay.replay_artifact` believes none of it, and takes the expected goal and the
trusted environment from its caller.
"""

from __future__ import annotations

import json
from typing import Any

from glitnir_forseti.lean.closure import closure_identity
from glitnir_forseti.lean_release import (
    IMPORT_CLOSURE,
    LOCKFILE_SHA256,
    RELEASE_SHA256,
    SUPPORT_VERSION,
    TOOLCHAIN,
)

from .algebraic import AlgebraicWitness, encode_algebraic
from .evidence import (
    Certificate,
    PsatzCertificate,
    Witness,
    encode_certificate,
    encode_psatz,
    encode_witness,
)
from .limits import EntailmentLimits
from .render import IMPORTS, Application, imports_for
from .syntax import Goal, encode_goal

#: v2: the environment names its import set and that set's closure digest.
ARTIFACT_SCHEMA = "gimle.forseti.entailment-artifact/v2"
THEOREM = "forsetiSearch"
ARTIFACT_KEYS = frozenset(
    {
        "schema",
        "status",
        "goal",
        "evidence",
        "theorem",
        "source",
        "environment",
        "limits",
        "producer",
    }
)


def environment_identity(imports: tuple[str, ...] = IMPORTS) -> dict[str, Any]:
    """Describe the pinned Lean environment for an import set.

    It is the environment an application importing ``imports`` is checked in
    on this installation.
    """
    return {
        "toolchain": TOOLCHAIN,
        "imports": list(imports),
        "support_version": SUPPORT_VERSION,
        "release_sha256": RELEASE_SHA256,
        "lockfile_sha256": LOCKFILE_SHA256,
        "closure_sha256": closure_identity(imports),
        "dependencies": [
            {"name": name, "revision": revision} for name, revision in IMPORT_CLOSURE
        ],
    }


def build_artifact(
    goal: Goal,
    evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness,
    application: Application,
    limits: EntailmentLimits,
    producer: dict[str, Any],
) -> bytes:
    """Encode an artifact for evidence that is about to be replayed."""
    proven = isinstance(evidence, (Certificate, PsatzCertificate))
    encoded: dict[str, Any]
    if isinstance(evidence, Certificate):
        encoded = encode_certificate(evidence)
    elif isinstance(evidence, PsatzCertificate):
        encoded = encode_psatz(evidence)
    elif isinstance(evidence, AlgebraicWitness):
        encoded = encode_algebraic(evidence)
    else:
        encoded = encode_witness(evidence)
    document = {
        "schema": ARTIFACT_SCHEMA,
        "status": "PROVEN" if proven else "REFUTED",
        "goal": encode_goal(goal),
        "evidence": encoded,
        "theorem": {
            "name": THEOREM,
            "statement": "entailment" if proven else "negation",
        },
        "source": application.source,
        "environment": environment_identity(imports_for(evidence)),
        "limits": limits.as_dict(),
        "producer": producer,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
