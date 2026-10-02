import json
import os
import time

from click.testing import CliRunner

from quse._shared import UsageWindow
from quse.claude_quota import ClaudeQuotaStatus
from quse.cli import app
from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow, CodexResetCredit
from quse.copilot_quota import CopilotQuotaStatus
from quse.grok_quota import GrokQuotaStatus, GrokQuotaWindow, GrokReset
from quse.usage import (
    collect_usage,
    format_usage_line,
    normalize_usage_provider,
    usage_window_record,
)
from quse.zai_quota import ZaiQuotaStatus, ZaiQuotaWindow


def test_usage_single_provider_json(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            primary_window=CodexQuotaWindow(
                used_percent=40,
                reset_at="2026-04-30",
            ),
            secondary_window=CodexQuotaWindow(
                used_percent=25,
                reset_at="2026-05-01",
            ),
        ),
    )

    result = CliRunner().invoke(app, ["codex", "--json"])

    assert result.exit_code == 0
    record = json.loads(result.stdout)
    assert set(record) == {"codex"}
    assert record["codex"]["status"] == "ok"
    assert record["codex"]["windows"] == {
        "5h": {
            "percent_remaining": 60.0,
            "reset_at": "2026-04-30T00:00:00Z",
            "rolling": False,
        },
        "7d": {
            "percent_remaining": 75.0,
            "reset_at": "2026-05-01T00:00:00Z",
        },
        "monthly": {
            "percent_remaining": None,
            "reset_at": None,
        },
    }
    assert result.stdout.startswith("{\n  ")


def test_codex_json_includes_banked_resets(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            reset_credits=[
                CodexResetCredit(
                    status="available",
                    title="Full reset (Weekly + 5 hr)",
                    expires_at="2026-05-24T15:53:01Z",
                )
            ],
        ),
    )

    result = CliRunner().invoke(app, ["codex", "--json"])

    assert result.exit_code == 0
    details = json.loads(result.stdout)["codex"]["details"]
    assert details["banked_resets_available"] == 1
    assert details["banked_resets"] == [
        {
            "available": True,
            "expires_at": "2026-05-24T15:53:01Z",
            "label": "Full reset (Weekly + 5 hr)",
        }
    ]


def test_codex_human_usage_shows_banked_resets(monkeypatch):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            reset_credits=[
                CodexResetCredit(
                    status="available",
                    title="Full reset (Weekly + 5 hr)",
                    expires_at="2026-05-24T15:53:01Z",
                )
            ],
        ),
    )

    try:
        result = CliRunner().invoke(app, ["codex"])

        assert result.exit_code == 0
        assert (
            "banked_resets:\n    expires: 24-05-2026 15:53 (UTC) / overdue"
        ) in result.stdout
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_grok_json_includes_banked_resets(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_grok_quota",
        lambda: GrokQuotaStatus(
            resets=[
                GrokReset(
                    token_id="reset-1",
                    expires_at="2027-09-01T00:00:00Z",
                )
            ],
        ),
    )

    result = CliRunner().invoke(app, ["grok", "--json"])

    assert result.exit_code == 0
    details = json.loads(result.stdout)["grok"]["details"]
    assert details["banked_resets_available"] == 1
    assert details["banked_resets"] == [
        {
            "available": True,
            "expires_at": "2027-09-01T00:00:00Z",
            "label": "reset-1",
        }
    ]


def test_grok_human_usage_shows_banked_resets(monkeypatch):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(
        "quse.usage.check_grok_quota",
        lambda: GrokQuotaStatus(
            resets=[
                GrokReset(
                    token_id="reset-1",
                    expires_at="2026-05-24T15:53:01Z",
                )
            ],
        ),
    )

    try:
        result = CliRunner().invoke(app, ["grok"])

        assert result.exit_code == 0
        assert (
            "banked_resets:\n    expires: 24-05-2026 15:53 (UTC) / overdue"
        ) in result.stdout
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_codex_human_usage_omits_absent_short_term_window(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            primary_window=CodexQuotaWindow(
                used_percent=90,
                limit_window_seconds=604800,
            ),
            secondary_window=CodexQuotaWindow(present=False),
        ),
    )

    result = CliRunner().invoke(app, ["codex"])

    assert result.exit_code == 0
    assert result.stdout == (
        "7d:\n"
        "    remaining: 10.0%\n"
        "    reset: unknown\n"
    )


def test_usage_all_providers_json_is_keyed_by_provider(monkeypatch):
    records = [
        {"provider": "codex", "status": "ok", "error": None},
        {"provider": "claude", "status": "ok", "error": None},
    ]
    monkeypatch.setattr("quse.cli.collect_usage", lambda provider: records)

    result = CliRunner().invoke(app, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "claude": {"error": None, "status": "ok"},
        "codex": {"error": None, "status": "ok"},
    }
    assert result.stdout.startswith("{\n  ")


def test_usage_window_record_unified_schema():
    """Every provider record has the same canonical three-window shape."""
    record = usage_window_record(
        provider="claude",
        status="ok",
        windows={
            "5h": UsageWindow(percent_remaining=90.0),
            "7d": UsageWindow(percent_remaining=80.0),
        },
    )
    assert set(record) == {
        "provider",
        "status",
        "windows",
        "error",
        "details",
    }
    assert set(record["windows"]) == {"5h", "7d", "monthly"}
    assert set(record["windows"]["5h"]) == {
        "percent_remaining",
        "reset_at",
        "rolling",
    }
    assert set(record["windows"]["7d"]) == {"percent_remaining", "reset_at"}
    assert set(record["windows"]["monthly"]) == {
        "percent_remaining",
        "reset_at",
    }
    assert record["windows"]["5h"]["rolling"] is False


def test_provider_window_span_labels_are_authoritative():
    """quse owns the concrete span label for every provider window — downstream
    consumers must not re-derive it."""
    for status, expected in (
        (ClaudeQuotaStatus(), ("5h", "7d")),
        (CodexQuotaStatus(), ("5h", "7d")),
        (CopilotQuotaStatus(), (None, "monthly")),
        (ZaiQuotaStatus(), ("5h", "weekly")),
        (
            GrokQuotaStatus(
                weekly=GrokQuotaWindow(present=True, used_percent=10),
                monthly=GrokQuotaWindow(present=True, used_percent=20, limit=100),
            ),
            ("weekly", "monthly"),
        ),
    ):
        if status.short_term is None:
            short_window = None
        else:
            short_window = status.short_term.window
        if status.long_term is None:
            long_window = None
        else:
            long_window = status.long_term.window
        assert (short_window, long_window) == expected


def test_grok_build_alias_normalizes_to_grok(monkeypatch):
    monkeypatch.setattr("quse.usage.check_grok_quota", lambda: GrokQuotaStatus())

    record = normalize_usage_provider("grok-build")

    assert record["provider"] == "grok"


def test_usage_unknown_provider_exits_non_zero():
    result = CliRunner().invoke(app, ["wat"])

    assert result.exit_code != 0
    assert "Unknown provider 'wat'" in result.stderr


def test_zai_usage_handles_missing_limit_values(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_zai_quota", lambda: __import__("quse").ZaiQuotaStatus()
    )

    record = normalize_usage_provider("zai")

    assert record["windows"] == {
        "5h": {
            "percent_remaining": None,
            "reset_at": None,
            "rolling": False,
        },
        "7d": {
            "percent_remaining": None,
            "reset_at": None,
        },
        "monthly": {
            "percent_remaining": None,
            "reset_at": None,
        },
    }


def test_zai_usage_marks_five_hour_window_as_rolling(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_zai_quota",
        lambda: ZaiQuotaStatus(
            five_hour=ZaiQuotaWindow(used_percent=0, window_hours=5),
            weekly=ZaiQuotaWindow(used_percent=0),
        ),
    )

    record = normalize_usage_provider("zai")

    assert record["windows"]["5h"] == {
        "percent_remaining": 100.0,
        "reset_at": None,
        "rolling": True,
    }
    assert record["windows"]["7d"] == {
        "percent_remaining": 100.0,
        "reset_at": None,
    }


def test_zai_human_usage_shows_rolling_windows(monkeypatch):
    monkeypatch.setattr(
        "quse.usage.check_zai_quota",
        lambda: ZaiQuotaStatus(
            five_hour=ZaiQuotaWindow(used_percent=0, window_hours=5),
            weekly=ZaiQuotaWindow(used_percent=0),
        ),
    )

    record = normalize_usage_provider("zai")

    assert format_usage_line(record) == (
        "zai:\n"
        "    5h:\n"
        "        remaining: 100.0%\n"
        "        reset: rolling 5h\n"
        "    7d:\n"
        "        remaining: 100.0%\n"
        "        reset: unknown"
    )


def test_collect_usage_without_provider_runs_checks_in_parallel(monkeypatch):
    calls: list[str] = []

    def fake_normalize(provider: str) -> dict:
        calls.append(provider)
        time.sleep(0.1)
        return {"provider": provider}

    monkeypatch.setattr("quse.usage.normalize_usage_provider", fake_normalize)

    started_at = time.monotonic()
    records = collect_usage()
    elapsed = time.monotonic() - started_at

    assert sorted(calls) == [
        "claude",
        "codex",
        "copilot",
        "gemini",
        "go",
        "grok",
        "zai",
    ]
    assert records == [
        {"provider": "codex"},
        {"provider": "claude"},
        {"provider": "zai"},
        {"provider": "copilot"},
        {"provider": "grok"},
        {"provider": "go"},
        {"provider": "gemini"},
    ]
    assert elapsed < 0.25


def test_human_usage_line_uses_normalized_windows(monkeypatch):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(
        "quse.usage.check_codex_quota",
        lambda: CodexQuotaStatus(
            primary_window=CodexQuotaWindow(
                used_percent=40,
                reset_at="2026-04-30",
            ),
            secondary_window=CodexQuotaWindow(
                used_percent=25,
                reset_at="2026-05-01",
            ),
        ),
    )

    try:
        result = CliRunner().invoke(app, ["codex"])

        assert result.exit_code == 0
        assert result.stdout.strip() == (
            "5h:\n"
            "    remaining: 60.0%\n"
            "    reset: 30-04-2026 00:00 (UTC) / overdue\n"
            "7d:\n"
            "    remaining: 75.0%\n"
            "    reset: 01-05-2026 00:00 (UTC) / overdue"
        )
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()
