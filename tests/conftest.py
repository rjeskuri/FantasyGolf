"""Shared fixtures. Every test runs with the network closed: CI must never hit a live site."""

import ipaddress
import socket

import pytest

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to reach a non-loopback host."""


def _is_loopback(host) -> bool:
    if host in (None, "", b""):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check(address) -> None:
    # AF_UNIX addresses are paths (str/bytes), not (host, port) tuples: always local.
    if not isinstance(address, tuple):
        return
    host = address[0]
    if not _is_loopback(host):
        raise NetworkBlockedError(
            f"network access blocked in tests: attempted to connect to {host!r}"
            f" (port {address[1] if len(address) > 1 else '?'}); use a recorded fixture"
            " or a loopback server instead"
        )


@pytest.fixture(autouse=True)
def _block_outside_network(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, port, *args, **kwargs):
        _check((host, port))
        return real_getaddrinfo(host, port, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        _check(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
