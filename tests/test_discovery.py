"""mDNS tests. The network test skips when multicast cannot work here."""

from __future__ import annotations

import time
import uuid

import pytest

import discovery

pytest.importorskip("zeroconf")


def test_sanitize_instance_name_removes_dns_separators():
    cleaned = discovery.sanitize_instance_name("My.Host/Name@home")

    assert "." not in cleaned
    assert "/" not in cleaned
    assert "@" not in cleaned
    assert cleaned


def test_sanitize_instance_name_has_a_fallback():
    assert discovery.sanitize_instance_name("///") == "LANWatch"


def test_sanitize_host_label_is_a_dns_label():
    assert discovery.sanitize_host_label("My PC!.local") == "mypclocal"
    assert discovery.sanitize_host_label("") == "lanwatch"


def test_local_addresses_never_include_loopback():
    for address in discovery.local_addresses():
        assert not address.startswith("127.")
        assert ":" not in address


def test_host_is_advertised_then_removed():
    if not discovery.zeroconf_available():
        pytest.skip("zeroconf is unavailable")

    port = 20000 + (uuid.uuid4().int % 20000)
    display_name = f"CI Host {uuid.uuid4().hex[:6]}"

    advertiser = discovery.ServiceAdvertiser(port, display_name)
    if not advertiser.start():
        pytest.skip("mDNS could not start (no non-loopback interface or blocked multicast)")

    updates: list[list[dict]] = []
    browser = discovery.HostBrowser(on_update=lambda hosts: updates.append(hosts))
    assert browser.start(), "the browser should start when zeroconf is available"

    try:
        deadline = time.monotonic() + 5.0
        found = None
        while time.monotonic() < deadline and found is None:
            for hosts in list(updates):
                for host in hosts:
                    if host["port"] == port and host["display_name"] == display_name:
                        found = host
                        break
                if found is not None:
                    break
            time.sleep(0.1)

        assert found is not None, f"service was not discovered; updates={updates}"
        assert found["address"] and not found["address"].startswith("127.")

        advertiser.stop()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and browser.hosts:
            time.sleep(0.1)
        assert browser.hosts == [], "removal must be reported within 5 s"
    finally:
        advertiser.stop()
        browser.stop()
