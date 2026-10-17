"""The reviewed Lean program that reports a module's constants and axioms.

Project checking runs it after replaying a snapshot; it lives apart from any
particular Lean release.
"""

from pathlib import Path

REPORT_PROGRAM = Path(__file__).with_name("AxiomReport.lean")
#: The reviewed report program's bytes; a changed file is refused.
REPORT_SHA256 = "3ac14575ee8f8485d2c41fcdad0deae24493cc4e2c95b837b55b6d231fa8bd59"
