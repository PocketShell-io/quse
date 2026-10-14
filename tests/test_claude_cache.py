import json
import time

import pytest

from quse import claude_quota
from quse.claude_quota import ClaudeQuotaStatus, _parse_usage_response
from quse.usage import ClaudeUsageProvider

PAYLOAD = {
    "five_hour": {"utilization": 93.0, "resets_at": "2026-10-14T10:00:00Z"},
    "seven_day": {"utilization": 80.0, "resets_at": "2026-10-17T10:00:00Z"},
}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    claude_quota.reset_cache()
    monkeypatch.setattr(claude_quota, "_read_access_token", lambda _p=None: "tok")
    yield
    claude_quota.reset_cache()


def _ok(_token):
    return _parse_usage_response(PAYLOAD)


def _rate_limited(_token):
    return ClaudeQuotaStatus(
        checked_at=time.monotonic(),
        error="HTTP Error 429: Too Many Requests",
        http_status=429,
        retry_after=0.0,
    )


def _check(fetch, **kw):
    claude_quota.reset_cache()
    return claude_quota.check_claude_quota(_fetch=fetch, **kw)


def test_success_is_shared_through_disk_cache_without_credentials(tmp_path):
    calls = []

    def fetch(token):
        calls.append(token)
        return _ok(token)

    first = _check(fetch)
    second = _check(fetch)
    assert calls == ["tok"]
    assert first.error is None and not first.stale
    assert second.observed_at is not None
    assert second.five_hour.used_percent == 93.0
    cache_text = (tmp_path / "quse-cache" / "claude-usage.json").read_text()
    assert "tok" not in cache_text
    assert "accessToken" not in cache_text


def test_429_returns_stale_reading_and_backs_off():
    _check(_ok)
    calls = []

    def fetch(token):
        calls.append(token)
        return _rate_limited(token)

    stale = _check(fetch, disk_cache_ttl=0)
    assert stale.stale is True
    assert "429" in stale.error and "stale" in stale.error
    assert stale.observed_at is not None
    assert stale.next_retry_at is not None
    assert stale.five_hour.used_percent == 93.0
    # Within backoff: no further endpoint calls.
    again = _check(fetch, disk_cache_ttl=0)
    assert calls == ["tok"]
    assert again.stale is True


def test_429_backoff_grows_exponentially():
    _check(_rate_limited)
    path = claude_quota._disk_cache_path()
    first = json.loads(path.read_text())
    assert first["failures"] == 1
    first_delay = first["backoff_until"] - time.time()
    state = dict(first, backoff_until=0)
    path.write_text(json.dumps(state))
    _check(_rate_limited)
    second = json.loads(path.read_text())
    assert second["failures"] == 2
    assert second["backoff_until"] - time.time() > first_delay * 1.5


def test_429_without_cache_is_clear_error():
    status = _check(_rate_limited)
    assert status.stale is False
    assert "no cached reading" in status.error


def test_success_after_backoff_clears_failures():
    _check(_rate_limited)
    path = claude_quota._disk_cache_path()
    state = json.loads(path.read_text())
    path.write_text(json.dumps(dict(state, backoff_until=0)))
    status = _check(_ok)
    assert status.error is None
    assert "failures" not in json.loads(path.read_text())


def test_provider_record_marks_stale(monkeypatch):
    _check(_ok)
    stale = _check(_rate_limited, disk_cache_ttl=0)
    monkeypatch.setattr("quse.usage.check_claude_quota", lambda: stale)
    record = ClaudeUsageProvider().normalize()
    assert record["status"] == "stale"
    assert record["error"].startswith("stale:")
    assert record["details"]["stale"] is True
    assert record["details"]["observed_at"] is not None
    assert record["windows"]["5h"]["percent_remaining"] == 7.0


def test_fresh_provider_record_has_observed_at(monkeypatch):
    fresh = _check(_ok)
    monkeypatch.setattr("quse.usage.check_claude_quota", lambda: fresh)
    record = ClaudeUsageProvider().normalize()
    assert record["status"] == "ok"
    assert record["details"]["stale"] is False
    assert record["details"]["observed_at"] is not None
