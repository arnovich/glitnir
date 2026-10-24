"""Shared monkeypatch helpers for tests that stand in for the environment."""

from typing import Any, Callable

import pytest

from glitnir.notebook import source_preview
from glitnir.preview import snapshot


def patch_fingerprint(
    monkeypatch: pytest.MonkeyPatch, fake: Callable[..., Any]
) -> None:
    """Replace `environment_fingerprint` where the service and the snapshot use it.

    The service's runs call it through `source_preview`; the snapshot's
    `Fingerprints` calls it through `glitnir.preview.snapshot`.
    """
    monkeypatch.setattr(source_preview, "environment_fingerprint", fake)
    monkeypatch.setattr(snapshot, "environment_fingerprint", fake)
