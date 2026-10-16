"""
Tests for the /whois Telegram command and WHOIS reply buffering.

Regression focus (previously-reported bug):
    bot.handle_command("/whois bad nick here") must be REJECTED with a clear
    error message and must NOT register any pending WHOIS buffer in state.
"""

from __future__ import annotations

import pytest

from awaybox.config import (
    AppConfig,
    AutoReplyConfig,
    IRCConfig,
    LoggingConfig,
    ReconnectConfig,
    TelegramConfig,
)
from awaybox.logger import AwayBoxLogger
from awaybox.reply_manager import ReplyManager
from awaybox.state import RuntimeState
from awaybox.telegram_bot import TelegramBot, _NICK_RE, is_valid_nick


def make_config(log_dir: str) -> AppConfig:
    return AppConfig(
        irc=IRCConfig(server="irc.example.net"),
        autoreply=AutoReplyConfig(),
        telegram=TelegramConfig(allowed_user_id=12345),
        logging=LoggingConfig(log_dir=log_dir),
        reconnect=ReconnectConfig(),
    )


@pytest.fixture()
def bot(tmp_path) -> TelegramBot:
    config = make_config(str(tmp_path / "logs"))
    logger = AwayBoxLogger(tmp_path / "logs")
    state = RuntimeState(config)
    state.irc_connected = True

    whois_calls: list[str] = []

    def fake_whois(nick: str) -> bool:
        whois_calls.append(nick)
        return True

    tb = TelegramBot(
        config=config,
        state=state,
        logger=logger,
        reply_manager=ReplyManager(),
        irc_send_func=lambda t, m: True,
        irc_whois_func=fake_whois,
    )
    tb._awaybox_test_whois_calls = whois_calls
    return tb


# ---- Regex sanity: space is NOT in the character class ----

def test_nick_regex_rejects_spaces():
    assert _NICK_RE.fullmatch("bad nick here") is None
    assert not is_valid_nick("bad nick here")


def test_nick_regex_accepts_valid_nicks():
    for nick in ["Alice", "bob_99", "user`name", "^op|~guest", "a"]:
        assert is_valid_nick(nick), nick


def test_nick_regex_rejects_edge_cases():
    for nick in ["", " ", "nick name", "#channel", "a" * 31, "nick!user@host", "nick,name"]:
        assert not is_valid_nick(nick), nick


# ---- Core regression: invalid nick must be rejected without side effects ----

def test_whois_bad_nick_is_rejected_and_registers_nothing(bot: TelegramBot):
    reply = bot.handle_command("/whois bad nick here")

    assert reply != ""
    assert "Nickname tidak valid" in reply
    # No pending buffer may be created for any fragment of the bad input.
    assert bot.state.pending_whois == {}
    # No WHOIS request may hit the IRC socket.
    assert bot._awaybox_test_whois_calls == []


def test_whois_no_args_shows_usage(bot: TelegramBot):
    reply = bot.handle_command("/whois")
    assert "Gunakan: /whois Nick" in reply
    assert bot.state.pending_whois == {}


# ---- Happy path: valid nick registers exactly one pending buffer ----

def test_whois_valid_nick_registers_pending(bot: TelegramBot):
    reply = bot.handle_command("/whois Alice")

    assert "WHOIS" in reply and "Alice" in reply
    assert bot.state.has_pending_whois("alice")
    assert list(bot.state.pending_whois.keys()) == ["alice"]
    assert bot._awaybox_test_whois_calls == ["Alice"]


def test_whois_duplicate_request_not_spammed(bot: TelegramBot):
    bot.handle_command("/whois Alice")
    reply = bot.handle_command("/whois Alice")
    assert "masih diproses" in reply
    assert bot._awaybox_test_whois_calls == ["Alice"]


def test_whois_disconnected_returns_error(bot: TelegramBot):
    bot.state.irc_connected = False
    reply = bot.handle_command("/whois Alice")
    assert "terputus" in reply
    assert bot.state.pending_whois == {}


# ---- Reply accumulation & delivery ----

def test_whois_reply_buffer_and_final_delivery(bot: TelegramBot, monkeypatch):
    bot.handle_command("/whois Alice")

    sent: list[str] = []
    monkeypatch.setattr(bot, "send_message", lambda text, **kw: sent.append(text) or True)

    bot.handle_whois_reply("Alice", "realname: Alice A.", final=False)
    bot.handle_whois_reply("Alice", "server: irc.example.net", final=False)
    assert sent == []  # not delivered yet

    bot.handle_whois_reply("Alice", "end of WHOIS", final=True)
    assert len(sent) == 1
    assert "WHOIS Alice" in sent[0]
    assert "realname: Alice A." in sent[0]
    # Buffer consumed after final delivery.
    assert not bot.state.has_pending_whois("alice")


def test_whois_second_command_reports_still_processing(bot: TelegramBot):
    bot.handle_command("/whois Alice")
    bot.handle_whois_reply("alice", "realname: Alice A.", final=False)
    # Not final yet -> second /whois reports still processing.
    assert "masih diproses" in bot.handle_command("/whois Alice")

    bot.handle_whois_reply("alice", "end of WHOIS", final=True)
    # After auto-delivery the buffer is gone; a fresh request starts cleanly.
    reply = bot.handle_command("/whois Alice")
    assert "diminta" in reply
    assert bot.state.has_pending_whois("alice")


def test_whois_unsolicited_reply_ignored(bot: TelegramBot, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(bot, "send_message", lambda text, **kw: sent.append(text) or True)
    # No pending request for this nick -> line must be dropped silently.
    bot.handle_whois_reply("Ghost", "stray reply", final=True)
    assert sent == []
