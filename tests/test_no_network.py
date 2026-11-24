"""The network guard in conftest.py: outside hosts fail loudly, loopback still works."""

import socket
import threading

import pytest

OUTSIDE = [
    ("example.com", 80),
    ("93.184.216.34", 80),
    ("2606:2800:220:1:248:1893:25c8:1946", 80),
]


def test_outside_connection_is_blocked():
    with pytest.raises(Exception, match=r"(?i)network access blocked.*example\.com"):
        socket.create_connection(("example.com", 80), timeout=2)


@pytest.mark.parametrize("address", OUTSIDE)
def test_outside_address_is_blocked_with_clear_message(address):
    with pytest.raises(Exception, match=r"(?i)network access blocked") as info:
        socket.create_connection(address, timeout=2)
    assert address[0] in str(info.value)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_raw_socket_connect_to_outside_ip_is_blocked(method):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        with pytest.raises(Exception, match=r"(?i)network access blocked"):
            getattr(sock, method)(("93.184.216.34", 80))


def test_name_lookup_of_outside_host_is_blocked():
    with pytest.raises(Exception, match=r"(?i)network access blocked"):
        socket.getaddrinfo("example.org", 443)


def test_loopback_connections_still_work():
    received = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            received.append(conn.recv(16))
            conn.sendall(b"pong")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"ping")
            assert client.recv(16) == b"pong"
    finally:
        thread.join(timeout=2)
        server.close()
    assert received == [b"ping"]
