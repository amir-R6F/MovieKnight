"""mDNS (Zeroconf) advertisement and discovery for LANWatch.

The host advertises ``_lanwatch._tcp.local.`` with its display name and port;
clients browse for the same service type to fill in the IP/port fields without
anybody typing addresses.

Two rules shape this module:

* **Never break the app.** mDNS is a convenience. Every failure (library
  missing, no usable network interface, firewall blocking multicast) is logged
  and reported as ``False``; manual IP hosting/joining keeps working.
* **Never block the UI.** Registering and browsing both happen on background
  threads, and results are delivered through a plain callback that the GUI
  funnels into its existing event queue.

Addresses are taken from real non-loopback IPv4 interfaces. Advertising only
on loopback would put an unreachable ``127.0.0.1`` in front of every client.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
import time
from typing import Any, Callable

import config

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the environment
    from zeroconf import (
        IPVersion,
        ServiceBrowser,
        ServiceInfo,
        ServiceListener,
        Zeroconf,
    )

    _IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover
    IPVersion = None  # type: ignore[assignment]
    ServiceBrowser = None  # type: ignore[assignment]
    ServiceInfo = None  # type: ignore[assignment]
    ServiceListener = object  # type: ignore[assignment,misc]
    Zeroconf = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(_exc)

#: A discovered host, as handed to the GUI.
HostRecord = dict[str, Any]
HostUpdate = Callable[[list[HostRecord]], None]


def zeroconf_available() -> bool:
    return _IMPORT_ERROR is None


def zeroconf_error() -> str | None:
    return _IMPORT_ERROR


# --- helpers -----------------------------------------------------------------


def local_addresses() -> list[str]:
    """Usable non-loopback IPv4 addresses of this machine."""
    found: list[str] = []

    def offer(value: str) -> None:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return
        if not isinstance(address, ipaddress.IPv4Address):
            return
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            return
        text = str(address)
        if text not in found:
            found.append(text)

    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            offer(entry[4][0])
    except OSError:
        pass

    # Same trick as config.get_local_ip: learn the outbound interface without
    # sending a packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.3)
            sock.connect(("8.8.8.8", 80))
            offer(sock.getsockname()[0])
    except OSError:
        pass

    return found


def sanitize_instance_name(name: str, fallback: str = "LANWatch") -> str:
    """Make a service instance label safe for mDNS.

    Dots, slashes and '@' are separators/escapes in DNS-SD names, so they are
    dropped. Unicode (e.g. Persian display names) is allowed and left alone.
    """
    cleaned = "".join(
        character
        for character in str(name)
        if character.isprintable() and character not in "./\\@"
    )
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned[:40].rstrip()
    return cleaned or fallback


def sanitize_host_label(name: str) -> str:
    """A DNS label for the ``server`` field (letters, digits and hyphens)."""
    cleaned = "".join(
        character
        for character in str(name).strip().lower()
        if character.isalnum() or character == "-"
    ).strip("-")
    return (cleaned or "lanwatch")[:40]


def _decode_property(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return ""


# --- advertising (host side) -------------------------------------------------


class ServiceAdvertiser:
    """Publishes one LANWatch host as an mDNS service."""

    def __init__(
        self,
        port: int,
        display_name: str,
        *,
        host_label: str | None = None,
    ) -> None:
        self.port = int(port)
        self.display_name = str(display_name)
        self.host_label = sanitize_host_label(host_label or socket.gethostname())
        self.instance_name = sanitize_instance_name(
            f"LANWatch on {socket.gethostname()} ({self.display_name})"
        )
        self.service_name = f"{self.instance_name}.{config.MDNS_SERVICE_TYPE}"
        self._zeroconf: Any = None
        self._info: Any = None

    @property
    def is_advertising(self) -> bool:
        return self._zeroconf is not None and self._info is not None

    def start(self) -> bool:
        """Register the service. Blocking (name probing); call from a thread."""
        if _IMPORT_ERROR is not None:
            log.warning("mDNS advertising unavailable: %s", _IMPORT_ERROR)
            return False
        if self._zeroconf is not None:
            return True

        addresses = local_addresses()
        if not addresses:
            log.warning(
                "No non-loopback IPv4 address found; not advertising over mDNS "
                "(loopback addresses are useless to other machines)."
            )
            return False

        try:
            zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        except Exception as exc:  # pragma: no cover - environment dependent
            log.warning("Could not start mDNS: %s", exc)
            return False

        try:
            info = ServiceInfo(
                config.MDNS_SERVICE_TYPE,
                self.service_name,
                addresses=[socket.inet_aton(address) for address in addresses],
                port=self.port,
                properties={
                    "display_name": self.display_name,
                    "version": "1",
                    "requires_password": "0",
                },
                server=f"{self.host_label}.local.",
            )
            zeroconf.register_service(info, allow_name_change=True)
        except Exception as exc:
            log.warning("Could not register the mDNS service: %s", exc)
            with contextlib.suppress(Exception):
                zeroconf.close()
            return False

        self._zeroconf = zeroconf
        self._info = info
        log.info(
            "Advertising %r on port %d (%s)",
            self.instance_name,
            self.port,
            ", ".join(addresses),
        )
        return True

    def stop(self) -> None:
        """Unregister and close. Safe to call twice; never raises."""
        zeroconf, info = self._zeroconf, self._info
        self._zeroconf = None
        self._info = None
        if zeroconf is None:
            return
        try:
            if info is not None:
                zeroconf.unregister_service(info)
        except Exception as exc:  # pragma: no cover - shutdown best effort
            log.debug("Could not unregister the mDNS service: %s", exc)
        with contextlib.suppress(Exception):
            zeroconf.close()


# --- browsing (client side) --------------------------------------------------


class _Listener(ServiceListener):
    """Zeroconf callbacks; runs on zeroconf's own threads."""

    def __init__(self, owner: "HostBrowser") -> None:
        self._owner = owner

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._owner._handle_service(zeroconf, service_type, name)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._owner._handle_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._owner._handle_removed(name)


class HostBrowser:
    """Browses for LANWatch hosts and reports a sorted list on every change."""

    def __init__(
        self,
        on_update: HostUpdate | None = None,
        on_error: Callable[[str], None] | None = None,
        service_type: str = config.MDNS_SERVICE_TYPE,
    ) -> None:
        self.service_type = service_type
        self._on_update = on_update
        self._on_error = on_error
        self._hosts: dict[str, HostRecord] = {}
        self._lock = threading.Lock()
        self._zeroconf: Any = None
        self._browser: Any = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    @property
    def is_browsing(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def hosts(self) -> list[HostRecord]:
        with self._lock:
            records = list(self._hosts.values())
        return sorted(records, key=lambda record: record["display_name"].lower())

    def start(self) -> bool:
        """Begin browsing on a background thread. False = mDNS unavailable."""
        if _IMPORT_ERROR is not None:
            self._report_error(f"mDNS is not available: {_IMPORT_ERROR}")
            return False
        if self.is_browsing:
            return True

        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="lanwatch-mdns-browse", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop browsing and wait briefly for the thread to finish."""
        self._stopping = True
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        with self._lock:
            self._hosts.clear()
        self._emit()

    def refresh(self) -> None:
        """Clear the list and ask again (picks up currently cached hosts)."""
        with self._lock:
            self._hosts.clear()
        self._emit()

        zeroconf = self._zeroconf
        if zeroconf is None:
            self.start()
            return
        try:
            if self._browser is not None:
                self._browser.cancel()
            self._browser = ServiceBrowser(zeroconf, self.service_type, _Listener(self))
        except Exception as exc:  # pragma: no cover - environment dependent
            log.debug("mDNS refresh failed: %s", exc)

    # --- worker ----------------------------------------------------------

    def _run(self) -> None:
        try:
            zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        except Exception as exc:  # pragma: no cover - environment dependent
            self._report_error(f"mDNS is not available: {exc}")
            return

        self._zeroconf = zeroconf
        try:
            self._browser = ServiceBrowser(zeroconf, self.service_type, _Listener(self))
            self._emit()
            while not self._stopping:
                time.sleep(0.2)
        except Exception as exc:  # pragma: no cover - environment dependent
            log.warning("mDNS browsing failed: %s", exc)
            self._report_error(f"mDNS browsing failed: {exc}")
        finally:
            browser, self._browser = self._browser, None
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.cancel()
            self._zeroconf = None
            with contextlib.suppress(Exception):
                zeroconf.close()

    # --- zeroconf callbacks ----------------------------------------------

    def _handle_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        if name == getattr(self, "_own_service_name", None):
            return
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
        except Exception:  # pragma: no cover - resolution races
            return
        if info is None:
            return

        addresses = [
            address
            for address in info.parsed_addresses()
            if ":" not in address and not address.startswith("127.")
        ]
        if not addresses:
            return

        properties = info.properties or {}
        display_name = _decode_property(properties.get(b"display_name")).strip()
        if not display_name:
            display_name = name.split(".")[0]

        record: HostRecord = {
            "id": name,
            "name": name,
            "display_name": display_name[:40],
            "address": addresses[0],
            "port": int(info.port or 0),
        }

        with self._lock:
            self._hosts[name] = record
        self._emit()

    def _handle_removed(self, name: str) -> None:
        with self._lock:
            removed = self._hosts.pop(name, None)
        if removed is not None:
            log.info("mDNS host disappeared: %s", removed["display_name"])
            self._emit()

    # --- plumbing --------------------------------------------------------

    def _emit(self) -> None:
        callback = self._on_update
        if callback is None:
            return
        try:
            callback(self.hosts)
        except Exception:  # pragma: no cover - a UI bug must not kill mDNS
            log.exception("mDNS update callback raised")

    def _report_error(self, message: str) -> None:
        log.warning("%s", message)
        callback = self._on_error
        if callback is None:
            return
        try:
            callback(message)
        except Exception:  # pragma: no cover
            log.exception("mDNS error callback raised")
