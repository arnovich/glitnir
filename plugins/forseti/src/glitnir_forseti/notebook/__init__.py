"""What the Forseti providers put into a Glitnir notebook.

`decisions` turns a decided goal into the self-contained Lean block the
decision provider proposes, `readable` states that goal as the property it is
about, and `evidence` reads and runs ``-- @evidence`` directives for the
evidence provider. Glitnir's core reads the notebook and its directives; these
modules grant no proof authority, which only the project's Lean check has.
"""
