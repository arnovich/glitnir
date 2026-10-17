"""Render the root theorem and its proof templates from a validated goal.

The prelude defines the goal's pieces from the *expected* goal and the model
entry, and ``forsetiRoot`` states the whole root claim:

    the model's initial ports, in state order, are the goal's IDs
    ∧ the observed output's port ID is the goal's
    ∧ the evolution's axis ID is the goal's
    ∧ Contract circuit time (Initialized time (fun _ => True) (· = initial))
        (Always time fun o => post.holds (fun _ => o observation))

so the IDs are checked by Lean, not only by Python. For a driven model the
driver predicate is ``forsetiDrivers``, never ``True``: the drivers are
admitted by the model's own bindings (``Evolution.Admitted``) and each bounded
driver stays within the goal's bound on the domain; the identities then also
fix the driver port IDs and the bounded driver's index. A proof is one of two
bounded templates, rendered from validated evidence:

- *prove*: ``Contract.consequence`` of the model's named contract, with task
  111's certificate that the contract's invariant entails ``post``;
- *refute*: the admitted declared input's solution (for a driven model, under
  the zero driver, which every bound admits), read at the start time
  where the named lemma gives the observation's exact value, at which
  ``post`` is decided false by ``Formula.holdsQ`` — a proof of
  ``forsetiIdentities ∧ ¬ forsetiContract``, so the identities are proved in
  a refutation too.

Either fails in Lean if the goal's time, initial state or observation differ
from what the cited theorems are about.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from glitnir_forseti.entailment.evidence import Certificate
from glitnir_forseti.entailment.render import formula, rational
from glitnir_forseti.entailment.syntax import Formula

from .goal import TrajectoryGoal
from .models import ModelEntry, ObservationBand

_SYNTAX = "Gimle.Forseti.Syntax"
_TRAJECTORY = "Gimle.Forseti.Trajectory"


@dataclass(frozen=True, slots=True)
class RootApplication:
    """The parts `check_independently` takes, and the lemmas the proof cites."""

    prelude: str
    statement: str
    script: tuple[str, ...]
    dependencies: tuple[str, ...]

    @property
    def source(self) -> str:
        """Render the whole unit, as a documentary record."""
        from glitnir_forseti.lean.unit import render_unit

        return render_unit(self.prelude, self.statement, self.script)


def _real(value: Fraction) -> str:
    return f"(({rational(value)} : ℚ) : ℝ)"


def initial_point(goal: TrajectoryGoal, entry: ModelEntry) -> tuple[Fraction, ...]:
    """Order the goal's initial values as the model's states.

    Raises:
        ValueError: If the goal does not name exactly the model's initial IDs.
    """
    values = dict(goal.initial)
    if set(values) != set(entry.initial_ids):
        raise ValueError("the goal must give exactly the model's initial ports")
    return tuple(values[identifier] for identifier in entry.initial_ids)


def _identities_proof(entry: ModelEntry) -> str:
    """Prove ``forsetiIdentities``; driver IDs need the kernel to decide."""
    driven = "" if entry.drivers is None else ", by decide +kernel, by decide +kernel"
    return f"⟨by decide, by decide, by decide{driven}⟩"


def _driver_definitions(goal: TrajectoryGoal, entry: ModelEntry) -> str:
    """Define the bounded driver's index and ``forsetiDrivers``.

    `replay.supported` admits exactly one bounded driver, the entry's.
    """
    assert entry.drivers is not None
    ((_, bound),) = goal.drivers
    width = f"(Gimle.Asgard.Model.DriverBinding.width {entry.drivers.bindings})"
    return (
        f"def forsetiDriver0 : Fin {width} :=\n"
        f"  ⟨{entry.drivers.index}, by decide⟩\n\n"
        f"def forsetiDrivers : Gimle.Asgard.Dynamics.Signal {width} → Prop :=\n"
        f"  fun d => {entry.evolution}.Admitted {entry.drivers.bindings} d ∧\n"
        f"    (∀ t ∈ forsetiTime.domain, |d t forsetiDriver0| ≤ {_real(bound)})\n\n"
    )


def _driver_identities(goal: TrajectoryGoal, entry: ModelEntry) -> str:
    """Render the driver port IDs, and the bounded driver's index, as conjuncts."""
    assert entry.drivers is not None
    ((port, _),) = goal.drivers
    ids = f"(Gimle.Asgard.Model.DriverBinding.ids {entry.drivers.bindings})"
    ports = ", ".join(f'"{p}"' for p in entry.drivers.ports)
    return (
        f" ∧\n  List.ofFn {ids} = [{ports}]"
        f' ∧\n  Gimle.Asgard.Model.index {ids} "{port}" = some forsetiDriver0'
    )


def prelude(
    goal: TrajectoryGoal,
    entry: ModelEntry,
    band: ObservationBand,
    leaf: Formula | None = None,
) -> str:
    """Render the imports, the goal's definitions and ``forsetiRoot``."""
    point = ", ".join(_real(v) for v in initial_point(goal, entry))
    ids = ", ".join(f'"{identifier}"' for identifier in entry.initial_ids)
    imports = "".join(f"import {module}\n" for module in entry.imports)
    text = (
        f"{imports}\n"
        "def forsetiTime : Gimle.Asgard.Dynamics.TimeDomain :=\n"
        f'  ⟨"{goal.axis}", {_real(goal.start)}⟩\n\n'
        f"def forsetiInitial : Gimle.Asgard.Point {entry.states} :=\n"
        f"  ![{point}]\n\n"
        f"def forsetiObservation : Fin {entry.outputs} := ⟨{band.index}, by decide⟩\n\n"
        f"def forsetiPost : {_SYNTAX}.Formula 1 :=\n"
        f"  {formula(goal.post)}\n\n"
    )
    if leaf is not None:
        text += (
            f"def forsetiLeaf : {_SYNTAX}.Goal :=\n"
            f'  ⟨⟨["{goal.name}"], by decide, by decide, by decide⟩,\n'
            f"    {formula(leaf)},\n"
            "    forsetiPost⟩\n\n"
        )
    driven = entry.drivers is not None
    if driven:
        text += _driver_definitions(goal, entry)
    drivers = "forsetiDrivers" if driven else "(fun _ => True)"
    identities = _driver_identities(goal, entry) if driven else ""
    return text + (
        "def forsetiIdentities : Prop :=\n"
        f"  {entry.evolution}.states.map (·.initialId) = [{ids}] ∧\n"
        f"  ({entry.body}.observations.get forsetiObservation).port.id =\n"
        f'    "{goal.observation}" ∧\n'
        f'  {entry.evolution}.axis.id = "{goal.axis}"{identities}\n\n'
        "def forsetiContract : Prop :=\n"
        f"  {_TRAJECTORY}.Contract {entry.circuit} forsetiTime\n"
        f"    ({_TRAJECTORY}.Initialized forsetiTime {drivers}\n"
        "      (· = forsetiInitial))\n"
        f"    ({_TRAJECTORY}.Always forsetiTime\n"
        "      fun o => forsetiPost.holds (fun _ => o forsetiObservation))\n\n"
        "def forsetiRoot : Prop := forsetiIdentities ∧ forsetiContract\n"
    )


def _bind(
    entry: ModelEntry, band: ObservationBand, driver: bool = True
) -> tuple[str, ...]:
    """Rewrite the goal's time, initial state and index to the model's.

    A driven model's bounded-driver index is bound too, unless ``driver`` is
    false: a refutation at the start never rewrites with it.
    """
    # `simp` reduces the goal's `mkRat` start and closes it for some starts
    # but not others; `<;>` runs `norm_num` only on what remains, so the one
    # script works for every start (tests/test_trajectory_lean.py).
    return (
        f"have time : forsetiTime = {entry.evolution}.time := by "
        f"simp [forsetiTime, Gimle.Asgard.Model.Evolution.time, {entry.evolution}] "
        "<;> norm_num [Rat.mkRat_eq_div]",
        f"have init : forsetiInitial = {entry.model}.initial := by "
        f"rw [{entry.initial_eq}]; funext i; fin_cases i <;> "
        "norm_num [forsetiInitial, Rat.mkRat_eq_div]",
        f"have obs : forsetiObservation = {band.index_constant} := rfl",
        *(
            ()
            if entry.drivers is None or not driver
            else (f"have drv : forsetiDriver0 = {entry.drivers.index_constant} := rfl",)
        ),
    )


def _strengthen(entry: ModelEntry) -> str:
    """Show the goal's precondition implies the named contract's.

    A driven goal's bound may be below the contract's, never above
    (`replay.supported`); regularity is the same ``Admitted`` on both sides.
    """
    if entry.drivers is None:
        return "(fun _ admitted => admitted)"
    return (
        "(fun _ admitted => ⟨⟨admitted.1.1, fun t within => "
        "(admitted.1.2 t within).trans (by norm_num [Rat.mkRat_eq_div])⟩, "
        "admitted.2⟩)"
    )


def _squares(certificate: Certificate) -> str:
    """Render a certificate as `EntailmentCertificate` data."""
    from glitnir_forseti.entailment.render import _square_sum

    rows = ", ".join(
        f"⟨{_square_sum(row.base)}, "
        f"[{', '.join(_square_sum(m) for m in row.multipliers)}]⟩"
        for row in certificate.rows
    )
    return f"⟨[{rows}]⟩"


def prove(
    goal: TrajectoryGoal,
    entry: ModelEntry,
    band: ObservationBand,
    certificate: Certificate,
) -> RootApplication:
    """Prove ``forsetiRoot`` from the named contract and a leaf certificate."""
    driven = entry.drivers is not None
    script = (
        "unfold forsetiRoot forsetiIdentities forsetiContract"
        + (" forsetiDrivers" if driven else ""),
        f"refine ⟨{_identities_proof(entry)}, ?_⟩",
        *_bind(entry, band),
        "rw [time, init, obs" + (", drv" if driven else "") + "]",
        f"refine {_TRAJECTORY}.Contract.consequence {band.contract} "
        f"{_strengthen(entry)} ?_",
        "intro o band t within",
        f"have leaf := {_SYNTAX}.EntailmentCertificate.sound forsetiLeaf "
        f"{_squares(certificate)} (by decide +kernel)",
        "apply leaf",
        f"simp only [forsetiLeaf, {_SYNTAX}.Formula.holds, {_SYNTAX}.Relation.holds, "
        "Gimle.Asgard.Polynomial.Expr.eval]",
        *band.band_lines,
    )
    return RootApplication(
        prelude(goal, entry, band, band.invariant),
        ": forsetiRoot",
        script,
        (band.contract, f"{_SYNTAX}.EntailmentCertificate.sound", entry.initial_eq),
    )


def refute(
    goal: TrajectoryGoal,
    entry: ModelEntry,
    band: ObservationBand,
    value: Fraction,
) -> RootApplication:
    """Prove ``forsetiIdentities ∧ ¬ forsetiContract`` at the declared start.

    The identities are proved, not assumed: a refutation says the contract
    fails for the circuit, time, state and output the goal names, never merely
    that a mis-stated identity is false.
    """
    if entry.drivers is not None:
        return _refute_driven(goal, entry, band, value)
    assert entry.exists and entry.reads and entry.admitted, "an undriven entry"
    evolution = entry.evolution
    script = (
        "unfold forsetiIdentities forsetiContract",
        f"refine ⟨{_identities_proof(entry)}, ?_⟩",
        "intro contract",
        *_bind(entry, band),
        "rw [time, init, obs] at contract",
        f"obtain ⟨state, realized⟩ := {entry.exists}",
        f"have start : state {evolution}.time.start = {entry.model}.initial := by "
        "unfold Gimle.Asgard.Model.ContinuousModel.Realizes "
        "Gimle.Asgard.Model.ContinuousModel.feedback at realized; "
        f"exact (({_TRAJECTORY}.close_rel _ _ _ _ _ _).mp realized).2.1",
        f"have bounded := contract.holds _ {entry.admitted} _ "
        f"⟨state, ({entry.reads} _ {entry.admitted} state).mpr realized, rfl⟩ "
        f"{evolution}.time.start "
        "(by simp [Gimle.Asgard.Dynamics.TimeDomain.domain])",
        "simp only at bounded",
        f"rw [start, {band.initial_value_lemma}] at bounded",
        *_witness_lines(value),
    )
    return RootApplication(
        prelude(goal, entry, band),
        ": forsetiIdentities ∧ ¬ forsetiContract",
        script,
        (
            entry.exists,
            entry.reads,
            entry.admitted,
            band.initial_value_lemma,
            f"{_SYNTAX}.Formula.holdsQ_iff",
        ),
    )


def _witness_lines(value: Fraction) -> tuple[str, ...]:
    """Close the refutation from ``bounded``: post at the observation's value."""
    return (
        # The witness: the observation's exact value there, as a rational point.
        f"have witness : forsetiPost.holds (fun i => "
        f"(((fun _ : Fin 1 => {rational(value)}) i : ℚ) : ℝ)) := by "
        "convert bounded using 2; norm_num [Rat.mkRat_eq_div]",
        f"rw [← {_SYNTAX}.Formula.holdsQ_iff] at witness",
        "exact absurd witness (by decide +kernel)",
    )


def _refute_driven(
    goal: TrajectoryGoal,
    entry: ModelEntry,
    band: ObservationBand,
    value: Fraction,
) -> RootApplication:
    """Refute a driven root under the zero driver, at the declared start.

    The zero driver is admitted (the entry's ``zero_admitted``) and within
    every nonnegative bound, so it satisfies the goal's own ``forsetiDrivers``;
    the named lemma gives the observation's value at the start for any
    admitted driver.
    """
    drivers = entry.drivers
    assert drivers is not None
    # `output_at_start` reads the observation at t = 0; a model starting
    # elsewhere fails in Lean at `origin`, never proves a different claim.
    evolution = entry.evolution
    zero = f"({drivers.with_initial} fun _ _ => 0)"
    admission = (
        f" (by simpa [{drivers.with_initial}] using zero)"
        if drivers.start_needs_admission
        else ""
    )
    script = (
        "unfold forsetiIdentities forsetiContract",
        f"refine ⟨{_identities_proof(entry)}, ?_⟩",
        "intro contract",
        *_bind(entry, band, driver=False),
        "rw [time, init, obs] at contract",
        f"have zero : {evolution}.Admitted {drivers.bindings} (fun _ _ => 0) := "
        f"{drivers.zero_admitted}",
        f"have admit : {_TRAJECTORY}.Initialized {evolution}.time forsetiDrivers "
        f"(· = {entry.model}.initial) {zero} := "
        f"⟨⟨by simpa [{drivers.with_initial}] using zero, fun t _ => by "
        f"simp [{drivers.with_initial}] <;> norm_num [Rat.mkRat_eq_div]⟩, "
        f"{drivers.constant_start} _⟩",
        "obtain ⟨output, related⟩ := contract.realizable _ admit",
        f"have origin : {evolution}.time.start = 0 := by "
        f"simp [Gimle.Asgard.Model.Evolution.time, {evolution}]",
        f"have bounded := contract.holds _ admit output related {evolution}.time.start "
        "(by simp [Gimle.Asgard.Dynamics.TimeDomain.domain])",
        "simp only at bounded",
        f"rw [origin, {band.initial_value_lemma} _ ({drivers.constant_start} _)"
        f"{admission} output related] at bounded",
        *_witness_lines(value),
    )
    return RootApplication(
        prelude(goal, entry, band),
        ": forsetiIdentities ∧ ¬ forsetiContract",
        script,
        (
            drivers.zero_admitted,
            drivers.constant_start,
            band.initial_value_lemma,
            f"{_SYNTAX}.Formula.holdsQ_iff",
        ),
    )
