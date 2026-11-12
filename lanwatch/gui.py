"""CustomTkinter UI for LANWatch.

The asyncio loop lives in a background thread (started by main.py). This module
never awaits anything on the Tk thread: it schedules coroutines with
``asyncio.run_coroutine_threadsafe`` and receives updates from the host/client
nodes through a thread-safe queue that is drained by ``after()``.

That split is deliberate: tkinter must only be touched from the thread that
created it, while the networking and mpv work must not block the UI.
"""

from __future__ import annotations

import asyncio
import math
import os
import queue
import tkinter as tk
from tkinter import filedialog
from typing import Any

import customtkinter as ctk

import config
import discovery
import protocol
from client import ClientNode
from host import HostNode
from overlay import CountdownOverlay
from player import MpvPlayer, PlayerError, open_in_external_player

COLOR_OK = "#4ade80"
COLOR_TEXT = "#e5e7eb"
COLOR_MUTED = "#9ca3af"
COLOR_DIM = "#6b7280"
COLOR_WARN = "#fbbf24"
COLOR_ERROR = "#f87171"

VIDEO_TYPES = [
    ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.wmv *.flv"),
    ("All files", "*.*"),
]


def format_time(seconds: float) -> str:
    """mm:ss, or h:mm:ss for long films."""
    value = max(0.0, protocol.as_float(seconds))
    total = int(value)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class LanWatchApp(ctk.CTk):
    """The LANWatch window."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        player: MpvPlayer | None,
        player_error: str | None = None,
    ) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__()

        self.title("LANWatch")
        self.geometry("1020x760")
        self.minsize(900, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._loop = loop
        self._player = player
        self._player_error = player_error
        self._events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._host: HostNode | None = None
        self._client: ClientNode | None = None
        self._mode = "host"
        self._closing = False
        self._poll_id: str | None = None
        self._seek_job: str | None = None
        self._scrubbing = False

        # Playback mirror (fed by the nodes' "state" events).
        self._position = 0.0
        self._duration = 0.0
        self._slider_duration = 0.0
        self._is_playing = False
        self._offset: float | None = None
        self._rtt: float | None = None
        self._media_path: str | None = None

        # Countdown preference (persisted), LAN discovery and transient text.
        self.countdown_enabled = config.countdown_default()
        self._browser: discovery.HostBrowser | None = None
        self._hosts: dict[str, dict[str, Any]] = {}
        self._local_countdown_job: str | None = None
        self._transient_job: str | None = None
        self._transient_text = ""

        self._build_ui()
        self._bind_shortcuts()
        self._poll_id = self.after(60, self._drain_events)
        self._refresh_ips()

        if player_error:
            self._set_status(f"mpv is not available: {player_error}", error=True)
        elif player is None:
            self._set_status(
                "mpv is not available — you can still open files with “Open File in mpv”.",
                warn=True,
            )

    # --- layout --------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_settings_row()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=6)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_connection_panel(body)
        self._build_playback_panel(body)

        self._build_peers_panel()
        self._build_status_bar()

        # Hidden until a countdown starts; placed above everything when shown.
        self.overlay = CountdownOverlay(self, on_cancel=self._cancel_countdown_by_click)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 6))

        ctk.CTkLabel(
            header, text="LANWatch", font=ctk.CTkFont(size=26, weight="bold")
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="synchronized local video playback",
            text_color=COLOR_MUTED,
        ).pack(side="left", padx=(12, 0), pady=(10, 0))

        self.mode_switch = ctk.CTkSegmentedButton(
            header,
            values=["Host Session", "Join Session"],
            command=self._on_mode_change,
        )
        self.mode_switch.set("Host Session")
        self.mode_switch.pack(side="right")

    def _build_settings_row(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 4))

        self.countdown_var = tk.BooleanVar(value=self.countdown_enabled)
        self.countdown_check = ctk.CTkCheckBox(
            bar,
            text="Countdown before play",
            variable=self.countdown_var,
            command=self._on_countdown_toggle,
            checkbox_width=18,
            checkbox_height=18,
            border_width=2,
            font=ctk.CTkFont(size=12),
        )
        self.countdown_check.pack(side="right")

    def _build_connection_panel(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, corner_radius=14)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)

        # --- host mode ---
        self.host_panel = ctk.CTkFrame(card, fg_color="transparent")
        self.host_panel.grid(row=0, column=0, sticky="nsew", padx=16, pady=14)
        self.host_panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.host_panel,
            text="Host session",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ctk.CTkLabel(self.host_panel, text="LAN IP", text_color=COLOR_MUTED).grid(
            row=1, column=0, sticky="w", pady=2
        )
        self.lan_ip_label = ctk.CTkLabel(
            self.host_panel, text="…", anchor="w", font=ctk.CTkFont(family="Consolas", size=13)
        )
        self.lan_ip_label.grid(row=1, column=1, sticky="w", padx=(10, 0))

        ctk.CTkLabel(self.host_panel, text="Public IP", text_color=COLOR_MUTED).grid(
            row=2, column=0, sticky="w", pady=2
        )
        self.public_ip_label = ctk.CTkLabel(
            self.host_panel,
            text="checking…",
            anchor="w",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(family="Consolas", size=13),
        )
        self.public_ip_label.grid(row=2, column=1, sticky="w", padx=(10, 0))

        ctk.CTkLabel(self.host_panel, text="Port", text_color=COLOR_MUTED).grid(
            row=3, column=0, sticky="w", pady=(10, 2)
        )
        self.host_port_var = tk.StringVar(value=str(config.DEFAULT_PORT))
        ctk.CTkEntry(self.host_panel, textvariable=self.host_port_var, width=100).grid(
            row=3, column=1, sticky="w", padx=(10, 0), pady=(10, 2)
        )

        self.host_button = ctk.CTkButton(
            self.host_panel,
            text="Start Hosting",
            height=38,
            command=self._toggle_hosting,
        )
        self.host_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 6))

        ctk.CTkLabel(
            self.host_panel,
            text="Share the LAN IP (or your Tailscale IP) with the other PCs.",
            text_color=COLOR_DIM,
            wraplength=360,
            justify="left",
            anchor="w",
        ).grid(row=5, column=0, columnspan=2, sticky="w")

        # --- join mode ---
        self.join_panel = ctk.CTkFrame(card, fg_color="transparent")
        self.join_panel.grid(row=0, column=0, sticky="nsew", padx=16, pady=14)
        self.join_panel.grid_columnconfigure(1, weight=1)
        self.join_panel.grid_remove()

        ctk.CTkLabel(
            self.join_panel,
            text="Join session",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.discovery_frame = ctk.CTkFrame(self.join_panel, fg_color="transparent")
        self.discovery_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.discovery_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self.discovery_frame, text="Discovered hosts", text_color=COLOR_MUTED
        ).grid(row=0, column=0, sticky="w")
        self.refresh_hosts_button = ctk.CTkButton(
            self.discovery_frame,
            text="Refresh",
            width=78,
            height=26,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._refresh_hosts,
        )
        self.refresh_hosts_button.grid(row=0, column=1, sticky="e")

        self.host_combo = ctk.CTkComboBox(
            self.discovery_frame,
            values=[],
            command=self._on_host_selected,
        )
        self.host_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.host_combo.set("Searching for hosts…")

        ctk.CTkLabel(self.join_panel, text="Host IP", text_color=COLOR_MUTED).grid(
            row=2, column=0, sticky="w", pady=3
        )
        self.host_ip_var = tk.StringVar(value=config.DEFAULT_HOST)
        ctk.CTkEntry(
            self.join_panel,
            textvariable=self.host_ip_var,
            placeholder_text="192.168.1.20",
        ).grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=3)

        ctk.CTkLabel(self.join_panel, text="Port", text_color=COLOR_MUTED).grid(
            row=3, column=0, sticky="w", pady=3
        )
        self.join_port_var = tk.StringVar(value=str(config.DEFAULT_PORT))
        ctk.CTkEntry(self.join_panel, textvariable=self.join_port_var).grid(
            row=3, column=1, sticky="ew", padx=(10, 0), pady=3
        )

        ctk.CTkLabel(self.join_panel, text="Display name", text_color=COLOR_MUTED).grid(
            row=4, column=0, sticky="w", pady=3
        )
        self.name_var = tk.StringVar(value=config.default_display_name())
        ctk.CTkEntry(self.join_panel, textvariable=self.name_var).grid(
            row=4, column=1, sticky="ew", padx=(10, 0), pady=3
        )

        self.join_button = ctk.CTkButton(
            self.join_panel,
            text="Connect",
            height=38,
            command=self._toggle_connection,
        )
        self.join_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(16, 6))

        ctk.CTkLabel(
            self.join_panel,
            text="Tip: Tailscale lets you join a friend's host over the internet "
            "without port forwarding — use their 100.x.y.z address.",
            text_color=COLOR_DIM,
            wraplength=360,
            justify="left",
            anchor="w",
        ).grid(row=6, column=0, columnspan=2, sticky="w")

        # No zeroconf: the dropdown is pointless, so it is hidden and the
        # manual IP field remains the whole UI.
        if not discovery.zeroconf_available():
            self.discovery_frame.grid_remove()

    def _build_playback_panel(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(parent, corner_radius=14)
        card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        card.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="nsew", padx=16, pady=14)
        inner.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            inner, text="Playback", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        file_row = ctk.CTkFrame(inner, fg_color="transparent")
        file_row.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.open_button = ctk.CTkButton(
            file_row, text="Open Video", width=130, command=self._open_video
        )
        self.open_button.pack(side="left")
        self.mpv_button = ctk.CTkButton(
            file_row,
            text="Open File in mpv",
            width=150,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self._open_in_mpv,
        )
        self.mpv_button.pack(side="left", padx=8)

        self.file_label = ctk.CTkLabel(
            inner,
            text="No video loaded",
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.file_label.grid(row=2, column=0, sticky="ew")

        controls = ctk.CTkFrame(inner, fg_color="transparent")
        controls.grid(row=3, column=0, sticky="w", pady=(16, 4))
        self.play_button = ctk.CTkButton(
            controls, text="Play", width=90, command=self._on_play
        )
        self.play_button.pack(side="left")
        self.pause_button = ctk.CTkButton(
            controls,
            text="Pause",
            width=90,
            fg_color="#374151",
            hover_color="#4b5563",
            command=lambda: self._local_action("pause"),
        )
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = ctk.CTkButton(
            controls,
            text="Stop",
            width=90,
            fg_color="#7f1d1d",
            hover_color="#991b1b",
            command=lambda: self._local_action("stop"),
        )
        self.stop_button.pack(side="left")
        self.resync_button = ctk.CTkButton(
            controls,
            text="Resync",
            width=96,
            fg_color="#1d4ed8",
            hover_color="#2563eb",
            command=self._resync,
        )
        self.resync_button.pack(side="left", padx=(8, 0))

        # number_of_steps=None keeps the slider continuous: with the default
        # step count, a two-hour film would snap to ~72 s increments.
        self.seek_slider = ctk.CTkSlider(
            inner,
            from_=0,
            to=1,
            number_of_steps=None,
            command=self._on_slider_move,
        )
        self.seek_slider.grid(row=4, column=0, sticky="ew", pady=(14, 2))
        self.seek_slider.set(0)

        self.time_label = ctk.CTkLabel(
            inner, text="00:00 / 00:00", anchor="w", font=ctk.CTkFont(size=13)
        )
        self.time_label.grid(row=5, column=0, sticky="w")

        self.sync_label = ctk.CTkLabel(
            inner, text="Paused", text_color=COLOR_MUTED, anchor="w"
        )
        self.sync_label.grid(row=6, column=0, sticky="w", pady=(4, 0))

        ctk.CTkLabel(
            inner,
            text="Space: play/pause    ·    S: stop    ·    R: resync    ·    ← / →: seek 10 s",
            text_color=COLOR_DIM,
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).grid(row=7, column=0, sticky="w", pady=(12, 0))

    def _build_peers_panel(self) -> None:
        card = ctk.CTkFrame(self, corner_radius=14)
        card.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 8))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text="Peers", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 2))

        self.peer_rows = ctk.CTkScrollableFrame(card, height=120, fg_color="transparent")
        self.peer_rows.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        self._render_peers([])

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 12))

        self.status_label = ctk.CTkLabel(
            bar, text="Ready.", anchor="w", text_color=COLOR_TEXT
        )
        self.status_label.pack(side="left")

        ctk.CTkLabel(
            bar, text=f"LANWatch {config.APP_VERSION}", text_color=COLOR_DIM
        ).pack(side="right")

    # --- event pump (async thread -> Tk thread) ------------------------------

    def _push_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Thread-safe: nodes and futures call this from the asyncio thread."""
        self._events.put((kind, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:  # pragma: no cover - UI must survive
                    self._set_status(f"UI error: {exc}", error=True)
        except queue.Empty:
            pass

        if not self._closing:
            self._poll_id = self.after(60, self._drain_events)

    def _handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "state":
            self._handle_state(payload)
        elif kind == "peers":
            self._render_peers(payload.get("peers") or [])
        elif kind == "connection":
            self._handle_connection(payload)
        elif kind == "warning":
            self._set_status(str(payload.get("text") or ""), warn=True)
        elif kind == "error":
            self._handle_error(payload)
        elif kind == "status":
            self._set_status(str(payload.get("text") or ""))
        elif kind == "sync":
            self._offset = protocol.as_float(payload.get("offset"))
            self._rtt = protocol.as_float(payload.get("rtt"))
            self._refresh_sync_label()
        elif kind == "lan_ip":
            self.lan_ip_label.configure(text=str(payload.get("text") or "?"))
        elif kind == "public_ip":
            text = str(payload.get("text") or "unavailable")
            self.public_ip_label.configure(
                text=text, text_color=COLOR_MUTED if text == "unavailable" else COLOR_TEXT
            )
        elif kind == "discovered_hosts":
            self._handle_discovered_hosts(payload)
        elif kind == "discovery_error":
            self.discovery_frame.grid_remove()
            self._set_status(
                str(payload.get("text") or "mDNS is unavailable"), warn=True
            )
        elif kind == "countdown":
            self.overlay.start(protocol.as_float(payload.get("remaining")))
        elif kind == "countdown_cancel":
            self.overlay.cancel()
        elif kind == "countdown_finished":
            # The overlay's own schedule is what shows GO and then hides it.
            pass

    def _handle_error(self, payload: dict[str, Any]) -> None:
        self._set_status(str(payload.get("text") or "Something went wrong"), error=True)
        category = payload.get("category")
        owner = payload.get("owner")
        if category == "host" and (owner is None or owner is self._host):
            self._host = None
            self.host_button.configure(text="Start Hosting")
        elif category == "connect" and (owner is None or owner is self._client):
            self._client = None
            self.join_button.configure(text="Connect")

    def _handle_connection(self, payload: dict[str, Any]) -> None:
        role = payload.get("role")
        connected = bool(payload.get("connected"))
        detail = str(payload.get("detail") or "")

        if not connected:
            # Never leave a countdown card on screen after the session ends.
            self.overlay.cancel()

        if role == "client":
            self.join_button.configure(text="Disconnect" if connected else "Reconnect")
            if connected:
                # Discovery has done its job; stop the multicast traffic.
                self._stop_browser()
            else:
                self._is_playing = False
                self._render_peers([])
                if self._mode == "join":
                    self._start_browser()
        elif role == "host":
            self.host_button.configure(text="Stop Hosting" if connected else "Start Hosting")
            if not connected:
                self._render_peers([])

        self._set_status(detail, error=not connected)

    def _handle_state(self, payload: dict[str, Any]) -> None:
        self._position = protocol.as_float(payload.get("position"))
        duration = protocol.as_float(payload.get("duration"))
        if duration > 0:
            self._duration = duration
        self._is_playing = bool(payload.get("is_playing"))

        offset = payload.get("offset")
        rtt = payload.get("rtt")
        if offset is not None:
            self._offset = protocol.as_float(offset)
        if rtt is not None:
            self._rtt = protocol.as_float(rtt)

        self.time_label.configure(
            text=f"{format_time(self._position)} / {format_time(self._duration)}"
        )

        if not self._scrubbing:
            if self._duration > 0 and abs(self._duration - self._slider_duration) > 0.01:
                self.seek_slider.configure(to=self._duration)
                self._slider_duration = self._duration
            self.seek_slider.set(
                min(self._position, self._duration) if self._duration else 0.0
            )

        self._refresh_sync_label()

    def _refresh_sync_label(self) -> None:
        parts = ["Playing" if self._is_playing else "Paused"]
        if self._offset is not None:
            parts.append(f"clock offset {self._offset * 1000:+.0f} ms")
        if self._rtt:
            parts.append(f"RTT {self._rtt * 1000:.0f} ms")
        self.sync_label.configure(text="   ·   ".join(parts))

    def _render_peers(self, peers: list[dict[str, Any]]) -> None:
        for child in list(self.peer_rows.winfo_children()):
            child.destroy()

        if not peers:
            ctk.CTkLabel(
                self.peer_rows,
                text="No peer connected yet.",
                text_color=COLOR_DIM,
                anchor="w",
            ).pack(anchor="w", padx=6, pady=4)
            return

        for peer in peers:
            row = ctk.CTkFrame(self.peer_rows, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)

            ctk.CTkLabel(
                row, text="●", text_color=COLOR_OK, font=ctk.CTkFont(size=15)
            ).pack(side="left", padx=(6, 4))

            name = str(peer.get("display_name") or "Peer")
            if peer.get("self"):
                name += " (you)"
            ctk.CTkLabel(row, text=name, anchor="w").pack(side="left")

            role = "Host" if peer.get("role") == "host" else "Client"
            ctk.CTkLabel(row, text=role, text_color=COLOR_MUTED).pack(
                side="right", padx=6
            )
            duration = peer.get("video_duration")
            if isinstance(duration, (int, float)) and duration > 0:
                ctk.CTkLabel(
                    row, text=format_time(float(duration)), text_color=COLOR_MUTED
                ).pack(side="right", padx=6)

    # --- async plumbing ------------------------------------------------------

    def _run(
        self,
        coro: Any,
        context: str,
        category: str | None = None,
        owner: Any = None,
    ) -> Any:
        """Schedule a coroutine on the asyncio thread; report failures in the UI.

        *owner* is the session node the coroutine belongs to. It travels with
        the error so a late failure from a node the user already replaced
        cannot reset the newer session's UI state.
        """
        loop = self._loop

        def report(message: str) -> None:
            self._push_event(
                "error",
                {"text": message, "category": category, "owner": owner},
            )

        if loop is None or loop.is_closed():
            close = getattr(coro, "close", None)
            if close:
                close()
            report(f"{context} failed: the event loop is not running")
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as exc:
            close = getattr(coro, "close", None)
            if close:
                close()
            report(f"{context} failed: {exc}")
            return None

        def _done(finished: Any) -> None:
            if finished.cancelled():
                return
            try:
                error = finished.exception()
            except Exception:  # pragma: no cover - defensive
                return
            if error is not None:
                report(f"{context} failed: {error}")

        future.add_done_callback(_done)
        return future

    # --- session control -----------------------------------------------------

    def _on_mode_change(self, value: str) -> None:
        self._mode = "host" if value.startswith("Host") else "join"
        if self._mode == "host":
            self._stop_browser()
            self.join_panel.grid_remove()
            self.host_panel.grid()
        else:
            self.host_panel.grid_remove()
            self.join_panel.grid()
            self._start_browser()

    # --- LAN discovery -------------------------------------------------------

    def _start_browser(self) -> None:
        """Run the mDNS browser while Join mode is open."""
        if not discovery.zeroconf_available():
            self.discovery_frame.grid_remove()
            return
        self.discovery_frame.grid()
        if self._browser is None:
            self._browser = discovery.HostBrowser(
                on_update=self._on_hosts_found,
                on_error=self._on_discovery_error,
            )
        if not self._browser.start():
            self.discovery_frame.grid_remove()
            return
        self._hosts = {}
        self.host_combo.configure(values=[])
        self.host_combo.set("Searching for hosts…")

    def _stop_browser(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            browser.stop()
        self._hosts = {}
        if hasattr(self, "host_combo"):
            self.host_combo.configure(values=[])
            self.host_combo.set("")

    def _refresh_hosts(self) -> None:
        self._hosts = {}
        self.host_combo.configure(values=[])
        self.host_combo.set("Searching for hosts…")
        if self._browser is None:
            self._start_browser()
        else:
            self._browser.refresh()

    def _on_hosts_found(self, hosts: list[dict[str, Any]]) -> None:
        # Called from the mDNS thread: only the queue is thread-safe here.
        self._push_event("discovered_hosts", {"hosts": hosts})

    def _on_discovery_error(self, message: str) -> None:
        self._push_event("discovery_error", {"text": message})

    def _handle_discovered_hosts(self, payload: dict[str, Any]) -> None:
        hosts = payload.get("hosts")
        if not isinstance(hosts, list):
            return

        labels: list[str] = []
        self._hosts = {}
        for host in hosts:
            if not isinstance(host, dict):
                continue
            address = str(host.get("address") or "")
            port = host.get("port")
            if not address or not isinstance(port, int):
                continue
            name = str(host.get("display_name") or "LANWatch")
            label = f"{name} — {address}:{port}"
            self._hosts[label] = host
            labels.append(label)

        labels.sort(key=str.lower)
        self.host_combo.configure(values=labels)
        if not labels:
            self.host_combo.set("No hosts found — enter the IP manually")
            return
        if self.host_combo.get() not in labels:
            self.host_combo.set(labels[0])

    def _on_host_selected(self, value: str) -> None:
        host = self._hosts.get(value)
        if host is None:
            return
        address = str(host.get("address") or "")
        port = host.get("port")
        if not address or not isinstance(port, int):
            return
        self.host_ip_var.set(address)
        self.join_port_var.set(str(port))
        self._set_status(
            f"Selected {host.get('display_name', 'host')} at {address}:{port}"
        )

    def _on_countdown_toggle(self) -> None:
        self.countdown_enabled = bool(self.countdown_var.get())
        config.set_countdown_default(self.countdown_enabled)

    def _parse_port(self, raw: str) -> int:
        port = int(str(raw).strip())
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return port

    def _toggle_hosting(self) -> None:
        # Stop hosting.
        if self._host is not None and self._host.is_hosting:
            node, self._host = self._host, None
            self.host_button.configure(text="Start Hosting")
            self._run(node.stop(), "Stopping host")
            self._render_peers([])
            return

        # Drop a node left behind by a failed start.
        if self._host is not None:
            stale, self._host = self._host, None
            self._run(stale.stop(), "Stopping host")

        try:
            port = self._parse_port(self.host_port_var.get())
        except ValueError as exc:
            self._set_status(f"Invalid port: {exc}", error=True)
            return

        # One role at a time: leave the other session if it is active.
        if self._client is not None:
            client, self._client = self._client, None
            self.join_button.configure(text="Connect")
            self._run(client.disconnect(), "Disconnecting")

        node = HostNode(
            player=self._player,
            display_name=config.default_display_name(),
            port=port,
            on_event=self._push_event,
        )
        self._host = node
        self.host_button.configure(text="Stop Hosting")
        self._set_status(f"Starting host on port {port} …")
        self._run(node.start(), "Starting host", category="host", owner=node)

    def _toggle_connection(self) -> None:
        # Disconnect.
        if self._client is not None and self._client.is_connected:
            node, self._client = self._client, None
            self.join_button.configure(text="Connect")
            self._run(node.disconnect(), "Disconnecting")
            self._render_peers([])
            return

        # Reconnect: a lost connection leaves the old node in place, so clear
        # it before joining again.
        if self._client is not None:
            stale, self._client = self._client, None
            self._run(stale.disconnect(), "Disconnecting")

        host = self.host_ip_var.get().strip() or config.DEFAULT_HOST
        try:
            port = self._parse_port(self.join_port_var.get())
        except ValueError as exc:
            self._set_status(f"Invalid port: {exc}", error=True)
            return

        # One role at a time: stop hosting first.
        if self._host is not None:
            hnode, self._host = self._host, None
            self.host_button.configure(text="Start Hosting")
            self._run(hnode.stop(), "Stopping host")

        node = ClientNode(
            player=self._player,
            display_name=self.name_var.get().strip() or config.default_display_name(),
            host=host,
            port=port,
            on_event=self._push_event,
        )
        self._client = node
        self.join_button.configure(text="Disconnect")
        self._set_status(f"Connecting to {host}:{port} …")
        self._run(node.connect(), "Connecting", category="connect", owner=node)

    def _active_node(self) -> HostNode | ClientNode | None:
        if self._host is not None and self._host.is_hosting:
            return self._host
        if self._client is not None and self._client.is_connected:
            return self._client
        return None

    # --- playback commands ---------------------------------------------------

    def _local_action(
        self,
        action: str,
        position: float | None = None,
        *,
        start_in_seconds: float = 0.0,
    ) -> None:
        if self._duration <= 0 and action != "stop":
            self._set_status("Open a video first (Open Video).", warn=True)
            return

        # Any explicit command replaces a countdown we are running ourselves.
        self._cancel_local_countdown()

        node = self._active_node()
        if node is not None:
            self._run(
                node.local_action(action, position, start_in_seconds=start_in_seconds),
                "Playback command",
                category="playback",
            )
            return

        player = self._player
        if player is None:
            self._set_status(
                "mpv is not available — use “Open File in mpv”.", error=True
            )
            return

        if position is None:
            position = self._position
        playing = self._is_playing
        if action == protocol.MSG_PLAY:
            playing = True
        elif action == protocol.MSG_PAUSE:
            playing = False
        elif action == protocol.MSG_STOP:
            position, playing = 0.0, False

        if action == protocol.MSG_PLAY and start_in_seconds > 0:
            self._begin_local_countdown(position, start_in_seconds)
            return

        self._run(
            player.apply_state(
                position, playing, force=action == protocol.MSG_SEEK
            ),
            "Playback command",
            category="playback",
        )

    def _on_play(self) -> None:
        """Play button / Space: honour the countdown preference."""
        if self._is_playing:
            self._set_status("Already playing.", warn=True)
            return
        seconds = (
            float(config.PLAY_COUNTDOWN_SECONDS) if self.countdown_enabled else 0.0
        )
        self._local_action("play", start_in_seconds=seconds)

    def _begin_local_countdown(self, position: float, seconds: float) -> None:
        """Countdown for a bare player (no host, no session)."""
        player = self._player
        if player is None:
            return
        self._run(
            player.apply_state(position, False, force=True),
            "Playback command",
            category="playback",
        )
        self._is_playing = False
        self._local_countdown_job = self.after(
            max(0, int(seconds * 1000)), self._finish_local_countdown
        )
        self.overlay.start(seconds)
        self._set_status(f"Starting in {int(math.ceil(seconds))}…")

    def _finish_local_countdown(self) -> None:
        self._local_countdown_job = None
        player = self._player
        if player is None:
            return
        self._run(
            player.apply_state(None, True),
            "Playback command",
            category="playback",
        )

    def _cancel_countdown_by_click(self) -> None:
        """Clicking the 3-2-1 card pauses, which cancels the countdown."""
        self._local_action("pause")

    def _cancel_local_countdown(self) -> None:
        if self._local_countdown_job is None:
            return
        try:
            self.after_cancel(self._local_countdown_job)
        except Exception:
            pass
        self._local_countdown_job = None
        self.overlay.cancel()

    # --- resync --------------------------------------------------------------

    def _resync(self) -> None:
        """Pull every peer onto the host's current position and state."""
        node = self._active_node()
        if node is None:
            self._set_status("Resync needs a host or a connected session.", warn=True)
            return

        self._run(node.local_resync(), "Resync", category="playback")
        self._show_transient("Resyncing...", config.RESYNC_STATUS_MS)

    def _show_transient(self, text: str, milliseconds: int) -> None:
        self._set_status(text)
        self._transient_text = text
        if self._transient_job is not None:
            try:
                self.after_cancel(self._transient_job)
            except Exception:
                pass
        self._transient_job = self.after(milliseconds, self._clear_transient)

    def _clear_transient(self) -> None:
        self._transient_job = None
        try:
            current = str(self.status_label.cget("text"))
        except Exception:  # pragma: no cover - during teardown
            return
        if current == self._transient_text:
            self._set_status("Ready.")

    def _seek_relative(self, delta: float) -> None:
        if self._duration <= 0:
            self._set_status("Open a video first (Open Video).", warn=True)
            return
        target = min(max(self._position + delta, 0.0), self._duration)
        self._local_action("seek", target)

    def _on_slider_move(self, value: float) -> None:
        # Called only for user interaction (not by programmatic .set()).
        self._scrubbing = True
        self.time_label.configure(
            text=f"{format_time(value)} / {format_time(self._duration)}"
        )
        if self._seek_job is not None:
            try:
                self.after_cancel(self._seek_job)
            except Exception:
                pass
        self._seek_job = self.after(220, self._commit_seek)

    def _commit_seek(self) -> None:
        self._seek_job = None
        if not self._scrubbing:
            return
        self._scrubbing = False
        position = float(self.seek_slider.get())
        if self._duration > 0:
            self._local_action("seek", position)

    # --- files ---------------------------------------------------------------

    def _open_video(self) -> None:
        if self._player is None:
            self._set_status(
                self._player_error or "mpv is not available — use “Open File in mpv”.",
                error=True,
            )
            return

        path = filedialog.askopenfilename(title="Open video", filetypes=VIDEO_TYPES)
        if not path:
            return

        self._media_path = path
        self.file_label.configure(text=os.path.basename(path))
        self._run(self._load_video(path), "Opening video", category="playback")

    async def _load_video(self, path: str) -> None:
        player = self._player
        if player is None:
            return
        duration = await player.open_file(path)
        if duration:
            self._duration = duration
            self._position = 0.0
        self._set_status(f"Loaded {os.path.basename(path)}")

    def _open_in_mpv(self) -> None:
        path = self._media_path
        if not path:
            path = filedialog.askopenfilename(
                title="Open file in mpv", filetypes=VIDEO_TYPES
            )
            if not path:
                return
            self._media_path = path
            self.file_label.configure(text=os.path.basename(path))

        try:
            open_in_external_player(path)
        except PlayerError as exc:
            self._set_status(str(exc), error=True)
        else:
            self._set_status(f"Opened {os.path.basename(path)} in mpv")

    # --- shortcuts -----------------------------------------------------------

    def _bind_shortcuts(self) -> None:
        self.bind("<space>", self._on_space)
        self.bind("<Key-s>", self._on_stop_key)
        self.bind("<Key-S>", self._on_stop_key)
        self.bind("<Key-r>", self._on_resync_key)
        self.bind("<Key-R>", self._on_resync_key)
        self.bind("<Left>", self._on_seek_back)
        self.bind("<Right>", self._on_seek_forward)

    def _is_typing(self) -> bool:
        try:
            focused = self.focus_get()
        except Exception:  # pragma: no cover - focus can be gone during close
            return False
        return isinstance(focused, (tk.Entry, tk.Text))

    def _on_space(self, _event: Any = None) -> str | None:
        if self._is_typing():
            return None
        if self._is_playing:
            self._local_action("pause")
        else:
            self._on_play()
        return "break"

    def _on_resync_key(self, _event: Any = None) -> str | None:
        if self._is_typing():
            return None
        self._resync()
        return "break"

    def _on_stop_key(self, _event: Any = None) -> str | None:
        if self._is_typing():
            return None
        self._local_action("stop")
        return "break"

    def _on_seek_back(self, _event: Any = None) -> str | None:
        if self._is_typing():
            return None
        self._seek_relative(-10.0)
        return "break"

    def _on_seek_forward(self, _event: Any = None) -> str | None:
        if self._is_typing():
            return None
        self._seek_relative(10.0)
        return "break"

    # --- addresses / status --------------------------------------------------

    def _refresh_ips(self) -> None:
        self.lan_ip_label.configure(text=config.get_local_ip())
        self.public_ip_label.configure(text="checking…")

        loop = self._loop
        if loop is None or loop.is_closed():
            self.public_ip_label.configure(text="unavailable")
            return

        future = asyncio.run_coroutine_threadsafe(config.fetch_public_ip(), loop)

        def _done(finished: Any) -> None:
            try:
                text = finished.result()
            except Exception:
                text = "unavailable"
            self._push_event("public_ip", {"text": text})

        future.add_done_callback(_done)

    def _set_status(
        self, text: str, *, error: bool = False, warn: bool = False
    ) -> None:
        if not text:
            return
        color = COLOR_ERROR if error else COLOR_WARN if warn else COLOR_TEXT
        self.status_label.configure(text=text, text_color=color)

    # --- shutdown ------------------------------------------------------------

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True

        self._cancel_local_countdown()
        self._stop_browser()

        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

        try:
            self._shutdown()
        finally:
            self.destroy()

    def _shutdown(self) -> None:
        """Close sessions and mpv, then stop the asyncio loop."""
        async def _close_everything() -> None:
            host, self._host = self._host, None
            client, self._client = self._client, None

            if host is not None:
                await host.stop()
            if client is not None:
                await client.disconnect()
            if self._player is not None:
                await self._player.close()

            # Cancel anything still running (tick loops, a pending connect...).
            current = asyncio.current_task()
            others = [task for task in asyncio.all_tasks() if task is not current]
            for task in others:
                task.cancel()
            if others:
                await asyncio.gather(*others, return_exceptions=True)

        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                future = asyncio.run_coroutine_threadsafe(_close_everything(), loop)
                future.result(timeout=15)
            except Exception:  # pragma: no cover - best effort shutdown
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass


def run_gui(
    loop: asyncio.AbstractEventLoop,
    player: MpvPlayer | None,
    player_error: str | None = None,
) -> None:
    """Create the window and run the Tk main loop."""
    app = LanWatchApp(loop=loop, player=player, player_error=player_error)
    app.mainloop()
