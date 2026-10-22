"""OpenCode Go adapter and canonical display mapping tests."""

import json
from pathlib import Path
import time

from click.testing import CliRunner

from quse import opencode_go_quota, zai_quota
from quse.cli import app
from quse._shared import reset_at_to_iso
from quse.usage import normalize_usage_provider


OPENCODE_GO_PAYLOAD = {
    "usage": {
        "rolling": {
            "status": "ok",
            "percent": 17,
            "resetsAt": "2026-08-22T11:21:36.310Z",
        },
        "weekly": {
            "status": "ok",
            "percent": 7,
            "resetsAt": "2026-08-24T00:00:00.310Z",
        },
        "monthly": {
            "status": "ok",
            "percent": 3,
            "resetsAt": "2026-09-22T06:20:28.310Z",
        },
    }
}


def test_opencode_go_parse_native_three_windows() -> None:
    status = opencode_go_quota._parse_usage_response(OPENCODE_GO_PAYLOAD)

    assert status.rolling is not None
    assert status.rolling.used_percent == 17.0
    assert status.rolling.percent_remaining == 83.0
    assert reset_at_to_iso(status.rolling.reset_at) == "2026-08-22T11:21:36Z"
    assert status.weekly is not None
    assert status.weekly.used_percent == 7.0
    assert status.monthly is not None
    assert status.monthly.used_percent == 3.0
    assert status.limit_reached is False
    assert status.short_term is not None
    assert status.short_term.rolling is True


def test_opencode_go_normalizes_to_canonical_windows(monkeypatch) -> None:
    status = opencode_go_quota._parse_usage_response(OPENCODE_GO_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_opencode_go_quota", lambda: status)

    record = normalize_usage_provider("go")

    assert record["windows"]["5h"]["percent_remaining"] == 83.0
    assert reset_at_to_iso(record["windows"]["5h"]["reset_at"]) == (
        "2026-08-22T11:21:36Z"
    )
    assert record["windows"]["5h"]["rolling"] is True
    assert record["windows"]["7d"]["percent_remaining"] == 93.0
    assert reset_at_to_iso(record["windows"]["7d"]["reset_at"]) == (
        "2026-08-24T00:00:00Z"
    )
    assert record["windows"]["monthly"]["percent_remaining"] == 97.0
    assert reset_at_to_iso(record["windows"]["monthly"]["reset_at"]) == (
        "2026-09-22T06:20:28Z"
    )


def test_opencode_go_human_output_uses_canonical_window_names(monkeypatch) -> None:
    status = opencode_go_quota._parse_usage_response(OPENCODE_GO_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_opencode_go_quota", lambda: status)
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()

    result = CliRunner().invoke(app, ["go"])

    assert result.exit_code == 0
    assert result.output.startswith(
        "5h:\n"
        "    remaining: 83.0%\n"
        "    reset: 22-08-2026 11:21"
    )
    assert "7d:\n" in result.output
    assert "monthly:\n" in result.output


def test_opencode_go_reads_native_auth_entry(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "go-token"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setattr(
        opencode_go_quota, "read_console_access_token", lambda: None
    )

    assert opencode_go_quota._read_access_token(auth_path) == "go-token"


def test_opencode_go_api_key_environment_overrides_auth(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "file-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "env-token")
    monkeypatch.setattr(
        opencode_go_quota, "read_console_access_token", lambda: "console-token"
    )

    assert opencode_go_quota._read_access_token(auth_path) == "env-token"


def test_opencode_go_prefers_console_device_token_over_stale_file_key(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "stale-file-token"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setattr(
        opencode_go_quota, "read_console_access_token", lambda: "console-token"
    )

    assert opencode_go_quota._read_access_tokens(auth_path) == [
        "console-token",
        "stale-file-token",
    ]
    assert opencode_go_quota._read_access_token(auth_path) == "console-token"


def test_opencode_go_falls_back_to_next_token_on_401(monkeypatch) -> None:
    from urllib.error import HTTPError

    monkeypatch.setattr(
        opencode_go_quota,
        "_read_access_tokens",
        lambda auth_path=None: ["console-token", "legacy-token"],
    )

    def fake_payload(token: str, *, timeout: float = 15.0) -> dict:
        if token == "console-token":
            raise HTTPError(
                "https://opencode.ai/zen/go/v1/usage",
                401,
                "Unauthorized",
                {},
                None,
            )
        return OPENCODE_GO_PAYLOAD

    monkeypatch.setattr(opencode_go_quota, "_fetch_usage_payload", fake_payload)

    status = opencode_go_quota._fetch_usage()

    assert status.error is None
    assert status.rolling is not None
    assert status.rolling.used_percent == 17.0


def test_opencode_go_reports_error_when_all_tokens_rejected(
    monkeypatch,
) -> None:
    from urllib.error import HTTPError

    monkeypatch.setattr(
        opencode_go_quota,
        "_read_access_tokens",
        lambda auth_path=None: ["console-token", "legacy-token"],
    )

    def fake_payload(token: str, *, timeout: float = 15.0) -> dict:
        raise HTTPError(
            "https://opencode.ai/zen/go/v1/usage", 401, "Unauthorized", {}, None
        )

    monkeypatch.setattr(opencode_go_quota, "_fetch_usage_payload", fake_payload)

    status = opencode_go_quota._fetch_usage()

    assert status.error is not None
    assert "401" in status.error


def test_opencode_go_no_credentials_without_any_token(monkeypatch) -> None:
    monkeypatch.setattr(
        opencode_go_quota, "_read_access_tokens", lambda auth_path=None: []
    )

    status = opencode_go_quota._fetch_usage()

    assert status.error == "no-credentials"


def test_zai_prefers_opencode_auth(monkeypatch) -> None:
    def fake_read(provider: str) -> str | None:
        if provider == "zai-coding-plan":
            return "opencode-zai-token"
        return None

    monkeypatch.setattr(zai_quota, "read_auth_token", fake_read)

    config = zai_quota._read_zai_config()

    assert config.token == "opencode-zai-token"


def test_zai_falls_back_to_goz_config(tmp_path: Path, monkeypatch) -> None:
    goz_path = tmp_path / "config.json"
    goz_path.write_text(
        json.dumps({"zai_token": "goz-token", "timeout": 9}),
        encoding="utf-8",
    )
    monkeypatch.setattr(zai_quota, "read_auth_token", lambda provider: None)
    monkeypatch.setattr(zai_quota, "_DEFAULT_CONFIG_PATH", goz_path)

    config = zai_quota._read_zai_config()

    assert config.token == "goz-token"
    assert config.timeout == 9.0
