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
- Disconnect/reconnect alerts forwarded through callbacks so the owner can be
  notified via Telegram and choose to reconnect or pause auto-reconnect
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

# Numeric WHOIS reply codes whose target nickname lives in params[1].
# Imported from telegram_bot as the single source of truth so the two modules
# can never drift apart again (bug #4 fix). RPL_AWAY (301) is now included.
from awaybox.telegram_bot import _WHOIS_RPL_CODES


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
        on_whois_reply_callback: Optional[Callable[[str, str, bool], None]] = None,
        on_disconnect_callback: Optional[Callable[[str], None]] = None,
        on_reconnect_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        on_message_callback(sender, target, text, is_channel)
        on_whois_reply_callback(target_nick, line, final)
        on_disconnect_callback(reason) - invoked when the IRC link drops
            (unexpectedly or after a failed connection attempt), so the
            application can alert the owner via Telegram.
        on_reconnect_callback() - invoked when the bot successfully
            re-registers on IRC (RPL_WELCOME) after a prior disconnect alert.
        """
        self.config = config
        self.state = state
        self.logger = logger
        self.on_message = on_message_callback
        self.on_whois_reply = on_whois_reply_callback
        self.on_disconnect = on_disconnect_callback
        self.on_reconnect = on_reconnect_callback

        self._socket: Optional[socket.socket | ssl.SSLSocket] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        self._backoff = float(config.reconnect.initial_delay_seconds)

        # Nickname actually in use on the network. Starts as the configured
        # nick; switches to the fallback (e.g. "AwayBox_") when registration
        # collides with ERR_NICKNAMEINUSE (433). Echo filtering and logging
        # must compare against THIS value, not the config default, otherwise
        # our own echoed messages get logged, notified and auto-replied to
        # after a nick collision (bug #2 fix).
        self._active_nick = config.irc.nick

        # --- Keepalive / timeout hardening -----------------------------------
        # Read timeout on the socket: short enough to notice dead/half-open
        # links quickly, long enough that normal traffic keeps us awake.
        self._read_timeout = float(getattr(config.reconnect, "read_timeout_seconds", 60.0))
        # How often we proactively send our own PING when the link is idle.
        self._ping_interval = float(getattr(config.reconnect, "ping_interval_seconds", 45.0))
        # If the server has not sent ANYTHING for this many seconds since our
        # last PING, consider the link dead and force a reconnect.
        self._pong_deadline = float(getattr(config.reconnect, "pong_deadline_seconds", 150.0))
        self._last_recv_time = 0.0
        self._last_ping_sent = 0.0
        self._awaiting_pong = False

    @property
    def active_nick(self) -> str:
        """The nickname currently used on IRC (may differ from config after 433)."""
        return self._active_nick

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
        """Sends a raw CRLF-terminated line to the IRC server.

        Any CR/LF embedded in the middle of the line is stripped first:
        ``rstrip`` alone only trims trailing newlines, so an unsanitized value
        (e.g. a config realname or channel name containing "\\r\\n") could be
        split into two protocol lines — an IRC command injection (bug #7 fix).
        """
        with self._lock:
            if not self._socket or not self._connected:
                return False
            try:
                safe = line.replace("\r", " ").replace("\n", " ").rstrip()
                data = (safe + "\r\n").encode("utf-8", errors="replace")
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

    def send_whois(self, nickname: str) -> bool:
        """Sends a WHOIS request for a single nickname."""
        nick = nickname.strip()
        if not nick or not self.is_connected:
            return False
        return self._send_raw(f"WHOIS {nick}")

    def join_channel(self, channel: str) -> bool:
        """Sends JOIN command and tracks in runtime state.

        When offline the channel is still remembered in runtime state so it
        will be joined automatically on the next successful registration; we
        report success because the request IS honored (just deferred), instead
        of silently mutating state while telling the owner it failed (bug #10).
        """
        ch = channel.strip()
        if not ch.startswith("#") and not ch.startswith("&"):
            ch = f"#{ch}"
        self.state.add_channel(ch)
        if self.is_connected:
            self._send_raw(f"JOIN {ch}")
            self._apply_chanserv(ch)
        return True

    def leave_channel(self, channel: str) -> bool:
        """Sends PART command and removes from runtime state."""
        ch = channel.strip()
        self.state.remove_channel(ch)
        if self.is_connected:
            return self._send_raw(f"PART {ch} :Leaving")
        return False

    def force_reconnect(self) -> bool:
        """Drops the current link (if any) and requests an immediate reconnect.

        Used by the Telegram /reconnect command and the inline Reconnect button.
        Respects the /disconirc hold: while the owner has explicitly kept the
        link closed, a manual reconnect must NOT silently override that choice
        (bug #2 fix) — the caller reports this to the user instead.
        Returns True when the reconnect was initiated, False when held.
        """
        if self.state.owner_disconnect_requested:
            return False
        self.state.request_manual_reconnect()
        if self._connected:
            self.logger.log_service("INFO", "Forcing disconnect for immediate manual reconnect...")
            self._disconnect(reason="Reconnect manual diminta dari Telegram.")
        return True

    def connect_now(self) -> bool:
        """Connects to IRC immediately from Telegram (/conirc).

        If already connected, drops the current link first so a fresh session
        is established; otherwise clears the /disconirc hold and wakes the
        worker loop so it connects without waiting for the backoff delay.
        Returns True when the connection attempt was initiated.
        """
        if self._stop_event.is_set():
            self.logger.log_service("WARN", "connect_now ignored: IRC client is shutting down.")
            return False

        if self._connected:
            self.logger.log_service("INFO", "Reconnecting to IRC (requested from Telegram via /conirc)...")
            self.state.request_owner_connect()
            # Owner-initiated drop: send_quit=True suppresses the disconnect
            # alert while still transmitting QUIT and closing the socket.
            self._disconnect(send_quit=True)
            return True

        self.logger.log_service("INFO", "Menghubungkan ke IRC (diminta dari Telegram via /conirc)...")
        self.state.request_owner_connect()
        return True

    def disconnect(self) -> bool:
        """Disconnects from IRC on owner demand (/disconirc) and holds closed.

        The bot sends QUIT, closes the socket, and stays offline (no automatic
        or manual reconnection attempts) until the owner issues /conirc.
        Returns True if a live link was terminated; False if already offline.
        """
        was_connected = self._connected
        # Hold the link closed FIRST so the worker loop cannot race into a
        # new connection attempt while we are tearing the socket down.
        self.state.request_owner_disconnect()
        if was_connected:
            self.logger.log_service("INFO", "Disconnecting from IRC (requested from Telegram via /disconirc)...")
            self._disconnect(send_quit=True)
        else:
            self.logger.log_service("INFO", "Already disconnected from IRC; /disconirc hold engaged.")
        return was_connected

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

            sock.settimeout(self._read_timeout)  # wake up regularly for keepalive checks
            self._socket = sock
            self._connected = True
            self.state.irc_connected = True
            self.state.reconnect_attempts = 0

            # Seed keepalive timers so the first proactive PING fires after
            # _ping_interval seconds of idleness, not immediately.
            now = time.monotonic()
            self._last_recv_time = now
            self._last_ping_sent = now
            self._awaiting_pong = False

            # Handshake: NICK and USER. Reset the active-nick tracker on every
            # (re)connection attempt; a later 433 switches it to the fallback.
            self._active_nick = cfg.nick
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

    def _disconnect(self, send_quit: bool = False, reason: str = "") -> None:
        """Closes the socket connection.

        When an established link drops unexpectedly (send_quit=False), the
        on_disconnect callback is fired once so the owner can be alerted via
        Telegram. Graceful shutdowns never trigger alerts.
        """
        was_connected = self._connected
        if was_connected:
            # Record the outage moment for every drop path (not just failed
            # connect attempts) so the Telegram alert shows a real "N detik
            # lalu" instead of always claiming "baru saja" (bug #11 fix).
            self.state.last_disconnect_time = time.time()
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

        if was_connected and not send_quit and self.on_disconnect:
            try:
                self.on_disconnect(reason or "Koneksi IRC terputus secara tak terduga.")
            except Exception as e:
                self.logger.log_service("ERROR", f"Disconnect callback error: {e}")

    def _handle_registration_complete(self) -> None:
        """Triggered when server responds with 001 (RPL_WELCOME)."""
        self.logger.log_service("INFO", f"Registered successfully on IRC as {self.config.irc.nick}.")
        self._reset_backoff()

        # Reconnection succeeded: clear the dedup flag and notify the owner.
        was_down = self.state.disconnect_notified
        self.state.clear_disconnect_alert()
        if was_down and self.on_reconnect:
            try:
                self.on_reconnect()
            except Exception as e:
                self.logger.log_service("ERROR", f"Reconnect callback error: {e}")

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
            # Track the fallback as our ACTIVE nick so echo filtering and
            # logging compare against what is really registered on the
            # network, not the config default (bug #2 fix).
            self._active_nick = alt_nick
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
            return

        # Handle WHOIS numeric replies (RPL_* / ERR_NOSUCHNICK)
        if msg.command.isdigit():
            self._handle_whois_numeric(msg)

    def _handle_whois_numeric(self, msg: IRCMessage) -> None:
        """Routes a numeric reply that belongs to a pending WHOIS query."""
        code = msg.command
        if code not in _WHOIS_RPL_CODES and code != "401":
            return
        if len(msg.params) < 3:
            return

        target_nick = msg.params[1]
        if not self.state.has_pending_whois(target_nick):
            return

        line = " ".join(msg.params[2:]).strip()
        final = code in ("318", "401")
        if self.on_whois_reply:
            self.on_whois_reply(target_nick, line, final)

    def _run_loop(self) -> None:
        """Main IRC worker thread with automatic backoff reconnection loop."""
        buffer = ""

        while not self._stop_event.is_set():
            if not self._connected:
                # Owner explicitly disconnected via /disconirc: stay fully
                # idle until /conirc clears the hold. Never alert, never retry.
                if self.state.owner_disconnect_requested:
                    self._stop_event.wait(2.0)
                    continue

                # Manual "don't reconnect" switch: idle until the owner asks
                # for a reconnect via Telegram (/reconnect or the button).
                if not self.state.auto_reconnect_enabled:
                    if self.state.consume_manual_reconnect():
                        self.logger.log_service("INFO", "Manual reconnect requested from Telegram. Trying now...")
                    else:
                        self._stop_event.wait(2.0)
                        continue

                success = self._connect()
                if not success:
                    delay = self._calculate_next_backoff()
                    # A pending manual reconnect request shortens the wait
                    delay = self.state.get_reconnect_sleep(delay)
                    self.state.reconnect_attempts += 1
                    self.state.last_disconnect_time = time.time()
                    self.logger.log_service(
                        "WARN",
                        f"Reconnecting in {int(delay)}s (attempt {self.state.reconnect_attempts})..."
                    )
                    # Claim the dedup flag HERE only (atomic check-and-set) and
                    # dispatch unconditionally. The app handler must not re-check
                    # the same flag, or every "connection failed" alert would be
                    # silently dropped (bug #1 fix).
                    if self.state.should_send_disconnect_alert():
                        self._fire_disconnect_alert(
                            f"Gagal terhubung ke IRC server {self.config.irc.server}:{self.config.irc.port}."
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
                    self._disconnect(reason="Server IRC menutup koneksi (socket closed by remote).")
                    continue

                buffer += chunk.decode("utf-8", errors="replace")
                # Parse this chunk's complete lines, remembering which of them
                # were server-originated (non-numeric) traffic.
                chunk_lines: List[str] = []
                while "\r\n" in buffer:
                    line, buffer = buffer.split("\r\n", 1)
                    if line:
                        chunk_lines.append(line)

                live_traffic = False
                for ln in chunk_lines:
                    if not IRCMessage(ln).command.isdigit():
                        live_traffic = True
                    self._process_line(ln)

                # Keepalive bookkeeping runs AFTER the lines are parsed so a
                # PONG arriving in this same chunk is correctly recognized.
                # Only server-originated (non-numeric) traffic — PRIVMSG,
                # NOTICE, server PING, JOIN, ... — proves the link is alive.
                # Numeric replies merely answer our own queries; a half-open
                # link can still deliver stale queued numerics, so they must
                # never reset the dead-link timer (bug #1 fix).
                if live_traffic:
                    self._last_recv_time = time.monotonic()
                    self._awaiting_pong = False
                else:
                    # No live evidence yet: if we already have an outstanding
                    # PING, give its reply one more read cycle before the
                    # deadline check below declares the link dead.
                    if self._awaiting_pong:
                        continue

                # Proactive keepalive: if the link has been idle past the
                # ping interval, send our own PING so neither we nor the
                # server ever considers the connection stale.
                if time.monotonic() - self._last_ping_sent >= self._ping_interval:
                    self._send_raw(f"PING :{self.config.irc.server}")
                    self._last_ping_sent = time.monotonic()
                    self._awaiting_pong = True
            except (socket.timeout, ssl.SSLError) as e:
                # Idle socket read timeout: this is EXPECTED every
                # _read_timeout seconds and is how we get a chance to run
                # keepalive checks on a quiet link.
                if "timed out" in str(e).lower():
                    now = time.monotonic()

                    # Server silent too long since our last PING -> the link
                    # is dead/half-open. Drop it and let the backoff loop
                    # re-establish a fresh connection instead of hanging.
                    if self._awaiting_pong and (now - self._last_recv_time) >= self._pong_deadline:
                        self.logger.log_service(
                            "WARN",
                            f"No data from IRC server for {int(now - self._last_recv_time)}s "
                            f"after PING; treating link as dead."
                        )
                        self._disconnect(reason=(
                            "Timeout: server tidak merespons PING "
                            f"(> {int(self._pong_deadline)}s tanpa data)."
                        ))
                        continue

                    # Otherwise just refresh the keepalive PING.
                    if now - self._last_ping_sent >= self._ping_interval:
                        self._send_raw(f"PING :{self.config.irc.server}")
                        self._last_ping_sent = now
                        self._awaiting_pong = True
                    continue
                self.logger.log_service("WARN", f"IRC socket read error: {e}")
                self._disconnect(reason=f"Kesalahan socket IRC: {e}")
            except OSError as e:
                self.logger.log_service("WARN", f"IRC connection dropped: {e}")
                self._disconnect(reason=f"Koneksi IRC terputus: {e}")
            except Exception as e:
                self.logger.log_service("ERROR", f"Unexpected IRC error: {e}")
                self._disconnect(reason=f"Kesalahan tak terduga pada koneksi IRC: {e}")

        self._disconnect(send_quit=True)

    def _fire_disconnect_alert(self, reason: str) -> None:
        """Dispatches the disconnect alert callback once per outage window."""
        if not self.on_disconnect:
            return
        try:
            self.on_disconnect(reason)
        except Exception as e:
            self.logger.log_service("ERROR", f"Disconnect callback error: {e}")
