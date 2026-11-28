"""Conservative saved-source projection of optional Lean explanation prose.

This reader recognizes one annotation spelling. It does not elaborate Lean or
infer proof status; the independent checker and proof trace own those jobs.
"""

from __future__ import annotations

import bisect
import json
from typing import TYPE_CHECKING, TypedDict

from glitnir.lean.lexing import code_tokens

if TYPE_CHECKING:
    from glitnir.preview.snapshot import Cell


class Explanation(TypedDict):
    """Authored prose located in a saved source cell."""

    authored_id: str
    markdown: str
    line: int
    cell: int


def project_explanations(text: str, cells: list[Cell]) -> list[Explanation]:
    """Show unambiguous `explain ID MARKDOWN =>` lines from saved source."""
    spans = code_tokens(text)
    if spans is None:
        return []
    line_starts = [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]
    source_cells = [
        cell
        for cell in cells
        if cell["kind"] == "source"
        and (cell.get("authored_claim") or cell.get("claim"))
        and "`(" not in cell["text"]
    ]
    source_starts = [cell["start"] for cell in source_cells]
    projected: list[Explanation] = []
    for index in range(len(spans) - 4):
        start, stop = spans[index]
        if text[start:stop] != "explain":
            continue
        line = bisect.bisect_right(line_starts, start)
        prefix = text[line_starts[line - 1] : start]
        if not prefix or not prefix.isspace() or "\n" in prefix:
            continue
        tokens = [text[a:b] for a, b in spans[index + 1 : index + 5]]
        if (
            "\n" in text[start : spans[index + 4][1]]
            or tokens[2:] != ["=", ">"]
            or not all(
                token.startswith('"') and token.endswith('"') for token in tokens[:2]
            )
        ):
            continue
        try:
            authored_id, markdown = (json.loads(token) for token in tokens[:2])
        except (ValueError, TypeError):
            continue
        if not isinstance(authored_id, str) or not isinstance(markdown, str):
            continue
        try:
            id_size, markdown_size = len(authored_id.encode()), len(markdown.encode())
        except UnicodeEncodeError:
            continue
        if not authored_id or id_size > 128 or markdown_size > 20_000:
            continue
        cell_index = bisect.bisect_right(source_starts, line) - 1
        if cell_index < 0 or line > source_cells[cell_index]["end"]:
            continue
        cell = source_cells[cell_index]
        projected.append(
            {
                "authored_id": authored_id,
                "markdown": markdown,
                "line": line,
                "cell": cell["index"],
            }
        )
        if len(projected) > 4096:
            return []
    counts: dict[tuple[int, str], int] = {}
    for item in projected:
        key = (item["cell"], item["authored_id"])
        counts[key] = counts.get(key, 0) + 1
    return [
        item for item in projected if counts[(item["cell"], item["authored_id"])] == 1
    ]
