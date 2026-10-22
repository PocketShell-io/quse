"""OpenCode Console/device credential selection tests."""

import json
import subprocess

from quse import _opencode_auth


def _device_entry(*, active: bool = True, access: str = "st_token") -> dict:
    return {
        "id": "cred_test",
        "integrationID": "opencode",
        "label": "Default",
        "active": active,
        "value": {
            "type": "oauth",
            "methodID": "device",
            "refresh": "rt_token",
            "access": access,
            "expires": "1795233812409",
            "metadata": {"server": "https://opencode.ai/console"},
        },
    }


def test_select_device_token_prefers_active() -> None:
    entries = [
        _device_entry(active=False, access="st_old"),
        _device_entry(active=True, access="st_new"),
    ]

    assert _opencode_auth._select_device_access_token(entries) == "st_new"


def test_select_device_token_uses_inactive_when_nothing_active() -> None:
    entries = [_device_entry(active=False, access="st_only")]

    assert _opencode_auth._select_device_access_token(entries) == "st_only"


def test_select_device_token_skips_non_device_credentials() -> None:
    entries = [
        {
            "id": "cred_key",
            "integrationID": "opencode-go",
            "label": "API key",
            "active": True,
            "value": {"type": "key", "key": "sk-stale"},
        },
        _device_entry(active=True, access="st_device"),
    ]

    assert _opencode_auth._select_device_access_token(entries) == "st_device"


def test_select_device_token_returns_none_without_device_oauth() -> None:
    assert _opencode_auth._select_device_access_token([]) is None
    assert _opencode_auth._select_device_access_token([{"nope": True}]) is None
    assert (
        _opencode_auth._select_device_access_token(
            [_device_entry(active=True, access="   ")]
        )
        is None
    )


def test_read_console_access_token_parses_export(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = json.dumps([_device_entry(active=True, access="st_live")])
        stderr = ""

    monkeypatch.setattr(
        _opencode_auth, "_find_opencode_executable", lambda: "/usr/bin/opencode"
    )
    monkeypatch.setattr(
        _opencode_auth.subprocess, "run", lambda *args, **kwargs: Result()
    )

    assert _opencode_auth.read_console_access_token() == "st_live"


def test_read_console_access_token_fails_open(monkeypatch) -> None:
    monkeypatch.setattr(
        _opencode_auth, "_find_opencode_executable", lambda: "/usr/bin/opencode"
    )

    def boom(*args, **kwargs):
        raise OSError("no service")

    monkeypatch.setattr(_opencode_auth.subprocess, "run", boom)

    assert _opencode_auth.read_console_access_token() is None


def test_read_console_access_token_without_binary(monkeypatch) -> None:
    monkeypatch.setattr(
        _opencode_auth, "_find_opencode_executable", lambda: None
    )

    assert _opencode_auth.read_console_access_token() is None


def test_read_console_access_token_rejects_bad_export(monkeypatch) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "not logged in"

    monkeypatch.setattr(
        _opencode_auth, "_find_opencode_executable", lambda: "/usr/bin/opencode"
    )
    monkeypatch.setattr(
        _opencode_auth.subprocess, "run", lambda *args, **kwargs: Result()
    )

    assert _opencode_auth.read_console_access_token() is None


def test_find_opencode_executable_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENCODE_BIN", "definitely-not-a-binary-xyz")
    monkeypatch.setattr(_opencode_auth.shutil, "which", lambda name: None)

    assert _opencode_auth._find_opencode_executable() is None


def test_export_timeout_is_bounded() -> None:
    assert _opencode_auth._EXPORT_TIMEOUT_SECONDS <= 30.0


def test_subprocess_timeout_maps_to_none(monkeypatch) -> None:
    monkeypatch.setattr(
        _opencode_auth, "_find_opencode_executable", lambda: "/usr/bin/opencode"
    )

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=10.0)

    monkeypatch.setattr(_opencode_auth.subprocess, "run", slow)

    assert _opencode_auth.read_console_access_token() is None
