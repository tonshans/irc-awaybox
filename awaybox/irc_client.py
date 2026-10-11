"""
AwayBox IRC Client Module

Manages persistent connection to an IRC network:
- TLS socket support
- Welcome event detection (RPL_WELCOME 001)
- NickServ authentication
- Automatic channel joining (default channels or preserved runtime channels)
- Optional ChanServ template expansion ({channel})
- Standard PING / PONG keepalive
- Clean PRIVMSG parsing for channel and private messages
- Exponential backoff reconnect: 5, 10, 20, 40, 80, 160, max 300s
- Resilient non-blocking or worker thread architecture
"""

from __future__ import annotations

import re
import socket
import ssl
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

from awaybox.config import AppConfig
from awaybox.logger import AwayBoxLogger
from awaybox.state import RuntimeState


class IRCMessage:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prefix: str = ""
        self.command: str = ""
        self.params: List[str] = []
        self.nick: str = ""
        self.user: str = ""
        self.host: str = ""
        self._parse()

    def _parse(self) -> None:
        line = self.raw.strip("\r\n")
        if not line:
            return

        # 1. Parse prefix (e.g. :nick!user@host)
        if line.startswith(":"):
            parts = line[1:].split(" ", 1)
            self.prefix = parts[0]
            line = parts[1] if len(parts) > 1 else ""

            # Extract nick!user@host
            if "!" in self.prefix:
                self.nick = self.prefix.split("!", 1)[0]
                rest = self.prefix.split("!", 1)[1]
                if "@" in rest:
                    self.user, self.host = rest.split("@", 1)
                else:
                    self.user = rest
            else:
                self.nick = self.prefix

        # 2. Parse command and trailing argument
        trailing = ""
        if " :" in line:
            line, trailing = line.split(" :", 1)

        tokens = line.split()
        if tokens:
            self.command = tokens[0].upper()
            self.params = tokens[1:]

        if trailing or raw_trailing_check(self.raw):
            self.params.append(trailing)


def raw_trailing_check(raw: str) -> bool:
    return " :" in raw


class IRCClient:
    def __init__(
        self,
        config: AppConfig,
        state: RuntimeState,
        logger: AwayBoxLogger,
        on_message_callback: Optional[Callable[[str, str, str, bool], None]] = None,
    ) -> None:
        """
        on_message_callback(sender, target, text, is_channel)
        """
        self.config = config
        self.state = state
        self.logger = logger
        self.on_message = on_message_callback

        self._socket: Optional[socket.socket | ssl.SSLSocket] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        self._backoff = float(config.reconnect.initial_delay_seconds)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        """Starts the IRC client worker thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AwayBox-IRCClient", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Gracefully disconnects from IRC and terminates thread."""
        self._stop_event.set()
        self._disconnect(send_quit=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _send_raw(self, line: str) -> bool:
        """Sends a raw CRLF-terminated line to the IRC server."""
        with self._lock:
            if not self._socket or not self._connected:
                return False
            try:
                data = (line.rstrip("\r\n") + "\r\n").encode("utf-8", errors="replace")
                self._socket.sendall(data)

                return True
            except OSError as e:
                self.logger.log_service("ERROR", f"Failed to send IRC line: {e}")
                self._connected = False
                self.state.irc_connected = False
                return False

    def send_privmsg(self, target: str, text: str) -> bool:
        """Sends a standard PRIVMSG to a channel or nickname."""
        if not self.is_connected:
            return False
        # Normalize CR/LF in the outgoing message to avoid line injection
        clean_text = text.replace("\r", " ").replace("\n", " ").strip()
        if not clean_text:
            return False
        return self._send_raw(f"PRIVMSG {target} :{clean_text}")

    def join_channel(self, channel: str) -> bool:
        """Sends JOIN command and tracks in runtime state."""
        ch = channel.strip()
        if not ch.startswith("#") and not ch.startswith("&"):
            ch = f"#{ch}"
        self.state.add_channel(ch)
        if self.is_connected:
            self._send_raw(f"JOIN {ch}")
            self._apply_chanserv(ch)
            return True
        return False

    def leave_channel(self, channel: str) -> bool:
        """Sends PART command and removes from runtime state."""
        ch = channel.strip()
        self.state.remove_channel(ch)
        if self.is_connected:
            return self._send_raw(f"PART {ch} :Leaving")
        return False

    def _apply_chanserv(self, channel: str) -> None:
        """Executes optional ChanServ template command if configured."""
        if self.config.irc.chanserv_enabled and self.config.irc.chanserv_command_template:
            cmd = self.config.irc.chanserv_command_template.format(channel=channel)
            self._send_raw(f"PRIVMSG {cmd}")

    def _calculate_next_backoff(self) -> float:
        """Returns the current backoff delay and calculates the next exponential step."""
        current = self._backoff
        next_val = min(current * self.config.reconnect.backoff_multiplier, float(self.config.reconnect.max_delay_seconds))
        self._backoff = next_val
        return current

    def _reset_backoff(self) -> None:
        self._backoff = float(self.config.reconnect.initial_delay_seconds)

    def _connect(self) -> bool:
        """Establishes TCP/TLS connection to IRC server."""
        cfg = self.config.irc
        self.logger.log_service("INFO", f"Connecting to IRC server {cfg.server}:{cfg.port} (TLS={cfg.tls})...")

        raw_sock = None
        try:
            raw_sock = socket.create_connection((cfg.server, cfg.port), timeout=20.0)
            if cfg.tls:
                ssl_context = ssl.create_default_context()
                sock = ssl_context.wrap_socket(raw_sock, server_hostname=cfg.server)
            else:
                sock = raw_sock

            sock.settimeout(300.0)  # PING timeout headroom
            self._socket = sock
            self._connected = True
            self.state.irc_connected = True
            self.state.reconnect_attempts = 0

            # Handshake: NICK and USER
            self._send_raw(f"NICK {cfg.nick}")
            self._send_raw(f"USER {cfg.username} 0 * :{cfg.realname}")
            return True
        except Exception as e:
            self.logger.log_service("ERROR", f"IRC connection error: {e}")
            if raw_sock:
                try:
                    raw_sock.close()
                except Exception:
                    pass
            self._connected = False
            self.state.irc_connected = False
            return False

    def _disconnect(self, send_quit: bool = False) -> None:
        """Closes the socket connection."""
        self._connected = False
        self.state.irc_connected = False
        self.state.irc_authenticated = False
        with self._lock:
            if self._socket:
                try:
                    if send_quit:
                        self._socket.sendall(b"QUIT :AwayBox Shutting Down\r\n")
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None

    def _handle_registration_complete(self) -> None:
        """Triggered when server responds with 001 (RPL_WELCOME)."""
        self.logger.log_service("INFO", f"Registered successfully on IRC as {self.config.irc.nick}.")
        self._reset_backoff()

        if self.config.irc.nickserv_enabled and self.config.irc_password:
            self.logger.log_service("INFO", "Authenticating with NickServ...")
            cmd = self.config.irc.nickserv_command.format(password=self.config.irc_password)
            self._send_raw(f"PRIVMSG {cmd}")
            self.state.irc_authenticated = True

        for channel in sorted(self.state.active_channels):
            self.logger.log_service("INFO", f"Joining channel {channel}...")
            self._send_raw(f"JOIN {channel}")
            #self._apply_chanserv(channel)

        if self.config.irc.chanserv_enabled and self.config.irc_chan_password:
            cmd = self.config.irc.chanserv_command.format(password=self.config.irc_chan_password)
            self._send_raw(f"PRIVMSG {cmd}")

    def _process_line(self, line: str) -> None:
        if not line:
            return

        msg = IRCMessage(line)

        # Handle PING keepalive immediately
        if msg.command == "PING":
            daemon = msg.params[0] if msg.params else ""
            self._send_raw(f"PONG :{daemon}")
            return

        # 001 RPL_WELCOME
        if msg.command == "001":
            self._handle_registration_complete()
            return

        # 433 ERR_NICKNAMEINUSE
        if msg.command == "433":
            alt_nick = f"{self.config.irc.nick}_"
            self.logger.log_service("WARN", f"Nickname in use, retrying with {alt_nick}...")
            self._send_raw(f"NICK {alt_nick}")
            return

        # Handle PRIVMSG
        if msg.command == "PRIVMSG" and len(msg.params) >= 2:
            target = msg.params[0]
            text = msg.params[1]
            sender = msg.nick

            is_channel = target.startswith("#") or target.startswith("&")

            if self.on_message:
                self.on_message(sender, target, text, is_channel)

    def _run_loop(self) -> None:
        """Main IRC worker thread with automatic backoff reconnection loop."""
        buffer = ""

        while not self._stop_event.is_set():
            if not self._connected:
                success = self._connect()
                if not success:
                    delay = self._calculate_next_backoff()
                    self.state.reconnect_attempts += 1
                    self.state.last_disconnect_time = time.time()
                    self.logger.log_service(
                        "WARN",
                        f"Reconnecting in {int(delay)}s (attempt {self.state.reconnect_attempts})..."
                    )
                    # Interruptible sleep
                    self._stop_event.wait(delay)
                    continue

            # Connected: read stream
            try:
                assert self._socket is not None
                chunk = self._socket.recv(4096)
                if not chunk:
                    self.logger.log_service("WARN", "IRC socket closed by remote server.")
                    self._disconnect()
                    continue

                buffer += chunk.decode("utf-8", errors="replace")
                while "\r\n" in buffer:
                    line, buffer = buffer.split("\r\n", 1)
                    if line:
                        self._process_line(line)
            except (socket.timeout, ssl.SSLError) as e:
                # Idle socket timeout or SSL timeout
                if "timed out" in str(e).lower():
                    # Send keepalive ping
                    self._send_raw(f"PING :{self.config.irc.server}")
                    continue
                self.logger.log_service("WARN", f"IRC socket read error: {e}")
                self._disconnect()
            except OSError as e:
                self.logger.log_service("WARN", f"IRC connection dropped: {e}")
                self._disconnect()
            except Exception as e:
                self.logger.log_service("ERROR", f"Unexpected IRC error: {e}")
                self._disconnect()

        self._disconnect()
