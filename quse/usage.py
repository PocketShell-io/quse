"""Normalized usage records for supported coding-agent providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from typing import Any, TypeAlias

from quse._shared import BankedReset, UsageWindow
from quse.claude_quota import check_claude_quota
from quse.codex_quota import check_codex_quota
from quse.copilot_quota import check_copilot_quota
from quse.grok_quota import check_grok_quota
from quse.opencode_go_quota import check_opencode_go_quota
from quse.zai_quota import check_zai_quota


CANONICAL_WINDOWS = ("5h", "7d", "monthly")


class UnknownProviderError(ValueError):
    """Raised when a provider name is not supported."""


def usage_provider_error_message(name: str) -> str:
    valid_names = ", ".join(USAGE_PROVIDER_CHOICES)
    return f"Unknown provider '{name}'. Valid provider names: {valid_names}."


def _window_record(window: Any, *, name: str) -> dict[str, Any]:
    if window is None:
        remaining = None
        reset_at = None
        rolling = False
    else:
        remaining = window.percent_remaining
        reset_at = window.reset_at
        rolling = bool(getattr(window, "rolling", False))
    if remaining is None:
        percent = None
    else:
        percent = round(float(remaining), 2)
    record = {
        "percent_remaining": percent,
        # A real ``datetime`` (or ``None``) — the JSON boundary serializes it to
        # ISO-8601 UTC; the human formatter renders it directly.
        "reset_at": reset_at,
    }
    if name == "5h":
        record["rolling"] = rolling
    return record


def _format_reset_at(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone().strftime("%d-%m-%Y %H:%M (%Z)")


def _format_relative(value: datetime, now: datetime) -> str:
    """Render a reset time as a compact 'in Xd Yh' style countdown.

    ``now`` and ``value`` are compared in UTC; the result is independent of the
    machine's local timezone so output is stable. Days and hours are the only
    meaningful units for these (hours-to-weeks) quota windows.
    """
    delta = value - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= -60:
        return "overdue"
    if total_seconds <= 0:
        return "now"
    minutes, _ = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and not hours:
        parts.append(f"{minutes}m")
    return "in " + " ".join(parts)


def _format_reset_or_window(
    name: str,
    window: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    reset_at = _format_reset_at(window["reset_at"])
    if reset_at != "unknown":
        if now is not None:
            current = now
        else:
            current = datetime.now(tz=window["reset_at"].tzinfo)
        return f"{reset_at} / {_format_relative(window['reset_at'], current)}"
    if name == "5h" and window.get("rolling", False):
        return "rolling 5h"
    return reset_at


def _format_percent(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value}%"


def _banked_reset_record(
    *, expires_at: Any, available: bool, label: Any
) -> dict[str, Any]:
    """Build one unified ``banked_resets`` entry.

    Every provider maps its internal reset representation onto this single
    shape so human and JSON output share the same model.
    """
    return asdict(
        BankedReset(expires_at=expires_at, available=available, label=label)
    )


def _banked_resets_from_codex(status_obj: Any) -> list[dict[str, Any]]:
    banked: list[dict[str, Any]] = []
    for credit in getattr(status_obj, "reset_credits", []):
        banked.append(
            _banked_reset_record(
                expires_at=getattr(credit, "expires_at", None),
                available=bool(getattr(credit, "is_available", False)),
                label=getattr(credit, "title", None),
            )
        )
    return banked


def _banked_resets_from_grok(status_obj: Any) -> list[dict[str, Any]]:
    banked: list[dict[str, Any]] = []
    for reset in getattr(status_obj, "resets", []):
        banked.append(
            _banked_reset_record(
                expires_at=getattr(reset, "expires_at", None),
                available=bool(getattr(reset, "is_available", False)),
                label=getattr(reset, "token_id", None),
            )
        )
    return banked


_ZAI_BANKED_SCOPE_LABELS = {
    "five_hour": "5-hour reset",
    "week": "weekly reset",
}


def _banked_resets_from_zai(status_obj: Any) -> list[dict[str, Any]]:
    banked: list[dict[str, Any]] = []
    for reset in getattr(status_obj, "resets", []):
        scope = getattr(reset, "scope", None)
        label = _ZAI_BANKED_SCOPE_LABELS.get(scope)
        if label is None:
            label = scope
        banked.append(
            _banked_reset_record(
                expires_at=getattr(reset, "expires_at", None),
                available=bool(getattr(reset, "is_available", False)),
                label=label,
            )
        )
    return banked


def _banked_resets_from_claude(status_obj: Any) -> list[dict[str, Any]]:
    banked: list[dict[str, Any]] = []
    for reset in getattr(status_obj, "resets", []):
        label = getattr(reset, "label", None)
        if label is None:
            label = getattr(reset, "grant_id", None)
        banked.append(
            _banked_reset_record(
                expires_at=getattr(reset, "expires_at", None),
                available=bool(getattr(reset, "is_available", False)),
                label=label,
            )
        )
    return banked


def _format_banked_resets_lines(
    record: dict[str, Any], *, header: bool = True, now: datetime | None = None
) -> list[str]:
    details = record.get("details")
    if not isinstance(details, dict):
        return []
    banked = details.get("banked_resets")
    if not isinstance(banked, list) or not banked:
        return []
    indent, field_indent = _usage_indents(header)
    lines: list[str] = [f"{indent}banked_resets:"]
    for item in banked:
        if not isinstance(item, dict):
            continue
        lines.append(f"{field_indent}{_format_banked_reset_body(item, now=now)}")
    return lines


def _usage_indents(header: bool) -> tuple[str, str]:
    if header:
        return "    ", "        "
    return "", "    "


def _format_banked_reset_body(
    item: dict[str, Any], *, now: datetime | None = None
) -> str:
    expires_at = item.get("expires_at")
    if expires_at is None:
        expires_at = item.get("validity_end")
    formatted = _format_reset_at(expires_at)
    if formatted == "unknown":
        return "expires: unknown"
    if now is not None:
        current = now
    else:
        current = datetime.now(tz=expires_at.tzinfo)
    return f"expires: {formatted} / {_format_relative(expires_at, current)}"


def _format_windows(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    windows = record.get("windows")
    if isinstance(windows, dict):
        return [
            (name, windows[name])
            for name in CANONICAL_WINDOWS
            if isinstance(windows.get(name), dict)
        ]
    return []


def usage_window_record(
    *,
    provider: str,
    status: str,
    windows: dict[str, Any] | None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if windows is None:
        windows = {}
    return {
        "provider": provider,
        "status": status,
        "windows": {
            name: _window_record(windows.get(name), name=name)
            for name in CANONICAL_WINDOWS
        },
        "error": error,
        "details": details or {},
    }


class UsageProvider(ABC):
    name: str
    supported: bool = True

    def normalize(self) -> dict[str, Any]:
        if not self.supported:
            return usage_window_record(
                provider=self.name,
                status="unsupported",
                windows={},
                error="unsupported",
            )

        status_obj = self.check_status()
        windows = {}
        if not status_obj.error:
            windows = self.windows(status_obj)
        return usage_window_record(
            provider=self.name,
            status=self.status_label(status_obj),
            windows=windows,
            error=status_obj.error,
            details=self.details(status_obj),
        )

    @abstractmethod
    def check_status(self) -> Any:
        raise NotImplementedError

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {"limit_reached": status_obj.limit_reached}

    def status_label(self, status_obj: Any) -> str:
        if status_obj.error:
            return "error"
        return "ok"

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {}


def _as_usage_window(
    window: Any, *, label: str, rolling: bool = False
) -> UsageWindow | None:
    if window is None or not getattr(window, "present", True):
        return None
    return UsageWindow(
        percent_remaining=window.percent_remaining,
        reset_at=window.reset_at,
        window=label,
        rolling=rolling,
    )


class CodexUsageProvider(UsageProvider):
    name = "codex"

    def check_status(self) -> Any:
        return check_codex_quota()

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {
            "5h": status_obj.short_term,
            "7d": status_obj.long_term,
            "monthly": None,
        }

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "banked_resets": _banked_resets_from_codex(status_obj),
            "banked_resets_available": len(status_obj.available_reset_credits),
            "banked_resets_error": status_obj.reset_credits_error,
            "windows": {
                "primary_window": asdict(status_obj.primary_window),
                "secondary_window": asdict(status_obj.secondary_window),
            },
        }


class ClaudeUsageProvider(UsageProvider):
    name = "claude"

    def check_status(self) -> Any:
        return check_claude_quota()

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {
            "5h": status_obj.short_term,
            "7d": status_obj.long_term,
            "monthly": None,
        }

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "subscription": status_obj.subscription,
            "banked_resets": _banked_resets_from_claude(status_obj),
            "banked_resets_available": len(status_obj.available_resets),
            "windows": {
                "five_hour": asdict(status_obj.five_hour),
                "seven_day": asdict(status_obj.seven_day),
            },
        }


class CopilotUsageProvider(UsageProvider):
    name = "copilot"

    def check_status(self) -> Any:
        return check_copilot_quota()

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {
            "5h": None,
            "7d": None,
            "monthly": status_obj.long_term,
        }

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "premium_percent_remaining": status_obj.premium_percent_remaining,
            "limit_reached": status_obj.limit_reached,
            "premium_remaining": status_obj.premium_remaining,
            "premium_entitlement": status_obj.premium_entitlement,
        }


class ZaiUsageProvider(UsageProvider):
    name = "zai"

    def check_status(self) -> Any:
        return check_zai_quota()

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {
            "5h": _as_usage_window(
                status_obj.five_hour, label="5h", rolling=True
            ),
            "7d": _as_usage_window(status_obj.weekly, label="7d"),
            "monthly": None,
        }

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "max_used_percent": status_obj.max_used_percent,
            "banked_resets": _banked_resets_from_zai(status_obj),
            "banked_resets_available": len(status_obj.available_resets),
            "banked_resets_error": status_obj.resets_error,
            "windows": {
                "five_hour": asdict(status_obj.five_hour),
                "weekly": asdict(status_obj.weekly),
            },
        }


class GrokUsageProvider(UsageProvider):
    name = "grok"

    def check_status(self) -> Any:
        return check_grok_quota()

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {
            "5h": None,
            "7d": _as_usage_window(status_obj.weekly, label="7d"),
            "monthly": _as_usage_window(status_obj.monthly, label="monthly"),
        }

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "has_grok_code_access": status_obj.has_grok_code_access,
            "is_unified_billing_user": status_obj.is_unified_billing_user,
            "prepaid_balance": status_obj.prepaid_balance,
            "on_demand_cap": status_obj.on_demand_cap,
            "on_demand_used": status_obj.on_demand_used,
            "product_usage": status_obj.product_usage,
            "banked_resets": _banked_resets_from_grok(status_obj),
            "banked_resets_available": len(status_obj.available_resets),
            "banked_resets_error": status_obj.resets_error,
            "windows": {
                "weekly": asdict(status_obj.weekly),
                "monthly": asdict(status_obj.monthly),
            },
        }


class GoUsageProvider(UsageProvider):
    name = "go"

    def check_status(self) -> Any:
        return check_opencode_go_quota()

    def windows(self, status_obj: Any) -> dict[str, Any]:
        return {
            "5h": status_obj.short_term,
            "7d": _as_usage_window(status_obj.weekly, label="7d"),
            "monthly": _as_usage_window(status_obj.monthly, label="monthly"),
        }

    def details(self, status_obj: Any) -> dict[str, Any]:
        return {
            "limit_reached": status_obj.limit_reached,
            "max_used_percent": status_obj.max_used_percent,
        }


class GeminiUsageProvider(UsageProvider):
    name = "gemini"
    supported = False

    def check_status(self) -> Any:
        raise NotImplementedError


UsageProviderClass: TypeAlias = type[UsageProvider]


USAGE_PROVIDER_CLASSES: tuple[UsageProviderClass, ...] = (
    CodexUsageProvider,
    ClaudeUsageProvider,
    ZaiUsageProvider,
    CopilotUsageProvider,
    GrokUsageProvider,
    GoUsageProvider,
    GeminiUsageProvider,
)
USAGE_PROVIDER_ALIASES = {
    "grok-build": "grok",
}
USAGE_PROVIDER_CHOICES = tuple(
    [provider.name for provider in USAGE_PROVIDER_CLASSES]
    + [alias for alias in USAGE_PROVIDER_ALIASES if alias not in {
        provider.name for provider in USAGE_PROVIDER_CLASSES
    }]
)
SUPPORTED_USAGE_PROVIDERS = tuple(
    provider.name for provider in USAGE_PROVIDER_CLASSES if provider.supported
)


def _canonical_provider_name(name: str) -> str:
    aliased = USAGE_PROVIDER_ALIASES.get(name)
    if aliased is not None:
        return aliased
    return name


def usage_provider_for(name: str) -> UsageProvider:
    canonical = _canonical_provider_name(name)
    for provider_class in USAGE_PROVIDER_CLASSES:
        if provider_class.name == canonical:
            return provider_class()
    raise UnknownProviderError(usage_provider_error_message(name))


def normalize_usage_provider(provider: str) -> dict[str, Any]:
    return usage_provider_for(provider).normalize()


def format_usage_line(
    record: dict[str, Any], *, header: bool = True, now: datetime | None = None
) -> str:
    indent, field_indent = _usage_indents(header)
    lines: list[str] = []
    if header:
        lines.append(f"{record['provider']}:")
    for term, window in _format_windows(record):
        # Keep the normalized JSON shape stable, but do not print a
        # nonexistent human-facing row when a provider omits a window.
        if window["percent_remaining"] is None and window["reset_at"] is None:
            continue
        usage = _format_percent(window["percent_remaining"])
        lines.extend(
            [
                f"{indent}{term}:",
                f"{field_indent}remaining: {usage}",
                f"{field_indent}reset: {_format_reset_or_window(term, window, now=now)}",
            ]
        )
    lines.extend(_format_banked_resets_lines(record, header=header, now=now))
    if record["error"]:
        lines.append(f"{indent}error: {record['error']}")
    return "\n".join(lines)


def selected_providers(provider: str | None) -> list[str]:
    if provider is None:
        return list(SUPPORTED_USAGE_PROVIDERS)
    if provider not in USAGE_PROVIDER_CHOICES:
        raise UnknownProviderError(usage_provider_error_message(provider))
    return [_canonical_provider_name(provider)]


def collect_usage(provider: str | None = None) -> list[dict[str, Any]]:
    providers = selected_providers(provider)
    if provider is not None:
        return [normalize_usage_provider(providers[0])]
    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        return list(executor.map(normalize_usage_provider, providers))
