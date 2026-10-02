"""Proactive Gemini / Antigravity quota checking via Antigravity CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from quse._shared import UsageWindow, normalize_reset_at

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_SEARCH_PATHS = (
    Path.home() / ".local" / "bin" / "agy",
    Path.home() / ".gemini" / "antigravity-cli" / "bin" / "agy",
)


def _find_agy_executable() -> str | None:
    env_bin = os.environ.get("AGY_BIN")
    if not env_bin:
        env_bin = os.environ.get("ANTIGRAVITY_BIN")
    if not env_bin:
        env_bin = os.environ.get("GEMINI_BIN")
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        return shutil.which(env_bin)

    which_agy = shutil.which("agy")
    if which_agy:
        return which_agy

    which_antigravity = shutil.which("antigravity")
    if which_antigravity:
        return which_antigravity

    for candidate in _DEFAULT_SEARCH_PATHS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


@dataclass(slots=True)
class GeminiQuotaBucket:
    id: str | None = None
    name: str | None = None
    description: str | None = None
    window: str | None = None
    remaining_fraction: float | None = None
    reset_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.remaining_fraction is not None:
            self.remaining_fraction = float(self.remaining_fraction)
        self.reset_time = normalize_reset_at(self.reset_time)

    @property
    def percent_remaining(self) -> float | None:
        if self.remaining_fraction is None:
            return None
        return max(0.0, min(100.0, round(self.remaining_fraction * 100.0, 2)))

    @property
    def reset_at(self) -> datetime | None:
        return self.reset_time

    @property
    def used_percent(self) -> float | None:
        if self.percent_remaining is None:
            return None
        return round(max(0.0, 100.0 - self.percent_remaining), 2)


@dataclass(slots=True)
class GeminiQuotaGroup:
    name: str | None = None
    description: str | None = None
    buckets: list[GeminiQuotaBucket] = field(default_factory=list)


@dataclass(slots=True)
class GeminiQuotaStatus:
    groups: list[GeminiQuotaGroup] = field(default_factory=list)
    description: str | None = None
    checked_at: float = 0.0
    error: str | None = None

    @property
    def limit_reached(self) -> bool:
        if self.error is not None:
            return False
        for group in self.groups:
            for bucket in group.buckets:
                if (
                    bucket.remaining_fraction is not None
                    and bucket.remaining_fraction <= 0.0
                ):
                    return True
        return False

    def _find_bucket(
        self, window_name: str, *, prefer_group: str = "Gemini"
    ) -> GeminiQuotaBucket | None:
        # First check preferred group
        for group in self.groups:
            group_name = group.name or ""
            if prefer_group.lower() in group_name.lower():
                for bucket in group.buckets:
                    b_window = (bucket.window or "").lower()
                    b_id = (bucket.id or "").lower()
                    if window_name == "5h":
                        if b_window == "5h" or "5h" in b_id or "5 hour" in b_id:
                            return bucket
                    elif window_name in ("7d", "weekly"):
                        if (
                            b_window in ("7d", "weekly")
                            or "weekly" in b_id
                            or "7d" in b_id
                        ):
                            return bucket
        # Fallback to any group
        for group in self.groups:
            for bucket in group.buckets:
                b_window = (bucket.window or "").lower()
                b_id = (bucket.id or "").lower()
                if window_name == "5h":
                    if b_window == "5h" or "5h" in b_id or "5 hour" in b_id:
                        return bucket
                elif window_name in ("7d", "weekly"):
                    if (
                        b_window in ("7d", "weekly")
                        or "weekly" in b_id
                        or "7d" in b_id
                    ):
                        return bucket
        return None

    @property
    def short_term(self) -> UsageWindow | None:
        bucket = self._find_bucket("5h")
        if bucket is None:
            return None
        return UsageWindow(
            percent_remaining=bucket.percent_remaining,
            reset_at=bucket.reset_at,
            window="5h",
            rolling=False,
        )

    @property
    def long_term(self) -> UsageWindow | None:
        bucket = self._find_bucket("7d")
        if bucket is None:
            return None
        return UsageWindow(
            percent_remaining=bucket.percent_remaining,
            reset_at=bucket.reset_at,
            window="7d",
        )


def _parse_tsv_response(text: str) -> GeminiQuotaStatus:
    groups_map: dict[str, list[GeminiQuotaBucket]] = {}
    lines = text.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("Quota:"):
            continue
        if "\t" in stripped:
            cols = [col.strip() for col in stripped.split("\t") if col.strip()]
        else:
            cols = [col.strip() for col in re.split(r"\s{2,}", stripped) if col.strip()]
        if len(cols) < 4:
            continue
        group_name = cols[0]
        bucket_name = cols[1]
        pct_str = cols[2].rstrip("%")
        reset_str = cols[3]

        fraction = None
        try:
            fraction = float(pct_str) / 100.0
        except ValueError:
            pass

        b_lower = bucket_name.lower()
        if "5 hour" in b_lower or "5h" in b_lower:
            window = "5h"
            b_id = "gemini-5h"
        elif "week" in b_lower or "7d" in b_lower:
            window = "weekly"
            b_id = "gemini-weekly"
        else:
            window = bucket_name
            b_id = bucket_name

        bucket = GeminiQuotaBucket(
            id=b_id,
            name=bucket_name,
            window=window,
            remaining_fraction=fraction,
            reset_time=normalize_reset_at(reset_str),
        )
        if group_name not in groups_map:
            groups_map[group_name] = []
        groups_map[group_name].append(bucket)

    groups = []
    for g_name, buckets in groups_map.items():
        groups.append(GeminiQuotaGroup(name=g_name, buckets=buckets))

    return GeminiQuotaStatus(groups=groups, checked_at=time.monotonic())


def _parse_quota_response(payload: dict[str, Any]) -> GeminiQuotaStatus:
    if not isinstance(payload, dict):
        return GeminiQuotaStatus(
            checked_at=time.monotonic(), error="Invalid payload format"
        )

    if payload.get("status") in ("ERROR", "FAILED"):
        err = payload.get("error")
        if not err:
            err = payload.get("message")
        if not err:
            err = "CLI reported error status"
        return GeminiQuotaStatus(checked_at=time.monotonic(), error=str(err))

    data = payload
    if "command" in data and isinstance(data["command"], dict):
        data = data["command"]
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    description = data.get("description")
    if not isinstance(description, str):
        description = None

    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        raw_buckets = data.get("buckets")
        if isinstance(raw_buckets, list):
            raw_groups = [{"name": "Gemini Models", "buckets": raw_buckets}]
        else:
            raw_groups = []

    if not raw_groups and isinstance(payload.get("response"), str):
        tsv_status = _parse_tsv_response(payload["response"])
        if tsv_status.groups:
            tsv_status.description = description
            return tsv_status

    groups: list[GeminiQuotaGroup] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        g_name = g.get("name")
        g_desc = g.get("description")
        buckets: list[GeminiQuotaBucket] = []
        for b in g.get("buckets", []):
            if not isinstance(b, dict):
                continue
            fraction = b.get("remaining_fraction")
            if fraction is None and "percent_remaining" in b:
                try:
                    fraction = float(b["percent_remaining"]) / 100.0
                except (ValueError, TypeError):
                    fraction = None
            elif fraction is None and "used_percent" in b:
                try:
                    fraction = max(0.0, 100.0 - float(b["used_percent"])) / 100.0
                except (ValueError, TypeError):
                    fraction = None

            reset_val = b.get("reset_time")
            if not reset_val:
                reset_val = b.get("reset_at")

            buckets.append(
                GeminiQuotaBucket(
                    id=b.get("id"),
                    name=b.get("name"),
                    description=b.get("description"),
                    window=b.get("window"),
                    remaining_fraction=fraction,
                    reset_time=reset_val,
                )
            )
        groups.append(
            GeminiQuotaGroup(
                name=g_name,
                description=g_desc,
                buckets=buckets,
            )
        )

    return GeminiQuotaStatus(
        groups=groups,
        description=description,
        checked_at=time.monotonic(),
    )


_cached_status: GeminiQuotaStatus | None = None


def _fetch_quota(*, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> GeminiQuotaStatus:
    executable = _find_agy_executable()
    if not executable:
        return GeminiQuotaStatus(checked_at=time.monotonic(), error="agy not on PATH")

    try:
        result = subprocess.run(
            [executable, "-p", "/quota", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            err_msg = result.stderr.strip()
            if not err_msg:
                err_msg = result.stdout.strip()
            if not err_msg:
                err_msg = f"agy exit {result.returncode}"
            return GeminiQuotaStatus(checked_at=time.monotonic(), error=err_msg)

        stdout = result.stdout.strip()
        if not stdout:
            return GeminiQuotaStatus(
                checked_at=time.monotonic(), error="agy returned empty output"
            )

        try:
            data = json.loads(stdout)
            return _parse_quota_response(data)
        except json.JSONDecodeError:
            tsv_status = _parse_tsv_response(stdout)
            if tsv_status.groups:
                return tsv_status
            return GeminiQuotaStatus(
                checked_at=time.monotonic(), error="invalid JSON from agy"
            )

    except subprocess.TimeoutExpired as exc:
        logger.warning("gemini quota check timed out: %s", exc)
        return GeminiQuotaStatus(
            checked_at=time.monotonic(), error=f"timeout after {timeout}s"
        )
    except (OSError, Exception) as exc:
        logger.warning("gemini quota check failed (fail-open): %s", exc)
        return GeminiQuotaStatus(checked_at=time.monotonic(), error=str(exc))


def check_gemini_quota(
    *,
    cache_ttl: float = _CACHE_TTL_SECONDS,
    _fetch: object = None,
) -> GeminiQuotaStatus:
    """Check Gemini / Antigravity quota via agy CLI. Returns cached result within TTL."""
    global _cached_status
    if (
        _cached_status is not None
        and time.monotonic() - _cached_status.checked_at < cache_ttl
    ):
        return _cached_status

    fetcher = _fetch_quota
    if callable(_fetch):
        fetcher = _fetch
    _cached_status = fetcher()
    return _cached_status


def reset_cache() -> None:
    global _cached_status
    _cached_status = None
