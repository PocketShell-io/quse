"""Tests for proactive codex quota checking."""

import json

import pytest

from quse._shared import reset_at_to_iso
from quse.codex_quota import (
    _fetch_quota,
    _parse_quota_response,
    _parse_reset_credits_response,
    _read_bearer_token,
    check_codex_quota,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


# --- Token reading ---


def test_read_token_success(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok-abc"}}))
    assert _read_bearer_token(auth) == "tok-abc"


def test_read_token_missing_file(tmp_path):
    assert _read_bearer_token(tmp_path / "nope.json") is None


def test_read_token_bad_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("not json")
    assert _read_bearer_token(auth) is None


def test_read_token_missing_field(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}))
    assert _read_bearer_token(auth) is None


# --- Response parsing ---


def _make_api_response(
    limit_reached=False,
    primary_pct=50.0,
    secondary_pct=20.0,
    primary_reset="2026-04-08T05:00:00Z",
    secondary_reset="2026-04-13T00:00:00Z",
):
    return {
        "rate_limit": {
            "limit_reached": limit_reached,
            "primary_window": {
                "used_percent": primary_pct,
                "reset_at": primary_reset,
            },
            "secondary_window": {
                "used_percent": secondary_pct,
                "reset_at": secondary_reset,
            },
        }
    }


def test_parse_limit_not_reached():
    status = _parse_quota_response(_make_api_response(limit_reached=False))
    assert status.limit_reached is False
    assert status.short_term.percent_remaining == 50.0
    assert reset_at_to_iso(status.short_term.reset_at) == "2026-04-08T05:00:00Z"
    assert status.long_term.percent_remaining == 80.0
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-04-13T00:00:00Z"


def test_parse_limit_reached():
    status = _parse_quota_response(
        _make_api_response(limit_reached=True, secondary_pct=85.0)
    )
    assert status.limit_reached is True


def test_parse_empty_response():
    status = _parse_quota_response({})
    assert status.limit_reached is False
    # An empty rate_limit returns no windows — neither is present, so neither is
    # emitted as a phantom window (was: fake 100%-remaining default windows).
    assert status.short_term is None
    assert status.long_term is None


def test_window_labelled_from_actual_span_not_slot():
    """Label the window by Codex's real ``limit_window_seconds``, not its slot.

    Regression for pocketshell #1564: Codex temporarily removed the 5h window,
    so ``primary_window`` now carries the WEEKLY (604800s) span with
    ``secondary_window: null``. Labelling by slot (primary->"5h", secondary->"7d")
    mislabels weekly data as a "5h window" and emits a phantom "7d" ghost row.
    """
    status = _parse_quota_response(
        {
            "rate_limit": {
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 25.0,
                    "limit_window_seconds": 604800,
                    "reset_at": "2026-07-21T20:37:32Z",
                },
                "secondary_window": None,
            }
        }
    )
    assert status.short_term is None
    assert status.long_term is not None
    assert status.long_term.window == "7d"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-07-21T20:37:32Z"
    assert status.long_term.percent_remaining == 75.0


def test_weekly_primary_window_drives_limit_reached_threshold():
    status = _parse_quota_response(
        {
            "rate_limit": {
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 90.0,
                    "limit_window_seconds": 604800,
                },
                "secondary_window": None,
            }
        }
    )

    assert status.limit_reached is True


def test_single_window_is_weekly_even_without_duration_metadata():
    status = _parse_quota_response(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 30.0,
                    "reset_at": "2026-07-21T20:37:32Z",
                },
                "secondary_window": None,
            }
        }
    )

    assert status.short_term is None
    assert status.long_term is not None
    assert status.long_term.window == "7d"
    assert status.long_term.percent_remaining == 70.0


def test_window_span_label_falls_back_to_slot_when_span_absent():
    """Older payloads without ``limit_window_seconds`` keep the slot label."""
    status = _parse_quota_response(_make_api_response())
    assert status.short_term.window == "5h"
    assert status.long_term.window == "7d"


def test_window_span_labels_five_hour_and_weekly_from_seconds():
    """Both live spans (18000s=5h, 604800s=7d) label correctly."""
    status = _parse_quota_response(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10.0,
                    "limit_window_seconds": 18000,
                    "reset_at": "2026-07-21T20:37:32Z",
                },
                "secondary_window": {
                    "used_percent": 40.0,
                    "limit_window_seconds": 604800,
                    "reset_at": "2026-07-25T20:37:32Z",
                },
            }
        }
    )
    assert status.short_term.window == "5h"
    assert status.long_term.window == "7d"


def test_parse_unix_reset_timestamps():
    status = _parse_quota_response(
        _make_api_response(
            primary_reset=1779637981,
            secondary_reset=1780173234,
        )
    )

    assert reset_at_to_iso(status.short_term.reset_at) == "2026-05-24T15:53:01Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-05-30T20:33:54Z"


def test_parse_millisecond_reset_timestamps():
    status = _parse_quota_response(
        _make_api_response(
            primary_reset=1779637981000,
            secondary_reset=1780173234000,
        )
    )

    assert reset_at_to_iso(status.short_term.reset_at) == "2026-05-24T15:53:01Z"
    assert reset_at_to_iso(status.long_term.reset_at) == "2026-05-30T20:33:54Z"


def test_parse_reset_credits_response():
    credits = _parse_reset_credits_response(
        {
            "credits": [
                {
                    "status": "available",
                    "title": "Usage reset",
                    "expires_at": "2026-05-24T15:53:01Z",
                },
                {
                    "status": "expired",
                    "title": "Old reset",
                    "expires_at": 1779637981,
                },
                "ignored",
            ]
        }
    )

    assert len(credits) == 2
    assert credits[0].is_available is True
    assert credits[0].title == "Usage reset"
    assert reset_at_to_iso(credits[0].expires_at) == "2026-05-24T15:53:01Z"
    assert credits[1].is_available is False
    assert reset_at_to_iso(credits[1].expires_at) == "2026-05-24T15:53:01Z"


def test_parse_reset_credits_response_handles_missing_list():
    assert _parse_reset_credits_response({}) == []


# --- Quota check with caching ---


def test_check_quota_limit_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(
            _make_api_response(limit_reached=True, secondary_pct=85.0)
        )

    status = check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert status.limit_reached is True


def test_check_quota_not_reached(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    def fake_fetch(token):
        return _parse_quota_response(
            _make_api_response(limit_reached=False, secondary_pct=30.0)
        )

    status = check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert status.limit_reached is False


def test_check_quota_cache(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    call_count = 0

    def fake_fetch(token):
        nonlocal call_count
        call_count += 1
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth, _fetch=fake_fetch)
    assert call_count == 1  # second call used cache


def test_check_quota_cache_expires(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))

    call_count = 0

    def fake_fetch(token):
        nonlocal call_count
        call_count += 1
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth, cache_ttl=0, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth, cache_ttl=0, _fetch=fake_fetch)
    assert call_count == 2  # cache expired immediately


def test_fetch_quota_fetches_reset_credits(monkeypatch):
    requested_urls = []

    def fake_fetch_json(url, token, *, timeout):
        requested_urls.append(url)
        assert token == "tok"
        assert timeout == 3.0
        if url.endswith("/usage"):
            return _make_api_response()
        return {
            "credits": [
                {
                    "status": "available",
                    "title": "Usage reset",
                    "expires_at": "2026-05-24T15:53:01Z",
                }
            ]
        }

    monkeypatch.setattr("quse.codex_quota._fetch_json", fake_fetch_json)

    status = _fetch_quota("tok", timeout=3.0)

    assert [url.rsplit("/", 1)[-1] for url in requested_urls] == [
        "usage",
        "rate-limit-reset-credits",
    ]
    assert status.error is None
    assert status.reset_credits_error is None
    assert len(status.reset_credits) == 1
    assert len(status.available_reset_credits) == 1


def test_fetch_quota_reset_credits_failure_is_non_blocking(monkeypatch):
    def fake_fetch_json(url, token, *, timeout):
        if url.endswith("/usage"):
            return _make_api_response()
        raise OSError("credits unavailable")

    monkeypatch.setattr("quse.codex_quota._fetch_json", fake_fetch_json)

    status = _fetch_quota("tok")

    assert status.error is None
    assert status.limit_reached is False
    assert status.reset_credits == []
    assert status.reset_credits_error == "credits unavailable"


def test_check_quota_no_auth(tmp_path):
    status = check_codex_quota(auth_path=tmp_path / "nope.json")
    assert status.error == "no auth token"
    assert status.limit_reached is False


# Monitoring integration tests live in the litehive test suite — they exercise
# litehive.observability.record_codex_quota_check against heru's UsageStatus,
# which is a litehive-side concern.


# --- Per-account caching (codex + codex-orin) ---


def test_cache_is_per_account(tmp_path):
    """Each account's CODEX_HOME caches independently.

    Orin's ~/.codex-orin and the main ~/.codex must never serve each other a
    cached status: two different ChatGPT accounts have different windows.
    """
    auth_a = tmp_path / "a" / "auth.json"
    auth_b = tmp_path / "b" / "auth.json"
    auth_a.parent.mkdir(parents=True)
    auth_b.parent.mkdir(parents=True)
    auth_a.write_text(json.dumps({"tokens": {"access_token": "tok-a"}}))
    auth_b.write_text(json.dumps({"tokens": {"access_token": "tok-b"}}))

    calls = []

    def fake_fetch(token):
        calls.append(token)
        return _parse_quota_response(_make_api_response())

    check_codex_quota(auth_path=auth_a, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth_b, _fetch=fake_fetch)
    check_codex_quota(auth_path=auth_a, _fetch=fake_fetch)

    assert calls == ["tok-a", "tok-b"]  # third call hit a's cache
