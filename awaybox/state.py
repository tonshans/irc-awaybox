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

import time
from typing import Dict, Iterable, Optional, Set
from awaybox.config import AppConfig


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

        # 5. Runtime stats & status
        self.irc_connected: bool = False
        self.irc_authenticated: bool = False
        self.telegram_active: bool = False
        self.start_time: float = time.time()
        self.reconnect_attempts: int = 0
        self.last_disconnect_time: Optional[float] = None

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
