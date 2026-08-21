import socket

import pytest


class _Flow:
    server_name = "demo"

    def deliver_callback(self, **_kwargs):
        pass


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_loopback_listener_honors_fixed_port_and_redirect_host():
    from tui_gateway.mcp_oauth_sessions import _start_loopback_listener

    port = _free_port()
    httpd, redirect_uri = _start_loopback_listener(
        _Flow(), {"oauth": {"redirect_port": port, "redirect_host": "127.0.0.1"}}
    )
    try:
        assert httpd.server_address[1] == port
        assert httpd.server_address[0] == "127.0.0.1"
        assert redirect_uri == f"http://127.0.0.1:{port}/callback"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_busy_fixed_port_fails_before_authorization_worker_starts(monkeypatch):
    from tui_gateway import mcp_oauth_sessions as sessions

    blocker = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    started = []
    monkeypatch.setattr(sessions.threading.Thread, "start", lambda self: started.append(self))
    try:
        with pytest.raises(RuntimeError, match=f"configured OAuth callback port {port}.*cannot bind"):
            sessions.start_flow(
                "/profile/home",
                "demo",
                {"url": "https://mcp.example.test", "oauth": {"redirect_port": port}},
            )
        assert started == []
    finally:
        blocker.close()
