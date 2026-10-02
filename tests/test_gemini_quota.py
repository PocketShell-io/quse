"""Tests for Gemini / Antigravity quota checking via Antigravity CLI."""

import json
from pathlib import Path
import subprocess
import time

from click.testing import CliRunner

from quse import gemini_quota
from quse._shared import reset_at_to_iso
from quse.cli import app
from quse.gemini_quota import (
    GeminiQuotaBucket,
    GeminiQuotaGroup,
    GeminiQuotaStatus,
    _find_agy_executable,
    _parse_quota_response,
    _parse_tsv_response,
    check_gemini_quota,
    reset_cache,
)


GEMINI_TEST_PAYLOAD = {
    "command": {
        "description": "Show quota for all models.",
        "groups": [
            {
                "name": "Gemini Models",
                "description": "Quota for Gemini family models",
                "buckets": [
                    {
                        "id": "gemini-5h",
                        "name": "5 Hour Quota",
                        "description": "5 hour quota",
                        "remaining_fraction": 0.85,
                        "reset_time": "2026-10-02T15:00:00Z",
                        "window": "5h",
                    },
                    {
                        "id": "gemini-weekly",
                        "name": "Weekly Quota",
                        "description": "Weekly quota",
                        "remaining_fraction": 0.90,
                        "reset_time": "2026-10-09T12:00:00Z",
                        "window": "weekly",
                    },
                ],
            }
        ],
    }
}


def test_gemini_quota_bucket_properties():
    bucket = GeminiQuotaBucket(
        id="test-bucket",
        name="Test Bucket",
        window="5h",
        remaining_fraction=0.754,
        reset_time="2026-10-02T12:00:00Z",
    )
    assert bucket.percent_remaining == 75.4
    assert bucket.used_percent == 24.6
    assert bucket.reset_at is not None
    assert reset_at_to_iso(bucket.reset_at) == "2026-10-02T12:00:00Z"

    empty_bucket = GeminiQuotaBucket()
    assert empty_bucket.percent_remaining is None
    assert empty_bucket.used_percent is None
    assert empty_bucket.reset_at is None


def test_gemini_quota_bucket_fraction_clamping():
    overflow_bucket = GeminiQuotaBucket(remaining_fraction=1.5)
    assert overflow_bucket.percent_remaining == 100.0
    assert overflow_bucket.used_percent == 0.0

    underflow_bucket = GeminiQuotaBucket(remaining_fraction=-0.2)
    assert underflow_bucket.percent_remaining == 0.0
    assert underflow_bucket.used_percent == 100.0


def test_gemini_quota_status_limit_reached():
    ok_status = GeminiQuotaStatus(
        groups=[
            GeminiQuotaGroup(
                name="Gemini Models",
                buckets=[
                    GeminiQuotaBucket(id="5h", remaining_fraction=0.5),
                    GeminiQuotaBucket(id="weekly", remaining_fraction=0.8),
                ],
            )
        ]
    )
    assert ok_status.limit_reached is False

    exhausted_status = GeminiQuotaStatus(
        groups=[
            GeminiQuotaGroup(
                name="Gemini Models",
                buckets=[
                    GeminiQuotaBucket(id="5h", remaining_fraction=0.0),
                    GeminiQuotaBucket(id="weekly", remaining_fraction=0.8),
                ],
            )
        ]
    )
    assert exhausted_status.limit_reached is True

    error_status = GeminiQuotaStatus(
        groups=[
            GeminiQuotaGroup(
                name="Gemini Models",
                buckets=[GeminiQuotaBucket(id="5h", remaining_fraction=0.0)],
            )
        ],
        error="CLI failed",
    )
    assert error_status.limit_reached is False


def test_gemini_quota_status_find_bucket_and_windows():
    status = _parse_quota_response(GEMINI_TEST_PAYLOAD)

    assert status.short_term is not None
    assert status.short_term.percent_remaining == 85.0
    assert status.short_term.window == "5h"
    assert reset_at_to_iso(status.short_term.reset_at) == "2026-10-02T15:00:00Z"

    assert status.long_term is not None
    assert status.long_term.percent_remaining == 90.0
    assert status.long_term.window == "7d"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-10-09T12:00:00Z"


def test_find_agy_executable_env_vars(monkeypatch, tmp_path: Path):
    custom_bin = tmp_path / "custom_agy"
    custom_bin.write_text("#!/bin/sh\nexit 0\n")
    custom_bin.chmod(0o755)

    monkeypatch.setenv("AGY_BIN", str(custom_bin))
    assert _find_agy_executable() == str(custom_bin)

    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.setenv("ANTIGRAVITY_BIN", str(custom_bin))
    assert _find_agy_executable() == str(custom_bin)

    monkeypatch.delenv("ANTIGRAVITY_BIN", raising=False)
    monkeypatch.setenv("GEMINI_BIN", str(custom_bin))
    assert _find_agy_executable() == str(custom_bin)


def test_find_agy_executable_shutil_which(monkeypatch):
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BIN", raising=False)
    monkeypatch.delenv("GEMINI_BIN", raising=False)

    def fake_which(cmd: str) -> str | None:
        if cmd == "agy":
            return "/opt/bin/agy"
        return None

    monkeypatch.setattr(gemini_quota.shutil, "which", fake_which)
    assert _find_agy_executable() == "/opt/bin/agy"


def test_find_agy_executable_default_search_paths(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BIN", raising=False)
    monkeypatch.delenv("GEMINI_BIN", raising=False)
    monkeypatch.setattr(gemini_quota.shutil, "which", lambda cmd: None)

    fake_agy = tmp_path / "agy"
    fake_agy.write_text("#!/bin/sh\nexit 0\n")
    fake_agy.chmod(0o755)

    monkeypatch.setattr(
        gemini_quota, "_DEFAULT_SEARCH_PATHS", (fake_agy,)
    )
    assert _find_agy_executable() == str(fake_agy)


def test_find_agy_executable_none_found(monkeypatch):
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BIN", raising=False)
    monkeypatch.delenv("GEMINI_BIN", raising=False)
    monkeypatch.setattr(gemini_quota.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(gemini_quota, "_DEFAULT_SEARCH_PATHS", ())

    assert _find_agy_executable() is None


def test_parse_quota_response_error_status():
    payload = {"status": "ERROR", "error": "Authentication token expired"}
    status = _parse_quota_response(payload)
    assert status.error == "Authentication token expired"
    assert status.groups == []


def test_parse_quota_response_non_dict():
    status = _parse_quota_response("not a dict")  # type: ignore[arg-type]
    assert status.error == "Invalid payload format"


def test_parse_quota_response_alternative_bucket_keys():
    payload = {
        "groups": [
            {
                "name": "Alternative Group",
                "buckets": [
                    {
                        "id": "b1",
                        "name": "5h limit",
                        "window": "5h",
                        "percent_remaining": 60,
                        "reset_at": "2026-10-02T18:00:00Z",
                    },
                    {
                        "id": "b2",
                        "name": "7d limit",
                        "window": "7d",
                        "used_percent": 30,
                        "reset_at": "2026-10-09T18:00:00Z",
                    },
                ],
            }
        ]
    }
    status = _parse_quota_response(payload)
    assert status.error is None
    assert len(status.groups) == 1
    assert status.short_term is not None
    assert status.short_term.percent_remaining == 60.0
    assert status.long_term is not None
    assert status.long_term.percent_remaining == 70.0


def test_parse_tsv_response_tab_and_spaces():
    tsv_text = (
        "Quota:\n"
        "Gemini Models\t5 Hour Quota\t85%\t2026-10-02T15:00:00Z\n"
        "Gemini Models   Weekly Quota   90%   2026-10-09T12:00:00Z\n"
    )
    status = _parse_tsv_response(tsv_text)
    assert len(status.groups) == 1
    group = status.groups[0]
    assert group.name == "Gemini Models"
    assert len(group.buckets) == 2
    assert status.short_term is not None
    assert status.short_term.percent_remaining == 85.0
    assert status.long_term is not None
    assert status.long_term.percent_remaining == 90.0


def test_fetch_quota_executable_not_found(monkeypatch):
    monkeypatch.setattr(gemini_quota, "_find_agy_executable", lambda: None)
    status = gemini_quota._fetch_quota()
    assert status.error == "agy not on PATH"


def test_fetch_quota_nonzero_exit_code(monkeypatch):
    monkeypatch.setattr(gemini_quota, "_find_agy_executable", lambda: "/usr/bin/agy")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="Subscription required",
        )

    monkeypatch.setattr(gemini_quota.subprocess, "run", fake_run)
    status = gemini_quota._fetch_quota()
    assert status.error == "Subscription required"


def test_fetch_quota_empty_stdout(monkeypatch):
    monkeypatch.setattr(gemini_quota, "_find_agy_executable", lambda: "/usr/bin/agy")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="   \n",
            stderr="",
        )

    monkeypatch.setattr(gemini_quota.subprocess, "run", fake_run)
    status = gemini_quota._fetch_quota()
    assert status.error == "agy returned empty output"


def test_fetch_quota_timeout(monkeypatch):
    monkeypatch.setattr(gemini_quota, "_find_agy_executable", lambda: "/usr/bin/agy")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=15.0)

    monkeypatch.setattr(gemini_quota.subprocess, "run", fake_run)
    status = gemini_quota._fetch_quota()
    assert "timeout after" in (status.error or "")


def test_fetch_quota_success(monkeypatch):
    monkeypatch.setattr(gemini_quota, "_find_agy_executable", lambda: "/usr/bin/agy")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(GEMINI_TEST_PAYLOAD),
            stderr="",
        )

    monkeypatch.setattr(gemini_quota.subprocess, "run", fake_run)
    status = gemini_quota._fetch_quota()
    assert status.error is None
    assert status.short_term is not None
    assert status.short_term.percent_remaining == 85.0


def test_check_gemini_quota_caching():
    reset_cache()
    calls = 0

    def fake_fetch():
        nonlocal calls
        calls += 1
        return GeminiQuotaStatus(checked_at=time.monotonic())

    first = check_gemini_quota(_fetch=fake_fetch)
    second = check_gemini_quota(_fetch=fake_fetch)
    assert first is second
    assert calls == 1

    reset_cache()
    third = check_gemini_quota(_fetch=fake_fetch)
    assert calls == 2
    assert third is not first
    reset_cache()


def test_gemini_cli_json_aliases(monkeypatch):
    status = _parse_quota_response(GEMINI_TEST_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_gemini_quota", lambda: status)

    for alias in ("gemini", "antigravity", "agy", "antigrativy"):
        result = CliRunner().invoke(app, [alias, "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "gemini" in payload
        record = payload["gemini"]
        assert record["status"] == "ok"
        assert record["windows"]["5h"]["percent_remaining"] == 85.0
        assert record["windows"]["5h"]["reset_at"] == "2026-10-02T15:00:00Z"
        assert record["windows"]["7d"]["percent_remaining"] == 90.0
        assert record["windows"]["7d"]["reset_at"] == "2026-10-09T12:00:00Z"
        assert len(record["details"]["groups"]) == 1
