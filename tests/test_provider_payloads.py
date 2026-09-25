"""Golden-payload tests anchored to real provider API responses.

Each payload below was captured from the live API (codex usage + reset-credit
endpoints, claude usage, copilot user endpoint, z.ai quota limit, grok billing).
They lock in
the actual shapes each provider returns and guard the central invariant:

    Every UsageWindow.reset_at is a timezone-aware UTC datetime (or None),
    never a raw string. A str there crashes the human formatter (.astimezone)
    and silently bypasses JSON datetime normalization.
"""

import json
import time
from datetime import datetime, timezone

from click.testing import CliRunner

from quse._shared import reset_at_to_iso
from quse.cli import app
from quse.codex_quota import _parse_quota_response, _parse_reset_credits_response
from quse.usage import format_usage_line, normalize_usage_provider


# --- captured 2026-07-07 from chatgpt.com/backend-api/wham/usage ---

CODEX_USAGE_PAYLOAD = {
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 0,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 18000,
            "reset_at": 1783475303,
        },
        "secondary_window": {
            "used_percent": 98,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 293732,
            "reset_at": 1783751035,
        },
    },
}

# --- captured 2026-07-07 from chatgpt.com/backend-api/wham/rate-limit-reset-credits ---

CODEX_CREDITS_PAYLOAD = {
    "credits": [
        {
            "status": "available",
            "reset_type": "codex_rate_limits",
            "granted_at": "2026-06-26T23:52:15.041002Z",
            "expires_at": "2026-07-26T23:52:15.041002Z",
            "title": "Full reset (Weekly + 5 hr)",
        },
        {
            "status": "available",
            "reset_type": "codex_rate_limits",
            "granted_at": "2026-07-01T19:09:12.591317Z",
            "expires_at": "2026-07-31T19:09:12.591317Z",
            "title": "Full reset (Weekly + 5 hr)",
        },
    ],
    "available_count": 2,
}

# --- captured 2026-07-07 from api.anthropic.com/api/oauth/usage ---

CLAUDE_USAGE_PAYLOAD = {
    "five_hour": {
        "utilization": 38.0,
        "resets_at": "2026-07-07T23:19:59.903305+00:00",
    },
    "seven_day": {
        "utilization": 76.0,
        "resets_at": "2026-07-09T14:59:59.903330+00:00",
    },
}

# --- captured 2026-07-07 from gh api /copilot_internal/user ---

COPILOT_PAYLOAD = {
    "login": "alexeygrigorev",
    "access_type_sku": "free_engaged_oss_quota",
    "quota_reset_date": "2026-08-01",
    "quota_reset_date_utc": "2026-08-01T00:00:00.000Z",
    "quota_snapshots": {
        "premium_interactions": {
            "percent_remaining": 97.1,
            "quota_remaining": 1457.8,
            "remaining": 1457,
            "entitlement": 1500,
            "unlimited": False,
            "has_quota": True,
        },
    },
}

# --- captured 2026-07-07 from api.z.ai/api/monitor/usage/quota/limit ---

# --- captured 2026-08-18 from cli-chat-proxy.grok.com/v1/billing ---

GROK_MONTHLY_PAYLOAD = {
    "config": {
        "monthlyLimit": {"val": 0},
        "used": {"val": 0},
        "onDemandCap": {"val": 0},
        "billingPeriodStart": "2026-08-01T00:00:00+00:00",
        "billingPeriodEnd": "2026-09-01T00:00:00+00:00",
        "history": [
            {
                "billingCycle": {"year": 2026, "month": 7},
                "includedUsed": {"val": 0},
                "onDemandUsed": {"val": 0},
                "totalUsed": {"val": 0},
            }
        ],
    }
}

# --- captured 2026-08-18 from cli-chat-proxy.grok.com/v1/billing?format=credits ---

GROK_CREDITS_PAYLOAD = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-18T00:08:17.671111+00:00",
            "end": "2026-08-25T00:08:17.671111+00:00",
        },
        "creditUsagePercent": 1.0,
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "productUsage": [{"product": "GrokBuild", "usagePercent": 1.0}],
        "isUnifiedBillingUser": True,
        "prepaidBalance": {"val": 0},
        "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
        "billingPeriodStart": "2026-08-18T00:08:17.671111+00:00",
        "billingPeriodEnd": "2026-08-25T00:08:17.671111+00:00",
    }
}

GROK_USER_PAYLOAD = {
    "subscriptionTier": "SuperGrokPlus",
    "hasGrokCodeAccess": True,
}

GROK_CREDITS_WITH_PERCENT_PAYLOAD = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-18T00:08:17.671111+00:00",
            "end": "2026-08-25T00:08:17.671111+00:00",
        },
        "creditUsagePercent": 37.5,
        "onDemandCap": {"val": 80},
        "onDemandUsed": {"val": 30},
        "isUnifiedBillingUser": True,
        "billingPeriodEnd": "2026-08-25T00:08:17.671111+00:00",
    }
}

GROK_MONTHLY_WITH_LIMIT_PAYLOAD = {
    "config": {
        "monthlyLimit": {"val": 200},
        "used": {"val": 50},
        "billingPeriodStart": "2026-08-01T00:00:00+00:00",
        "billingPeriodEnd": "2026-09-01T00:00:00+00:00",
    }
}

ZAI_PAYLOAD = {
    "code": 200,
    "msg": "Operation successful",
    "success": True,
    "data": {
        "limits": [
            {
                "type": "TOKENS_LIMIT",
                "unit": 3,
                "number": 5,
                "percentage": 47,
                "nextResetTime": 1783462930808,
            },
            {
                "type": "TOKENS_LIMIT",
                "unit": 6,
                "number": 1,
                "percentage": 45,
                "nextResetTime": 1783778698997,
            },
            {
                "type": "TIME_LIMIT",
                "unit": 5,
                "number": 1,
                "usage": 4000,
                "currentValue": 38,
                "remaining": 3962,
                "percentage": 1,
                "nextResetTime": 1785161098991,
            },
        ],
        "level": "max",
    },
}


def _assert_reset_at_is_datetime(reset_at: object) -> None:
    """reset_at must be a tz-aware UTC datetime or None -- never a raw string."""
    if reset_at is None:
        return
    assert isinstance(reset_at, datetime), (
        f"reset_at must be a datetime, got {type(reset_at).__name__}: {reset_at!r}"
    )
    assert reset_at.tzinfo is not None, "reset_at datetime must be timezone-aware"


def _set_utc_tz(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()


def _freeze_clock(monkeypatch, fixed_now: datetime) -> None:
    """Pin the relative-countdown clock so human-output assertions are stable."""

    class _FrozenDateTime(datetime):
       @classmethod
       def now(cls, tz=None):
            if tz is None:
                return fixed_now
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("quse.usage.datetime", _FrozenDateTime)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def test_codex_parse_real_payload():
    status = _parse_quota_response(CODEX_USAGE_PAYLOAD)

    _assert_reset_at_is_datetime(status.primary_window.reset_at)
    _assert_reset_at_is_datetime(status.secondary_window.reset_at)

    assert reset_at_to_iso(status.short_term.reset_at) == "2026-07-08T01:48:23Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-07-11T06:23:55Z"
    assert status.short_term.percent_remaining == 100.0
    assert status.long_term.percent_remaining == 2.0


def test_codex_parse_real_reset_credits_payload():
    credits = _parse_reset_credits_response(CODEX_CREDITS_PAYLOAD)

    assert len(credits) == 2
    assert [c.is_available for c in credits] == [True, True]
    assert [reset_at_to_iso(c.expires_at) for c in credits] == [
        "2026-07-26T23:52:15Z",
        "2026-07-31T19:09:12Z",
    ]
    for credit in credits:
        _assert_reset_at_is_datetime(credit.expires_at)


def test_codex_json_round_trips_real_payload(monkeypatch):
    from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow

    status = CodexQuotaStatus(
        primary_window=CodexQuotaWindow(used_percent=0, reset_at=1783475303),
        secondary_window=CodexQuotaWindow(used_percent=98, reset_at=1783751035),
        reset_credits=_parse_reset_credits_response(CODEX_CREDITS_PAYLOAD),
    )
    monkeypatch.setattr("quse.usage.check_codex_quota", lambda: status)

    output = CliRunner().invoke(app, ["codex", "--json"]).output
    record = json.loads(output)["codex"]

    assert record["windows"]["5h"] == {
        "percent_remaining": 100.0,
        "reset_at": "2026-07-08T01:48:23Z",
        "rolling": False,
    }
    assert record["windows"]["7d"] == {
        "percent_remaining": 2.0,
        "reset_at": "2026-07-11T06:23:55Z",
    }
    assert record["windows"]["monthly"] == {
        "percent_remaining": None,
        "reset_at": None,
    }
    assert record["details"]["banked_resets_available"] == 2
    assert record["details"]["banked_resets"][0] == {
        "available": True,
        "expires_at": "2026-07-26T23:52:15Z",
        "label": "Full reset (Weekly + 5 hr)",
    }


def test_codex_human_round_trips_real_payload(monkeypatch):
    _set_utc_tz(monkeypatch)
    from quse.codex_quota import CodexQuotaStatus, CodexQuotaWindow

    status = CodexQuotaStatus(
        primary_window=CodexQuotaWindow(used_percent=0, reset_at=1783475303),
        secondary_window=CodexQuotaWindow(used_percent=98, reset_at=1783751035),
        reset_credits=_parse_reset_credits_response(CODEX_CREDITS_PAYLOAD),
    )
    monkeypatch.setattr("quse.usage.check_codex_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["codex"]).output

    assert output.strip() == (
        "5h:\n"
        "    remaining: 100.0%\n"
        "    reset: 08-07-2026 01:48 (UTC) / in 13h\n"
        "7d:\n"
        "    remaining: 2.0%\n"
        "    reset: 11-07-2026 06:23 (UTC) / in 3d 18h\n"
        "banked_resets:\n"
        "    expires: 26-07-2026 23:52 (UTC) / in 19d 11h\n"
        "    expires: 31-07-2026 19:09 (UTC) / in 24d 7h"
    )


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def test_claude_parse_real_payload():
    from quse.claude_quota import _parse_usage_response

    status = _parse_usage_response(CLAUDE_USAGE_PAYLOAD)

    _assert_reset_at_is_datetime(status.five_hour.reset_at)
    _assert_reset_at_is_datetime(status.seven_day.reset_at)

    assert reset_at_to_iso(status.short_term.reset_at) == "2026-07-07T23:19:59Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-07-09T14:59:59Z"
    assert status.short_term.percent_remaining == 62.0
    assert status.long_term.percent_remaining == 24.0


def test_claude_json_round_trips_real_payload(monkeypatch):
    from quse.claude_quota import _parse_usage_response

    status = _parse_usage_response(CLAUDE_USAGE_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_claude_quota", lambda: status)

    output = CliRunner().invoke(app, ["claude", "--json"]).output
    record = json.loads(output)["claude"]

    assert record["windows"]["5h"]["reset_at"] == "2026-07-07T23:19:59Z"
    assert record["windows"]["7d"]["reset_at"] == "2026-07-09T14:59:59Z"


def test_claude_human_round_trips_real_payload(monkeypatch):
    _set_utc_tz(monkeypatch)
    from quse.claude_quota import _parse_usage_response

    status = _parse_usage_response(CLAUDE_USAGE_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_claude_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["claude"]).output

    assert output.strip() == (
        "5h:\n"
        "    remaining: 62.0%\n"
        "    reset: 07-07-2026 23:19 (UTC) / in 11h\n"
        "7d:\n"
        "    remaining: 24.0%\n"
        "    reset: 09-07-2026 14:59 (UTC) / in 2d 2h"
    )


# ---------------------------------------------------------------------------
# Claude reset grants (banked resets)
# ---------------------------------------------------------------------------

# Status block captured 2026-09-24 from
# api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1 (this account
# was ineligible and returned no grants, so the grant entry follows the shape
# Claude Code's /limit-reset parser enforces).

CLAUDE_RESETS_PAYLOAD = {
    "five_hour": {
        "utilization": 0.0,
        "resets_at": None,
    },
    "seven_day": {
        "utilization": 2.0,
        "resets_at": "2026-10-01T14:59:59.870695+00:00",
    },
    "cedar_ember": {
        "at_limit": False,
        "cooldown_until": None,
        "eligible": True,
        "event_props": None,
        "exhausted": [],
        "grants": [
            {
                "id": "reset-1",
                "label": "Full limit reset",
                "resets_total": 1,
                "resets_left": 1,
                "starts_at": "2026-09-01T00:00:00+00:00",
                "ends_at": "2026-10-15T23:59:59+00:00",
                "clears": ["five_hour", "seven_day"],
                "paused": False,
                "usable_now": True,
                "use_requires_limit": False,
                "percent_used": {},
                "blocking": [],
            }
        ],
        "ineligible_reason": None,
        "next_grant_id": "reset-1",
        "weekly_resets_at": "2026-10-01T15:00:00+00:00",
    },
}


def test_claude_parse_reset_grants_payload():
    from quse.claude_quota import _parse_usage_response

    status = _parse_usage_response(CLAUDE_RESETS_PAYLOAD)

    assert len(status.resets) == 1
    reset = status.resets[0]
    assert reset.grant_id == "reset-1"
    assert reset.label == "Full limit reset"
    assert reset.resets_left == 1
    _assert_reset_at_is_datetime(reset.expires_at)
    assert reset_at_to_iso(reset.expires_at) == "2026-10-15T23:59:59Z"
    assert reset.is_available is True
    assert len(status.available_resets) == 1


def test_claude_parse_ignores_missing_or_malformed_reset_grants():
    from quse.claude_quota import _parse_usage_response

    # The plain (unflagged) usage response carries cedar_ember: null.
    assert _parse_usage_response(CLAUDE_USAGE_PAYLOAD).resets == []
    assert _parse_usage_response({"cedar_ember": None}).resets == []

    malformed = _parse_usage_response(
        {
            "cedar_ember": {
                "eligible": True,
                "grants": [
                    "not-a-grant",
                    {"label": "no id"},
                    {"id": "  ", "label": "blank id"},
                    {"id": "ok", "usable_now": True},
                ],
            }
        }
    )

    assert [reset.grant_id for reset in malformed.resets] == ["ok"]


def test_claude_reset_unavailable_states():
    from quse.claude_quota import ClaudeReset

    not_usable = ClaudeReset(grant_id="a", usable_now=False, expires_at="2027-01-01")
    paused = ClaudeReset(
        grant_id="a", usable_now=True, paused=True, expires_at="2027-01-01"
    )
    exhausted = ClaudeReset(
        grant_id="a", usable_now=True, resets_left=0, expires_at="2027-01-01"
    )
    expired = ClaudeReset(
        grant_id="a", usable_now=True, resets_left=1, expires_at="2020-01-01"
    )

    for reset in (not_usable, paused, exhausted, expired):
        assert reset.is_available is False

    # No expiry listed: stays redeemable, like the other providers' resets.
    undated = ClaudeReset(grant_id="a", usable_now=True, resets_left=1)
    assert undated.is_available is True


def test_claude_json_round_trips_reset_grants(monkeypatch):
    from quse.claude_quota import _parse_usage_response

    status = _parse_usage_response(CLAUDE_RESETS_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_claude_quota", lambda: status)

    output = CliRunner().invoke(app, ["claude", "--json"]).output
    record = json.loads(output)["claude"]

    assert record["windows"]["5h"]["reset_at"] is None
    assert record["windows"]["7d"]["reset_at"] == "2026-10-01T14:59:59Z"
    assert record["details"]["banked_resets_available"] == 1
    assert record["details"]["banked_resets"] == [
        {
            "available": True,
            "expires_at": "2026-10-15T23:59:59Z",
            "label": "Full limit reset",
        }
    ]


def test_claude_human_round_trips_reset_grants(monkeypatch):
    _set_utc_tz(monkeypatch)
    from quse.claude_quota import _parse_usage_response

    status = _parse_usage_response(CLAUDE_RESETS_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_claude_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["claude"]).output

    assert output.strip() == (
        "5h:\n"
        "    remaining: 100.0%\n"
        "    reset: unknown\n"
        "7d:\n"
        "    remaining: 98.0%\n"
        "    reset: 01-10-2026 14:59 (UTC) / in 7d 2h\n"
        "banked_resets:\n"
        "    expires: 15-10-2026 23:59 (UTC) / in 21d 11h"
    )


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------


def _fake_gh_run(payload):
    import subprocess

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    return fake_run


def test_copilot_parse_real_payload(monkeypatch):
    import quse.copilot_quota as copilot

    monkeypatch.setattr(copilot.subprocess, "run", _fake_gh_run(COPILOT_PAYLOAD))
    copilot.reset_cache()

    status = copilot._fetch_quota()

    _assert_reset_at_is_datetime(status.quota_reset_date)
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-08-01T00:00:00Z"
    assert status.long_term.percent_remaining == 97.1


def test_copilot_json_round_trips_real_payload(monkeypatch):
    import quse.copilot_quota as copilot

    monkeypatch.setattr(copilot.subprocess, "run", _fake_gh_run(COPILOT_PAYLOAD))
    copilot.reset_cache()

    output = CliRunner().invoke(app, ["copilot", "--json"]).output
    record = json.loads(output)["copilot"]

    assert record["windows"]["monthly"]["reset_at"] == "2026-08-01T00:00:00Z"
    assert record["windows"]["monthly"]["percent_remaining"] == 97.1


def test_copilot_human_round_trips_real_payload(monkeypatch):
    _set_utc_tz(monkeypatch)
    import quse.copilot_quota as copilot

    monkeypatch.setattr(copilot.subprocess, "run", _fake_gh_run(COPILOT_PAYLOAD))
    copilot.reset_cache()
    _freeze_clock(monkeypatch, datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["copilot"]).output

    assert output.strip() == (
        "monthly:\n"
        "    remaining: 97.1%\n"
        "    reset: 01-08-2026 00:00 (UTC) / in 24d 12h"
    )


# ---------------------------------------------------------------------------
# Z.AI -- this is the provider the bug shipped on.
# ---------------------------------------------------------------------------


def test_zai_parse_real_payload():
    from quse.zai_quota import _parse_usage_response

    status = _parse_usage_response(ZAI_PAYLOAD)

    _assert_reset_at_is_datetime(status.five_hour.reset_at)
    _assert_reset_at_is_datetime(status.weekly.reset_at)
    _assert_reset_at_is_datetime(status.monthly_web_search.reset_at)

    assert reset_at_to_iso(status.five_hour.reset_at) == "2026-07-07T22:22:10Z"
    assert reset_at_to_iso(status.weekly.reset_at) == "2026-07-11T14:04:58Z"
    assert status.short_term.percent_remaining == 53.0
    assert status.long_term.percent_remaining == 55.0


def test_zai_json_round_trips_real_payload(monkeypatch):
    from quse.zai_quota import _parse_usage_response

    status = _parse_usage_response(ZAI_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_zai_quota", lambda: status)

    output = CliRunner().invoke(app, ["zai", "--json"]).output
    record = json.loads(output)["zai"]

    assert record["windows"]["5h"] == {
        "percent_remaining": 53.0,
        "reset_at": "2026-07-07T22:22:10Z",
        "rolling": True,
    }
    assert record["windows"]["7d"] == {
        "percent_remaining": 55.0,
        "reset_at": "2026-07-11T14:04:58Z",
    }
    assert record["windows"]["monthly"] == {
        "percent_remaining": None,
        "reset_at": None,
    }


def test_zai_human_round_trips_real_payload(monkeypatch):
    """The path that crashed in the shipped release: a z.ai long_term reset
    time flowed through as a str and died on .astimezone()."""
    _set_utc_tz(monkeypatch)
    from quse.zai_quota import _parse_usage_response

    status = _parse_usage_response(ZAI_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_zai_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["zai"]).output

    assert output.strip() == (
        "5h:\n"
        "    remaining: 53.0%\n"
        "    reset: 07-07-2026 22:22 (UTC) / in 10h\n"
        "7d:\n"
        "    remaining: 55.0%\n"
        "    reset: 11-07-2026 14:04 (UTC) / in 4d 2h"
    )


def test_zai_normalized_record_has_datetime_reset_at():
    """format_usage_line must never receive a str reset_at. Guards the
    boundary directly through normalize_usage_provider."""
    from quse.zai_quota import _parse_usage_response

    import quse.usage

    status = _parse_usage_response(ZAI_PAYLOAD)
    original = quse.usage.check_zai_quota
    quse.usage.check_zai_quota = lambda: status
    try:
        record = normalize_usage_provider("zai")
    finally:
        quse.usage.check_zai_quota = original

    _assert_reset_at_is_datetime(record["windows"]["5h"]["reset_at"])
    _assert_reset_at_is_datetime(record["windows"]["7d"]["reset_at"])
    assert record["windows"]["monthly"]["reset_at"] is None
    format_usage_line(record)


# ---------------------------------------------------------------------------
# _format_relative unit tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Grok Build
# ---------------------------------------------------------------------------


def test_grok_parse_real_payload():
    from quse.grok_quota import _parse_billing_payloads

    status = _parse_billing_payloads(
        monthly=GROK_MONTHLY_PAYLOAD,
        credits=GROK_CREDITS_PAYLOAD,
        user=GROK_USER_PAYLOAD,
    )

    assert status.short_term is None
    assert status.long_term is not None
    _assert_reset_at_is_datetime(status.long_term.reset_at)
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-08-25T00:08:17Z"
    assert status.long_term.percent_remaining == 99.0
    assert status.long_term.window == "weekly"
    assert status.subscription == "SuperGrokPlus"


def test_grok_json_round_trips_real_payload(monkeypatch):
    from quse.grok_quota import _parse_billing_payloads

    status = _parse_billing_payloads(
        monthly=GROK_MONTHLY_PAYLOAD,
        credits=GROK_CREDITS_PAYLOAD,
        user=GROK_USER_PAYLOAD,
    )
    monkeypatch.setattr("quse.usage.check_grok_quota", lambda: status)

    output = CliRunner().invoke(app, ["grok", "--json"]).output
    record = json.loads(output)["grok"]

    assert record["windows"]["5h"] == {
        "percent_remaining": None,
        "reset_at": None,
        "rolling": False,
    }
    assert record["windows"]["7d"]["reset_at"] == "2026-08-25T00:08:17Z"
    assert record["windows"]["7d"]["percent_remaining"] == 99.0
    assert "subscription" not in record["details"]
    assert record["details"]["product_usage"] == [
        {"product": "GrokBuild", "usage_percent": 1.0}
    ]


def test_grok_human_round_trips_real_payload(monkeypatch):
    _set_utc_tz(monkeypatch)
    from quse.grok_quota import _parse_billing_payloads

    status = _parse_billing_payloads(
        monthly=GROK_MONTHLY_PAYLOAD,
        credits=GROK_CREDITS_PAYLOAD,
        user=GROK_USER_PAYLOAD,
    )
    monkeypatch.setattr("quse.usage.check_grok_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["grok"]).output

    assert output.strip() == (
        "7d:\n"
        "    remaining: 99.0%\n"
        "    reset: 25-08-2026 00:08 (UTC) / in 6d 12h"
    )


def test_grok_human_round_trips_percent_and_monthly(monkeypatch):
    _set_utc_tz(monkeypatch)
    from quse.grok_quota import _parse_billing_payloads

    status = _parse_billing_payloads(
        monthly=GROK_MONTHLY_WITH_LIMIT_PAYLOAD,
        credits=GROK_CREDITS_WITH_PERCENT_PAYLOAD,
        user=GROK_USER_PAYLOAD,
    )
    monkeypatch.setattr("quse.usage.check_grok_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["grok-build"]).output

    assert output.strip() == (
        "7d:\n"
        "    remaining: 62.5%\n"
        "    reset: 25-08-2026 00:08 (UTC) / in 6d 12h\n"
        "monthly:\n"
        "    remaining: 75.0%\n"
        "    reset: 01-09-2026 00:00 (UTC) / in 13d 12h"
    )


def test_grok_normalized_record_has_datetime_reset_at():
    from quse.grok_quota import _parse_billing_payloads

    import quse.usage

    status = _parse_billing_payloads(
        monthly=GROK_MONTHLY_PAYLOAD,
        credits=GROK_CREDITS_PAYLOAD,
        user=GROK_USER_PAYLOAD,
    )
    original = quse.usage.check_grok_quota
    quse.usage.check_grok_quota = lambda: status
    try:
        record = normalize_usage_provider("grok")
    finally:
        quse.usage.check_grok_quota = original

    _assert_reset_at_is_datetime(record["windows"]["7d"]["reset_at"])
    _assert_reset_at_is_datetime(record["windows"]["monthly"]["reset_at"])
    format_usage_line(record)


# ---------------------------------------------------------------------------
# Z.AI reset cards (banked resets)
# ---------------------------------------------------------------------------

# Shape captured 2026-09-04 from
# api.z.ai/api/biz/customer-package-reset/list?targetType=PERSONAL
# (recordId redacted, expiry kept: dashboard wall time without an offset).

ZAI_RESET_CARDS_PAYLOAD = {
    "code": 200,
    "msg": "Operation successful",
    "data": {
        "targetType": "PERSONAL",
        "lastFiveHourResetTime": None,
        "lastWeekResetTime": None,
        "fiveHourResets": [],
        "weekResets": [
            {
                "recordId": 294120,
                "expireTime": "2026-10-01 23:59:59",
                "available": True,
            }
        ],
    },
    "success": True,
}


def test_zai_parse_reset_cards_payload():
    from quse.zai_quota import _parse_reset_cards_response

    resets = _parse_reset_cards_response(ZAI_RESET_CARDS_PAYLOAD)

    assert len(resets) == 1
    assert resets[0].record_id == 294120
    assert resets[0].scope == "week"
    _assert_reset_at_is_datetime(resets[0].expires_at)
    # Dashboard wall time is Singapore (UTC+8), not UTC.
    assert reset_at_to_iso(resets[0].expires_at) == "2026-10-01T15:59:59Z"
    assert resets[0].is_available is True
    assert len([reset for reset in resets if reset.is_available]) == 1


def test_zai_parse_reset_cards_marks_expired_unavailable():
    from quse.zai_quota import ZaiReset

    reset = ZaiReset(
        record_id=1, scope="five_hour", expires_at="2020-01-01 00:00:00"
    )

    assert reset_at_to_iso(reset.expires_at) == "2019-12-31T16:00:00Z"
    assert reset.is_available is False


def test_zai_json_round_trips_reset_cards(monkeypatch):
    from quse.zai_quota import _parse_reset_cards_response, _parse_usage_response

    status = _parse_usage_response(ZAI_PAYLOAD)
    status.resets = _parse_reset_cards_response(ZAI_RESET_CARDS_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_zai_quota", lambda: status)

    output = CliRunner().invoke(app, ["zai", "--json"]).output
    record = json.loads(output)["zai"]

    assert record["details"]["banked_resets_available"] == 1
    assert record["details"]["banked_resets"] == [
        {
            "available": True,
            "expires_at": "2026-10-01T15:59:59Z",
            "label": "weekly reset",
        }
    ]
    assert record["details"]["banked_resets_error"] is None


def test_zai_human_round_trips_reset_cards(monkeypatch):
    _set_utc_tz(monkeypatch)
    from quse.zai_quota import _parse_reset_cards_response, _parse_usage_response

    status = _parse_usage_response(ZAI_PAYLOAD)
    status.resets = _parse_reset_cards_response(ZAI_RESET_CARDS_PAYLOAD)
    monkeypatch.setattr("quse.usage.check_zai_quota", lambda: status)
    _freeze_clock(monkeypatch, datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc))

    output = CliRunner().invoke(app, ["zai"]).output

    assert output.strip() == (
        "5h:\n"
        "    remaining: 53.0%\n"
        "    reset: 07-07-2026 22:22 (UTC) / overdue\n"
        "7d:\n"
        "    remaining: 55.0%\n"
        "    reset: 11-07-2026 14:04 (UTC) / overdue\n"
        "banked_resets:\n"
        "    expires: 01-10-2026 15:59 (UTC) / in 27d 3h"
    )


def test_zai_reset_cards_failure_is_non_blocking(monkeypatch):
    import quse.zai_quota as zai_quota
    from quse.zai_quota import ZaiConfig

    monkeypatch.setattr(
        zai_quota, "_read_zai_config", lambda config_path=None: ZaiConfig(token="tok")
    )
    monkeypatch.setattr(
        zai_quota, "_fetch_quota_limit", lambda config: {"data": {"limits": []}}
    )

    def fail_reset_cards(config):
        raise OSError("reset cards unavailable")

    monkeypatch.setattr(zai_quota, "_fetch_reset_cards", fail_reset_cards)

    status = zai_quota._fetch_usage()

    assert status.error is None
    assert status.resets == []
    assert status.resets_error == "reset cards unavailable"


def test_format_relative_days_and_hours():
    from datetime import timedelta

    from quse.usage import _format_relative

    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    value = now + timedelta(days=6, hours=4)
    assert _format_relative(value, now) == "in 6d 4h"


def test_format_relative_hours_only():
    from datetime import timedelta

    from quse.usage import _format_relative

    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    value = now + timedelta(hours=4)
    assert _format_relative(value, now) == "in 4h"


def test_format_relative_minutes_only():
    from datetime import timedelta

    from quse.usage import _format_relative

    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    value = now + timedelta(minutes=30)
    assert _format_relative(value, now) == "in 30m"


def test_format_relative_now():
    from quse.usage import _format_relative

    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_relative(now, now) == "now"


def test_format_relative_overdue():
    from datetime import timedelta

    from quse.usage import _format_relative

    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    value = now - timedelta(days=2)
    assert _format_relative(value, now) == "overdue"
