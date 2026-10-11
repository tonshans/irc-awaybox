"""
AwayBox Plain-Text Logger Module

Handles logging of:
1. Channel messages -> <log_dir>/channels/<channel>.log
2. Private messages -> <log_dir>/private/<sender>.log
3. Service recorder & errors -> <log_dir>/recorder.log

Guarantees:
- UTF-8 text encoding
- Path traversal prevention & safe filename sanitization
- CR/LF normalization so a single message never splits log lines
- Password & token redaction in all logs
- Standard ISO UTC timestamps: [YYYY-MM-DD HH:MM:SS +0000]
"""

from __future__ import annotations

import datetime
import os
import re
import sys
from pathlib import Path
from typing import List, Optional


def sanitize_filename(name: str) -> str:
    """
    Sanitizes a channel name or nickname for safe use as a filename.
    Strips leading # or &, prevents directory traversal, null bytes, and path separators.
    """
    if not name:
        return "unnamed"
    
    if name.strip() in (".", ".."):
        return "safe_channel"

    # Strip leading channel prefixes (#, &, +, !)
    cleaned = name.lstrip("#&+!")

    # Remove dangerous filesystem characters: / \ : * ? " < > | and null bytes
    cleaned = re.sub(r'[\/\\:\*\?"<>\|\x00]', '_', cleaned)

    # Disallow relative traversal patterns (replace consecutive dots with underscores)
    cleaned = re.sub(r'\.{2,}', '__', cleaned)

    # Disallow relative traversal patterns (. and ..)
    cleaned = cleaned.strip(". ")
    if not cleaned or cleaned in (".", "..") or set(cleaned).issubset({"_", "."}):
        return "safe_channel"

    # Limit length
    return cleaned[:100]


def normalize_message_text(text: str) -> str:
    """
    Normalizes CR and LF characters to space so a single message
    cannot break the plain-text line format.
    """
    if not text:
        return ""
    # Replace \r\n, \r, \n with a space
    cleaned = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    # Replace non-printable ASCII control codes except standard printable unicode
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
    return cleaned.strip()


def redact_secrets(text: str, secrets: Optional[List[str]] = None) -> str:
    """Redacts known sensitive strings (passwords, tokens) from text."""
    if not text:
        return ""
    result = text
    if secrets:
        for secret in secrets:
            if secret and len(secret) >= 4:
                result = result.replace(secret, "[REDACTED]")
    # Also generic regex redacting common patterns
    result = re.sub(r'(IDENTIFY\s+)(\S+)', r'\1[REDACTED]', result, flags=re.IGNORECASE)
    result = re.sub(r'(bot\d{6,12}:)[A-Za-z0-9_-]{20,}', r'\1[REDACTED_TOKEN]', result)
    return result


def get_utc_timestamp() -> str:
    """Generates standard timestamp: [YYYY-MM-DD HH:MM:SS +0000]"""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("[%Y-%m-%d %H:%M:%S +0000]")


class AwayBoxLogger:
    def __init__(self, log_dir: str | Path, secrets: Optional[List[str]] = None) -> None:
        self.base_dir = Path(log_dir).resolve()
        self.channels_dir = self.base_dir / "channels"
        self.private_dir = self.base_dir / "private"
        self.recorder_file = self.base_dir / "recorder.log"
        self.secrets = [s for s in (secrets or []) if s]

        self._ensure_directories()

    def _ensure_directories(self) -> None:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.channels_dir.mkdir(parents=True, exist_ok=True)
            self.private_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Fall back to logging to stderr if filesystem write fails
            print(f"[AwayBoxLogger] ERROR creating log directories: {e}", file=sys.stderr)

    def log_service(self, level: str, message: str) -> None:
        """
        Logs service-level operations to recorder.log (startup, connection, reconnect, errors).
        """
        ts = get_utc_timestamp()
        safe_msg = redact_secrets(normalize_message_text(message), self.secrets)
        line = f"{ts} [{level.upper()}] {safe_msg}\n"
        self._append_line(self.recorder_file, line)

    def log_channel_message(self, channel: str, sender: str, text: str) -> None:
        """
        Logs a channel message:
        Example: [2026-10-09 00:12:04 +0000] <Alice> Hello Ton
        """
        ts = get_utc_timestamp()
        safe_channel_file = sanitize_filename(channel) + ".log"
        file_path = self.channels_dir / safe_channel_file

        safe_sender = normalize_message_text(sender)
        safe_msg = redact_secrets(normalize_message_text(text), self.secrets)
        line = f"{ts} <{safe_sender}> {safe_msg}\n"
        self._append_line(file_path, line)

    def log_private_incoming(self, sender: str, text: str) -> None:
        """
        Logs an incoming private message:
        Example: [2026-10-09 00:20:11 +0000] IN  <Alice> Are you still using IRC?
        """
        ts = get_utc_timestamp()
        safe_sender_file = sanitize_filename(sender) + ".log"
        file_path = self.private_dir / safe_sender_file

        safe_sender = normalize_message_text(sender)
        safe_msg = redact_secrets(normalize_message_text(text), self.secrets)
        line = f"{ts} IN  <{safe_sender}> {safe_msg}\n"
        self._append_line(file_path, line)

    def log_private_outgoing(self, target_nick: str, my_nick: str, text: str) -> None:
        """
        Logs an outgoing private message (auto-reply or Telegram relay):
        Example: [2026-10-09 00:20:12 +0000] OUT <Ton> Ton sedang offline. Pesan kamu sudah direkam.
        """
        ts = get_utc_timestamp()
        safe_file = sanitize_filename(target_nick) + ".log"
        file_path = self.private_dir / safe_file

        safe_my_nick = normalize_message_text(my_nick)
        safe_msg = redact_secrets(normalize_message_text(text), self.secrets)
        line = f"{ts} OUT <{safe_my_nick}> {safe_msg}\n"
        self._append_line(file_path, line)

    def _append_line(self, target_file: Path, line: str) -> None:
        try:
            with open(target_file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            # Never crash silently without reporting
            print(f"[AwayBoxLogger] WRITE ERROR to {target_file}: {e} | Line: {line.strip()}", file=sys.stderr)

    def read_tail(self, target_name: str, max_lines: int = 20) -> List[str]:
        """
        Retrieves the last N lines from a channel or private log file.
        Target can be '#channel' or 'Nick'.
        """
        if not target_name:
            return []

        clean_name = target_name.strip()
        is_channel = clean_name.startswith("#") or clean_name.startswith("&")
        safe_filename = sanitize_filename(clean_name) + ".log"

        if is_channel:
            target_path = self.channels_dir / safe_filename
        else:
            # Check private first, fallback to channel if file exists there
            target_path = self.private_dir / safe_filename
            if not target_path.is_file() and (self.channels_dir / safe_filename).is_file():
                target_path = self.channels_dir / safe_filename

        if not target_path.is_file():
            return []

        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            trimmed = [line.rstrip("\r\n") for line in lines[-max_lines:]]
            return trimmed
        except OSError as e:
            self.log_service("ERROR", f"Failed to read tail for {target_name}: {e}")
            return []
