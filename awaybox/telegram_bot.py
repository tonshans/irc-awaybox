"""
AwayBox Telegram Bot Module

Provides the remote control interface via Telegram Bot API using long polling:
- Strict authorization check (numeric allowed_user_id only)
- Interactive notifications with inline reply buttons
- Reply session workflow management
- Commands: /start, /status, /channels, /msg, /join, /leave, /autoreply, /ignore, /unignore, /log, /cancel
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
    ) -> None:
        self.config = config
        self.state = state
        self.logger = logger
        self.reply_manager = reply_manager
        self.irc_send = irc_send_func
        self.irc_join = irc_join_func
        self.irc_leave = irc_leave_func

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
            "text": text[:4000],  # Telegram message size safety
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
        clean_text = text[:500]
        msg = f"<b>[{channel}]</b> &lt;{sender}&gt;: {clean_text}"

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": f"Reply {channel}",
                        "callback_data": f"reply:channel:{channel}",
                    },
                    {
                        "text": f"Private {sender}",
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
        clean_text = text[:500]
        msg = f"<b>[PM]</b> &lt;{sender}&gt;: {clean_text}"

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": f"Reply {sender}",
                        "callback_data": f"reply:private:{sender}",
                    }
                ]
            ]
        }
        return self.send_message(msg, reply_markup=keyboard, parse_mode="HTML")

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
                "/log [#ch|Nick] - Ambil cuplikan riwayat pesan\n"
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
                f"• Reconnect Attempts: {self.state.reconnect_attempts}"
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
                        f"Gunakan /join {target} terlebih dahulu."
                    )
            else:
                # Validate nickname format
                if not re.match(r"^[A-Za-z0-9_\\\[\]\{\}\^\`\|-]{1,30}$", target):
                    return "Nickname tidak valid."

            if not self.state.irc_connected:
                return "⚠️ Gagal mengirim: Koneksi IRC sedang terputus."

            sent = False
            if self.irc_send:
                sent = self.irc_send(target, message)

            if not sent:
                return f"⚠️ Gagal mengirim pesan ke <code>{target}</code> melalui IRC."

            # Log as OUT message
            if is_channel:
                self.logger.log_channel_message(target, self.config.irc.nick, message)
            else:
                self.logger.log_private_outgoing(target, self.config.irc.nick, message)

            return f"✅ Pesan berhasil dikirim ke <code>{target}</code> di IRC."

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
                return f"Meminta AwayBox bergabung ke <code>{target_ch}</code>."
            return (
                f"⚠️ Gagal bergabung ke <code>{target_ch}</code>: "
                f"koneksi IRC sedang terputus atau channel sudah aktif."
            )

        elif cmd == "/leave":
            if not args:
                return "Gunakan: /leave #channel"
            target_ch = args[0]
            if not self.state.has_channel(target_ch):
                return f"AwayBox tidak berada di channel <code>{target_ch}</code>."

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
                return f"Meminta AwayBox meninggalkan <code>{resolved}</code>."
            return f"⚠️ Gagal meninggalkan <code>{resolved}</code>: koneksi IRC sedang terputus."

        elif cmd == "/autoreply":
            if not args:
                status = "Aktif" if self.state.autoreply_enabled else "Nonaktif"
                chs = ", ".join(sorted(self.state.autoreply_channels)) or "(tidak ada)"
                return (
                    f"<b>Pengaturan Auto-Reply:</b>\n"
                    f"• Status: {status}\n"
                    f"• Pesan: \"{self.config.autoreply.message}\"\n"
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
                return f"Channel <code>{ch}</code> {'ditambahkan ke' if added else 'sudah ada di'} daftar auto-reply."
            elif sub == "remove":
                if len(args) < 2:
                    return "Gunakan: /autoreply remove #channel"
                ch = args[1]
                removed = self.state.remove_autoreply_channel(ch)
                return f"Channel <code>{ch}</code> {'dihapus dari' if removed else 'tidak ditemukan di'} daftar auto-reply."
            else:
                return "Opsi /autoreply tidak dikenal. Gunakan: on, off, add, atau remove."

        elif cmd == "/ignore":
            if not args:
                ignored = sorted(self.state.ignore_list)
                if not ignored:
                    return "Daftar ignore kosong."
                return "<b>Nickname Diabaikan:</b>\n" + "\n".join(f"• {n}" for n in ignored)
            nick = args[0]
            if not re.match(r"^[A-Za-z0-9_\\\[\]\{\}\^\`\|-]+$", nick):
                return "Nickname tidak valid."
            added = self.state.add_ignore(nick)
            if added:
                return f"Nickname <code>{nick}</code> sekarang diabaikan (pesan tidak dicatat & tidak dinotifikasi)."
            return f"Nickname <code>{nick}</code> sudah ada dalam ignore list."

        elif cmd == "/unignore":
            if not args:
                return "Gunakan: /unignore Nick"
            nick = args[0]
            removed = self.state.remove_ignore(nick)
            if removed:
                return f"Nickname <code>{nick}</code> dihapus dari ignore list."
            return f"Nickname <code>{nick}</code> tidak ditemukan di ignore list."

        elif cmd == "/log":
            if not args:
                return "Gunakan: /log #channel atau /log Nick"
            target = args[0]
            lines = self.logger.read_tail(target, max_lines=self.config.telegram.log_max_lines)
            if not lines:
                return f"Tidak ada riwayat log ditemukan untuk <code>{target}</code>."
            
            header = f"<b>Cuplikan Log untuk {target} ({len(lines)} baris terakhir):</b>\n<pre>"
            content = "\n".join(lines)
            footer = "</pre>"
            # Telegram character limit check
            if len(header) + len(content) + len(footer) > 4000:
                content = content[-3500:]
            return f"{header}{content}{footer}"

        elif cmd == "/cancel":
            cancelled = self.reply_manager.cancel_session()
            if cancelled:
                return "Sesi balasan telah dibatalkan."
            return "Tidak ada sesi balasan aktif yang perlu dibatalkan."

        return "Perintah tidak dikenal. Ketik /start untuk melihat panduan."

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
                    if update_id > self._last_update_id:
                        self._last_update_id = update_id

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

            except Exception as e:
                # Isolates Telegram errors from crashing service
                time.sleep(3)
