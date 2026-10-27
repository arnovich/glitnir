"""What a provider's untrusted output must look like before it is shown or saved."""

import json
import math
import re
import xml.etree.ElementTree as ElementTree
from typing import Any, Literal, NotRequired, TypedDict, cast, overload

MAX_SVG_BYTES = 1_000_000


class Drawing(TypedDict, total=False):
    """A checked drawing: its SVG and, from a diagram report, its structured parts."""

    svg: str
    graph: Any
    model: Any
    detail: Any
    contracts: Any


class DrawingError(TypedDict):
    """Why a provider's drawing is not shown."""

    error: str


class Series(TypedDict):
    """Evidence samples: the times, one list of values per named series."""

    times: list[float]
    values: list[list[float | None]]
    names: list[str]


class Evidence(TypedDict):
    """Checked evidence as the preview shows and the records save it."""

    format: str
    series: Series
    plane: NotRequired[list[int]]
    provenance: list[list[str]]
    finite: bool


class ProvedDecision(TypedDict):
    """A proven or refuted outcome: its block and the theorem the block declares."""

    status: Literal["proven", "refuted"]
    reason: str
    stages: list[list[str]]
    block: str
    theorem: str


class UnknownDecision(TypedDict):
    """An outcome that decides nothing: no block, no theorem."""

    status: Literal["unknown"]
    reason: str
    stages: list[list[str]]
    block: None
    theorem: None


#: What `checked_decision` returns.
Decision = ProvedDecision | UnknownDecision


def checked_drawing(text: str) -> Drawing | DrawingError:
    """Accept a bounded presentation report, retaining the isolated SVG fallback."""
    if len(text.encode("utf-8")) > MAX_SVG_BYTES:
        return {"error": "the drawing is too large to show"}
    if not text.startswith("{"):
        return _checked_svg(text)
    try:
        report = json.loads(text)
    except (ValueError, RecursionError):
        return {"error": "invalid diagram report"}
    if not isinstance(report, dict) or not isinstance(report.get("svg"), str):
        return {"error": "invalid diagram report"}
    checked = _checked_svg(report["svg"])
    if "error" in checked:
        return checked
    # The app validates structured geometry before rendering. Raw SVG is used
    # only as an image; no markup from a report enters the notebook DOM.
    drawing: Drawing = {"svg": report["svg"]}
    if "graph" in report:
        drawing["graph"] = report["graph"]
    if "model" in report:
        drawing["model"] = report["model"]
    if "detail" in report:
        drawing["detail"] = report["detail"]
    if "contracts" in report:
        drawing["contracts"] = report["contracts"]
    return drawing


def _checked_svg(text: str) -> Drawing | DrawingError:
    if len(text.encode("utf-8")) > MAX_SVG_BYTES:
        return {"error": "the drawing is too large to show"}
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        return {"error": f"the drawing is not valid SVG: {error}"}
    if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
        return {"error": "the drawing is not an SVG document"}
    return {"svg": text}


EVIDENCE_FORMAT = "glitnir.evidence/v1"
MAX_SAMPLES = 4000
MAX_SERIES = 16
MAX_TEXT = 200
MAX_PAIRS = 32
MAX_BYTES = 2_000_000
_BODY_KEYS = {"format", "series", "plane", "provenance", "finite"}
_SERIES_KEYS = {"times", "values", "names"}


@overload
def _number(value: Any, what: str) -> float: ...


@overload
def _number(value: Any, what: str, *, nullable: bool) -> float | None: ...


def _number(value: Any, what: str, *, nullable: bool = False) -> float | None:
    """Return `value` as a finite float; booleans are not numbers here."""
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number.")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{what} is out of range.") from None
    if not math.isfinite(number):
        raise ValueError(f"{what} must be finite.")
    return number


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(f"{what} must be text of at most {MAX_TEXT} characters.")
    return value


def _list(value: Any, what: str, limit: int) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list.")
    if len(value) > limit:
        raise ValueError(f"{what} may hold at most {limit} entries.")
    return value


def _checked_series(series: Any) -> Series:
    if not isinstance(series, dict) or set(series) != _SERIES_KEYS:
        raise ValueError("series must hold exactly times, values and names.")
    times = [
        _number(time, "A time") for time in _list(series["times"], "times", MAX_SAMPLES)
    ]
    names = [
        _text(name, "A series name")
        for name in _list(series["names"], "names", MAX_SERIES)
    ]
    rows = _list(series["values"], "values", MAX_SAMPLES)
    if len(rows) != len(times):
        raise ValueError("values needs one row for each time.")
    values = []
    for row in rows:
        row = _list(row, "A row of values", MAX_SERIES)
        if len(row) != len(names):
            raise ValueError("Each row of values needs one value for each name.")
        values.append([_number(item, "A value", nullable=True) for item in row])
    return {"times": times, "values": values, "names": names}


def _checked_plane(plane: Any, width: int) -> list[int]:
    if not isinstance(plane, list) or len(plane) != 2:
        raise ValueError("plane must be two series indices.")
    for index in plane:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("plane must be two series indices.")
        if not 0 <= index < width:
            raise ValueError("plane refers to a series that does not exist.")
    return list(plane)


def _checked_provenance(provenance: Any) -> list[list[str]]:
    pairs = []
    for pair in _list(provenance, "provenance", MAX_PAIRS):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("provenance must hold [label, value] pairs.")
        pairs.append([_text(pair[0], "A label"), _text(pair[1], "A value")])
    return pairs


def checked_evidence(body: Any) -> Evidence:
    """Return a normalised copy of provider output, or raise ValueError.

    Provider output is untrusted: the shape, bounds and finiteness checked here
    are all the preview and the saved file ever see of it.
    """
    if not isinstance(body, dict):
        raise ValueError("Evidence must be an object.")
    unknown = set(body) - _BODY_KEYS
    if unknown:
        raise ValueError(f"Evidence has unknown keys: {', '.join(sorted(unknown))}.")
    if body.get("format") != EVIDENCE_FORMAT:
        raise ValueError(f"Evidence format must be {EVIDENCE_FORMAT}.")
    for key in ("series", "provenance", "finite"):
        if key not in body:
            raise ValueError(f"Evidence needs {key}.")
    if not isinstance(body["finite"], bool):
        raise ValueError("finite must be true or false.")
    series = _checked_series(body["series"])
    checked: Evidence = {
        "format": EVIDENCE_FORMAT,
        "series": series,
        "provenance": _checked_provenance(body["provenance"]),
        "finite": body["finite"],
    }
    if "plane" in body:
        checked["plane"] = _checked_plane(body["plane"], len(series["names"]))
    size = len(json.dumps(checked, separators=(",", ":")).encode("utf-8"))
    if size > MAX_BYTES:
        raise ValueError(f"Evidence may hold at most {MAX_BYTES} bytes.")
    return checked


OUTCOME_STATUSES = ("proven", "refuted", "unknown")
_OUTCOME_KEYS = frozenset({"status", "reason", "stages", "block", "theorem"})
MAX_REASON = 200
MAX_STAGES = 32
MAX_STAGE_TEXT = 500
MAX_BLOCK_BYTES = 200_000
MAX_THEOREM = 200
# The theorem a block declares: a Lean identifier, possibly qualified.
_THEOREM = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")
# A line that, once inserted, would be read as a `@decide`, `@evidence` or
# `@view` directive (or a mistyped one).
_DIRECTIVE_LINE = re.compile(r"\s*--\s*@")


def _bounded(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{what} must be text of at most {limit} characters.")
    return value


def _checked_stages(stages: Any) -> list[list[str]]:
    if not isinstance(stages, list) or len(stages) > MAX_STAGES:
        raise ValueError(f"stages must be a list of at most {MAX_STAGES} entries.")
    checked = []
    for stage in stages:
        if not isinstance(stage, list) or len(stage) != 3:
            raise ValueError("stages must hold [stage, outcome, detail] entries.")
        checked.append(
            [_bounded(part, "A stage entry", MAX_STAGE_TEXT) for part in stage]
        )
    return checked


def _checked_block(block: Any) -> str:
    """Return the block exactly as given; anything that needs cleaning is refused."""
    if not isinstance(block, str) or not block.strip():
        raise ValueError("A proven or refuted decision needs a Lean block.")
    if "\r" in block or "\x00" in block:
        raise ValueError("The block may not contain carriage returns or NUL.")
    lines = block.split("\n")
    # Inserted after a directive, a leading comment line would continue its goal.
    if lines[0].lstrip().startswith("--"):
        raise ValueError("The block may not start with a comment line.")
    for number, line in enumerate(lines, start=1):
        if _DIRECTIVE_LINE.match(line):
            raise ValueError(
                f"The block may not contain directive comments (line {number})."
            )
    # Editors may trim these on save, and the inserted block would then differ.
    if block.endswith("\n"):
        raise ValueError("The block may not end with a newline.")
    if any(line != line.rstrip() for line in lines):
        raise ValueError("The block may not end a line with whitespace.")
    try:
        size = len(block.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("The block is not valid text.") from None
    if size > MAX_BLOCK_BYTES:
        raise ValueError(f"The block may hold at most {MAX_BLOCK_BYTES} bytes.")
    return block


def checked_decision(outcome: Any) -> Decision:
    """Return a normalised copy of a provider's outcome, or raise ValueError.

    `status` is required; `reason` and `stages` default to empty. A proven or
    refuted outcome names the `theorem` its `block` declares; an unknown one
    has neither. The block is never rewritten, so the block the checker sees is
    exactly the one saved and inserted.
    """
    if not isinstance(outcome, dict):
        raise ValueError("A decision outcome must be an object.")
    unknown = set(outcome) - _OUTCOME_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"The decision outcome has unknown keys: {names}.")
    status = outcome.get("status")
    if not isinstance(status, str) or status not in OUTCOME_STATUSES:
        raise ValueError("status must be proven, refuted or unknown.")
    reason = _bounded(outcome.get("reason", ""), "reason", MAX_REASON)
    stages = _checked_stages(outcome.get("stages", []))
    block, theorem = outcome.get("block"), outcome.get("theorem")
    if status == "unknown":
        if block is not None or theorem is not None:
            raise ValueError("An unknown decision has no block or theorem.")
        return {
            "status": "unknown",
            "reason": reason,
            "stages": stages,
            "block": None,
            "theorem": None,
        }
    checked_block = _checked_block(block)
    if (
        not isinstance(theorem, str)
        or len(theorem) > MAX_THEOREM
        or not _THEOREM.fullmatch(theorem)
    ):
        raise ValueError("theorem must name the Lean theorem the block declares.")
    return {
        # Checked against OUTCOME_STATUSES above, and not "unknown" here.
        "status": cast(Literal["proven", "refuted"], status),
        "reason": reason,
        "stages": stages,
        "block": checked_block,
        "theorem": theorem,
    }
