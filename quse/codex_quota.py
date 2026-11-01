"""Proactive codex quota checking via chatgpt.com API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
import time
from pathlib import Path

import urllib.error
import urllib.request

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
# Alexey's main Codex account and Orin's second account each live in
# their own CODEX_HOME; both are read with the same ChatGPT usage API.
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_ORIN_AUTH_PATH = Path.home() / ".codex-orin" / "auth.json"
# Legacy alias kept for callers that imported the private name.
_AUTH_PATH = DEFAULT_CODEX_AUTH_PATH
_CACHE_TTL_SECONDS = 60

# Known Codex rate-limit spans → the unified span label. Codex reports the real
# window duration on each rate-limit window as `limit_window_seconds`; we label
# the window from that ACTUAL span rather than assuming primary==5h /
# secondary==7d. Codex temporarily removed the 5h window (mid-2026), so its
# `primary_window` now carries the WEEKLY (604800s) span — labelling it "5h" by
# position (the old behaviour) mislabels weekly data as a 5h window.
_WINDOW_LABELS = {
    18000: "5h",  # 5 hours
    604800: "7d",  # 7 days (weekly)
}


def _span_label(limit_window_seconds: int | None, default: str) -> str:
    """Derive the unified span label from Codex's `limit_window_seconds`.

    Uses the ACTUAL window duration Codex reports so a window is labelled by
    what it really is, not by its slot. Falls back to `default` (the slot's
    historical label) only when Codex omits the duration, preserving the
    legacy positional behaviour for older payloads that lacked the field.
    """
    if limit_window_seconds is None:
        return default
    seconds = int(limit_window_seconds)
    label = _WINDOW_LABELS.get(seconds)
    if label is not None:
        return label
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


@dataclass(slots=True)
class CodexQuotaWindow:
    used_percent: float = 0.0
    reset_at: datetime | None = None
    # Codex reports each window's real span here (e.g. 18000=5h, 604800=7d);
    # used to label the window by its ACTUAL duration. `None` when Codex omits
    # it (older payloads) → the slot's historical default label is used.
    limit_window_seconds: int | None = None
    # True when Codex actually returned this window. Codex now returns
    # `secondary_window: null` (the 5h window was dropped) — a window that is
    # NOT present must not be emitted as a phantom "0% / no reset" ghost row.
    # Defaults True so directly-constructed windows (tests / callers) still
    # render; only the parser marks an absent API window `present=False`.
    present: bool = True

    def __post_init__(self) -> None:
        self.used_percent = float(self.used_percent)
        if self.limit_window_seconds is not None:
            self.limit_window_seconds = int(self.limit_window_seconds)
        # normalize_reset_at handles Codex's millisecond epochs (and seconds /
        # ISO), returning a canonical UTC datetime — one normalizer for all.
        self.reset_at = normalize_reset_at(self.reset_at)

    @property
    def percent_remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class CodexResetCredit:
    status: str | None = None
    title: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = self.status.strip() or None
        else:
            self.status = None
        if isinstance(self.title, str):
            self.title = self.title.strip() or None
        else:
            self.title = None
        self.expires_at = normalize_reset_at(self.expires_at)

    @property
    def is_available(self) -> bool:
        return self.status == "available"


@dataclass(slots=True)
class CodexQuotaStatus:
    primary_window: CodexQuotaWindow = field(default_factory=CodexQuotaWindow)
    secondary_window: CodexQuotaWindow = field(default_factory=CodexQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None
    reset_credits: list[CodexResetCredit] = field(default_factory=list)
    reset_credits_error: str | None = None

    @property
    def short_term(self) -> UsageWindow | None:
        window = _window_for_term(
            self.primary_window, self.secondary_window, long_term=False
        )
        return _as_usage_window(window, default_label="5h")

    @property
    def long_term(self) -> UsageWindow | None:
        window = _window_for_term(
            self.primary_window, self.secondary_window, long_term=True
        )
        return _as_usage_window(window, default_label="7d")

    @property
    def earliest_reset_at(self) -> str | None:
        reset_candidates = [
            value
            for value in (self.primary_window.reset_at, self.secondary_window.reset_at)
            if value
        ]
        if not reset_candidates:
            return None
        return min(reset_candidates)

    @property
    def available_reset_credits(self) -> list[CodexResetCredit]:
        return [credit for credit in self.reset_credits if credit.is_available]


def _as_usage_window(
    window: CodexQuotaWindow | None,
    *,
    default_label: str,
) -> UsageWindow | None:
    """Project a Codex window onto the unified [UsageWindow], or `None`.

    Returns `None` when Codex did not return this window (`present=False`) so a
    dropped window (e.g. the removed 5h `secondary_window: null`) is omitted
    rather than emitted as a phantom "0% / no reset" ghost. The span label
    comes from the window's ACTUAL duration (`limit_window_seconds`), falling
    back to the slot's historical `default_label` only when Codex omits it.
    """
    if window is None or not window.present:
        return None
    return UsageWindow(
        percent_remaining=window.percent_remaining,
        reset_at=window.reset_at,
        window=_span_label(window.limit_window_seconds, default_label),
    )


def _window_for_term(
    primary: CodexQuotaWindow,
    secondary: CodexQuotaWindow,
    *,
    long_term: bool,
) -> CodexQuotaWindow | None:
    """Select a short- or long-term window from Codex's active windows.

    With two windows Codex exposes the 5-hour window first and the weekly
    window second. With one window, that sole window is the weekly window.
    This cardinality rule also works when duration metadata is absent.
    """
    windows = [window for window in (primary, secondary) if window.present]
    if len(windows) == 1:
        if long_term:
            return windows[0]
        return None
    if len(windows) >= 2:
        if long_term:
            return windows[1]
        return windows[0]
    return None


# One cache entry per account auth path, so the two Codex accounts
# (~/.codex and ~/.codex-orin) never evict each other's status.
_cached_statuses: dict[str, CodexQuotaStatus] = {}


# Hub-managed second Codex account: the live credential lives in the
# managed-engine bundle installed from Hub's central store, while the legacy
# ~/.codex-orin/auth.json copy can go stale (the interactive CLI refreshes
# whichever copy it runs with; this probe never refreshes, redeems, or
# writes). Only the Orin provider falls back to the managed copy; the main
# `codex` provider keeps its long-standing home-file behavior.
_MANAGED_ORIN_LABEL = "codex-orin"


def _managed_bundle_root() -> Path:
    override = os.environ.get("AGENT_HUB_MANAGED_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "agent-hub" / "managed-engines"


def _is_orin_auth_path(auth_path: Path | None) -> bool:
    if auth_path is None:
        path = _AUTH_PATH
    else:
        path = auth_path
    try:
        return path.parent.name == ".codex-orin"
    except Exception:
        return False


def _read_managed_bearer_token(label: str) -> str | None:
    """Read the live Hub-managed credential without touching the home copy.

    Read-only: never writes, refreshes, or redeems anything. Returns None
    when no managed bundle for the label is installed or readable.
    """
    try:
        root = _managed_bundle_root()
        if not root.is_dir():
            return None
        best: Path | None = None
        best_mtime = -1.0
        for home in root.glob(label + "-*"):
            auth = home / "home" / ".codex" / "auth.json"
            try:
                if not auth.is_file():
                    continue
                mtime = auth.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = auth, mtime
        if best is None:
            return None
        data = json.loads(best.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            return None
        return token
    except Exception:
        logger.warning("managed codex bundle for %s unreadable", label)
        return None


def _is_unauthorized_error(error: str | None) -> bool:
    return isinstance(error, str) and "401" in error


def _read_bearer_token(auth_path: Path | None = None) -> str | None:
    path = auth_path or _AUTH_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("tokens", {}).get("access_token")
        if token:
            return token
        logger.warning("codex auth.json missing tokens.access_token")
        return None
    except FileNotFoundError:
        logger.warning("codex auth.json not found at %s", path)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("codex auth.json parse error: %s", exc)
        return None


def _parse_quota_response(data: dict) -> CodexQuotaStatus:
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}

    primary_window = _window_from_api(rate_limit.get("primary_window"))
    secondary_window = _window_from_api(rate_limit.get("secondary_window"))

    long_term_window = _window_for_term(
        primary_window, secondary_window, long_term=True
    )
    return CodexQuotaStatus(
        primary_window=primary_window,
        secondary_window=secondary_window,
        limit_reached=bool(rate_limit.get("limit_reached", False))
        or (
            long_term_window is not None
            and long_term_window.used_percent >= 100.0
        ),
        checked_at=time.monotonic(),
    )


def _window_from_api(window_data: object) -> CodexQuotaWindow:
    """Build a [CodexQuotaWindow] from Codex's raw rate-limit window.

    A window Codex OMITS (`null` / non-dict — e.g. the dropped 5h
    `secondary_window: null`) becomes `present=False` so it is not emitted as a
    phantom ghost row. A returned window records its real span
    (`limit_window_seconds`) so it is labelled by its actual duration.
    """
    if not isinstance(window_data, dict):
        return CodexQuotaWindow(present=False)
    return CodexQuotaWindow(
        used_percent=window_data.get("used_percent", 0),
        reset_at=window_data.get("reset_at"),
        limit_window_seconds=window_data.get("limit_window_seconds"),
        present=True,
    )


def _parse_reset_credits_response(data: dict) -> list[CodexResetCredit]:
    credits = data.get("credits")
    if not isinstance(credits, list):
        return []

    parsed: list[CodexResetCredit] = []
    for item in credits:
        if not isinstance(item, dict):
            continue
        parsed.append(
            CodexResetCredit(
                status=item.get("status"),
                title=item.get("title"),
                expires_at=item.get("expires_at"),
            )
        )
    return parsed


def _fetch_json(url: str, token: str, *, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


def _fetch_quota(token: str, *, timeout: float = 10.0) -> CodexQuotaStatus:
    try:
        data = _fetch_json(_USAGE_URL, token, timeout=timeout)
        status = _parse_quota_response(data)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        OSError,
    ) as exc:
        logger.warning("codex quota check failed (fail-open): %s", exc)
        # The usage probe never ran, so banked state is unknown too: an
        # empty grant list here would read as "no resets". Windows are
        # marked absent so error details carry no phantom 0% rows.
        return CodexQuotaStatus(
            checked_at=time.monotonic(),
            error=str(exc),
            reset_credits_error=str(exc),
            primary_window=CodexQuotaWindow(present=False),
            secondary_window=CodexQuotaWindow(present=False),
        )

    try:
        data = _fetch_json(_RESET_CREDITS_URL, token, timeout=timeout)
        status.reset_credits = _parse_reset_credits_response(data)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        OSError,
    ) as exc:
        logger.warning("codex reset credits check failed (non-blocking): %s", exc)
        status.reset_credits_error = str(exc)
    return status


def check_codex_quota(
    *,
    auth_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> CodexQuotaStatus:
    """Check codex quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    key = str((auth_path or _AUTH_PATH).resolve())
    now = time.monotonic()

    cached = _cached_statuses.get(key)
    if cached is not None and (now - cached.checked_at) < cache_ttl:
        return cached

    token = _read_bearer_token(auth_path)
    from_managed = False
    if token is None and _is_orin_auth_path(auth_path):
        token = _read_managed_bearer_token(_MANAGED_ORIN_LABEL)
        from_managed = token is not None
    if token is None:
        error = "no auth token"
        status = CodexQuotaStatus(
            checked_at=now,
            error=error,
            reset_credits_error=error,
            primary_window=CodexQuotaWindow(present=False),
            secondary_window=CodexQuotaWindow(present=False),
        )
        _cached_statuses[key] = status
        return status

    fetcher = _fetch_quota
    if callable(_fetch):
        fetcher = _fetch
    status = fetcher(token)
    if (
        status.error is not None
        and _is_unauthorized_error(status.error)
        and _is_orin_auth_path(auth_path)
        and not from_managed
    ):
        # The home copy is stale but the Hub-managed bundle may hold the
        # live credential (same account, refreshed centrally). Retry once
        # read-only with the managed copy; a managed success replaces the
        # stale failure, a managed failure keeps the original error.
        managed = _read_managed_bearer_token(_MANAGED_ORIN_LABEL)
        if managed and managed != token:
            retry = fetcher(managed)
            if retry.error is None:
                status = retry
    _cached_statuses[key] = status
    return status


def reset_cache() -> None:
    """Clear the cached quota statuses (useful for testing)."""
    _cached_statuses.clear()
