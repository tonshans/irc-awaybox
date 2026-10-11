"""
AwayBox Telegram Reply Session Manager

Manages the active reply session initiated via Telegram inline buttons:
- Single active session at any one time
- Target can be a channel (#channel) or private user (nick)
- Automatically expires after configured timeout seconds
- Can be aborted manually with /cancel
- Expired sessions never forward text to IRC
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ReplySession:
    target: str
    target_type: str  # "channel" | "private"
    created_at: float
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        current = now if now is not None else time.time()
        return current >= self.expires_at

    def remaining_seconds(self, now: Optional[float] = None) -> int:
        current = now if now is not None else time.time()
        return max(0, int(self.expires_at - current))


class ReplyManager:
    def __init__(self, default_timeout_seconds: int = 120) -> None:
        self.default_timeout_seconds = default_timeout_seconds
        self._current_session: Optional[ReplySession] = None

    def start_session(self, target: str, target_type: str, timeout_seconds: Optional[int] = None) -> ReplySession:
        """Starts a new active reply session, overriding any existing one."""
        now = time.time()
        duration = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        session = ReplySession(
            target=target.strip(),
            target_type=target_type.strip().lower(),
            created_at=now,
            expires_at=now + duration,
        )
        self._current_session = session
        return session

    def get_active_session(self, now: Optional[float] = None) -> Optional[ReplySession]:
        """
        Returns the active session if present and not expired.
        If expired, clears it and returns None.
        """
        if self._current_session is None:
            return None

        if self._current_session.is_expired(now):
            self._current_session = None
            return None

        return self._current_session

    def consume_active_session(self, now: Optional[float] = None) -> Optional[ReplySession]:
        """
        Retrieves the valid session and immediately clears it so
        subsequent messages won't trigger unwanted IRC transmits.
        """
        session = self.get_active_session(now)
        if session is not None:
            self._current_session = None
        return session

    def cancel_session(self) -> bool:
        """Cancels any active session. Returns True if a session was active."""
        if self._current_session is not None and not self._current_session.is_expired():
            self._current_session = None
            return True
        self._current_session = None
        return False
