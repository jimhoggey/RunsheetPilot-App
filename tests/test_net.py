"""Finding ProPresenter, and the guard on where the app will connect.

Both exist because of things established against a real ProPresenter:
PP does not always listen on 50001 (a machine here ran on 55416, which made
every lookup fail silently while the UI looked fine), and the app fetches
a host taken from the request body, which makes it an SSRF sink.
"""
import pytest

from propresenterrunsheet.propresenter import discovery, net
from propresenterrunsheet.propresenter.net import (
    UnreachableHost, pp_base, resolve_pp_host)


# ── port discovery ───────────────────────────────────────────────────────

def test_a_working_configured_port_is_never_second_guessed():
    """An operator who typed a port meant it."""
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: True)
    assert port == "50001" and note == ""


def test_the_real_port_is_found_when_the_default_is_wrong(monkeypatch):
    """The actual failure: PP on 55416 while the app assumed 50001."""
    monkeypatch.setattr(discovery, "local_pp_settings",
                        lambda platform=None: {"port": 55416, "enabled": True})
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: p == "55416")
    assert port == "55416"
    assert "55416" in note


def test_network_switched_off_is_named_as_the_cause(monkeypatch):
    monkeypatch.setattr(discovery, "local_pp_settings",
                        lambda platform=None: {"port": 55416, "enabled": False})
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: False)
    assert "Network" in note and "switched off" in note


def test_a_remote_host_is_told_where_to_look_not_scanned(monkeypatch):
    """Port-scanning someone else's machine is slow and rude; the useful
    output is telling them where the number lives."""
    def boom(platform=None):
        raise AssertionError("must not read local prefs for a remote host")
    monkeypatch.setattr(discovery, "local_pp_settings", boom)
    port, note = discovery.resolve_port(
        "192.168.1.50", "50001", probe=lambda h, p: False)
    assert port == "50001"
    assert "Integrations" in note and "not always 50001" in note


def test_prefs_port_that_does_not_answer_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(discovery, "local_pp_settings",
                        lambda platform=None: {"port": 55416, "enabled": True})
    port, note = discovery.resolve_port(
        "localhost", "50001", probe=lambda h, p: False)
    assert port == "50001"
    assert "is propresenter running" in note.lower()


@pytest.mark.parametrize("host,expected", [
    ("localhost", True), ("127.0.0.1", True), ("", True),
    ("192.168.1.50", False), ("Sound-Desk-Mac.local", False),
])
def test_is_local(host, expected):
    assert discovery.is_local(host) is expected


def test_local_pp_settings_never_raises_on_an_unknown_platform():
    assert discovery.local_pp_settings(platform="linux") == {}


# ── the SSRF boundary on the ProPresenter host ───────────────────────────
# The guard works by CONSTRUCTION: the host is validated, resolved ONCE,
# and the URL is built from the vetted IP — never from the caller's
# string. Pinning is what closes the validate-then-refetch gap (DNS
# rebinding) and makes IP-literal spelling tricks irrelevant.

def is_reachable_pp_host(host) -> bool:
    return resolve_pp_host(host) is not None


# Name lookups go through a fake resolver so these tests mean the same
# thing on this Mac (where Sound-Desk-Mac.local really resolves) and
# on a CI runner (where nothing does).
_FAKE_DNS = {
    "sound-desk-mac.local": ["192.168.1.50"],
    "macbook": ["192.168.1.20"],
    "av1": ["10.0.0.5"],
    "example.com": ["93.184.216.34"],
    "169.254.169.254.nip.io": ["169.254.169.254"],
    # Dual-stack LAN name that ALSO publishes a public v6 record — the
    # public record must not brick the name, and must never be dialled.
    "dualstack": ["192.168.1.30", "2001:db8::1"],
}


@pytest.fixture(autouse=True)
def _fake_resolver(monkeypatch):
    def fake_getaddrinfo(host, *_a, **_k):
        ips = _FAKE_DNS.get(host)
        if not ips:
            raise OSError(f"unresolvable: {host}")
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]
    monkeypatch.setattr(net.socket, "getaddrinfo", fake_getaddrinfo)
    net.reset_cache()
    yield
    net.reset_cache()


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "::1", "",
    "192.168.1.50",              # the operator's own LAN address
    "10.0.0.5", "172.16.4.9",
    "Sound-Desk-Mac.local",      # mDNS, what PP advertises itself as
    "macbook",                   # bare LAN name
    "dualstack",                 # LAN name with a stray public record
    "169.254.1.1",               # direct ethernet between two machines
])
def test_real_propresenter_hosts_are_allowed(host):
    assert is_reachable_pp_host(host) is True, host


@pytest.mark.parametrize("host", [
    "8.8.8.8", "1.1.1.1", "example.com",
    "169.254.169.254",            # cloud metadata — the classic SSRF target
    "169.254.169.254.nip.io",     # …and the DNS trick that reaches it
    "134744072", "0x8080808",     # 8.8.8.8 spelt as dotless int / hex
])
def test_public_and_metadata_hosts_are_refused(host):
    assert is_reachable_pp_host(host) is False, host


def test_an_unresolvable_name_is_treated_as_unreachable():
    """Failing closed: 'we couldn't check' must not mean 'allowed'.
    Unresolvable also means unconnectable, so nothing real is lost."""
    assert is_reachable_pp_host("no-such-host-anywhere.invalid") is False


# ── the pin: the URL never contains the caller's string ──────────────────

def test_pp_base_pins_a_name_to_its_vetted_address():
    """The rebinding defence: we connect to the address we vetted, so a
    name that answers differently at fetch time gains nothing."""
    assert pp_base("macbook", "50001") == "http://192.168.1.20:50001"


def test_pp_base_dials_the_private_record_of_a_dualstack_name():
    assert pp_base("dualstack", "50001") == "http://192.168.1.30:50001"


def test_pp_base_brackets_ipv6():
    """'::1' used to be mangled to 'http://1:50001' by the character
    strip — a request to host "1", not IPv6 loopback."""
    assert pp_base("::1", "50001") == "http://[::1]:50001"


def test_pp_base_refuses_with_a_message_an_operator_can_act_on():
    with pytest.raises(UnreachableHost) as e:
        pp_base("8.8.8.8", "50001")
    assert "localhost" in str(e.value)


def test_pp_base_keeps_accepting_what_operators_actually_type():
    """The port box is free text. Stray characters, a blank box and
    PP's default all behave exactly as they did before the int change."""
    assert pp_base("macbook", " 55416 ") == "http://192.168.1.20:55416"
    assert pp_base("macbook", "50,001") == "http://192.168.1.20:50001"
    assert pp_base("macbook", "") == "http://192.168.1.20:50001"
    assert pp_base("macbook", None) == "http://192.168.1.20:50001"
    assert pp_base("macbook", 50001) == "http://192.168.1.20:50001"


@pytest.mark.parametrize("port", ["0", "65536", "99999999999999999999"])
def test_pp_base_refuses_a_port_that_cannot_exist(port):
    """Refused rather than silently swapped for 50001: a request to a
    port the operator didn't type is worse than saying theirs can't
    exist. Same exception as a bad host, so every route already turns
    it into a plain message."""
    with pytest.raises(UnreachableHost) as e:
        pp_base("macbook", port)
    assert "port" in str(e.value)


def test_the_resolution_cache_expires(monkeypatch):
    """A machine that changes address is picked up after the TTL."""
    assert pp_base("macbook", "1") == "http://192.168.1.20:1"
    _FAKE_DNS["macbook"] = ["192.168.1.99"]
    try:
        real = __import__("time").time()
        monkeypatch.setattr(net.time, "time", lambda: real + 3600)
        assert pp_base("macbook", "1") == "http://192.168.1.99:1"
    finally:
        _FAKE_DNS["macbook"] = ["192.168.1.20"]
