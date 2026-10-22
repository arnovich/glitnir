"""CI's plugin-live shards run the whole suite exactly once."""

import importlib.util
import json
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools/live_shards.py"
_SPEC = importlib.util.spec_from_file_location("live_shards", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
shards = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shards)


def test_the_shards_partition_the_suite() -> None:
    names = [*shards.SHARDS, shards.REST]
    assigned = [module for name in names for module in shards.shard_modules(name)]
    assert sorted(assigned) == shards.test_modules()
    assert len(set(assigned)) == len(assigned)
    assert all(shards.shard_modules(name) for name in names)


def test_a_missing_or_doubly_listed_module_is_refused(monkeypatch) -> None:
    monkeypatch.setitem(shards.SHARDS, "gone", ("tests/test_absent.py",))
    with pytest.raises(ValueError, match="do not exist"):
        shards.shard_modules(shards.REST)
    monkeypatch.setitem(shards.SHARDS, "gone", ("tests/test_stepped_lean.py",))
    with pytest.raises(ValueError, match="more than one shard"):
        shards.shard_modules("stepped")


def test_the_command_prints_the_matrix_and_each_shard(capsys) -> None:
    assert shards.main(["--names"]) == 0
    assert json.loads(capsys.readouterr().out) == [*shards.SHARDS, shards.REST]
    assert shards.main(["stepped"]) == 0
    assert capsys.readouterr().out.split() == ["tests/test_stepped_lean.py"]
    assert shards.main(["unknown"]) == 2
    assert shards.main([]) == 2
