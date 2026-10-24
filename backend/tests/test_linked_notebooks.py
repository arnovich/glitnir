"""Another notebook of the same project is served read-only by its prose link.

The preview reads the linked file under the session capability, through no
symbolic link, below the notebook's project root and within the same size limit
as the notebook itself. It shows the linked notebook's own saved results and
runs nothing for it.
"""

import http.client
import json
import os
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from glitnir.lean.fingerprint import environment_fingerprint
from glitnir.notebook.check_records import (
    load_records,
    records_from_check,
    records_path,
    save_records,
)
from glitnir.notebook.server import make_server
from glitnir.notebook.source_preview import SourcePreviewService
from glitnir.preview.linked import (
    MAX_SOURCE_BYTES,
    NotebookLinkError,
    read_linked_notebook,
)

MAIN = "/-! # Scalar decay\n\nSee [the states](ThreeState.lean). -/\n"
LINKED = (
    "/-! # Three states\n\nA system with three states. -/\n\n"
    "namespace Notes\ntheorem settled : True := by\n  trivial\nend Notes\n"
)
RESULT = {
    "status": "checked_snapshot",
    "source_sha256": "s",
    "environment_sha256": "e",
    "withheld": None,
    "theorems": [
        {
            "name": "Notes.settled",
            "status": "checked",
            "axioms": [],
            "start_line": 6,
            "end_line": 7,
            "proposition_sha256": "p",
        }
    ],
}


@pytest.fixture
def project(tmp_path) -> Path:
    """A loose project: the main notebook and a sibling with saved results."""
    (tmp_path / "ScalarDecay.lean").write_text(MAIN, encoding="utf-8")
    linked = tmp_path / "ThreeState.lean"
    linked.write_text(LINKED, encoding="utf-8")
    fingerprint = environment_fingerprint(linked, LINKED)["sha256"]
    save_records(linked, records_from_check(LINKED, RESULT, fingerprint))
    return tmp_path


@pytest.fixture
def server(project):
    service = SourcePreviewService(project / "ScalarDecay.lean")
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    service.close()


def _get(server, link: str | None = None, *, query: str | None = None, app=True):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"X-Glitnir": server.RequestHandlerClass.auth_token} if app else {}
    path = "/api/linked?" + (query if query is not None else f"path={quote(link)}")
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, body


def test_a_sibling_is_served_with_its_own_saved_results(server, project) -> None:
    before = records_path(project / "ThreeState.lean").read_bytes()
    status, snapshot = _get(server, "ThreeState.lean")
    assert status == 200
    assert snapshot["file"] == "ThreeState.lean"
    assert snapshot["preview"]["linked"] == {
        "path": "ThreeState.lean",
        "file": "ThreeState.lean",
        "compared": True,
    }
    assert snapshot["preview"]["check"] is None
    [claim] = snapshot["project_claims"]
    assert (claim["name"], claim["status"], claim["current"]) == (
        "Notes.settled",
        "checked",
        True,
    )
    assert "prose" in [cell["kind"] for cell in snapshot["cells"]]
    # Reading it records nothing for either notebook.
    assert records_path(project / "ThreeState.lean").read_bytes() == before
    assert load_records(project / "ScalarDecay.lean") is None


def test_a_notebook_in_a_subdirectory_is_served(server, project) -> None:
    (project / "more").mkdir()
    (project / "more" / "Other.lean").write_text("def x := 1\n", encoding="utf-8")
    status, snapshot = _get(server, "more/Other.lean")
    assert status == 200
    assert snapshot["file"] == "Other.lean"
    assert snapshot["project_claims"] == []


def test_linked_reads_need_the_session_capability(server) -> None:
    status, body = _get(server, "ThreeState.lean", app=False)
    assert status == 403
    assert "file" not in body


@pytest.mark.parametrize(
    "query",
    ["", "path=", "path=A.lean&path=B.lean", "path=ThreeState.lean&notebook=Other"],
)
def test_a_linked_read_names_exactly_one_path(server, query) -> None:
    assert _get(server, query=query)[0] == 400


@pytest.mark.parametrize(
    "link",
    [
        "../ThreeState.lean",
        "more/../ThreeState.lean",
        "./ThreeState.lean",
        "/etc/ThreeState.lean",
        "more//ThreeState.lean",
        "more\\ThreeState.lean",
        "ThreeState.glitnir.json",
        "ThreeState",
        "Notes.txt",
        ".lean",
        "A" * 1100 + ".lean",
    ],
)
def test_links_that_could_leave_the_project_or_name_no_lean_file_are_refused(
    server, link
) -> None:
    status, body = _get(server, link)
    assert status == 400
    assert "relative" in body["error"]


def test_missing_and_non_regular_notebooks_are_not_found(server, project) -> None:
    (project / "Folder.lean").mkdir()
    for link in ("Missing.lean", "Folder.lean", "nowhere/Missing.lean"):
        assert _get(server, link)[0] == 404, link


def test_symbolic_links_are_never_followed(server, project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "Secret.lean").write_text("def secret := 1\n", encoding="utf-8")
    os.symlink(project / "ThreeState.lean", project / "Alias.lean")
    os.symlink(outside / "Secret.lean", project / "Secret.lean")
    os.symlink(outside, project / "away", target_is_directory=True)
    os.symlink(project, project / "here", target_is_directory=True)
    for link in (
        "Alias.lean",
        "Secret.lean",
        "away/Secret.lean",
        "here/ThreeState.lean",
    ):
        status, body = _get(server, link)
        assert status == 404, link
        assert "secret" not in json.dumps(body), link


def test_a_notebook_above_the_size_limit_is_refused(server, project) -> None:
    big = project / "Big.lean"
    big.write_bytes(b"-- " + b"x" * MAX_SOURCE_BYTES + b"\n")
    status, body = _get(server, "Big.lean")
    assert status == 413
    assert "16 MiB" in body["error"]


def test_links_resolve_under_the_lake_root(tmp_path) -> None:
    root = tmp_path / "proj"
    (root / "Notes").mkdir(parents=True)
    (root / "lakefile.toml").write_text('name = "proj"\n', encoding="utf-8")
    (root / "Notes" / "A.lean").write_text("def a := 1\n", encoding="utf-8")
    (root / "Notes" / "B.lean").write_text("def b := 2\r\n", encoding="utf-8")
    path, text = read_linked_notebook(root / "Notes" / "A.lean", "B.lean")
    assert path == root / "Notes" / "B.lean"
    assert text == "def b := 2\n"
    with pytest.raises(NotebookLinkError) as refused:
        read_linked_notebook(root / "Notes" / "A.lean", "../Notes/B.lean")
    assert refused.value.status == 400


def test_symbolic_link_loops_are_not_found_without_naming_a_path(
    server, project
) -> None:
    os.symlink("Loop.lean", project / "Loop.lean")
    os.symlink("b", project / "a")
    os.symlink("a", project / "b")
    for link in ("Loop.lean", "a/Loop.lean"):
        status, body = _get(server, link)
        assert status == 404, link
        assert str(project) not in json.dumps(body)
        assert "/" not in body["error"]
    assert _get(server, "ThreeState.lean")[0] == 200, "the server still answers"


def test_a_fifo_is_not_found(server, project) -> None:
    os.mkfifo(project / "Pipe.lean")
    assert _get(server, "Pipe.lean")[0] == 404


def test_the_previewed_notebook_is_never_its_own_linked_notebook(
    server, project
) -> None:
    status, body = _get(server, "ScalarDecay.lean")
    assert status == 404
    assert "itself" in body["error"]
    os.link(project / "ScalarDecay.lean", project / "Hard.lean")
    assert _get(server, "Hard.lean")[0] == 404


def test_a_link_must_spell_the_file_as_its_directory_does(server, project) -> None:
    # On a case-insensitive file system these would open existing files.
    for link in ("SCALARDECAY.lean", "scalardecay.lean", "threestate.lean"):
        assert _get(server, link)[0] == 404, link
    (project / "more").mkdir()
    (project / "more" / "Other.lean").write_text("def x := 1\n", encoding="utf-8")
    assert _get(server, "MORE/Other.lean")[0] == 404
    assert _get(server, "more/Other.lean")[0] == 200


def test_a_nested_project_is_shown_as_saved_and_never_fingerprinted(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "proj"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "lakefile.toml").write_text('name = "proj"\n', encoding="utf-8")
    (nested / "lakefile.toml").write_text('name = "nested"\n', encoding="utf-8")
    (root / "Main.lean").write_text(MAIN, encoding="utf-8")
    linked = nested / "ThreeState.lean"
    linked.write_text(LINKED, encoding="utf-8")
    save_records(linked, records_from_check(LINKED, RESULT, "5" * 64))
    from glitnir.notebook import source_preview

    def refuse(*args, **kwargs):
        raise AssertionError("a nested project must not be fingerprinted")

    monkeypatch.setattr(source_preview, "environment_fingerprint", refuse)
    service = SourcePreviewService(root / "Main.lean")
    snapshot = service.linked("nested/ThreeState.lean")
    assert snapshot["preview"]["linked"]["compared"] is False
    [claim] = snapshot["project_claims"]
    assert (claim["status"], claim["current"]) == ("checked", None)


def test_uses_of_other_notebooks_link_to_readable_files_only(project) -> None:
    """A use names its notebook by a link only when that file can be read here.

    Missing files, symbolic links and names that are no file name are shown
    as text; uses of this notebook's own claims need no link.
    """
    main = project / "ScalarDecay.lean"
    text = "theorem uses_all : True := by\n  trivial\n"
    main.write_text(text, encoding="utf-8")
    (project / "more").mkdir()
    (project / "more" / "Other.lean").write_text("def x := 1\n", encoding="utf-8")
    (project / "Linked.lean").symlink_to(project / "ThreeState.lean")
    uses = [
        {"module": "Main", "name": "uses_all", "in_notebook": True},
        {"module": "ThreeState", "name": "Notes.settled", "in_notebook": False},
        {"module": "more.Other", "name": "more.lemma", "in_notebook": False},
        {"module": "Missing", "name": "gone", "in_notebook": False},
        {"module": "Linked", "name": "Notes.settled", "in_notebook": False},
        {"module": "«odd name»", "name": "x", "in_notebook": False},
    ]
    result = {
        **RESULT,
        "theorems": [
            {
                **RESULT["theorems"][0],
                "name": "uses_all",
                "start_line": 1,
                "end_line": 2,
                "uses": uses,
            }
        ],
    }
    fingerprint = environment_fingerprint(main, text)["sha256"]
    save_records(main, records_from_check(text, result, fingerprint))
    service = SourcePreviewService(main)
    try:
        [claim] = service.snapshot()["project_claims"]
    finally:
        service.close()
    assert [use.get("path") for use in claim["uses"]] == [
        None,
        "ThreeState.lean",
        "more/Other.lean",
        None,
        None,
        None,
    ]
    assert load_records(main)["claims"][0]["uses"] == uses, "links are not saved"
