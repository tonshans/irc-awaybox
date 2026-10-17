"""
Tests for the four critical bug fixes found in the repository audit:

#1 Dead-link detection must NOT be reset by numeric replies (server answers
   to our own queries). Only server-originated non-numeric traffic (PRIVMSG,
   NOTICE, PING, ...) proves the link is alive.
#2 /reconnect (command and inline button) must respect the /disconirc hold
   instead of silently overriding it.
#3 User-supplied command arguments are HTML-escaped before being echoed back
   with parse_mode="HTML" (previously broke/lost notifications).
#4 IRC-controlled content (message bodies, senders, WHOIS text, log lines,
   disconnect reasons) is HTML-escaped in Telegram notifications.
"""

from __future__ import annotations

import socket
import time

from awaybox.config import (
    AppConfig,
    AutoReplyConfig,
    IRCConfig,
    LoggingConfig,
    ReconnectConfig,
    TelegramConfig,
)
from awaybox.irc_client import IRCClient
from awaybox.logger import AwayBoxLogger
from awaybox.reply_manager import ReplyManager
from awaybox.state import RuntimeState
from awaybox.telegram_bot import TelegramBot, html_escape

from tests.test_keepalive import FakeSock, _make_config


def _make_state_and_bot(**overrides):
    config = AppConfig(
        irc=IRCConfig(server="irc.example.net", port=6697, tls=False,
                      nick="AwayBox", username="_AwayBox_", realname="AwayBox"),
        autoreply=AutoReplyConfig(),
        telegram=TelegramConfig(allowed_user_id=1),
        logging=LoggingConfig(),
        reconnect=ReconnectConfig(),
    )
    state = RuntimeState(config)
    logger = AwayBoxLogger(log_dir="/tmp/awaybox-test-crit")
    reply_mgr = ReplyManager()
    bot = TelegramBot(config, state, logger, reply_mgr, **overrides)
    return bot, state


# ---------------------------------------------------------------- html_escape

def test_html_escape_basics():
    assert html_escape("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert html_escape("a & b") == "a &amp; b"
    # '&' must be escaped first so entities are not double-escaped
    assert html_escape("&lt;") == "&amp;lt;"


# --------------------------------------------------------- Bug #4: escaping

def test_notify_channel_message_escapes_untrusted_content(tmp_path):
    bot, _ = _make_state_and_bot()
    sent = {}

    def fake_send(text, reply_markup=None, parse_mode=None):
        sent["text"] = text
        sent["markup"] = reply_markup
        return True

    bot.send_message = fake_send
    bot.notify_channel_message("#ch", "ni<ck", "hello <script>alert(1)</script> & co")

    text = sent["text"]
    # Raw markup from IRC must never reach the HTML payload unescaped
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "ni&lt;ck" in text
    # Our own template tags survive
    assert "<b>[#ch]</b>" in text
    # Button labels truncated to Telegram's 64-char limit
    for row in sent["markup"]["inline_keyboard"]:
        for btn in row:
            assert len(btn["text"]) <= 64


def test_notify_disconnected_escapes_reason(tmp_path):
    bot, _ = _make_state_and_bot()
    captured = {}
    bot.send_message = lambda text, reply_markup=None, parse_mode=None: (
        captured.update(text=text) or True
    )
    bot.notify_disconnected("Socket error: <connection reset> & EOF")
    assert "&lt;connection reset&gt; &amp; EOF" in captured["text"]


def test_whois_reply_escapes_server_text():
    bot, state = _make_state_and_bot()
    captured = {}
    bot.send_message = lambda text, reply_markup=None, parse_mode=None: (
        captured.update(text=text) or True
    )
    state.register_whois("evi<l>")
    state.append_whois_reply("evi<l>", "realname <i>evil</i> & more")
    bot.handle_whois_reply("evi<l>", "line", final=True)
    assert "&lt;i&gt;evil&lt;/i&gt; &amp; more" in captured["text"]
    assert "<i>evil</i>" not in captured["text"]


# ------------------------------------------------------------- Bug #3: echo

def test_command_response_is_escaped_on_echo():
    """A user typing '/ignore <b>x' would previously produce broken HTML."""
    bot, state = _make_state_and_bot()
    sent = {}
    bot.send_message = lambda text, reply_markup=None, parse_mode=None: (
        sent.update(text=text) or True
    )
    bot.handle_user_text_message(1, "/ignore <bad")
    # The nickname itself is invalid, but even the error text path must be safe
    bot.handle_user_text_message(1, "/unignore <notanick>")
    assert "<notanick>" not in sent["text"]
    assert "&lt;notanick&gt;" in sent["text"]


def test_log_command_escapes_file_contents():
    bot, state = _make_state_and_bot()
    # Prime a log file containing raw markup
    bot.logger.log_channel_message("#test", "nick", "raw <b>markup</b> here")
    reply = bot.handle_command("/log #test")
    assert "<b>markup</b>" not in reply.replace("<b>Cuplikan", "")
    assert "&lt;b&gt;markup&lt;/b&gt;" in reply


# ------------------------------------------------------------- Bug #2: hold

def test_trigger_reconnect_blocked_by_disconirc_hold():
    bot, state = _make_state_and_bot()
    calls = []
    bot.irc_reconnect = lambda: calls.append(True) or True

    state.request_owner_disconnect()
    assert bot.trigger_reconnect() is False
    assert calls == [], "IRC force_reconnect must not run while held"
    # Hold must remain intact (auto-reconnect still disabled)
    assert state.auto_reconnect_enabled is False


def test_trigger_reconnect_works_without_hold():
    bot, state = _make_state_and_bot()
    calls = []
    bot.irc_reconnect = lambda: calls.append(True) or True
    assert bot.trigger_reconnect() is True
    assert calls == [True]


def test_reconnect_command_reports_hold():
    bot, state = _make_state_and_bot()
    state.request_owner_disconnect()
    reply = bot.handle_command("/reconnect now")
    assert "ditahan" in reply.lower()
    assert "/conirc" in reply


def test_reconnect_button_reports_hold():
    bot, state = _make_state_and_bot()
    state.request_owner_disconnect()
    sent = []
    bot.send_message = lambda text, reply_markup=None, parse_mode=None: (
        sent.append(text) or True
    )
    answered = []
    bot.answer_callback = lambda cb_id, text=None: answered.append(text)

    bot.handle_callback_query({
        "id": "cb1",
        "from": {"id": bot.allowed_user_id},
        "data": "reconnect:yes",
    })
    assert any("ditahan" in s.lower() for s in sent)


def test_force_reconnect_returns_false_while_held(tmp_path):
    config = _make_config()
    state = RuntimeState(config)
    client = IRCClient(config, state, AwayBoxLogger(log_dir=str(tmp_path)))
    state.request_owner_disconnect()
    assert client.force_reconnect() is False
    assert state.consume_manual_reconnect() is False
    state.request_owner_connect()
    assert client.force_reconnect() is True


# ----------------------------------------------------------- Bug #1: numerics

def test_numeric_only_traffic_does_not_reset_dead_link(monkeypatch, tmp_path):
    """Stale queued numerics must not keep a dead link 'alive'."""
    config = _make_config(read_timeout_seconds=0.1, ping_interval_seconds=0.3,
                          pong_deadline_seconds=0.5)
    state = RuntimeState(config)
    alerts = []
    client = IRCClient(config, state, AwayBoxLogger(log_dir=str(tmp_path)),
                       on_disconnect_callback=lambda r: alerts.append(r))
    fake = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    client.start()
    try:
        # Attach a stale-but-connected link awaiting a PONG
        client._socket = fake
        client._connected = True
        client.state.irc_connected = True
        now = time.monotonic()
        client._last_recv_time = now - 10
        client._last_ping_sent = now - 10
        client._awaiting_pong = True

        # Feed ONLY numeric replies (answers to our old WHOIS queries)
        fake.feed(b":server.pseudo 311 AwayBox victim Nick Real Host :info\r\n")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not alerts:
            time.sleep(0.05)

        assert alerts, "dead link was NOT detected despite numeric-only traffic"
        assert "PING" in alerts[0]
        assert client._connected is False
    finally:
        client.stop()


def test_privmsg_still_clears_dead_link_timer(monkeypatch, tmp_path):
    """Genuine live traffic (PRIVMSG) must keep the link considered alive."""
    config = _make_config(read_timeout_seconds=0.1, ping_interval_seconds=0.3,
                          pong_deadline_seconds=0.5)
    state = RuntimeState(config)
    alerts = []
    msgs = []
    client = IRCClient(config, state, AwayBoxLogger(log_dir=str(tmp_path)),
                       on_message_callback=lambda s, t, x, c: msgs.append((s, t, x, c)),
                       on_disconnect_callback=lambda r: alerts.append(r))
    fake = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: fake)

    client.start()
    try:
        client._socket = fake
        client._connected = True
        client.state.irc_connected = True
        now = time.monotonic()
        client._last_recv_time = now - 10
        client._last_ping_sent = now - 10
        client._awaiting_pong = True

        fake.feed(b":victim!u@h PRIVMSG #chan :hello world\r\n")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not msgs:
            time.sleep(0.05)
        assert msgs, "PRIVMSG was not delivered"
        assert client._awaiting_pong is False
        assert client._last_recv_time > now - 5
        # No alert should fire right after genuine live traffic
        assert alerts == []
    finally:
        client.stop()
