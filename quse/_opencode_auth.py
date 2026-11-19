"""Helpers for reading credentials stored by OpenCode."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)

_DEFAULT_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"

#: Integration ID OpenCode uses for the Console device login
#: (``opencode auth login "OpenCode Console" --method device``). Its OAuth
#: access token is accepted by the OpenCode Go usage API, unlike the stale
#: ``opencode-go`` API key that older logins left in ``auth.json``.
OPENCODE_CONSOLE_INTEGRATION_ID = "opencode"
_OPENCODE_DEVICE_METHOD_ID = "device"
_EXPORT_TIMEOUT_SECONDS = 10.0
OPENCODE_ORIN_DATA_HOME = Path.home() / ".local" / "share" / "opencode-orin"


def default_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if isinstance(data_home, str) and data_home.strip():
        return Path(data_home) / "opencode" / "auth.json"
    return _DEFAULT_AUTH_PATH


def read_auth_token(provider: str, auth_path: Path | None = None) -> str | None:
    path = auth_path or default_auth_path()
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    entry = data.get(provider)
    if not isinstance(entry, dict):
        return None
    token = entry.get("key")
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip()


def _nvm_opencode_candidates(home: Path) -> list[Path]:
    """OpenCode binaries installed under nvm-managed node versions.

    Service units (e.g. the Hub quota-server) run with a restricted ``PATH``
    that omits version-manager shims, so ``shutil.which("opencode")`` misses
    an nvm-installed binary even though the same user's login shell finds it.
    Only the binary location is probed here; credential handling is unchanged
    (read-only ``opencode auth export``).
    """
    root = home / ".nvm" / "versions" / "node"
    try:
        version_dirs = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []

    def _version_key(path: Path) -> tuple:
        try:
            return tuple(int(part) for part in path.name.lstrip("v").split("."))
        except ValueError:
            return ()

    candidates: list[Path] = []
    for version_dir in sorted(version_dirs, key=_version_key, reverse=True):
        binary = version_dir / "bin" / "opencode"
        try:
            if binary.is_file() and os.access(binary, os.X_OK):
                candidates.append(binary)
        except OSError:
            continue
    return candidates


def _find_opencode_executable() -> str | None:
    env_bin = os.environ.get("OPENCODE_BIN")
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        return shutil.which(env_bin)
    found = shutil.which("opencode")
    if found:
        return found
    # Fall back to well-known install locations before giving up: restricted
    # service environments often lack the login-shell PATH entries (nvm shims,
    # ~/.local/bin) where OpenCode is actually installed.
    home = Path.home()
    static_candidates = [
        home / ".local" / "bin" / "opencode",
        Path("/usr/local/bin/opencode"),
        Path("/usr/bin/opencode"),
    ]
    for candidate in static_candidates + _nvm_opencode_candidates(home):
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _exported_console_credentials(data_home: Path | None = None) -> list:
    """Return raw ``opencode auth export opencode`` entries, or []. Fail-open."""
    executable = _find_opencode_executable()
    if not executable:
        return []
    env = None
    if data_home is not None:
        env = {**os.environ, "XDG_DATA_HOME": str(data_home)}
    try:
        result = subprocess.run(
            [executable, "auth", "export", OPENCODE_CONSOLE_INTEGRATION_ID],
            capture_output=True,
            text=True,
            timeout=_EXPORT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("opencode auth export failed (fail-open): %s", exc)
        return []
    if result.returncode != 0:
        logger.warning(
            "opencode auth export failed (fail-open): exit %s: %s",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("opencode auth export returned invalid JSON: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("opencode auth export returned unexpected shape")
        return []
    return data


def _is_orin_entry(entry: dict) -> bool:
    """True when an exported Console credential belongs to Orin's account."""
    value = entry.get("value")
    if isinstance(value, dict):
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            email = metadata.get("email")
            if isinstance(email, str) and "orin" in email.split("@", 1)[0].lower():
                return True
    label = entry.get("label")
    if isinstance(label, str) and "orin" in label.lower():
        return True
    return False


def _select_device_access_token(
    entries: list,
    *,
    orin_only: bool = False,
    exclude_orin: bool = False,
) -> str | None:
    """Pick the Console device OAuth access token, preferring the active one."""
    candidates: list[tuple[bool, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        is_orin = _is_orin_entry(entry)
        if orin_only and not is_orin:
            continue
        if exclude_orin and is_orin:
            continue
        value = entry.get("value")
        if not isinstance(value, dict):
            continue
        if value.get("type") != "oauth":
            continue
        if value.get("methodID") != _OPENCODE_DEVICE_METHOD_ID:
            continue
        access = value.get("access")
        if not isinstance(access, str) or not access.strip():
            continue
        candidates.append((bool(entry.get("active")), access.strip()))
    if not candidates:
        return None
    # Stable sort: active credentials first, export order otherwise.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def read_console_access_token() -> str | None:
    """Read the OpenCode Console device access token OpenCode itself uses.

    Read-only: quse never writes to or refreshes OpenCode's credential store
    (the opencode service owns refresh); a missing/expired token simply
    yields ``None`` so callers can fall back to the legacy API key.
    """
    try:
        return _select_device_access_token(
            _exported_console_credentials(), exclude_orin=True
        )
    except Exception as exc:  # fail-open: never break quota checks here
        logger.warning(
            "opencode console credential read failed (fail-open): %s", exc
        )
        return None


def read_orin_console_access_token(data_home: Path | None = None) -> str | None:
    """Read Orin's OpenCode Console device access token (fail-open).

    Checks Orin's dedicated ``XDG_DATA_HOME`` (``~/.local/share/opencode-orin``)
    first when it contains an ``opencode`` directory, then falls back to any
    Orin-labeled/Orin-email entry in the default OpenCode Console store.
    """
    try:
        orin_home = data_home or OPENCODE_ORIN_DATA_HOME
        if (orin_home / "opencode").is_dir():
            token = _select_device_access_token(
                _exported_console_credentials(data_home=orin_home)
            )
            if token:
                return token
        return _select_device_access_token(
            _exported_console_credentials(), orin_only=True
        )
    except Exception as exc:  # fail-open: never break quota checks here
        logger.warning(
            "orin opencode console credential read failed (fail-open): %s", exc
        )
        return None
