"""What the checker returns: the declared shapes and their helpers."""

from glitnir.lean.results import with_message


def test_with_message_keeps_every_other_key():
    """Replacing the message touches nothing else, for either kind of result."""
    checked = {
        "status": "checked_snapshot",
        "message": "old",
        "theorems": [],
        "replay": "full",
    }
    unavailable = {"status": "unavailable", "message": "old", "theorems": []}
    for result in (checked, unavailable):
        changed = with_message(result, "new")
        assert changed == {**result, "message": "new"}
        assert result["message"] == "old"
