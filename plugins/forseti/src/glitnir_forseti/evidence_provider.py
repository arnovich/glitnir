"""Run `-- @evidence simulate|iterate|sample <target>` directives for Glitnir.

Glitnir's core elaborates the notebook up to the directive in its sandbox,
followed by the Lean `command` returns, and passes the JSON printed after the
marker to `compute`. That JSON is untrusted: `compute` reads it under the
bounds of `glitnir_forseti.notebook.evidence` and returns a
``glitnir.evidence/v1`` snapshot, which the core checks again before saving.

``simulate`` needs nothing beyond this package: the field arrives as exact
expression trees and rational pairs, read here. ``iterate`` decodes an update
circuit with Python Asgard's wire decoder (``gimle-asgard``), so it is served by
a provider of its own whose `unavailable` names that missing dependency: its
cards say so and cannot run, and the provider report lists ``iterate`` as
unavailable. ``sample`` needs nothing beyond this package either, and is served
with ``simulate``: it reads a registered stepped model's field the way
``simulate`` does and draws seeded shock paths through it
(`glitnir_forseti.stepped.sampler`). Its card shows a few paths and the running
breach fraction, with the final fraction and its sampling interval in the
provenance; it never shows a proved bound. Evidence is never a proof and
changes no claim's status.
"""

import hashlib
import importlib.util
import json
import math
from datetime import UTC, datetime
from fractions import Fraction
from importlib import metadata
from pathlib import Path
from typing import Any

from glitnir.notebook.evidence_cards import MAX_SERIES, MAX_TEXT
from glitnir.providers import EvidenceProvider, EvidenceRequest

from glitnir_forseti.notebook import evidence
from glitnir_forseti.notebook.evidence import EvidenceError, SamplePlan, Simulation
from glitnir_forseti.stepped import sampler

IMPORTS = ("Gimle.Asgard.Compile.RationalInterchange", "Gimle.Asgard.Model.Continuous")
#: Without the optional decoder, `iterate` cannot run.
NEEDS_ASGARD = (
    "`iterate` needs Python Asgard (gimle-asgard), which is not installed in the "
    "backend's environment; install it there from its wheel or checkout."
)
#: The module `iterate` decodes circuits with.
DECODER = "rational_interchange"
FORMAT = "glitnir.evidence/v1"


def asgard_installed() -> bool:
    """Whether Python Asgard and its circuit decoder can be imported.

    Neither is imported: `gimle.asgard` is only located, and its decoder looked
    for among its files, so this stays cheap enough for every preview refresh.
    """
    try:
        spec = importlib.util.find_spec("gimle.asgard")
    except (ModuleNotFoundError, ValueError):  # no `gimle` parent, or no spec
        return False
    if spec is None:
        return False
    for location in spec.submodule_search_locations or ():
        directory = Path(location)
        if (directory / f"{DECODER}.py").is_file() or (
            directory / DECODER / "__init__.py"
        ).is_file():
            return True
    return False


def _sentence(text: str) -> str:
    """Word a directive error as a sentence, as Glitnir's own card errors are."""
    text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def parse(verb: str, target: str, options: str) -> EvidenceRequest:
    """Read a directive's options; raise `ValueError` if it cannot run as written.

    The request carries the options as exact numbers written as text, so it
    is plain JSON and `compute` reads back exactly what was declared. A
    ``sample`` request carries its plan, with the defaults filled in.
    """
    simulation = evidence.read_directive(verb, target, options)
    if simulation.error:
        raise ValueError(_sentence(simulation.error))
    if simulation.sample is not None:
        return EvidenceRequest(verb, target, _plan_options(simulation.sample))
    if verb == "iterate" and len(simulation.state) > MAX_SERIES:
        raise ValueError(f"Evidence shows at most {MAX_SERIES} states.")
    return EvidenceRequest(
        verb,
        target,
        {
            "solver": simulation.solver,
            "step": str(simulation.step),
            "steps": simulation.steps,
            "state": [str(v) for v in simulation.state],
            "inputs": [[str(v) for v in row] for row in simulation.inputs],
            "parameter": [str(v) for v in simulation.parameter],
            "names": list(simulation.names),
            "plane": list(simulation.plane),
        },
    )


def _plan_options(plan: SamplePlan) -> dict[str, Any]:
    """Write a ``sample`` plan as JSON, its exact numbers as text."""
    return {
        "law": plan.law,
        "variance": str(plan.variance),
        "paths": plan.paths,
        "steps": plan.steps,
        "seed": plan.seed,
        "show": plan.show,
        "observe": plan.observe,
        "barrier": str(plan.barrier),
    }


def _simulation(request: EvidenceRequest) -> Simulation:
    """Rebuild the directive `parse` read from its request."""
    options = request.options
    if request.verb == "sample":
        plan = SamplePlan(
            law=options["law"],
            variance=Fraction(options["variance"]),
            paths=options["paths"],
            steps=options["steps"],
            seed=options["seed"],
            show=options["show"],
            observe=options["observe"],
            barrier=Fraction(options["barrier"]),
        )
        return Simulation(request.target, verb="sample", sample=plan)

    def exact(values: list[str]) -> tuple[Fraction, ...]:
        return tuple(Fraction(value) for value in values)

    return Simulation(
        request.target,
        solver=options["solver"],
        step=Fraction(options["step"]),
        steps=options["steps"],
        verb=request.verb,
        state=exact(options["state"]),
        inputs=tuple(exact(row) for row in options["inputs"]),
        parameter=exact(options["parameter"]),
        names=tuple(options["names"]),
        plane=tuple(options["plane"]),
    )


def command(request: EvidenceRequest) -> str:
    """Return the Lean that prints the target's field or update circuit.

    Only the target, a Lean name, is written into the template; option text
    never reaches Lean.
    """
    try:
        return evidence.emission_command(request.verb, request.target)
    except EvidenceError as error:
        raise ValueError(_sentence(str(error))) from None


def _text(value: str) -> str:
    """Shorten a provenance value to what Glitnir accepts."""
    return value if len(value) <= MAX_TEXT else value[: MAX_TEXT - 1] + "…"


def _numbers(values: Any) -> str:
    return ", ".join(str(value) for value in values)


def _hash(tree: Any) -> str:
    return hashlib.sha256(json.dumps(tree, separators=(",", ":")).encode()).hexdigest()


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "unknown"


def _names(given: list[str], count: int) -> list[str]:
    """Use the given names if there is a distinct, usable one for every series."""
    if (
        len(given) == count
        and len(set(given)) == count
        and all(0 < len(name) <= MAX_TEXT for name in given)
    ):
        return given
    return [f"x{index}" for index in range(count)]


def _snapshot(
    trajectory: dict[str, Any],
    names: list[str],
    provenance: list[tuple[str, str]],
    plane: list[int] | None = None,
) -> dict[str, Any]:
    """Shape a run as a ``glitnir.evidence/v1`` body, one row per time."""
    body: dict[str, Any] = {
        "format": FORMAT,
        "series": {
            "times": [float(t) for t in trajectory["times"]],
            "values": [list(row) for row in zip(*trajectory["values"])],
            "names": names,
        },
    }
    if plane is not None:
        body["plane"] = plane
    created = datetime.now(UTC).isoformat(timespec="seconds")
    body["provenance"] = [
        [label, _text(value)]
        for label, value in [
            *provenance,
            ("provider", f"glitnir-forseti {_version('glitnir-forseti')}"),
            ("created", created),
        ]
    ]
    body["finite"] = trajectory["finite"]
    return body


def _simulated(simulation: Simulation, emitted: dict[str, Any]) -> dict[str, Any]:
    """Integrate an emitted continuous field and return its snapshot."""
    denotation, initial, start = evidence.field_polynomials(emitted)
    if len(initial) > MAX_SERIES:
        raise EvidenceError(
            f"the model has {len(initial)} states; evidence shows at most "
            f"{MAX_SERIES}"
        )
    states = emitted.get("states")
    given = (
        [str(s).removeprefix("state-") for s in states]
        if isinstance(states, list)
        else []
    )
    names = _names(given, len(initial))
    plane = None
    if simulation.plane:
        for name in simulation.plane:
            if name not in names:
                raise EvidenceError(
                    f"the plane names {name!r}, not a state of the model; "
                    f"its states are {', '.join(names)}"
                )
        plane = [names.index(name) for name in simulation.plane]
    terms = sum(len(output) for output in denotation.outputs)
    stages = 4 if simulation.solver == "rk4" else 1
    if simulation.steps * max(terms, 1) * stages > evidence.MAX_WORK:
        raise EvidenceError(
            f"this run would take {simulation.steps * terms * stages:,} field-term "
            f"evaluations, more than the {evidence.MAX_WORK:,} allowed; use fewer "
            "steps"
        )
    try:
        end = float(start + simulation.steps * simulation.step)
    except OverflowError:
        end = math.inf
    if not math.isfinite(end):
        raise EvidenceError(
            "the run's times would exceed float64; use a smaller step or fewer steps"
        )
    trajectory = evidence.integrate(denotation, initial, start, simulation)
    rounding = evidence.rounded(
        evidence.coefficients(denotation), initial, [start, simulation.step]
    )
    provenance = [
        ("verb", "simulate"),
        ("model", simulation.model),
        ("solver", simulation.solver),
        ("step", str(simulation.step)),
        ("steps", str(simulation.steps)),
        ("start", str(start)),
        ("initial", _numbers(initial)),
        ("arithmetic", "float64"),
        ("rounded", _numbers(rounding) or "none"),
        ("field sha256", _hash(emitted["field"])),
    ]
    return _snapshot(trajectory, names, provenance, plane)


def _iterated(simulation: Simulation, emitted: dict[str, Any]) -> dict[str, Any]:
    """Iterate an emitted update circuit and return its snapshot."""
    try:
        import gimle.asgard
        import gimle.asgard.rational_interchange  # noqa: F401 - the decoder
    except ImportError as error:  # removed, or broken, since the card was read
        raise EvidenceError(f"{NEEDS_ASGARD} ({error})") from None

    denotation = evidence.update_polynomials(emitted, simulation)
    terms = sum(len(output) for output in denotation.outputs)
    if simulation.steps * max(terms, 1) > evidence.MAX_WORK:
        raise EvidenceError(
            f"this run would take {simulation.steps * terms:,} term evaluations, "
            f"more than the {evidence.MAX_WORK:,} allowed; use fewer steps"
        )
    trajectory = evidence.iterate(denotation, simulation)
    names = _names(
        list(simulation.names[: len(simulation.state)]), len(simulation.state)
    )
    rounding = evidence.rounded(
        evidence.coefficients(denotation),
        simulation.state,
        *simulation.inputs,
        simulation.parameter,
    )
    provenance = [
        ("verb", "iterate"),
        ("circuit", simulation.model),
        ("solver", "iterate"),
        ("step", "1"),
        ("steps", str(simulation.steps)),
        ("initial", _numbers(simulation.state)),
        ("inputs", "; ".join(_numbers(row) for row in simulation.inputs) or "none"),
        ("parameter", _numbers(simulation.parameter) or "none"),
        ("arithmetic", "float64"),
        ("rounded", _numbers(rounding) or "none"),
        ("field sha256", _hash(emitted["circuit"])),
        ("asgard", str(getattr(gimle.asgard, "__version__", "unknown"))),
    ]
    return _snapshot(trajectory, names, provenance)


def _sampled(simulation: Simulation, emitted: dict[str, Any]) -> dict[str, Any]:
    """Sample an emitted stepped model under random shocks; return its snapshot.

    The card shows the first paths of the observed state and the running
    breach fraction. The final fraction and its 95 % Wilson interval are
    sampled evidence, kept in the provenance; no proved bound is drawn.
    """
    plan = simulation.sample
    if plan is None:
        raise EvidenceError("only a sample directive can be sampled")
    payload = sampler.sample_payload(emitted)
    try:
        float(plan.steps * payload.step)
    except OverflowError:
        raise EvidenceError(
            "the run's times would exceed float64; use fewer steps"
        ) from None
    result = sampler.sample(payload, plan)
    trajectory = {
        "times": [mark * payload.step for mark in result.marks],
        "values": [*result.paths, result.breach_series],
        "finite": result.finite,
    }
    names = [f"path {index}" for index in range(1, len(result.paths) + 1)]
    low, high = result.interval
    provenance = [
        ("verb", "sample"),
        ("model", simulation.model),
        ("law", plan.law),
        ("variance", str(plan.variance)),
        ("paths", str(plan.paths)),
        ("steps", str(plan.steps)),
        ("step", str(payload.step)),
        ("seed", str(plan.seed)),
        ("barrier", str(plan.barrier)),
        ("observe", plan.observe),
        ("arithmetic", "float64"),
        ("breach fraction", f"{result.breach_fraction:.6g}"),
        ("sampling interval", f"{low:.4g} to {high:.4g} (95 %, Wilson)"),
        ("field sha256", _hash(emitted["field"])),
    ]
    return _snapshot(trajectory, [*names, "breach fraction"], provenance)


def compute(request: EvidenceRequest, payload: Any) -> dict[str, Any]:
    """Run the directive on what Lean printed; return a ``glitnir.evidence/v1`` body.

    Raises:
        EvidenceError: If the payload cannot be read or the run is refused.
    """
    if not isinstance(payload, dict):
        raise EvidenceError("Lean printed a malformed field")
    simulation = _simulation(request)
    if simulation.verb == "iterate":
        return _iterated(simulation, payload)
    if simulation.verb == "sample":
        return _sampled(simulation, payload)
    return _simulated(simulation, payload)


def unavailable() -> str | None:
    """Nothing is missing for ``simulate`` or ``sample``."""
    return None


def iterate_unavailable() -> str | None:
    """Name the missing Python Asgard ``iterate`` needs, or None."""
    return None if asgard_installed() else NEEDS_ASGARD


def _provider(verbs: frozenset[str], missing: Any) -> EvidenceProvider:
    return EvidenceProvider(
        name="forseti",
        verbs=verbs,
        imports=IMPORTS,
        preamble="",
        marker=evidence.MARK,
        parse=parse,
        command=command,
        compute=compute,
        unavailable=missing,
    )


def evidence_providers() -> tuple[EvidenceProvider, EvidenceProvider]:
    """Return one Forseti provider for ``simulate`` and ``sample``, one for ``iterate``.

    They differ only in what they lack: ``iterate``'s provider is unavailable
    without Python Asgard, so Glitnir shows why on its cards and never runs it.
    """
    simulate = frozenset(evidence.VERBS) - {"iterate"}
    return (
        _provider(simulate, unavailable),
        _provider(frozenset({"iterate"}), iterate_unavailable),
    )
