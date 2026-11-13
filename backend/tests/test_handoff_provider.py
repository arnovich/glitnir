"""Handoff-only packages remain valid alongside existing provider kinds."""

from types import SimpleNamespace

from glitnir.providers import HandoffProvider, load_providers
from glitnir.providers.handoff import load_handoffs


def test_handoff_does_not_change_existing_provider_tuple(monkeypatch):
    """Existing four-table consumers see no false provider load error."""
    provider = HandoffProvider("demo", "Send notebook", lambda p, t: p, lambda p, t: p)
    entry = SimpleNamespace(name="demo", load=lambda: lambda: [provider])
    monkeypatch.setattr("glitnir.providers._entry_points", lambda: [entry])
    monkeypatch.setattr(
        "glitnir.providers.handoff.metadata.entry_points", lambda **k: [entry]
    )
    views, evidence, decisions, errors = load_providers()
    assert not (views or evidence or decisions or errors)
    assert load_handoffs() == {"demo": provider}


def test_handoff_discovery_advertises_optional_status(monkeypatch, capsys):
    """The editor offers refresh only when the installed provider supports it."""
    import io
    import json

    from glitnir.service import handoff

    provider = HandoffProvider("demo", "Send notebook", lambda p, t: p, lambda p, t: p)
    monkeypatch.setattr(handoff, "load_handoffs", lambda: {"demo": provider})
    monkeypatch.setattr("sys.stdin", io.StringIO('{"action":"list"}'))
    handoff.main()
    assert json.loads(capsys.readouterr().out) == [
        {"name": "demo", "label": "Send notebook", "supportsStatus": False}
    ]
