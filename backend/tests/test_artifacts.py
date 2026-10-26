"""Lake's nested `importArts` entries are read strictly, in one shape."""

import threading

import pytest

from glitnir.lean import project_inspection as inspection
from glitnir.lean.artifacts import (
    artifact_files,
    map_artifacts,
    olean_path,
    valid_artifacts,
)

#: A module's entry, as Lake 4.35 writes it for `import all`.
MODULE = [["A.olean", "A.olean.server", "A.olean.private"], ["A.ir.sig", "A.ir"]]
#: A file outside the module system: its `.olean` alone.
PLAIN = [["A.olean"]]


@pytest.mark.parametrize(
    "value",
    [
        PLAIN,
        MODULE,
        [["A.olean", "A.olean.server"], ["A.ir.sig", "A.ir"]],
        [["A.olean"], ["A.ir.sig"]],
    ],
)
def test_the_nested_shape_is_valid(value):
    assert valid_artifacts(value)


@pytest.mark.parametrize(
    "value",
    [
        # Lean 4.32's flat shape, `[olean, ir?, oleanServer?, oleanPrivate?]`.
        ["A.olean"],
        ["A.olean", "A.ir", "A.olean.server", "A.olean.private"],
        [],
        [[]],
        [["A.olean"], []],
        [["A.olean", "B", "C", "D"]],
        [["A.olean"], ["A.ir.sig", "A.ir", "X"]],
        [["A.olean"], ["A.ir.sig"], ["X"]],
        [["A.olean", 1]],
        [["A.olean"], "A.ir.sig"],
        "A.olean",
        None,
        {"olean": "A.olean"},
    ],
)
def test_any_other_shape_is_refused(value):
    assert not valid_artifacts(value)
    for read in (artifact_files, olean_path, lambda v: map_artifacts(v, str)):
        with pytest.raises(ValueError, match="invalid import artifacts"):
            read(value)


def test_each_path_stands_for_its_suffix():
    assert artifact_files(MODULE) == [
        ("A.olean", ".olean"),
        ("A.olean.server", ".olean.server"),
        ("A.olean.private", ".olean.private"),
        ("A.ir.sig", ".ir.sig"),
        ("A.ir", ".ir"),
    ]
    assert artifact_files(PLAIN) == [("A.olean", ".olean")]


def test_rewriting_keeps_the_shape():
    seen = []

    def change(path: str, suffix: str) -> str:
        seen.append(suffix)
        return f"/frozen/B{suffix}"

    assert map_artifacts(MODULE, change) == [
        ["/frozen/B.olean", "/frozen/B.olean.server", "/frozen/B.olean.private"],
        ["/frozen/B.ir.sig", "/frozen/B.ir"],
    ]
    assert seen == [".olean", ".olean.server", ".olean.private", ".ir.sig", ".ir"]
    assert map_artifacts(PLAIN, change) == [["/frozen/B.olean"]]


def test_the_olean_is_the_first_path():
    assert olean_path(MODULE) == "A.olean"
    assert olean_path(PLAIN) == "A.olean"


def _setup(import_arts):
    return dict(
        name="A",
        isModule=False,
        importArts=import_arts,
        options={},
        plugins=[],
        dynlibs=[],
    )


def test_inspection_refuses_the_flat_shape(tmp_path):
    (tmp_path / "B.olean").write_bytes(b"")
    with pytest.raises(ValueError, match="invalid import artifacts"):
        inspection._validate_setup(
            _setup({"B": [str(tmp_path / "B.olean")]}), tmp_path, tmp_path
        )


def test_inspection_confines_every_nested_path(tmp_path):
    """The IR group's paths are checked like the `.olean`'s."""
    root = tmp_path / "project"
    root.mkdir()
    for name in ("B.olean", "B.olean.server", "B.ir.sig"):
        (root / name).write_bytes(b"")
    outside = tmp_path / "B.ir"
    outside.write_bytes(b"")
    good = [[str(root / "B.olean"), str(root / "B.olean.server")]]
    assert inspection._validate_setup(_setup({"B": good}), root, root)
    nested = good + [[str(root / "B.ir.sig"), str(outside)]]
    with pytest.raises(ValueError, match="outside"):
        inspection._validate_setup(_setup({"B": nested}), root, root)
    (root / "B.ir.sig").unlink()
    with pytest.raises(ValueError, match="regular"):
        inspection._validate_setup(
            _setup({"B": good + [[str(root / "B.ir.sig")]]}), root, root
        )


def test_the_snapshot_captures_every_part_in_its_place(
    tmp_path, project_snapshot_inputs
):
    """A module's `.ir.sig` and `.ir` are frozen beside its `.olean` parts."""
    from glitnir.lean.project_snapshot import capture

    root, toolchain, source, artifact, setup = project_snapshot_inputs
    parts = [[str(artifact)], []]
    for suffix in (".olean.server", ".olean.private"):
        (root / f"Dependency{suffix}").write_bytes(suffix.encode())
        parts[0].append(str(root / f"Dependency{suffix}"))
    for suffix in (".ir.sig", ".ir"):
        (root / f"Dependency{suffix}").write_bytes(suffix.encode())
        parts[1].append(str(root / f"Dependency{suffix}"))
    setup["importArts"] = {"Dependency": parts}
    captured = capture(
        tmp_path / "s", source, setup, toolchain, root, threading.Event()
    )
    frozen = captured.setup["importArts"]["Dependency"]
    suffixes = [".olean", ".olean.server", ".olean.private"], [".ir.sig", ".ir"]
    assert frozen == [
        [str(captured.artifacts / f"Dependency{suffix}") for suffix in group]
        for group in suffixes
    ]
    for suffix in (".olean.server", ".olean.private", ".ir.sig", ".ir"):
        assert (captured.artifacts / f"Dependency{suffix}").read_bytes() == (
            suffix.encode()
        )
