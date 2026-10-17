"""
AwayBox Runtime State Module

Maintains transient runtime state that is NOT persisted to the config file:
- Active joined channels
- Temporary auto-reply overrides (on/off, channel list)
- Nickname ignore list (case-insensitive)
- Cooldown timestamps per nickname
- Reconnect attempt counters
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set
from awaybox.config import AppConfig


@dataclass
class _WhoisBuffer:
    """A pending WHOIS reply buffer for one nickname."""
    lines: List[str] = field(default_factory=list)
    completed: bool = False  # True once the server sent RPL_ENDOFWHOIS


class RuntimeState:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        
        # 1. Active channels (starts with configured defaults)
        # Stored in lowercase or normalized with case preserved for IRC commands
        self._active_channels: Set[str] = set()
        for ch in config.irc.default_channels:
            norm = ch.strip()
            if norm:
                self._active_channels.add(norm)

        # 2. Runtime Ignore List (case-insensitive)
        self._ignore_list: Set[str] = set()

        # 3. Auto-reply runtime overrides
        self._autoreply_enabled: bool = config.autoreply.enabled
        self._autoreply_channels: Set[str] = {
            c.lower().strip() for c in config.autoreply.default_channels if c.strip()
        }

        # 4. Auto-reply cooldown timestamps: lowercase_nick -> float timestamp
        self._cooldown_timestamps: Dict[str, float] = {}

        # 5. Pending WHOIS buffers: nick -> _WhoisBuffer (reply lines + completion flag)
        self._pending_whois: Dict[str, _WhoisBuffer] = {}

        # 6. Runtime stats & status
        self.irc_connected: bool = False
        self.irc_authenticated: bool = False
        self.telegram_active: bool = False
        self.start_time: float = time.time()
        self.reconnect_attempts: int = 0
        self.last_disconnect_time: Optional[float] = None

        # 7. Disconnect alert tracking (Telegram notification dedup)
        # Set when a disconnect notification is dispatched; cleared once the
        # bot successfully re-registers on IRC (RPL_WELCOME).
        self.disconnect_notified: bool = False

        # 8. Manual reconnect control (from Telegram buttons / commands)
        # _auto_reconnect_enabled: automatic backoff reconnection allowed.
        # _manual_reconnect_requested: user asked for an immediate reconnect.
        # _owner_disconnect_requested: owner explicitly disconnected via
        #     /disconirc; the IRC loop stays fully idle until /conirc.
        # _immediate_reconnect_wait: seconds to wait before a manual reconnect
        #     attempt so the IRC worker loop can interrupt its current backoff.
        self._auto_reconnect_enabled: bool = True
        self._manual_reconnect_requested: bool = False
        self._owner_disconnect_requested: bool = False
        self._immediate_reconnect_wait: float = 1.0
        self._reconnect_lock = threading.Lock()

    # ---- Disconnect Alert Tracking ----
    def should_send_disconnect_alert(self) -> bool:
        """Returns True and marks the alert as sent (atomic check-and-set)."""
        with self._reconnect_lock:
            if self.disconnect_notified:
                return False
            self.disconnect_notified = True
            return True

    def clear_disconnect_alert(self) -> None:
        """Resets the disconnect alert flag after successful reconnection."""
        with self._reconnect_lock:
            self.disconnect_notified = False

    # ---- Manual Reconnect Controls ----
    @property
    def auto_reconnect_enabled(self) -> bool:
        return self._auto_reconnect_enabled

    def set_auto_reconnect(self, enabled: bool) -> None:
        with self._reconnect_lock:
            self._auto_reconnect_enabled = enabled

    def request_manual_reconnect(self) -> None:
        """Flags an immediate reconnect attempt and re-enables auto-reconnect."""
        with self._reconnect_lock:
            self._manual_reconnect_requested = True
            self._auto_reconnect_enabled = True

    def consume_manual_reconnect(self) -> bool:
        """Atomically checks and clears the manual reconnect request flag."""
        with self._reconnect_lock:
            requested = self._manual_reconnect_requested
            self._manual_reconnect_requested = False
            return requested

    # ---- Owner Connect / Disconnect Controls (/conirc & /disconirc) ----
    @property
    def owner_disconnect_requested(self) -> bool:
        """True while the owner has explicitly disconnected the bot via /disconirc."""
        return self._owner_disconnect_requested

    def request_owner_connect(self) -> None:
        """Clears the /disconirc hold and asks for an immediate IRC connection."""
        with self._reconnect_lock:
            self._owner_disconnect_requested = False
            self._manual_reconnect_requested = True
            self._auto_reconnect_enabled = True

    def request_owner_disconnect(self) -> None:
        """Holds the IRC link closed until the owner issues /conirc.

        Disables both automatic backoff reconnection and any pending manual
        reconnect request so the worker loop stays fully idle.
        """
        with self._reconnect_lock:
            self._owner_disconnect_requested = True
            self._auto_reconnect_enabled = False
            self._manual_reconnect_requested = False

    def get_reconnect_sleep(self, configured_delay: float) -> float:
        """Backoff sleep duration honoring a pending manual reconnect request."""
        with self._reconnect_lock:
            if self._manual_reconnect_requested:
                return min(configured_delay, self._immediate_reconnect_wait)
            return configured_delay

    # ---- Channel Management ----
    @property
    def active_channels(self) -> Set[str]:
        return set(self._active_channels)

    def add_channel(self, channel: str) -> bool:
        ch = channel.strip()
        if not ch:
            return False
        if not ch.startswith("#") and not ch.startswith("&"):
            ch = f"#{ch}"
        if ch not in self._active_channels:
            self._active_channels.add(ch)
            return True
        return False

    def remove_channel(self, channel: str) -> bool:
        ch = channel.strip()
        # Check direct or case-insensitive match
        for existing in list(self._active_channels):
            if existing.lower() == ch.lower():
                self._active_channels.remove(existing)
                return True
        return False

    def has_channel(self, channel: str) -> bool:
        ch_lower = channel.strip().lower()
        return any(c.lower() == ch_lower for c in self._active_channels)

    # ---- Ignore List Management (Strict Case-Insensitive) ----
    @property
    def ignore_list(self) -> Set[str]:
        return set(self._ignore_list)

    def is_ignored(self, nickname: str) -> bool:
        """Returns True if the nickname is in the runtime ignore list."""
        if not nickname:
            return False
        return nickname.strip().lower() in self._ignore_list

    def add_ignore(self, nickname: str) -> bool:
        nick = nickname.strip().lower()
        if not nick:
            return False
        if nick in self._ignore_list:
            return False
        self._ignore_list.add(nick)
        return True

    def remove_ignore(self, nickname: str) -> bool:
        nick = nickname.strip().lower()
        if nick in self._ignore_list:
            self._ignore_list.remove(nick)
            return True
        return False

    # ---- Auto-Reply Runtime Controls ----
    @property
    def autoreply_enabled(self) -> bool:
        return self._autoreply_enabled

    def set_autoreply_enabled(self, enabled: bool) -> None:
        self._autoreply_enabled = enabled

    @property
    def autoreply_channels(self) -> Set[str]:
        return set(self._autoreply_channels)

    def add_autoreply_channel(self, channel: str) -> bool:
        ch = channel.strip().lower()
        if not ch.startswith("#") and not ch.startswith("&"):
            ch = f"#{ch}"
        if ch not in self._autoreply_channels:
            self._autoreply_channels.add(ch)
            return True
        return False

    def remove_autoreply_channel(self, channel: str) -> bool:
        ch = channel.strip().lower()
        if ch in self._autoreply_channels:
            self._autoreply_channels.remove(ch)
            return True
        return False

    def is_autoreply_channel(self, channel: str) -> bool:
        return channel.strip().lower() in self._autoreply_channels

    # ---- Cooldown Management ----
    def is_on_cooldown(self, nickname: str, now: Optional[float] = None) -> bool:
        """Checks if a nickname is currently on auto-reply cooldown."""
        if not nickname:
            return False
        key = nickname.strip().lower()
        last_time = self._cooldown_timestamps.get(key)
        if last_time is None:
            return False
        curr = now if now is not None else time.time()
        elapsed = curr - last_time
        return elapsed < self.config.autoreply.cooldown_seconds

    def update_cooldown(self, nickname: str, timestamp: Optional[float] = None) -> None:
        """Updates the last auto-reply timestamp for a nickname."""
        if not nickname:
            return
        key = nickname.strip().lower()
        self._cooldown_timestamps[key] = timestamp if timestamp is not None else time.time()

    def get_remaining_cooldown(self, nickname: str, now: Optional[float] = None) -> int:
        """Returns remaining cooldown seconds for nickname, or 0 if expired."""
        key = nickname.strip().lower()
        last_time = self._cooldown_timestamps.get(key)
        if last_time is None:
            return 0
        curr = now if now is not None else time.time()
        remaining = self.config.autoreply.cooldown_seconds - (curr - last_time)
        return max(0, int(remaining))

    # ---- Pending WHOIS Buffers ----
    @property
    def pending_whois(self) -> Dict[str, List[str]]:
        return {k: list(v.lines) for k, v in self._pending_whois.items()}

    def has_pending_whois(self, nickname: str) -> bool:
        """Returns True if a pending WHOIS buffer exists for the nickname (case-insensitive)."""
        if not nickname:
            return False
        return nickname.strip().lower() in self._pending_whois

    def register_whois(self, nickname: str, completed: bool = False) -> bool:
        """Registers a pending WHOIS buffer for the nickname.

        Returns False if a buffer already exists for that nickname.
        `completed=True` marks the request as finished (server sent RPL_ENDOFWHOIS);
        the buffered lines stay available for delivery until popped.
        """
        nick = nickname.strip()
        if not nick:
            return False
        key = nick.lower()
        if key in self._pending_whois:
            return False
        self._pending_whois[key] = _WhoisBuffer(completed=completed)
        return True

    def mark_whois_completed(self, nickname: str) -> bool:
        """Flags an existing pending buffer as fully received. Returns False if absent."""
        buf = self._pending_whois.get(nickname.strip().lower())
        if buf is None:
            return False
        buf.completed = True
        return True

    def append_whois_reply(self, nickname: str, line: str) -> bool:
        """Appends a server reply line to the pending WHOIS buffer. Returns False if no buffer exists."""
        buf = self._pending_whois.get(nickname.strip().lower())
        if buf is None:
            return False
        buf.lines.append(line)
        return True

    def pop_whois_buffer(self, nickname: str) -> Optional[List[str]]:
        """Removes and returns the pending WHOIS buffer lines, or None if not found."""
        buf = self._pending_whois.pop(nickname.strip().lower(), None)
        if buf is None:
            return None
        return list(buf.lines)
