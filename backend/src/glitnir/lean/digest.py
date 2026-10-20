"""What an environment fingerprint looks like."""

import re
from typing import Any

# An environment fingerprint is a SHA-256 digest; anything else, '' included
# (what a failed fingerprint reads as), matches nothing and is never saved.
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def fingerprint_ok(fingerprint: Any) -> bool:
    """Whether `fingerprint` is an environment digest a result can match."""
    return (
        isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint) is not None
    )
