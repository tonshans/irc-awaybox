"""
Tests for the IRC keepalive / timeout hardening.

Covers:
- config defaults & parsing of the new [reconnect] keepalive keys
- client timers seeded on connect
- proactive PING after idle interval (both in-band and on read timeout)
- dead-link detection: server silent past pong deadline -> disconnect + alert
- any inbound traffic clears the awaiting-pong state
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from awaybox.config import AppConfig, IRCConfig, ReconnectConfig, AutoReplyConfig, TelegramConfig, LoggingConfig
from awaybox.irc_client import IRCClient
from awaybox.logger import AwayBoxLogger
from awaybox.state import RuntimeState


def _make_logger(tmp_path):
    return AwayBoxLogger(log_dir=str(tmp_path))


def _make_config(**rec_overrides):
    rec_kwargs = dict(
        initial_delay_seconds=1,
        max_delay_seconds=2,
        backoff_multiplier=2.0,
        read_timeout_seconds=0.2,
        ping_interval_seconds=0.3,
        pong_deadline_seconds=0.5,
    )
    rec_kwargs.update(rec_overrides)
    return AppConfig(
        irc=IRCConfig(server="irc.example.net", port=6697, tls=False,
                      nick="AwayBox", username="_AwayBox_", realname="AwayBox"),
        autoreply=AutoReplyConfig(),
        telegram=TelegramConfig(allowed_user_id=1),
        logging=LoggingConfig(),
        reconnect=ReconnectConfig(**rec_kwargs),
    )


class FakeSock:
    """Scriptable socket replacement used by the worker loop tests."""

    def __init__(self, chunks=None):
        self._chunks = list(chunks or [])
        self.sent = []
        self.closed = False
        self.timeout = None
        self._cv = threading.Condition()

    # -- setup API -----------------------------------------------------
    def settimeout(self, t):
        self.timeout = t

    def feed(self, data: bytes):
        with self._cv:
            self._chunks.append(data)
            self._cv.notify_all()

    # -- send side -----------------------------------------------------
    def sendall(self, data: bytes):
        if self.closed:
            raise OSError("socket closed")
        self.sent.append(data.decode("utf-8"))

    # -- recv side -----------------------------------------------------
    def recv(self, n):
        deadline = time.monotonic() + (self.timeout or 0.2)
        with self._cv:
            while not self._chunks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("timed out")
                self._cv.wait(min(remaining, 0.05))
                if self.closed:
                    raise OSError("socket closed")
            return self._chunks.pop(0)

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    # -- helpers -------------------------------------------------------
    def sent_containing(self, needle: str) -> bool:
        return any(needle in s for s in self.sent)


@pytest.fixture()
def env(tmp_path):
    logger = _make_logger(tmp_path)
    state = RuntimeState(_make_config())
    alerts = []
    client = IRCClient(
        _make_config(), state, logger,
        on_disconnect_callback=lambda r: alerts.append(r),
    )
    return client, state, alerts


def _attach(client, fake_sock):
    """Simulate an established connection without touching the network."""
    client._socket = fake_sock
    client._connected = True
    client.state.irc_connected = True
    now = time.monotonic()
    client._last_recv_time = now
    client._last_ping_sent = now
    client._awaiting_pong = False


def test_default_keepalive_values():
    rc = ReconnectConfig()
    assert rc.read_timeout_seconds == 60.0
    assert rc.ping_interval_seconds == 45.0
    assert rc.pong_deadline_seconds == 150.0


def test_timers_seeded_on_connect(monkeypatch, env):
    client, state, _ = env
    fake = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    assert client._connect() is True
    assert fake.timeout == 0.2  # read_timeout from config, not 300s
    assert client._last_recv_time > 0
    assert client._awaiting_pong is False


def test_proactive_ping_after_idle(monkeypatch, env):
    """With a live-but-quiet link, client must PING on its own schedule."""
    client, state, _ = env
    fake = FakeSock([b":server 001 AwayBox :Welcome\r\n"])
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    client.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if fake.sent_containing("PING :"):
                break
            time.sleep(0.05)
        assert fake.sent_containing("PING :"), "client never sent proactive PING"
        assert client._awaiting_pong is True
    finally:
        client.stop()


def test_inbound_traffic_clears_awaiting_pong(monkeypatch, env):
    client, state, _ = env
    fake = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    client.start()
    try:
        fake.feed(b"PING :abc\r\n")           # server PING -> we answer PONG
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not fake.sent_containing("PONG"):
            time.sleep(0.05)
        assert fake.sent_containing("PONG"), "did not answer server PING"
        assert client._awaiting_pong is False, "inbound traffic must clear pending-ping state"
    finally:
        client.stop()


def test_dead_link_detected_and_alerted(monkeypatch, env):
    """Server silent past pong deadline after our PING -> drop link + alert."""
    client, state, alerts = env
    fake = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    # Pretend the link came up already 'stale': last recv long ago,
    # a PING was sent, nothing ever came back.
    client.start()
    try:
        _attach(client, fake)
        client._last_recv_time = time.monotonic() - 10
        client._last_ping_sent = time.monotonic() - 10
        client._awaiting_pong = True

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not alerts:
            time.sleep(0.05)

        assert alerts, "no disconnect alert fired for dead link"
        assert "PING" in alerts[0]
        assert client._connected is False
        assert state.irc_connected is False
    finally:
        client.stop()


def test_read_error_non_timeout_disconnects(monkeypatch, env):
    """A non-timeout SSLError-style failure still tears the link down."""
    client, state, alerts = env
    fake = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    client.start()
    try:
        _attach(client, fake)
        fake.close()  # next recv raises OSError -> handled as dropped link
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and client._connected:
            time.sleep(0.05)
        assert not client._connected
    finally:
        client.stop()
