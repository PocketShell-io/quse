"""OpenCode Go quota checking via the OpenCode usage API."""

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from quse._opencode_auth import (
    read_auth_token,
    read_console_access_token,
    read_orin_console_access_token,
)
from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_DEFAULT_TIMEOUT_SECONDS = 15.0
_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
_AUTH_PROVIDER = "opencode-go"

# Orin's OpenCode Go account (the `go-orin` provider): its own API key env
# var and its own OpenCode auth file (~/.local/share/opencode-orin), the
# same layout OpenCode itself uses for the main account. The Console device
# login is the MAIN account's credential, so orin deliberately never falls
# back to it — both providers must never report the same account twice.
OPENCODE_GO_ORIN_AUTH_PATH = (
    Path.home() / ".local" / "share" / "opencode-orin" / "auth.json"
)
_OPENCODE_GO_ORIN_API_KEY_ENV = "OPENCODE_GO_ORIN_API_KEY"


def _read_access_tokens(
    auth_path: Path | None = None, *, orin: bool = False
) -> list[str]:
    """All usable tokens, highest priority first (de-duplicated).

    Main account: explicit ``OPENCODE_GO_API_KEY`` wins, then the live
    OpenCode Console device credential OpenCode itself uses, then the legacy
    ``opencode-go`` API key from ``auth.json`` (known-stale after the move to
    device auth). Orin's account reads only Orin-specific sources: the
    ``OPENCODE_GO_ORIN_API_KEY`` override and the legacy key from Orin's own
    ``opencode-orin`` auth file.
    """
    tokens: list[str] = []
    if orin:
        configured = os.environ.get(_OPENCODE_GO_ORIN_API_KEY_ENV)
        if isinstance(configured, str) and configured.strip():
            tokens.append(configured.strip())
        orin_console_token = read_orin_console_access_token()
        if orin_console_token:
            tokens.append(orin_console_token)
        legacy_token = read_auth_token(
            _AUTH_PROVIDER, auth_path or OPENCODE_GO_ORIN_AUTH_PATH
        )
        if legacy_token:
            tokens.append(legacy_token)
    else:
        configured = os.environ.get("OPENCODE_GO_API_KEY")
        if isinstance(configured, str) and configured.strip():
            tokens.append(configured.strip())
        console_token = read_console_access_token()
        if console_token:
            tokens.append(console_token)
        legacy_token = read_auth_token(_AUTH_PROVIDER, auth_path)
        if legacy_token:
            tokens.append(legacy_token)
    ordered: list[str] = []
    for token in tokens:
        if token not in ordered:
            ordered.append(token)
    return ordered


def _read_access_token(
    auth_path: Path | None = None, *, orin: bool = False
) -> str | None:
    tokens = _read_access_tokens(auth_path, orin=orin)
    if not tokens:
        return None
    return tokens[0]


@dataclass(slots=True)
class OpenCodeGoQuotaWindow:
    used_percent: float | None = None
    reset_at: datetime | None = None
    status: str | None = None
    window: str | None = None

    @property
    def percent_remaining(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class OpenCodeGoQuotaStatus:
    rolling: OpenCodeGoQuotaWindow | None = None
    weekly: OpenCodeGoQuotaWindow | None = None
    monthly: OpenCodeGoQuotaWindow | None = None
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None

    @property
    def short_term(self) -> UsageWindow | None:
        if self.rolling is None:
            return None
        return UsageWindow(
            percent_remaining=self.rolling.percent_remaining,
            reset_at=self.rolling.reset_at,
            window=self.rolling.window,
            rolling=True,
        )

    @property
    def long_term(self) -> UsageWindow | None:
        window = self.monthly or self.weekly
        if window is None:
            return None
        return UsageWindow(
            percent_remaining=window.percent_remaining,
            reset_at=window.reset_at,
            window=window.window,
        )

    @property
    def max_used_percent(self) -> float | None:
        values = [
            window.used_percent
            for window in (self.rolling, self.weekly, self.monthly)
            if window is not None and window.used_percent is not None
        ]
        if not values:
            return None
        return max(values)


# One cache entry per account, so the main and orin accounts never evict
# each other's status.
_cached_statuses: dict[str, OpenCodeGoQuotaStatus] = {}


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_window(data: object, *, label: str) -> OpenCodeGoQuotaWindow | None:
    if not isinstance(data, dict):
        return None
    used_percent = _number_or_none(data.get("percent"))
    if used_percent is None:
        used_percent = _number_or_none(data.get("usagePercent"))
    status = data.get("status")
    if not isinstance(status, str):
        status = None
    return OpenCodeGoQuotaWindow(
        used_percent=used_percent,
        reset_at=normalize_reset_at(data.get("resetsAt")),
        status=status,
        window=label,
    )


def _window_is_limited(window: OpenCodeGoQuotaWindow | None) -> bool:
    if window is None:
        return False
    if window.used_percent is not None and window.used_percent >= 100.0:
        return True
    if window.status is None:
        return False
    return window.status.lower().replace("_", "-") in {
        "exhausted",
        "limited",
        "rate-limited",
    }


def _parse_usage_response(data: dict) -> OpenCodeGoQuotaStatus:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("opencode-go usage is missing")

    rolling = _parse_window(usage.get("rolling"), label="5h")
    weekly = _parse_window(usage.get("weekly"), label="weekly")
    monthly = _parse_window(usage.get("monthly"), label="monthly")
    return OpenCodeGoQuotaStatus(
        rolling=rolling,
        weekly=weekly,
        monthly=monthly,
        limit_reached=(
            _window_is_limited(rolling)
            or _window_is_limited(weekly)
            or _window_is_limited(monthly)
        ),
    )


def _fetch_usage_payload(
    token: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> dict:
    request = Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "quse",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("opencode-go usage response is not an object")
    return data


def _fetch_usage(
    *, auth_path: Path | None = None, orin: bool = False
) -> OpenCodeGoQuotaStatus:
    tokens = _read_access_tokens(auth_path, orin=orin)
    if not tokens:
        return OpenCodeGoQuotaStatus(
            checked_at=time.monotonic(), error="no-credentials"
        )
    last_error: str | None = None
    for token in tokens:
        try:
            data = _fetch_usage_payload(token)
        except HTTPError as exc:
            # A rejected token (e.g. the stale auth.json API key) must not
            # shadow a working one: fall through to the next credential.
            if exc.code == 401:
                last_error = f"HTTP Error {exc.code}: Unauthorized"
                logger.warning(
                    "opencode-go token rejected (401), trying next credential"
                )
                continue
            logger.warning("opencode-go quota check failed (fail-open): %s", exc)
            return OpenCodeGoQuotaStatus(
                checked_at=time.monotonic(), error=str(exc)
            )
        except (
            ValueError,
            OSError,
            URLError,
            TimeoutError,
        ) as exc:
            logger.warning("opencode-go quota check failed (fail-open): %s", exc)
            return OpenCodeGoQuotaStatus(
                checked_at=time.monotonic(), error=str(exc)
            )
        try:
            status = _parse_usage_response(data)
        except ValueError as exc:
            logger.warning("opencode-go quota check failed (fail-open): %s", exc)
            return OpenCodeGoQuotaStatus(
                checked_at=time.monotonic(), error=str(exc)
            )
        status.checked_at = time.monotonic()
        return status
    return OpenCodeGoQuotaStatus(
        checked_at=time.monotonic(), error=last_error or "unauthorized"
    )


def check_opencode_go_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    auth_path: Path | None = None,
    orin: bool = False,
    _fetch: object = None,
) -> OpenCodeGoQuotaStatus:
    """Check OpenCode Go quota. Returns cached result within TTL. Fails open."""
    key = "default"
    if orin:
        key = "orin"
    now = time.monotonic()

    cached = _cached_statuses.get(key)
    if cached is not None and now - cached.checked_at < cache_ttl:
        return cached

    if callable(_fetch):
        status = _fetch()
    else:
        status = _fetch_usage(auth_path=auth_path, orin=orin)
    _cached_statuses[key] = status
    return status


def reset_cache() -> None:
    _cached_statuses.clear()
