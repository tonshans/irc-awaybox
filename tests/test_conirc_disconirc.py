"""
Tests for the Telegram /conirc and /disconirc manual IRC link controls.

Covers:
- RuntimeState owner connect/disconnect flag semantics
- IRCClient.connect_now / disconnect behavior (including worker-loop hold)
- TelegramBot command dispatch, fallbacks, and /start help text
"""

from __future__ import annotations

import threading
import unittest

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
from awaybox.telegram_bot import TelegramBot


def _make_config(log_dir: str = "/tmp/awaybox-test-logs") -> AppConfig:
    return AppConfig(
        irc=IRCConfig(server="irc.example.net", port=6667, tls=False),
        autoreply=AutoReplyConfig(),
        telegram=TelegramConfig(allowed_user_id=111),
        logging=LoggingConfig(log_dir=log_dir),
        reconnect=ReconnectConfig(),
    )


def _make_state(config: AppConfig) -> RuntimeState:
    return RuntimeState(config)


class _NullLogger:
    """Stand-in for AwayBoxLogger that records service log lines."""

    def __init__(self):
        self.lines = []

    def log_service(self, level, message):
        self.lines.append((level, message))

    def log_channel_message(self, *args, **kwargs):
        pass

    def log_private_incoming(self, *args, **kwargs):
        pass

    def log_private_outgoing(self, *args, **kwargs):
        pass


class StateOwnerControlsTest(unittest.TestCase):
    def setUp(self):
        self.config = _make_config()
        self.state = _make_state(self.config)

    def test_defaults(self):
        self.assertFalse(self.state.owner_disconnect_requested)
        self.assertTrue(self.state.auto_reconnect_enabled)

    def test_request_owner_disconnect_holds_link_closed(self):
        # Pre-arm a pending manual reconnect; /disconirc must clear it.
        self.state.request_manual_reconnect()
        self.state.request_owner_disconnect()

        self.assertTrue(self.state.owner_disconnect_requested)
        self.assertFalse(self.state.auto_reconnect_enabled)
        self.assertFalse(self.state.consume_manual_reconnect())

    def test_request_owner_connect_clears_hold_and_flags_attempt(self):
        self.state.request_owner_disconnect()
        self.state.request_owner_connect()

        self.assertFalse(self.state.owner_disconnect_requested)
        self.assertTrue(self.state.auto_reconnect_enabled)
        self.assertTrue(self.state.consume_manual_reconnect())

    def test_flags_are_thread_safe_consumption(self):
        self.state.request_owner_disconnect()
        self.state.request_owner_connect()
        # Second consume should be False (one-shot flag).
        self.assertTrue(self.state.consume_manual_reconnect())
        self.assertFalse(self.state.consume_manual_reconnect())


class IRCClientConnectDisconnectTest(unittest.TestCase):
    def setUp(self):
        self.config = _make_config()
        self.state = _make_state(self.config)
        self.logger = _NullLogger()
        self.client = IRCClient(self.config, self.state, self.logger)
        self.disconnect_events = []
        self.client.on_disconnect = lambda reason: self.disconnect_events.append(reason)

    def _fake_socket(self):
        class FakeSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

            def close(self):
                pass

        return FakeSock()

    def test_disconnect_when_offline_returns_false_and_holds(self):
        was_connected = self.client.disconnect()
        self.assertFalse(was_connected)
        self.assertTrue(self.state.owner_disconnect_requested)
        self.assertFalse(self.state.auto_reconnect_enabled)
        # No alert fired because nothing was connected.
        self.assertEqual(self.disconnect_events, [])

    def test_disconnect_when_online_sends_quit_and_no_alert(self):
        sock = self._fake_socket()
        self.client._socket = sock
        self.client._connected = True
        self.state.irc_connected = True

        was_connected = self.client.disconnect()
        self.assertTrue(was_connected)
        self.assertFalse(self.client._connected)
        self.assertFalse(self.state.irc_connected)
        self.assertIn(b"QUIT", sock.sent[0])
        # Owner-initiated disconnect must NOT trigger the disconnect alert.
        self.assertEqual(self.disconnect_events, [])
        self.assertTrue(self.state.owner_disconnect_requested)

    def test_connect_now_when_offline_clears_hold(self):
        self.state.request_owner_disconnect()
        initiated = self.client.connect_now()
        self.assertTrue(initiated)
        self.assertFalse(self.state.owner_disconnect_requested)
        self.assertTrue(self.state.consume_manual_reconnect())

    def test_connect_now_when_online_drops_link_for_fresh_session(self):
        sock = self._fake_socket()
        self.client._socket = sock
        self.client._connected = True
        self.state.irc_connected = True

        initiated = self.client.connect_now()
        self.assertTrue(initiated)
        self.assertFalse(self.client._connected)
        self.assertFalse(self.state.owner_disconnect_requested)
        self.assertTrue(self.state.consume_manual_reconnect())
        # Reconnect drop must not fire an owner alert either.
        self.assertEqual(self.disconnect_events, [])

    def test_connect_now_refused_during_shutdown(self):
        self.client._stop_event.set()
        self.assertFalse(self.client.connect_now())

    def test_worker_loop_stays_idle_while_held(self):
        """While /disconirc hold is active, the loop never attempts to connect."""
        attempts = []
        original_connect = self.client._connect

        def spy_connect():
            attempts.append(True)
            return original_connect()

        self.client._connect = spy_connect
        self.state.request_owner_disconnect()

        self.client.start()
        threading.Event().wait(0.5)
        self.client.stop()

        self.assertEqual(attempts, [])


class TelegramCommandDispatchTest(unittest.TestCase):
    def setUp(self):
        self.config = _make_config()
        self.state = _make_state(self.config)
        self.logger = _NullLogger()
        self.calls = []

        def fake_connect():
            self.calls.append("connect")
            self.state.request_owner_connect()
            return True

        def fake_disconnect():
            self.calls.append("disconnect")
            self.state.request_owner_disconnect()
            return True

        self.bot = TelegramBot(
            config=self.config,
            state=self.state,
            logger=self.logger,
            reply_manager=ReplyManager(60),
            irc_connect_func=fake_connect,
            irc_disconnect_func=fake_disconnect,
        )

    def test_conirc_command_invokes_connect(self):
        reply = self.bot.handle_command("/conirc")
        self.assertIn("connect", self.calls)
        self.assertIn("Koneksi IRC diminta", reply)
        self.assertFalse(self.state.owner_disconnect_requested)

    def test_conirc_with_at_botname_suffix(self):
        reply = self.bot.handle_command("/conirc@awaybox_bot")
        self.assertIn("connect", self.calls)

    def test_disconirc_command_invokes_disconnect(self):
        reply = self.bot.handle_command("/disconirc")
        self.assertIn("disconnect", self.calls)
        self.assertIn("diputus", reply)
        self.assertTrue(self.state.owner_disconnect_requested)

    def test_disconirc_when_already_offline(self):
        def already_offline():
            self.calls.append("disconnect")
            return False

        self.bot.irc_disconnect = already_offline
        reply = self.bot.handle_command("/disconirc")
        self.assertIn("sudah terputus", reply)

    def test_start_help_lists_new_commands(self):
        help_text = self.bot.handle_command("/start")
        self.assertIn("/conirc", help_text)
        self.assertIn("/disconirc", help_text)

    def test_fallback_without_hooks_uses_state(self):
        bot = TelegramBot(
            config=self.config,
            state=self.state,
            logger=self.logger,
            reply_manager=ReplyManager(60),
        )
        self.assertIn("Koneksi IRC diminta", bot.handle_command("/conirc"))
        self.assertFalse(self.state.owner_disconnect_requested)
        self.assertIn("diputus", bot.handle_command("/disconirc"))
        self.assertTrue(self.state.owner_disconnect_requested)


if __name__ == "__main__":
    unittest.main()
