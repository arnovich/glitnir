"""Embed the workbench styles and math fonts for inert notebook exports.

Export reports recorded observations. It neither runs Lean nor updates records.
"""

import base64
import re
from pathlib import Path

_APP = Path(__file__).parent / "app"


def export_styles() -> str:
    """Return offline CSS, retaining the vendored KaTeX and font licences."""
    katex = _APP / "katex"
    css = (katex / "katex.min.css").read_text()

    def embed(match: re.Match[str]) -> str:
        font = (katex / "fonts" / match[1]).read_bytes()
        encoded = base64.b64encode(font).decode("ascii")
        return f'src:url(data:font/woff2;base64,{encoded}) format("woff2")'

    # Only woff2 files are vendored. Drop the upstream woff/ttf fallbacks too.
    css = re.sub(r"src:url\(fonts/([\w-]+\.woff2)\)[^;}]*", embed, css)
    licence = (katex / "LICENSE").read_text().replace("*/", "* /")
    font_licence = (katex / "OFL.txt").read_text().replace("*/", "* /")
    return "\n".join(
        (
            f"/* KaTeX (MIT licence)\n{licence}\n*/",
            f"/* KaTeX fonts (SIL OFL 1.1)\n{font_licence}\n*/",
            (_APP / "app.css").read_text(),
            (_APP / "cards.css").read_text(),
            (_APP / "circuits.css").read_text(),
            css,
            (_APP / "export.css").read_text(),
        )
    )
