# Third-party notices

Glitnir-owned backend code is MIT licensed; see LICENSE. This does not change
the licences of third-party code, assets or separately installed dependencies.

The renderer bundles KaTeX 0.16.47, with MIT-licensed code and styles, from
https://github.com/KaTeX/KaTeX. Its MIT licence and copyright notice are retained
in `glitnir/notebook/app/katex/LICENSE` in the installed package
(`src/glitnir/notebook/app/katex/LICENSE` in the source tree).

The bundled KaTeX fonts use the SIL Open Font License 1.1. Their copyright,
reserved font names and full licence are in `katex/OFL.txt` alongside the
KaTeX licence. These notices also accompany fonts embedded in HTML exports.

The backend wheel has no runtime Python dependencies. Python, Lean, Lake project
dependencies, Mathlib and the optional `glitnir-forseti` package with its own
dependencies (Lark, Z3, simulation packages) are separate installations and
retain their respective licences. They are not included in the thin backend wheel.
