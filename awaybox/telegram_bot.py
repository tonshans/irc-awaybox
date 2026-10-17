"""
AwayBox Telegram Bot Module

Provides the remote control interface via Telegram Bot API using long polling:
- Strict authorization check (numeric allowed_user_id only)
- Interactive notifications with inline reply buttons
- Reply session workflow management
- Commands: /start, /status, /channels, /msg, /join, /leave, /autoreply, /ignore, /unignore, /log, /whois, /reconnect, /conirc, /disconirc, /cancel
- Disconnect alerts with inline choice buttons: Reconnect Sekarang / Jangan Reconnect
- Manual link control from Telegram: /conirc connects (or reconnects) the bot
  to IRC immediately; /disconirc disconnects and holds the link closed until /conirc
- Fault isolation: Telegram outages do not crash IRC logging or connection
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from awaybox.config import AppConfig
from awaybox.logger import AwayBoxLogger
from awaybox.reply_manager import ReplyManager
from awaybox.state import RuntimeState

# Canonical IRC nickname validation pattern (RFC 1459 / modern servers).
# NOTE: a space is intentionally NOT part of the character class.
# Use _NICK_RE.fullmatch(...) everywhere so partial matches can never slip through.
_NICK_RE = re.compile(r"[A-Za-z0-9_\\{}\^|`~-]{1,30}")

# Numeric WHOIS reply codes whose target nickname lives in params[1].
# Single source of truth: awaybox/irc_client.py imports this constant instead
# of keeping a second copy that can silently drift out of sync (bug #4 fix).
# RPL_AWAY (301) was missing here, so "away message" lines from WHOIS replies
# were dropped before reaching Telegram.
_WHOIS_RPL_CODES = frozenset(
    "250 251 252 253 254 255 256 257 258 259 260 261 262 265 276 278 "
    "301 307 311 312 313 317 318 319 320 321 323 330 335 337 338 340 378 379 402".split()
)


def is_valid_nick(nickname: str) -> bool:
    """Returns True only if the whole string is a valid single IRC nickname."""
    return bool(_NICK_RE.fullmatch(nickname))


def html_escape(text: str) -> str:
    """Escapes text for Telegram's HTML parse mode.

    All attacker-/server-controlled strings (IRC nicknames, message bodies,
    log lines, disconnect reasons) MUST pass through this before being
    embedded in messages sent with parse_mode="HTML", otherwise raw markup
    like "<b>" or malformed entities makes sendMessage fail and the
    notification is silently lost (bugs #3 & #4 fix).
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TelegramBot:
    def __init__(
        self,
        config: AppConfig,
        state: RuntimeState,
        logger: AwayBoxLogger,
        reply_manager: ReplyManager,
        irc_send_func: Optional[Callable[[str, str], bool]] = None,
        irc_join_func: Optional[Callable[[str], bool]] = None,
        irc_leave_func: Optional[Callable[[str], bool]] = None,
        irc_whois_func: Optional[Callable[[str], bool]] = None,
        irc_reconnect_func: Optional[Callable[[], bool]] = None,
        irc_connect_func: Optional[Callable[[], bool]] = None,
        irc_disconnect_func: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.config = config
        self.state = state
        self.logger = logger
        self.reply_manager = reply_manager
        self.irc_send = irc_send_func
        self.irc_join = irc_join_func
        self.irc_leave = irc_leave_func
        self.irc_whois = irc_whois_func
        self.irc_reconnect = irc_reconnect_func
        self.irc_connect = irc_connect_func
        self.irc_disconnect = irc_disconnect_func

        self.token = config.telegram_bot_token or ""
        self.allowed_user_id = config.telegram.allowed_user_id
        self.api_url = f"https://api.telegram.org/bot{self.token}"

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_update_id = 0

    def start(self) -> None:
        """Starts long-polling worker thread if token is configured."""
        if not self.token or self.allowed_user_id <= 0:
            self.logger.log_service(
                "WARN",
                "Telegram bot token or allowed_user_id not configured. Remote control disabled."
            )
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._polling_loop, name="AwayBox-Telegram", daemon=True)
        self._thread.start()
        self.state.telegram_active = True
        self.logger.log_service("INFO", "Telegram bot long-polling started.")

    def stop(self) -> None:
        """Stops the worker thread."""
        self._stop_event.set()
        self.state.telegram_active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # ---- Low-level HTTP API Request ----
    def _api_request(self, endpoint: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Optional[Dict[str, Any]]:
        if not self.token:
            return None

        url = f"{self.api_url}/{endpoint}"
        data = None
        headers = {"User-Agent": "AwayBox/0.1.0"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                res = json.loads(raw)
                if res.get("ok"):
                    return res.get("result")
                return None
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.logger.log_service("ERROR", "Invalid Telegram Bot Token (401 Unauthorized).")
            else:
                self.logger.log_service("WARN", f"Telegram API HTTP error {e.code}: {e.reason}")
            return None
        except Exception as e:
            # Network blip or timeout
            return None

    # ---- Sending Messages & Notifications ----
    @staticmethod
    def _truncate_html(text: str, limit: int = 4000) -> str:
        """Truncates text to `limit` characters WITHOUT breaking HTML markup.

        A naive ``text[:4000]`` after the markup was built can cut in the middle
        of a long message and drop the closing tag (e.g. ``</pre>``), which makes
        Telegram reject the send with error 400 and silently lose the whole
        notification (bug #5 fix). This helper truncates the plain content and
        re-appends any unbalanced closing tags.
        """
        if len(text) <= limit:
            return text

        # Find the last '>' at or before the limit that is NOT part of an
        # escaped entity (&amp; &lt; &gt;) so we never split a tag or entity.
        cut = -1
        i = limit
        while i >= 0:
            j = text.rfind(">", 0, i + 1)
            if j == -1:
                break
            k = text.rfind("&", 0, j)
            if k != -1 and (j - k) <= 5 and text[k:j + 1] in ("&amp;", "&lt;", "&gt;"):
                i = k - 1
                continue
            cut = j + 1
            break
        if cut <= 0:
            cut = limit

        truncated = text[:cut]
        # Re-close any block tags left open by the cut, innermost first.
        closers = []
        for tag in ("code", "i", "b", "pre"):
            opens = len(re.findall(rf"<{tag}(?=>|\s)", truncated))
            closes = len(re.findall(rf"</{tag}>", truncated))
            if opens > closes:
                closers.append(f"</{tag}>")
        # Nesting order: <pre> wraps <code>/<b>/<i>, so close inner tags first.
        suffix = "".join(
            c for tag in ("i", "code", "b", "pre")
            for c in closers if c == f"</{tag}>"
        )
        return truncated + suffix + "\n… (dipotong)"

    def send_message(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Sends a text message to the authorized user."""
        if self.allowed_user_id <= 0:
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.allowed_user_id,
            # Markup-aware truncation: never cut inside a tag/entity so long
            # WHOIS/log messages still parse correctly on Telegram (bug #5).
            "text": self._truncate_html(text) if parse_mode == "HTML" else text[:4000],
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode

        res = self._api_request("sendMessage", payload, timeout=15)
        return res is not None

    def answer_callback(self, callback_query_id: str, text: Optional[str] = None) -> None:
        """Acknowledges a callback button press."""
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._api_request("answerCallbackQuery", payload, timeout=10)

    def notify_channel_message(self, channel: str, sender: str, text: str) -> bool:
        """
        Sends Telegram notification for incoming channel message with buttons:
        [Reply Channel] [Private <sender>]
        """
        clean_text = html_escape(text[:500])
        msg = f"<b>[{html_escape(channel)}]</b> &lt;{html_escape(sender)}&gt;: {clean_text}"

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": f"Reply {channel}"[:64],
                        "callback_data": f"reply:channel:{channel}",
                    },
                    {
                        "text": f"Private {sender}"[:64],
                        "callback_data": f"reply:private:{sender}",
                    },
                ]
            ]
        }
        return self.send_message(msg, reply_markup=keyboard, parse_mode="HTML")

    def notify_private_message(self, sender: str, text: str) -> bool:
        """
        Sends Telegram notification for incoming PM with button:
        [Reply <sender>]
        """
        clean_text = html_escape(text[:500])
        msg = f"<b>[PM]</b> &lt;{html_escape(sender)}&gt;: {clean_text}"

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": f"Reply {sender}"[:64],
                        "callback_data": f"reply:private:{sender}",
                    }
                ]
            ]
        }
        return self.send_message(msg, reply_markup=keyboard, parse_mode="HTML")

    def notify_disconnected(self, reason: str) -> bool:
        """
        Sends Telegram alert when the bot loses its IRC connection, with a
        1-click choice: reconnect now or pause auto-reconnect entirely.
        Buttons: [🔄 Reconnect Sekarang] [⏸️ Jangan Reconnect]
        """
        if self.state.last_disconnect_time:
            ago = int(time.time() - self.state.last_disconnect_time)
            when = f"{ago} detik lalu"
        else:
            when = "baru saja"

        msg = (
            f"🔌 <b>PERHATIAN: Koneksi IRC Terputus!</b>\n\n"
            f"• Server: {self.config.irc.server}:{self.config.irc.port}\n"
            f"• Nick: {self.config.irc.nick}\n"
            f"• Waktu: {when}\n"
            f"• Percobaan ulang saat ini: {self.state.reconnect_attempts}\n"
            f"• Alasan: {html_escape(reason[:300])}\n\n"
            f"Pilih tindakan:\n"
            f"🔄 <i>Reconnect</i> — coba sambungkan kembali sekarang.\n"
            f"⏸️ <i>Jangan Reconnect</i> — hentikan percobaan otomatis sampai Anda memerintahkan /reconnect."
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Reconnect Sekarang", "callback_data": "reconnect:yes"},
                    {"text": "⏸️ Jangan Reconnect", "callback_data": "reconnect:no"},
                ]
            ]
        }
        return self.send_message(msg, reply_markup=keyboard, parse_mode="HTML")

    def notify_reconnected(self) -> bool:
        """Sends Telegram confirmation once the IRC connection is restored."""
        msg = (
            f"✅ <b>Koneksi IRC Pulih!</b>\n"
            f"Bot berhasil tersambung kembali ke {self.config.irc.server} "
            f"sebagai {self.config.irc.nick}."
        )
        return self.send_message(msg, parse_mode="HTML")

    # ---- Reconnect Control Helpers ----
    def trigger_reconnect(self) -> bool:
        """Requests an immediate IRC reconnect attempt (button / command).

        Returns False when the owner has explicitly held the link closed via
        /disconirc: in that state a manual reconnect must not silently
        override the hold (bug #2 fix) — use /conirc to resume instead.
        """
        if self.state.owner_disconnect_requested:
            return False
        self.state.set_auto_reconnect(True)
        if self.irc_reconnect:
            return bool(self.irc_reconnect())
        self.state.request_manual_reconnect()
        return True

    def halt_reconnect(self) -> None:
        """Pauses automatic reconnection until the owner manually resumes."""
        self.state.set_auto_reconnect(False)

    # ---- Manual Connect / Disconnect Control (/conirc & /disconirc) ----
    def connect_irc(self) -> str:
        """Connects (or reconnects) the bot to IRC immediately via /conirc."""
        if self.irc_connect:
            initiated = bool(self.irc_connect())
        else:
            # Fallback when no direct connect hook is wired: use state flags.
            self.state.request_owner_connect()
            initiated = True

        if not initiated:
            return "⚠️ Gagal: perintah IRC client tidak dapat dijalankan saat ini."

        if self.state.irc_connected:
            return (
                "🔄 <b>Sambungan ulang IRC diminta.</b>\n"
                "Bot sedang menyambung ulang ke server IRC sekarang."
            )
        return (
            "🔗 <b>Koneksi IRC diminta.</b>\n"
            f"Bot akan segera mencoba menyambung ke {self.config.irc.server}:{self.config.irc.port} "
            f"sebagai {self.config.irc.nick}. Anda akan dinotifikasi ketika berhasil."
        )

    def disconnect_irc(self) -> str:
        """Disconnects the bot from IRC on demand via /disconirc.

        The link stays closed (no auto-reconnect, no retry attempts) until the
        owner issues /conirc again.
        """
        was_connected: Optional[bool] = None
        if self.irc_disconnect:
            was_connected = bool(self.irc_disconnect())
        else:
            # Fallback when no direct disconnect hook is wired.
            self.state.request_owner_disconnect()

        if was_connected is False:
            return (
                "ℹ️ Koneksi IRC sudah terputus. Bot tetap offline sampai Anda mengetik /conirc."
            )
        return (
            "🔌 <b>Koneksi IRC diputus.</b>\n"
            "Bot mengirim QUIT dan menutup koneksi. Tidak ada percobaan sambung otomatis; "
            "ketik /conirc kapan saja untuk menghubungkan kembali."
        )

    # ---- Authorization Check ----
    def is_authorized(self, user_id: int) -> bool:
        """Strict check against single configured Telegram numeric user ID."""
        return user_id == self.allowed_user_id

    # ---- Command Dispatcher ----
    def handle_command(self, cmd_line: str) -> str:
        parts = cmd_line.strip().split()
        if not parts:
            return ""

        cmd = parts[0].lower().split("@")[0]  # strip @botname if present
        args = parts[1:]
        # Replies are sent with parse_mode="HTML" and contain our own markup,
        # so raw user-supplied tokens must be escaped HERE (at the source),
        # never by double-escaping the whole finished reply (bug #6 fix).
        safe_args = [html_escape(a) for a in args]

        if cmd == "/start":
            return (
                "<b>AwayBox IRC Mailbox Controller</b>\n\n"
                "Perintah tersedia:\n"
                "/status - Status koneksi IRC & layanan\n"
                "/channels - Daftar channel aktif\n"
                "/msg #channel|Nick pesan - Kirim pesan langsung ke channel/nickname IRC\n"
                "/join #channel - Masuk ke channel\n"
                "/leave #channel - Keluar dari channel\n"
                "/autoreply [on|off|add #ch|remove #ch] - Kelola auto-reply\n"
                "/ignore [Nick] - Lihat atau tambah ignore list\n"
                "/unignore Nick - Hapus dari ignore list\n"
                "/whois Nick - Informasi user IRC (realname, server, channels)\n"
                "/log [#ch|Nick] - Ambil cuplikan riwayat pesan\n"
                "/reconnect [auto|now|stop] - Kontrol reconnect IRC dari Telegram\n"
                "/conirc - Hubungkan koneksi bot ke IRC sekarang\n"
                "/disconirc - Putuskan koneksi bot dari IRC (tahan offline sampai /conirc)\n"
                "/cancel - Batalkan sesi balasan aktif"
            )

        elif cmd == "/status":
            uptime_sec = int(time.time() - self.state.start_time)
            hrs = uptime_sec // 3600
            mins = (uptime_sec % 3600) // 60
            irc_status = "Connected" if self.state.irc_connected else "Disconnected"
            auth_status = "Yes" if self.state.irc_authenticated else "No"
            channels = ", ".join(sorted(self.state.active_channels)) or "(none)"
            ar_status = "Enabled" if self.state.autoreply_enabled else "Disabled"
            ar_channels = ", ".join(sorted(self.state.autoreply_channels)) or "(all PMs only)"
            ignore_nicks = ", ".join(sorted(self.state.ignore_list)) or "(none)"

            session = self.reply_manager.get_active_session()
            session_info = (
                f"Active ({session.target}, {session.remaining_seconds()}s remaining)"
                if session
                else "None"
            )

            return (
                f"<b>AwayBox Status</b>\n"
                f"• IRC Server: {self.config.irc.server}:{self.config.irc.port}\n"
                f"• IRC Connection: {irc_status}\n"
                f"• NickServ Auth: {auth_status}\n"
                f"• Active Channels: {channels}\n"
                f"• Auto-Reply: {ar_status}\n"
                f"• Auto-Reply Channels: {ar_channels}\n"
                f"• Ignore List: {ignore_nicks}\n"
                f"• Active Reply Session: {session_info}\n"
                f"• Uptime: {hrs}h {mins}m\n"
                f"• Reconnect Attempts: {self.state.reconnect_attempts}\n"
                f"• Auto-Reconnect: {'Enabled' if self.state.auto_reconnect_enabled else 'Paused (manual control)'}"
            )

        elif cmd == "/channels":
            channels = sorted(self.state.active_channels)
            if not channels:
                return "Tidak ada channel aktif saat ini."
            return "<b>Channel Aktif:</b>\n" + "\n".join(f"• {c}" for c in channels)

        elif cmd == "/msg":
            if len(args) < 2:
                return "Gunakan: /msg #channel|Nick isi pesan"
            target = args[0]
            message = " ".join(args[1:]).strip()
            if not message:
                return "Pesan tidak boleh kosong. Gunakan: /msg #channel|Nick isi pesan"

            is_channel = target.startswith("#") or target.startswith("&")
            if is_channel:
                # Strict format validation: clean IRC channel name
                if not re.match(r"^[#&][^\x00\x07\x0a\x0d ,:]+$", target):
                    return "Nama channel tidak valid."
                if not self.state.has_channel(target):
                    return (
                        f"AwayBox belum berada di channel <code>{target}</code>.\n"
                        f"Gunakan /join {safe_args[0]} terlebih dahulu."
                    )
            else:
                # Validate nickname format
                if not is_valid_nick(target):
                    return "Nickname tidak valid."

            if not self.state.irc_connected:
                return "⚠️ Gagal mengirim: Koneksi IRC sedang terputus."

            sent = False
            if self.irc_send:
                sent = self.irc_send(target, message)

            if not sent:
                return f"⚠️ Gagal mengirim pesan ke <code>{safe_args[0]}</code> melalui IRC."

            # Log as OUT message
            if is_channel:
                self.logger.log_channel_message(target, self.config.irc.nick, message)
            else:
                self.logger.log_private_outgoing(target, self.config.irc.nick, message)

            return f"✅ Pesan berhasil dikirim ke <code>{safe_args[0]}</code> di IRC."

        elif cmd == "/join":
            if not args:
                return "Gunakan: /join #channel"
            target_ch = args[0]
            if not target_ch.startswith("#") and not target_ch.startswith("&"):
                target_ch = f"#{target_ch}"
            # Strict format validation: no spaces, clean IRC channel name
            if not re.match(r"^[#&][^\x00\x07\x0a\x0d ,:]+$", target_ch):
                return "Nama channel tidak valid."

            joined = False
            if self.irc_join:
                joined = bool(self.irc_join(target_ch))
            else:
                joined = self.state.add_channel(target_ch)

            if joined:
                if not self.state.irc_connected:
                    return (
                        f"📌 IRC sedang terputus: <code>{safe_args[0]}</code> "
                        f"dicatat dan akan otomatis di-JOIN saat koneksi pulih."
                    )
                return f"Meminta AwayBox bergabung ke <code>{safe_args[0]}</code>."
            return (
                f"⚠️ Gagal bergabung ke <code>{safe_args[0]}</code>: "
                f"koneksi IRC sedang terputus atau channel sudah aktif."
            )

        elif cmd == "/leave":
            if not args:
                return "Gunakan: /leave #channel"
            target_ch = args[0]
            if not self.state.has_channel(target_ch):
                return f"AwayBox tidak berada di channel <code>{safe_args[0]}</code>."

            # Resolve the exact case as tracked in runtime state for a reliable PART
            resolved = target_ch
            for existing in self.state.active_channels:
                if existing.lower() == target_ch.strip().lower():
                    resolved = existing
                    break

            left = False
            if self.irc_leave:
                left = bool(self.irc_leave(resolved))
            else:
                left = self.state.remove_channel(resolved)

            if left:
                return f"Meminta AwayBox meninggalkan <code>{html_escape(resolved)}</code>."
            return f"⚠️ Gagal meninggalkan <code>{html_escape(resolved)}</code>: koneksi IRC sedang terputus."

        elif cmd == "/autoreply":
            if not args:
                status = "Aktif" if self.state.autoreply_enabled else "Nonaktif"
                chs = ", ".join(sorted(self.state.autoreply_channels)) or "(tidak ada)"
                return (
                    f"<b>Pengaturan Auto-Reply:</b>\n"
                    f"• Status: {status}\n"
                    f"• Pesan: \"{html_escape(self.config.autoreply.message)}\"\n"
                    f"• Cooldown: {self.config.autoreply.cooldown_seconds} detik\n"
                    f"• Channel: {chs}\n\n"
                    f"Penggunaan: /autoreply [on | off | add #ch | remove #ch]"
                )

            sub = args[0].lower()
            if sub == "on":
                self.state.set_autoreply_enabled(True)
                return "Auto-reply telah <b>diaktifkan</b> (runtime)."
            elif sub == "off":
                self.state.set_autoreply_enabled(False)
                return "Auto-reply telah <b>dinonaktifkan</b> (runtime)."
            elif sub == "add":
                if len(args) < 2:
                    return "Gunakan: /autoreply add #channel"
                ch = args[1]
                added = self.state.add_autoreply_channel(ch)
                return f"Channel <code>{safe_args[1]}</code> {'ditambahkan ke' if added else 'sudah ada di'} daftar auto-reply."
            elif sub == "remove":
                if len(args) < 2:
                    return "Gunakan: /autoreply remove #channel"
                ch = args[1]
                removed = self.state.remove_autoreply_channel(ch)
                return f"Channel <code>{safe_args[1]}</code> {'dihapus dari' if removed else 'tidak ditemukan di'} daftar auto-reply."
            else:
                return "Opsi /autoreply tidak dikenal. Gunakan: on, off, add, atau remove."

        elif cmd == "/ignore":
            if not args:
                ignored = sorted(self.state.ignore_list)
                if not ignored:
                    return "Daftar ignore kosong."
                return "<b>Nickname Diabaikan:</b>\n" + "\n".join(f"• {n}" for n in ignored)
            nick = args[0]
            if not is_valid_nick(nick):
                return "Nickname tidak valid."
            added = self.state.add_ignore(nick)
            if added:
                return f"Nickname <code>{safe_args[0]}</code> sekarang diabaikan (pesan tidak dicatat & tidak dinotifikasi)."
            return f"Nickname <code>{safe_args[0]}</code> sudah ada dalam ignore list."

        elif cmd == "/unignore":
            if not args:
                return "Gunakan: /unignore Nick"
            nick = args[0]
            removed = self.state.remove_ignore(nick)
            if removed:
                return f"Nickname <code>{safe_args[0]}</code> dihapus dari ignore list."
            return f"Nickname <code>{safe_args[0]}</code> tidak ditemukan di ignore list."

        elif cmd == "/whois":
            if not args:
                return "Gunakan: /whois Nick"
            # handle_command splits on whitespace, so extra tokens mean the user
            # typed something like "/whois bad nick here". A WHOIS target must be
            # exactly ONE nickname — reject multi-token input outright instead of
            # silently validating only the first fragment ("bad").
            if len(args) != 1:
                return "Nickname tidak valid (hanya satu nickname, tanpa spasi). Gunakan: /whois Nick"
            target_nick = args[0]
            # Strict single-nickname validation (fullmatch): strings containing
            # spaces like "bad nick here" are rejected outright and must NOT
            # register a pending WHOIS buffer.
            if not is_valid_nick(target_nick):
                return "Nickname tidak valid. Gunakan: /whois Nick"

            if not self.state.irc_connected:
                return "⚠️ Tidak dapat melakukan WHOIS: Koneksi IRC sedang terputus."

            requested = target_nick.lower()

            # Reply still in transit: do not spam a duplicate WHOIS request.
            if self.state.has_pending_whois(requested):
                return f"Hasil WHOIS untuk <code>{safe_args[0]}</code> masih diproses, coba lagi sebentar."

            # Already-completed reply waiting in the buffer (auto-delivery may have
            # failed while Telegram was down): deliver it now instead of re-requesting.
            buffered = self.state.pop_whois_buffer(requested)
            if buffered is not None:
                body = "\n".join(buffered) if buffered else "(tidak ada detail dari server)"
                return f"<b>WHOIS {html_escape(target_nick)}:</b>\n<pre>{html_escape(body)}</pre>"

            sent = False
            if self.irc_whois:
                sent = bool(self.irc_whois(target_nick))
            if not sent:
                return f"⚠️ Gagal meminta WHOIS untuk <code>{safe_args[0]}</code>: koneksi IRC sedang terputus."

            self.state.register_whois(requested)
            return (
                f"WHOIS untuk <code>{safe_args[0]}</code> diminta. "
                f"Hasil akan dikirim otomatis begitu balasan server diterima."
            )

        elif cmd == "/log":
            if not args:
                return "Gunakan: /log #channel atau /log Nick"
            target = args[0]
            lines = self.logger.read_tail(target, max_lines=self.config.telegram.log_max_lines)
            if not lines:
                return f"Tidak ada riwayat log ditemukan untuk <code>{safe_args[0]}</code>."
            
            header = f"<b>Cuplikan Log untuk {html_escape(target)} ({len(lines)} baris terakhir):</b>\n<pre>"
            content = html_escape("\n".join(lines))
            footer = "</pre>"
            # Telegram character limit check
            if len(header) + len(content) + len(footer) > 4000:
                content = content[-3500:]
            return f"{header}{content}{footer}"

        elif cmd == "/reconnect":
            sub = args[0].lower() if args else "now"
            if sub in ("auto", "on"):
                self.state.set_auto_reconnect(True)
                return (
                    "▶️ Reconnect otomatis <b>diaktifkan</b>. "
                    "Bot akan mencoba sambung kembali dengan backoff eksponensial."
                )
            elif sub in ("stop", "off", "no"):
                self.halt_reconnect()
                return (
                    "⏸️ Reconnect <b>dihentikan</b>. Bot tidak akan mencoba menyambung "
                    "kembali sampai Anda mengetik /reconnect now atau menekan tombol 🔄 Reconnect."
                )
            elif sub == "now":
                if self.state.irc_connected:
                    return "ℹ️ Koneksi IRC saat ini sudah aktif. Tidak ada yang perlu disambungkan ulang."
                if not self.trigger_reconnect():
                    return (
                        "⏸️ <b>Reconnect ditahan oleh /disconirc.</b>\n"
                        "Bot sedang dalam mode offline atas perintah Anda sendiri, sehingga "
                        "/reconnect tidak diizinkan menyambung kembali. "
                        "Ketik /conirc untuk menghubungkan ke IRC lagi."
                    )
                return "🔄 Perintah reconnect diterima. Bot akan segera mencoba menyambung ke IRC..."
            else:
                return "Opsi /reconnect tidak dikenal. Gunakan: now, auto, atau stop."

        elif cmd == "/conirc":
            return self.connect_irc()

        elif cmd == "/disconirc":
            return self.disconnect_irc()

        elif cmd == "/cancel":
            cancelled = self.reply_manager.cancel_session()
            if cancelled:
                return "Sesi balasan telah dibatalkan."
            return "Tidak ada sesi balasan aktif yang perlu dibatalkan."

        return "Perintah tidak dikenal. Ketik /start untuk melihat panduan."

    # ---- WHOIS Reply Handling (invoked from the IRC worker thread) ----
    def handle_whois_reply(self, target_nick: str, line: str, final: bool) -> None:
        """
        Accumulates numeric WHOIS reply lines into the pending buffer for
        target_nick and pushes the finished report to Telegram on RPL_ENDOFWHOIS.
        Lines arriving without a matching pending request are ignored.
        """
        if not self.state.append_whois_reply(target_nick, line):
            return
        if not final:
            return

        # Mark the buffer complete FIRST: if the Telegram push below fails, the
        # finished result stays available so a follow-up /whois delivers it from
        # the cache instead of reporting "still processing" forever.
        self.state.mark_whois_completed(target_nick)
        buffered = self.state.pop_whois_buffer(target_nick) or []
        body = "\n".join(buffered) if buffered else "(tidak ada detail dari server)"
        try:
            # Server-controlled WHOIS text must be HTML-escaped or a nickname/
            # realname containing markup breaks sendMessage (bug #4 fix).
            self.send_message(
                f"<b>WHOIS {html_escape(target_nick)}:</b>\n<pre>{html_escape(body)}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            # Telegram outage must not lose the result: keep it cached for
            # delivery on the next /whois Nick command.
            self.state.register_whois(target_nick, completed=True)
            self.state.append_whois_reply(target_nick, body)

    # ---- Callback Query Handling (Button Clicks) ----
    def handle_callback_query(self, cb: Dict[str, Any]) -> None:
        from_user = cb.get("from", {})
        user_id = from_user.get("id", 0)
        cb_id = cb.get("id", "")

        # 1. Authorization check
        if not self.is_authorized(user_id):
            self.answer_callback(cb_id, text="Akses ditolak.")
            return

        data = cb.get("data", "")

        # format: reconnect:<yes|no> - response to the disconnect alert buttons
        if data.startswith("reconnect:"):
            choice = data.split(":", 1)[1] if ":" in data else ""
            if choice == "yes":
                if not self.trigger_reconnect():
                    self.answer_callback(cb_id, text="Ditahan oleh /disconirc.")
                    self.send_message(
                        "⏸️ <b>Reconnect ditahan oleh /disconirc.</b>\n"
                        "Bot dalam mode offline atas perintah Anda. "
                        "Ketik /conirc untuk menghubungkan ke IRC lagi.",
                        parse_mode="HTML",
                    )
                    return
                self.answer_callback(cb_id, text="Reconnect dimulai...")
                self.send_message(
                    "🔄 <b>Reconnect diminta.</b>\n"
                    "Bot akan segera mencoba menyambung kembali ke IRC server.",
                    parse_mode="HTML",
                )
            elif choice == "no":
                self.halt_reconnect()
                self.answer_callback(cb_id, text="Auto-reconnect dihentikan.")
                self.send_message(
                    "⏸️ <b>Auto-reconnect dihentikan.</b>\n"
                    "Bot tetap offline di IRC sampai Anda mengetik /reconnect now "
                    "atau menekan tombol 🔄 Reconnect Sekarang pada notifikasi terputus berikutnya.",
                    parse_mode="HTML",
                )
            else:
                self.answer_callback(cb_id)
            return

        # format: reply:<type>:<target>
        if data.startswith("reply:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                target_type = parts[1]
                target_name = parts[2]
                session = self.reply_manager.start_session(
                    target=target_name,
                    target_type=target_type,
                    timeout_seconds=self.config.telegram.reply_session_timeout_seconds,
                )
                self.answer_callback(cb_id, text=f"Sesi balasan ke {target_name} dimulai.")
                self.send_message(
                    f"✍️ <b>Balasan aktif untuk {target_name}</b> "
                    f"({session.remaining_seconds()} detik).\n"
                    f"Ketik pesan teks Anda untuk dikirim ke IRC, atau gunakan /cancel untuk membatalkan.",
                    parse_mode="HTML",
                )
                return

        self.answer_callback(cb_id)

    # ---- User Incoming Text Message Handler ----
    def handle_user_text_message(self, user_id: int, text: str) -> None:
        if not self.is_authorized(user_id):
            return

        clean = text.strip()
        if not clean:
            return

        # 1. Is it a command?
        if clean.startswith("/"):
            reply = self.handle_command(clean)
            if reply:
                # handle_command returns its OWN trusted HTML markup (<b>,
                # <code>, ...) and only escapes user-controlled fragments
                # internally. Escaping the whole reply again would show raw
                # tags like "<b>AwayBox Status</b>" to the owner (bug #6 fix).
                self.send_message(reply, parse_mode="HTML")
            return

        # 2. Regular text message -> check active reply session
        session = self.reply_manager.get_active_session()
        if session is None:
            self.send_message(
                "Tidak ada sesi balasan aktif.\n"
                "Tekan tombol <i>Reply</i> pada notifikasi pesan masuk, atau ketik /start untuk bantuan.",
                parse_mode="HTML",
            )
            return

        # Check IRC connection
        if not self.state.irc_connected:
            self.send_message(
                f"⚠️ <b>Gagal mengirim ke IRC:</b> Koneksi IRC sedang terputus.\n"
                f"Pesan ke <code>{session.target}</code> tidak terkirim. Gunakan /cancel untuk membatalkan sesi.",
                parse_mode="HTML",
            )
            return

        # Send to IRC
        sent = False
        if self.irc_send:
            sent = self.irc_send(session.target, clean)

        if sent:
            # Log as OUT
            if session.target_type == "channel":
                self.logger.log_channel_message(session.target, self.config.irc.nick, clean)
            else:
                self.logger.log_private_outgoing(session.target, self.config.irc.nick, clean)

            # Consume session
            self.reply_manager.consume_active_session()
            self.send_message(
                f"✅ Pesan berhasil dikirim ke <code>{session.target}</code> di IRC.",
                parse_mode="HTML",
            )
        else:
            self.send_message(
                f"⚠️ Gagal mengirim pesan ke <code>{session.target}</code> melalui IRC.",
                parse_mode="HTML",
            )

    # ---- Long Polling Thread Loop ----
    def _polling_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = self._api_request(
                    "getUpdates",
                    {"offset": self._last_update_id + 1, "timeout": 15},
                    timeout=25,
                )
                if not updates:
                    time.sleep(1)
                    continue

                for update in updates:
                    update_id = update.get("update_id", 0)

                    # 1. Message event
                    if "message" in update:
                        msg = update["message"]
                        from_user = msg.get("from", {})
                        uid = from_user.get("id", 0)
                        text = msg.get("text", "")
                        if text:
                            self.handle_user_text_message(uid, text)

                    # 2. Callback query event
                    elif "callback_query" in update:
                        self.handle_callback_query(update["callback_query"])

                    # Advance the offset only AFTER the update has been fully
                    # processed (bug #13 fix). Incrementing it beforehand meant
                    # a handler exception lost the owner's message forever:
                    # the offset moved past an update that was never handled.
                    if update_id > self._last_update_id:
                        self._last_update_id = update_id

            except Exception as e:
                # Isolates Telegram errors from crashing service
                time.sleep(3)
