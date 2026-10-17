"""Closed total mild-circuit proof roots reconstructed from caller goals.

Both certificate choices retain the identical classical, convergent and
spectrally dissipative postcondition. Python supplies only rational premises.
"""

from fractions import Fraction

from glitnir_forseti.entailment.render import rational
from glitnir_forseti.trajectory.render import RootApplication

from .goal import MildBandClaim, MildTruncationClaim, Modes, StreamGoal, band_of
from .mild import MildEvidence

IMPORTS = ("Gimle.Forseti.MildVorticity",)
_M = "Gimle.Asgard.Streams.Mild"
_V = "Gimle.Forseti.MildVorticity"
_T = "Gimle.Asgard.Streams.Torus"


def _real(value: Fraction) -> str:
    """Render a rational embedded into exact real semantics."""
    return f"(({rational(value)} : ℚ) : ℝ)"


def _viscosity(value: Fraction) -> str:
    """Keep the dependent circuit index in the public literal rational form."""
    if value.denominator == 1:
        return f"({value.numerator} : ℚ)"
    return f"({value.numerator} / {value.denominator} : ℚ)"


def prelude(goal: StreamGoal) -> str:
    """State the actual circuit and full output guarantee independently of evidence."""
    claim, start = goal.claim, goal.profile
    if (
        type(claim) is not MildTruncationClaim and type(claim) is not MildBandClaim
    ) or not isinstance(start, Modes):
        raise ValueError("a mild root needs a physical mild claim and a modes start")
    if goal.viscosity is None:
        raise ValueError("a mild root names its viscosity")
    nu, horizon = _viscosity(goal.viscosity), _real(claim.time)
    entries = ", ".join(f"(({k1}, {k2}), {rational(c)})" for k1, k2, c in start.terms)
    if type(claim) is MildTruncationClaim:
        post = (
            f"{_M}.MildTruncationBound {nu} (output 0) {horizon} {claim.window}"
            f" {_real(claim.epsilon)}"
        )
    else:
        assert type(claim) is MildBandClaim
        band = band_of(claim.post)
        if band is None:
            raise ValueError("a mild-band root needs a band on u")
        lo, hi = band
        field = f"{_M}.analyticField {nu} (output 0) t x"
        sides = ([] if lo is None else [f"{_real(lo)} ≤ {field}"]) + (
            [] if hi is None else [f"{field} ≤ {_real(hi)}"]
        )
        post = f"∀ t ∈ Set.Icc 0 {horizon}, ∀ x : ℝ × ℝ, " + " ∧ ".join(sides)
    return (
        f"import Gimle.Forseti.MildVorticity\n\ndef forsetiTable : {_T}.Table :="
        f" [{entries}]\n\nnoncomputable def forsetiP : {_T}.TrigPoly :="
        f" {_T}.Table.toTrig forsetiTable\n\ndef forsetiPost (output : {_M}.Point 1) :"
        f" Prop :=\n  {_V}.Certified {nu} forsetiP {horizon} output ∧\n "
        f" ({post})\n\ndef forsetiRoot : Prop :=\n  Gimle.Forseti.Mild.Contract"
        f" ({_M}.mildCircuit {nu})\n    (fun input => input 0 0 = {_M}.embed forsetiP)"
        " forsetiPost\n"
    )


def prove(goal: StreamGoal, evidence: MildEvidence) -> RootApplication:
    """Choose a proof of the caller's fixed root without trusting searched bounds."""
    claim = goal.claim
    if (
        type(claim) is not MildTruncationClaim and type(claim) is not MildBandClaim
    ) or goal.viscosity is None:
        raise ValueError("mild evidence requires a physical mild claim")
    head = prelude(goal)
    nu = _viscosity(goal.viscosity)
    time, weight = map(rational, (claim.time, evidence.weight))
    size, window = evidence.size, evidence.window
    calc = "by norm_num [Rat.mkRat_eq_div]"
    decide = "by decide +kernel"
    lines = [
        f"have hs : {_T}.SizeLE {size} forsetiP := by",
    ]
    if evidence.weight == 0:
        lines += [
            (
                f"  have hz := Gimle.Forseti.Euler.Table.toTrig_zero ({decide} :"
                " Gimle.Forseti.Euler.Table.IsZero forsetiTable)"
            ),
            (
                f"  simpa only [forsetiP, hz] using (show {_T}.SizeLE {size} 0 by simp"
                f" [{_T}.SizeLE])"
            ),
            f"have hl : {_T}.l1 forsetiP ≤ {weight} := by",
            (
                f"  have hz := Gimle.Forseti.Euler.Table.toTrig_zero ({decide} :"
                " Gimle.Forseti.Euler.Table.IsZero forsetiTable)"
            ),
            f"  simp [forsetiP, hz, {_T}.l1]",
        ]
    else:
        lines += [
            (
                f"  exact {_T}.Table.sizeLE_toTrig ({decide} : ∀ e ∈ forsetiTable,"
                f" {_T}.size e.1 ≤ {size})"
            ),
            (
                f"have hl : {_T}.l1 forsetiP ≤ {weight} := ({_T}.Table.l1_le_absSum"
                f" forsetiTable).trans ({decide} : {_T}.Table.absSum forsetiTable ≤"
                f" {weight})"
            ),
        ]
    lines += [
        f"have hc : Gimle.Forseti.Mild.Contract ({_M}.mildCircuit {nu})",
        f"    (fun input => input 0 0 = {_M}.embed forsetiP)",
        f"    ({_V}.Certified {nu} forsetiP {_real(claim.time)}) := by",
    ]
    if evidence.weight == 0:
        lines.append(
            f"  exact {_V}.Table.zero_contract forsetiTable ({decide}) ({calc})"
            f" ({calc})"
        )
    elif evidence.kind == "mild-scale":
        lines.append(
            f"  exact {_V}.Table.euler_contract forsetiTable ({decide}) ({decide})"
            f" ({calc}) (K := {size}) ({decide}) ({decide}) (L := {weight}) ({decide})"
            f" ({calc}) ({calc})"
        )
    else:
        if evidence.radius is None:
            raise ValueError("contraction evidence requires a radius")
        radius = rational(evidence.radius)
        lines.append(
            f"  exact {_V}.Table.catalan_contract forsetiTable ({decide}) ({decide}) (r"
            f" := {radius}) ({calc}) (K := {size}) ({decide}) (L := {weight})"
            f" ({decide}) ({calc}) ({calc}) ({calc}) ({calc})"
        )
    lines += ["unfold forsetiRoot forsetiPost", f"apply {_V}.contract_with forsetiP hc"]
    suffix = "truncation" if type(claim) is MildTruncationClaim else "band"
    if evidence.kind == "mild-scale":
        bound = (
            f"{_M}.euler{'Tail' if suffix == 'truncation' else 'Band'} {weight} {size}"
            f" {time} {window}"
        )
        last = str(window) if suffix == "truncation" else f"(N := {window}) ({decide})"
        lines.append(
            f"have h := {_M}.wild_euler_{suffix} (ν := {nu}) (T := {time}) ({calc})"
            f" ({calc}) ({_V}.boundary forsetiP) forsetiP rfl (K := {size}) ({decide})"
            f" hs (L := {weight}) hl ({calc}) {last}"
        )
    else:
        assert evidence.radius is not None
        radius = rational(evidence.radius)
        bound = (
            f"{_M}.catalan{'Tail' if suffix == 'truncation' else 'Band'} {weight}"
            f" {radius} {window}"
        )
        lines.append(
            f"have h := {_M}.wild_catalan_{suffix} (ν := {nu}) (r := {radius}) (T :="
            f" {_real(claim.time)}) ({calc}) ({calc}) ({calc}) ({calc}) ({_V}.boundary"
            f" forsetiP) forsetiP rfl (L := {weight}) hl ({calc}) {window}"
        )
    if type(claim) is MildTruncationClaim:
        lines += [
            "apply h.mono",
            (
                f"exact_mod_cast (show {bound} ≤ {rational(claim.epsilon)} by decide"
                " +kernel)"
            ),
        ]
    else:
        assert type(claim) is MildBandClaim
        band = band_of(claim.post)
        assert band is not None
        lo, hi = band
        lines += ["intro t ht x", "have hh := abs_le.mp (h t ht x)"]
        conclusions = []
        if lo is not None:
            conclusions.append(
                f"le_trans (by exact_mod_cast (show {rational(lo)} ≤ -({bound}) by"
                " decide +kernel)) hh.1"
            )
        if hi is not None:
            conclusions.append(
                f"le_trans hh.2 (by exact_mod_cast (show {bound} ≤ {rational(hi)} by"
                " decide +kernel))"
            )
        lines.append(
            "exact "
            + (
                "⟨" + ", ".join(conclusions) + "⟩"
                if len(conclusions) == 2
                else conclusions[0]
            )
        )
    route = "euler" if evidence.kind == "mild-scale" else "catalan"
    dependencies = (
        f"{_V}.contract_with",
        (
            f"{_V}.Table.zero_contract"
            if evidence.weight == 0
            else (f"{_V}.Table.{route}_contract")
        ),
        f"{_M}.wild_{'euler' if evidence.kind == 'mild-scale' else 'catalan'}_{suffix}",
    )
    return RootApplication(head, ": forsetiRoot", tuple(lines), dependencies)
