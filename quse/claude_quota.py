"""Proactive Claude quota checking via OAuth usage endpoint."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import time
from pathlib import Path

import urllib.error
import urllib.request

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The plain usage response always carries ``cedar_ember: null``; only the
# flagged read returns the reset-grant block. This is the same read Claude
# Code uses for its ``/limit-reset`` status.
_USAGE_RESETS_QUERY = "?cedar_ember=1&skip_spend=1"
_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_DEFAULT_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]
_OAUTH_USER_AGENT = "Claude-Code/2.1.198"
_CACHE_TTL_SECONDS = 60
_TOKEN_REFRESH_BUFFER_MS = 300_000


@dataclass(slots=True)
class ClaudeQuotaWindow:
    used_percent: float = 0.0
    reset_at: str | None = None

    def __post_init__(self) -> None:
        self.used_percent = float(self.used_percent)
        self.reset_at = normalize_reset_at(self.reset_at)

    @property
    def percent_remaining(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(slots=True)
class ClaudeReset:
    """A one-time Claude usage-limit reset grant (``cedar_ember.grants``).

    Anthropic surfaces these in Settings > Usage's "Resets" section and in
    Claude Code's ``/limit-reset`` flow: redeeming one refills the cleared
    windows immediately. quse only lists them; redemption is a state-changing
    ``POST /api/organizations/{org}/reset_rate_limits`` that quse never calls.
    """

    grant_id: str | None = None
    label: str | None = None
    resets_left: int | None = None
    usable_now: bool = False
    paused: bool = False
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.grant_id, str):
            self.grant_id = self.grant_id.strip() or None
        else:
            self.grant_id = None
        if isinstance(self.label, str):
            self.label = self.label.strip() or None
        else:
            self.label = None
        if isinstance(self.resets_left, bool):
            self.resets_left = None
        elif not isinstance(self.resets_left, int):
            self.resets_left = None
        self.usable_now = bool(self.usable_now)
        self.paused = bool(self.paused)
        self.expires_at = normalize_reset_at(self.expires_at)

    @property
    def is_available(self) -> bool:
        """Redeemable now, matching Claude Code's own offer check: the server
        must flag the grant usable, it must not be paused, resets must remain,
        and it must not have expired."""
        if not self.usable_now:
            return False
        if self.paused:
            return False
        if self.resets_left is not None and self.resets_left <= 0:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.now(timezone.utc)


@dataclass(slots=True)
class ClaudeQuotaStatus:
    five_hour: ClaudeQuotaWindow = field(default_factory=ClaudeQuotaWindow)
    seven_day: ClaudeQuotaWindow = field(default_factory=ClaudeQuotaWindow)
    limit_reached: bool = False
    checked_at: float = 0.0
    error: str | None = None
    subscription: str | None = None
    resets: list[ClaudeReset] = field(default_factory=list)

    @property
    def short_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.five_hour.percent_remaining,
            reset_at=self.five_hour.reset_at,
            window="5h",
        )

    @property
    def long_term(self) -> UsageWindow:
        return UsageWindow(
            percent_remaining=self.seven_day.percent_remaining,
            reset_at=self.seven_day.reset_at,
            window="7d",
        )

    @property
    def available_resets(self) -> list[ClaudeReset]:
        return [reset for reset in self.resets if reset.is_available]


@dataclass(slots=True)
class ClaudeOAuthCredentials:
    path: Path
    data: dict
    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None
    scopes: list[str] = field(default_factory=list)


def _default_credentials_path() -> Path:
    """Resolve Claude credentials path, respecting config dir overrides."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".credentials.json"
    return Path.home() / ".claude" / ".credentials.json"


_cached_status: ClaudeQuotaStatus | None = None


def _read_oauth_credentials(
    creds_path: Path | None = None,
) -> ClaudeOAuthCredentials | None:
    path = creds_path or _default_credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if isinstance(token, str) and token:
            refresh_token = oauth.get("refreshToken")
            if not isinstance(refresh_token, str) or not refresh_token:
                refresh_token = None
            expires_at = oauth.get("expiresAt")
            if not isinstance(expires_at, int):
                expires_at = None
            scopes = oauth.get("scopes")
            if not isinstance(scopes, list) or not all(
                isinstance(scope, str) for scope in scopes
            ):
                scopes = []
            return ClaudeOAuthCredentials(
                path=path,
                data=data,
                access_token=token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scopes=scopes,
            )
        logger.warning("claude credentials missing claudeAiOauth.accessToken")
        return None
    except FileNotFoundError:
        logger.warning("claude credentials not found at %s", path)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("claude credentials parse error: %s", exc)
        return None


def _oauth_token_expires_soon(expires_at: int | None) -> bool:
    if expires_at is None:
        return False
    return int(time.time() * 1000) + _TOKEN_REFRESH_BUFFER_MS >= expires_at


def _oauth_client_id() -> str:
    configured = os.environ.get("CLAUDE_CODE_OAUTH_CLIENT_ID")
    if configured:
        return configured
    return _DEFAULT_OAUTH_CLIENT_ID


def _write_oauth_credentials(credentials: ClaudeOAuthCredentials) -> None:
    tmp_path = credentials.path.with_suffix(f"{credentials.path.suffix}.tmp")
    tmp_path.write_text(json.dumps(credentials.data, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(credentials.path)


def _refresh_access_token(
    credentials: ClaudeOAuthCredentials, *, timeout: float = 30.0
) -> str:
    if not credentials.refresh_token:
        raise ValueError("claude refresh token is missing")
    scopes = credentials.scopes or _OAUTH_SCOPES
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
            "client_id": _oauth_client_id(),
            "scope": " ".join(scopes),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _OAUTH_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        refreshed = json.loads(response.read().decode("utf-8"))

    access_token = refreshed.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("claude oauth refresh response missing access_token")

    oauth = credentials.data.setdefault("claudeAiOauth", {})
    if not isinstance(oauth, dict):
        raise ValueError("claude credentials claudeAiOauth is not an object")
    oauth["accessToken"] = access_token
    refresh_token = refreshed.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        oauth["refreshToken"] = refresh_token
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int(time.time() * 1000 + expires_in * 1000)
    scope = refreshed.get("scope")
    if isinstance(scope, str):
        oauth["scopes"] = scope.split()
    _write_oauth_credentials(credentials)
    return access_token


def _read_access_token(creds_path: Path | None = None) -> str | None:
    credentials = _read_oauth_credentials(creds_path)
    if credentials is None:
        return None
    if _oauth_token_expires_soon(credentials.expires_at):
        try:
            return _refresh_access_token(credentials)
        except (
            ValueError,
            json.JSONDecodeError,
            OSError,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            logger.warning(
                "claude oauth refresh failed; using stored access token: %s", exc
            )
    return credentials.access_token


def _parse_reset_grants(data: dict) -> list[ClaudeReset]:
    """Parse the usage response's ``cedar_ember`` block into reset grants.

    The block is absent or ``null`` unless the request carries
    ``cedar_ember=1``. Malformed entries are skipped, matching Claude Code's
    own "ignore a malformed grant" behavior.
    """
    cedar = data.get("cedar_ember")
    if not isinstance(cedar, dict):
        return []
    grants = cedar.get("grants")
    if not isinstance(grants, list):
        return []
    parsed: list[ClaudeReset] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        reset = ClaudeReset(
            grant_id=grant.get("id"),
            label=grant.get("label"),
            resets_left=grant.get("resets_left"),
            usable_now=grant.get("usable_now"),
            paused=grant.get("paused"),
            expires_at=grant.get("ends_at"),
        )
        if reset.grant_id is None:
            continue
        parsed.append(reset)
    return parsed


def _parse_usage_response(data: dict) -> ClaudeQuotaStatus:
    five_hour_data = data.get("five_hour")
    if not isinstance(five_hour_data, dict):
        five_hour_data = {}
    seven_day_data = data.get("seven_day")
    if not isinstance(seven_day_data, dict):
        seven_day_data = {}

    five_hour = ClaudeQuotaWindow(
        used_percent=five_hour_data.get("utilization", 0),
        reset_at=five_hour_data.get("resets_at"),
    )
    seven_day = ClaudeQuotaWindow(
        used_percent=seven_day_data.get("utilization", 0),
        reset_at=seven_day_data.get("resets_at"),
    )
    subscription = data.get("subscription")

    normalized_subscription = None
    if isinstance(subscription, str) and subscription:
        normalized_subscription = subscription

    return ClaudeQuotaStatus(
        five_hour=five_hour,
        seven_day=seven_day,
        limit_reached=seven_day.percent_remaining <= 5.0,
        checked_at=time.monotonic(),
        subscription=normalized_subscription,
        resets=_parse_reset_grants(data),
    )


def _fetch_usage(token: str, *, timeout: float = 10.0) -> ClaudeQuotaStatus:
    req = urllib.request.Request(
        _USAGE_URL + _USAGE_RESETS_QUERY,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_usage_response(data)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning("claude quota check failed (fail-open): %s", exc)
        return ClaudeQuotaStatus(checked_at=time.monotonic(), error=str(exc))


def check_claude_quota(
    *,
    creds_path: Path | None = None,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> ClaudeQuotaStatus:
    """Check Claude quota proactively. Returns cached result within TTL.

    Fails open: if auth is missing or API call fails, returns a non-blocking status.
    """
    global _cached_status
    if (
        _cached_status is not None
        and time.monotonic() - _cached_status.checked_at < cache_ttl
    ):
        return _cached_status

    token = _read_access_token(creds_path)
    if token is None:
        return ClaudeQuotaStatus(checked_at=time.monotonic(), error="no-credentials")

    fetcher = _fetch_usage
    if callable(_fetch):
        fetcher = _fetch
    _cached_status = fetcher(token)
    return _cached_status


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
