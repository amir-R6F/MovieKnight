"""GUI smoke tests: no rendering, just the widget wiring.

They need Tk and a display, so a headless CI machine reports them as skipped.
The behaviour behind the widgets is covered headlessly by the integration
tests; these check that the buttons, checkbox and combobox are connected to the
right methods and initial state.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

import config  # noqa: E402
import gui  # noqa: E402


class FakeHostNode:
    """Stands in for HostNode so the smoke test binds no port and no mDNS."""

    instances: list["FakeHostNode"] = []

    def __init__(self, *, player, display_name, port, on_event) -> None:
        self.player = player
        self.display_name = display_name
        self.port = port
        self.on_event = on_event
        self.is_hosting = False
        self.stopped = False
        FakeHostNode.instances.append(self)

    async def start(self) -> None:
        self.is_hosting = True

    async def stop(self) -> None:
        self.stopped = True
        self.is_hosting = False

    async def local_action(self, action, position=None, *, start_in_seconds=0.0):
        return None

    async def local_resync(self) -> None:
        return None


@pytest.fixture
def app(settings_file, monkeypatch):
    # Keep the app's best-effort public-IP lookup off the network.
    async def _no_public_ip() -> str:
        return "unavailable"

    monkeypatch.setattr(config, "fetch_public_ip", _no_public_ip)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_forever, name="lanwatch-gui-loop", daemon=True
    )
    thread.start()

    try:
        application = gui.LanWatchApp(loop=loop, player=None)
    except tk.TclError as exc:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        pytest.skip(f"tkinter has no display here: {exc}")

    # A smoke test must not open sockets or send multicast.
    application._start_browser = lambda: None
    application._stop_browser = lambda: None
    application.update()

    try:
        yield application
    finally:
        application._closing = True
        try:
            application._cancel_local_countdown()
            application.update()
            application.destroy()
        except Exception:  # pragma: no cover - teardown best effort
            pass
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)


def _pump(app, predicate, timeout: float = 3.0) -> bool:
    """Run the Tk event loop until *predicate* holds (or time runs out)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --- initial state -----------------------------------------------------------


def test_window_widgets_exist_in_their_initial_state(app):
    assert app.title() == "LANWatch"
    assert app.mode_switch.get() == "Host Session"
    assert app._mode == "host"

    assert app.resync_button.cget("text") == "Resync"
    assert app.resync_button.cget("state") == "normal"

    assert app.countdown_check.cget("text") == "Countdown before play"
    assert app.countdown_var.get() is True, "the countdown defaults to on"

    assert app.host_button.cget("text") == "Start Hosting"
    assert app.join_button.cget("text") == "Connect"
    assert app._active_node() is None
    assert str(app.status_label.cget("text"))


# --- countdown overlay -------------------------------------------------------


def test_countdown_overlay_shows_numbers_and_hides(app):
    app.overlay.start(3.0)
    app.update()

    assert app.overlay.is_active
    assert app.overlay.current_text == "3"

    app.overlay.cancel()
    app.update()
    assert not app.overlay.is_active

    app.overlay.start(0.2)
    app.update()
    assert _pump(app, lambda: app.overlay.current_text == "GO", 1.0)

    app.overlay.cancel()
    app.update()
    assert not app.overlay.is_active


# --- resync ------------------------------------------------------------------


def test_resync_button_calls_the_controller(app, monkeypatch):
    calls: list[str] = []

    class FakeNode:
        def local_resync(self):
            calls.append("resync")

            async def _noop() -> None:
                return None

            return _noop()

    scheduled: list[str] = []

    def fake_run(coro, context, category=None, owner=None):
        scheduled.append(context)
        coro.close()
        return None

    monkeypatch.setattr(app, "_active_node", lambda: FakeNode())
    monkeypatch.setattr(app, "_run", fake_run)

    command = getattr(app.resync_button, "_command", None)
    assert callable(command), "the Resync button must have a command"
    command()

    assert calls == ["resync"]
    assert scheduled == ["Resync"]


# --- settings ----------------------------------------------------------------


def test_countdown_checkbox_persists_the_preference(app, settings_file):
    app.countdown_var.set(False)
    app._on_countdown_toggle()

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored[config.SETTINGS_COUNTDOWN_KEY] is False
    assert app.countdown_enabled is False

    app.countdown_var.set(True)
    app._on_countdown_toggle()

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored[config.SETTINGS_COUNTDOWN_KEY] is True
    assert app.countdown_enabled is True


# --- discovery ---------------------------------------------------------------


def test_discovered_hosts_fill_the_combobox_and_the_form(app):
    app._handle_event(
        "discovered_hosts",
        {
            "hosts": [
                {
                    "id": "svc-1",
                    "display_name": "Living Room",
                    "address": "192.168.1.50",
                    "port": 9000,
                },
                {
                    "id": "svc-2",
                    "display_name": "Attic",
                    "address": "192.168.1.51",
                    "port": 9001,
                },
            ]
        },
    )
    app.update()

    values = list(app.host_combo.cget("values"))
    assert "Living Room — 192.168.1.50:9000" in values
    assert "Attic — 192.168.1.51:9001" in values

    app._on_host_selected("Attic — 192.168.1.51:9001")

    assert app.host_ip_var.get() == "192.168.1.51"
    assert app.join_port_var.get() == "9001"


# --- session control ---------------------------------------------------------


def test_start_hosting_creates_and_starts_a_host(app, monkeypatch):
    FakeHostNode.instances.clear()
    monkeypatch.setattr(gui, "HostNode", FakeHostNode)
    app.host_port_var.set("8899")

    app._toggle_hosting()

    assert app._host is not None
    assert _pump(app, lambda: app._host.is_hosting, 2.0)
    assert app.host_button.cget("text") == "Stop Hosting"
    assert FakeHostNode.instances[-1].port == 8899

    app._toggle_hosting()

    assert _pump(app, lambda: app.host_button.cget("text") == "Start Hosting", 2.0)
    assert FakeHostNode.instances[-1].stopped is True


def test_mode_switch_shows_the_right_panel(app):
    app.mode_switch.set("Join Session")
    app._on_mode_change("Join Session")

    assert _pump(app, lambda: bool(app.join_panel.winfo_ismapped()), 2.0)
    assert app._mode == "join"
    assert not app.host_panel.winfo_ismapped()

    app.mode_switch.set("Host Session")
    app._on_mode_change("Host Session")

    assert _pump(app, lambda: bool(app.host_panel.winfo_ismapped()), 2.0)
    assert app._mode == "host"
