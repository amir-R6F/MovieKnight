"""LANWatch constants and small platform helpers.

Everything tunable lives here so the rest of the code stays free of magic
numbers. The defaults are chosen for a LAN, where one-way latency is a few
milliseconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

APP_NAME = "LANWatch"
APP_VERSION = "1.1"

log = logging.getLogger(__name__)

# --- networking --------------------------------------------------------------

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"

#: Seconds to wait for the TCP/WebSocket handshake.
CONNECT_TIMEOUT = 8.0
#: Seconds to wait for the welcome that answers our join.
JOIN_TIMEOUT = 8.0
#: The host broadcasts state (and the peer list) this often.
HEARTBEAT_INTERVAL = 2.0
#: A client that has been silent this long is dropped from the peer list
#: (three missed heartbeats, so a brief hiccup is not fatal).
PEER_TIMEOUT = 6.0
#: How often each peer reads its own player to refresh the GUI.
TICK_INTERVAL = 0.25
#: A client seeks only when it is further than this from the host position,
#: so ordinary mpv clock jitter never causes a visible correction.
DRIFT_THRESHOLD = 0.40

#: Best-effort public IP lookup for the host panel.
PUBLIC_IP_URL = "https://ifconfig.me/ip"
PUBLIC_IP_TIMEOUT = 5.0

# --- clock sync --------------------------------------------------------------

#: NTP-style rounds per client connect (the median offset is used).
CLOCK_SYNC_ROUNDS = 5
#: Per-round timeout; a lost pong just means one fewer sample.
CLOCK_SYNC_TIMEOUT = 1.0

# --- playback ----------------------------------------------------------------

#: Ignore re-seek requests smaller than this (seconds); avoids micro-jumps.
SEEK_EPSILON = 0.05
#: Duration difference (seconds) that triggers the "different video" warning.
DURATION_MISMATCH_TOLERANCE = 1.0
#: 3-2-1 countdown length used when "Countdown before play" is on.
PLAY_COUNTDOWN_SECONDS = 3
#: How long "Resyncing..." stays in the status bar at most.
RESYNC_STATUS_MS = 2000

# --- mDNS discovery ----------------------------------------------------------

MDNS_SERVICE_TYPE = "_lanwatch._tcp.local."
MDNS_ENABLED = True

# --- settings ----------------------------------------------------------------

SETTINGS_APP_DIR = "LANWatch"
SETTINGS_FILENAME = "settings.json"
SETTINGS_COUNTDOWN_KEY = "countdown_before_play"

# --- mpv ---------------------------------------------------------------------

#: Named-pipe base name. The PID is appended so two LANWatch instances can run
#: on the same machine (e.g. host and client for testing) without fighting over
#: one pipe. On Windows this becomes \\.\pipe\<name>_<pid>.
MPV_PIPE_BASE = "lanwatch_mpv"


def mpv_pipe_name() -> str:
    """Pipe/socket name for this process's mpv instance."""
    return f"{MPV_PIPE_BASE}_{os.getpid()}"


def find_mpv() -> str | None:
    """Locate the mpv executable, or return None.

    Order: the LANWATCH_MPV override, then PATH, then the usual Windows install
    locations. ``mpv`` must be at least 0.33 for full JSON IPC support.
    """
    override = os.environ.get("LANWATCH_MPV")
    if override and os.path.isfile(override):
        return override

    found = shutil.which("mpv")
    if found:
        return found

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Program Files (x86)\mpv\mpv.exe",
            r"C:\mpv\mpv.exe",
        ]
        if local_appdata:
            candidates.append(os.path.join(local_appdata, "Programs", "mpv", "mpv.exe"))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
    return None


# --- identity / addresses ----------------------------------------------------


def default_display_name() -> str:
    """A sensible default peer name (the Windows computer name)."""
    name = os.environ.get("COMPUTERNAME") or socket.gethostname() or "Peer"
    return name[:32]


def get_local_ip() -> str:
    """Best-effort LAN address of this machine.

    Opens a UDP socket towards a public address to learn which interface the
    OS would use. No packets are sent and no connection is established.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.3)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def _fetch_public_ip_blocking(timeout: float = PUBLIC_IP_TIMEOUT) -> str:
    with urllib.request.urlopen(PUBLIC_IP_URL, timeout=timeout) as response:
        return response.read(64).decode("ascii", "ignore").strip()


async def fetch_public_ip() -> str:
    """Public IP via ifconfig.me, or the string "unavailable".

    Best effort only: the app works fine on a LAN with no internet access.
    """
    try:
        address = await asyncio.to_thread(_fetch_public_ip_blocking)
    except Exception:
        return "unavailable"
    return address or "unavailable"


def is_windows() -> bool:
    return sys.platform.startswith("win")


# --- settings persistence ----------------------------------------------------


def settings_path() -> Path:
    r"""Where the user's preferences live.

    ``LANWATCH_SETTINGS`` overrides the location (used by tests); otherwise
    ``%APPDATA%\LANWatch\settings.json`` on Windows and the XDG config directory
    elsewhere. Never inside the program folder, which may be read-only.
    """
    override = os.environ.get("LANWATCH_SETTINGS")
    if override:
        return Path(override).expanduser()

    if is_windows():
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / SETTINGS_APP_DIR / SETTINGS_FILENAME

    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lanwatch" / SETTINGS_FILENAME


def load_settings() -> dict:
    """Read the settings file; a missing or broken file means defaults."""
    path = settings_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read settings from %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict) -> bool:
    """Persist the settings atomically. Returns False instead of raising."""
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, ensure_ascii=False)
        temporary.replace(path)
        return True
    except OSError as exc:
        log.warning("Could not save settings to %s: %s", path, exc)
        return False


def countdown_default() -> bool:
    """The persisted "countdown before play" preference (default: on)."""
    value = load_settings().get(SETTINGS_COUNTDOWN_KEY, True)
    return bool(value)


def set_countdown_default(enabled: bool) -> bool:
    """Persist the countdown preference, keeping any other settings intact."""
    settings = load_settings()
    settings[SETTINGS_COUNTDOWN_KEY] = bool(enabled)
    return save_settings(settings)
