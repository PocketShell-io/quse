"""Quota CLI startup must not require a POSIX-only provider dependency."""
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from quse import claude_quota


def test_aggregate_cli_import_without_fcntl(tmp_path):
    script = """
import importlib.abc, sys
class NoFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fcntl":
            raise ModuleNotFoundError("fcntl deliberately unavailable")
sys.modules.pop("fcntl", None)
sys.meta_path.insert(0, NoFcntl())
import quse
import quse.cli
assert "fcntl" not in sys.modules
"""
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("existing", [b"", b"existing byte"])
@pytest.mark.parametrize("body_fails", [False, True])
def test_windows_cache_lock_reserves_first_byte_and_closes(tmp_path, monkeypatch,
                                                         existing, body_fails):
    path = tmp_path / "usage.json"
    lock = path.with_suffix(".lock")
    lock.write_bytes(existing)
    calls = []
    handles = []
    real_open = open

    def tracked_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        handles.append(handle)
        return handle

    def locking(fd, mode, count):
        handle = handles[-1]
        assert fd == handle.fileno()
        calls.append((mode, count, handle.tell(), lock.stat().st_size))

    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(
        LK_LOCK=1, locking=locking))
    monkeypatch.setattr(claude_quota, "open", tracked_open, raising=False)
    # Patch only this module's OS view, leaving pathlib and subprocess native.
    monkeypatch.setattr(claude_quota, "os", types.SimpleNamespace(
        name="nt", SEEK_END=os.SEEK_END))
    try:
        with claude_quota._disk_cache_lock(path):
            assert not handles[-1].closed
            if body_fails:
                raise RuntimeError("body failed")
    except RuntimeError:
        assert body_fails
    assert len(calls) == 1
    assert calls[0][:3] == (1, 1, 0)
    assert calls[0][3] >= 1
    assert handles[-1].closed
    assert lock.read_bytes() == (existing or b"\0")


def test_windows_lock_failure_never_enters_probe(tmp_path, monkeypatch):
    handles = []
    real_open = open

    def tracked_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        handles.append(handle)
        return handle

    def refused(*args):
        raise OSError("lock unavailable")

    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(
        LK_LOCK=1, locking=refused))
    monkeypatch.setattr(claude_quota, "open", tracked_open, raising=False)
    monkeypatch.setattr(claude_quota, "os", types.SimpleNamespace(
        name="nt", SEEK_END=os.SEEK_END))
    with pytest.raises(OSError, match="lock unavailable"):
        with claude_quota._disk_cache_lock(tmp_path / "usage.json"):
            pytest.fail("Unserialized quota probe was entered")
    assert handles[-1].closed
