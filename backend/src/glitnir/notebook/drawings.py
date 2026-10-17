"""Validation shared by every drawing that reaches the renderer."""

import json
import xml.etree.ElementTree as ElementTree
from typing import Any

MAX_SVG_BYTES = 1_000_000


def checked_drawing(text: str) -> dict[str, Any]:
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
    return {
        key: report[key]
        for key in ("svg", "graph", "model", "detail", "contracts")
        if key in report
    }


def _checked_svg(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_SVG_BYTES:
        return {"error": "the drawing is too large to show"}
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        return {"error": f"the drawing is not valid SVG: {error}"}
    if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
        return {"error": "the drawing is not an SVG document"}
    return {"svg": text}
