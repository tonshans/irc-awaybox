"""
AwayBox Main Application & Orchestrator

Initializes components, wires callbacks, handles OS signals,
and governs the central message pipeline between IRC, text logs, and Telegram.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Optional

from awaybox.config import AppConfig, ConfigError, load_config
from awaybox.irc_client import IRCClient
from awaybox.logger import AwayBoxLogger
from awaybox.reply_manager import ReplyManager
from awaybox.state import RuntimeState
from awaybox.telegram_bot import TelegramBot


class AwayBoxApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

        # Collect sensitive secrets for log redaction
        secrets_list = [
            config.irc_password or "",
            config.irc_chan_password or "",
            config.telegram_bot_token or "",
        ]

        # 1. Core subsystems
        self.logger = AwayBoxLogger(config.logging.log_dir, secrets=secrets_list)
        self.state = RuntimeState(config)
        self.reply_manager = ReplyManager(config.telegram.reply_session_timeout_seconds)

        # 2. IRC Client
        self.irc_client = IRCClient(
            config=self.config,
            state=self.state,
            logger=self.logger,
            on_message_callback=self.on_irc_message_received,
        )

        # 3. Telegram Bot
        self.telegram_bot = TelegramBot(
            config=self.config,
            state=self.state,
            logger=self.logger,
            reply_manager=self.reply_manager,
            irc_send_func=self.irc_client.send_privmsg,
            irc_join_func=self.irc_client.join_channel,
            irc_leave_func=self.irc_client.leave_channel,
        )

        self._running = False

    def on_irc_message_received(self, sender: str, target: str, text: str, is_channel: bool) -> None:
        """
        Governs the strict message processing sequence:
        1. Ignore check (Strict drop: no log, no notification, no auto-reply)
        2. Plain-text logging
        3. Telegram notification with reply buttons
        4. Auto-reply evaluation & cooldown enforcement
        """
        # 1. Ignore Check: if sender is in ignore list, drop completely
        if self.state.is_ignored(sender):
            return

        # Do not process our own echo messages
        if sender.lower() == self.config.irc.nick.lower():
            return

        # 2. Text Logging
        if is_channel:
            self.logger.log_channel_message(channel=target, sender=sender, text=text)
        else:
            self.logger.log_private_incoming(sender=sender, text=text)

        # 3. Telegram Notification
        if self.state.telegram_active:
            try:
                if is_channel:
                    self.telegram_bot.notify_channel_message(channel=target, sender=sender, text=text)
                else:
                    self.telegram_bot.notify_private_message(sender=sender, text=text)
            except Exception as e:
                self.logger.log_service("WARN", f"Telegram notification dispatch error: {e}")

        # 4. Auto-Reply Evaluation
        self._evaluate_autoreply(sender=sender, target=target, is_channel=is_channel)

    def _evaluate_autoreply(self, sender: str, target: str, is_channel: bool) -> None:
        # Check if auto-reply is enabled in runtime state
        if not self.state.autoreply_enabled:
            return

        # For channels, only reply if the channel is explicitly in autoreply list
        if is_channel and not self.state.is_autoreply_channel(target):
            return

        # Check nickname cooldown (case-insensitive & shared between PM/channel)
        if self.state.is_on_cooldown(sender):
            return

        # Enforce cooldown timestamp
        self.state.update_cooldown(sender)

        # Transmit auto-reply to IRC
        reply_destination = target if is_channel else sender
        reply_text = self.config.autoreply.message
        success = self.irc_client.send_privmsg(reply_destination, reply_text)

        if success:
            # Log auto-reply as outgoing message
            if is_channel:
                self.logger.log_channel_message(target, self.config.irc.nick, reply_text)
            else:
                self.logger.log_private_outgoing(sender, self.config.irc.nick, reply_text)

    def start(self) -> None:
        """Starts all background worker threads and enters main loop."""
        self._running = True
        self.logger.log_service("INFO", f"AwayBox starting up. Nick={self.config.irc.nick}")

        # Start subsystems
        self.irc_client.start()
        self.telegram_bot.start()

    def stop(self) -> None:
        """Graceful shutdown of all subsystems."""
        if not self._running:
            return
        self._running = False
        self.logger.log_service("INFO", "AwayBox shutting down gracefully...")
        self.telegram_bot.stop()
        self.irc_client.stop()
        self.logger.log_service("INFO", "AwayBox stopped.")

    def run(self) -> None:
        """Runs the service until interrupted by SIGINT / SIGTERM."""
        self.start()
        try:
            while self._running:
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="AwayBox - Persistent IRC Mailbox & Telegram Relay")
    parser.add_argument("-c", "--config", help="Path to config.toml", default=None)
    parser.add_argument("-s", "--secrets", help="Path to secrets.env", default=None)
    args = parser.parse_args()

    try:
        cfg = load_config(args.config, args.secrets)
    except ConfigError as e:
        print(f"Error loading configuration: {e}", file=sys.stderr)
        sys.exit(1)

    app = AwayBoxApp(cfg)

    def sig_handler(signum, frame):
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    app.run()


if __name__ == "__main__":
    main()
