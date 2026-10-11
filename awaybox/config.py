"""
AwayBox Configuration Module

Handles loading, validating, and managing permanent configuration
from config.toml and secrets from secrets.env / environment variables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(Exception):
    """Raised when configuration is invalid or missing required values."""
    pass


def parse_simple_toml(content: str) -> Dict[str, Any]:
    """
    Lightweight fallback TOML parser for standard AwayBox config files.
    Works across Python 3.10+ without external dependencies.
    """
    result: Dict[str, Any] = {}
    current_section: Optional[str] = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Section header e.g. [irc]
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            if current_section not in result:
                result[current_section] = {}
            continue

        # Key-value pair
        if "=" in line:
            parts = line.split("=", 1)
            key = parts[0].strip()
            raw_val = parts[1].strip()

            # Inline comments strip (unless inside quotes)
            if not (raw_val.startswith('"') and raw_val.endswith('"')) and "#" in raw_val:
                raw_val = raw_val.split("#", 1)[0].strip()

            val: Any = raw_val
            # Parse booleans
            if raw_val.lower() == "true":
                val = True
            elif raw_val.lower() == "false":
                val = False
            # Parse integers
            elif raw_val.isdigit() or (raw_val.startswith("-") and raw_val[1:].isdigit()):
                val = int(raw_val)
            # Parse strings
            elif raw_val.startswith('"') and raw_val.endswith('"'):
                val = raw_val[1:-1].encode().decode("unicode-escape")
            elif raw_val.startswith("'") and raw_val.endswith("'"):
                val = raw_val[1:-1]
            # Parse lists of strings e.g. ["#indonesia", "#linux"]
            elif raw_val.startswith("[") and raw_val.endswith("]"):
                inner = raw_val[1:-1].strip()
                if not inner:
                    val = []
                else:
                    items = []
                    for item in inner.split(","):
                        item = item.strip()
                        if (item.startswith('"') and item.endswith('"')) or (item.startswith("'") and item.endswith("'")):
                            items.append(item[1:-1])
                        elif item:
                            items.append(item)
                    val = items

            if current_section:
                result[current_section][key] = val
            else:
                result[key] = val

    return result


def load_toml(path: Path) -> Dict[str, Any]:
    """Loads a TOML file using tomllib, tomli, or simple parser fallback."""
    try:
        import tomllib  # Python 3.11+
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        try:
            import tomli  # External package if installed
            with open(path, "rb") as f:
                return tomli.load(f)
        except ImportError:
            with open(path, "r", encoding="utf-8") as f:
                return parse_simple_toml(f.read())


def load_env_file(path: Path) -> Dict[str, str]:
    """Reads a simple key=value secrets.env file."""
    secrets: Dict[str, str] = {}
    if not path.is_file():
        return secrets

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                secrets[k] = v
    return secrets


@dataclass
class IRCConfig:
    server: str
    port: int = 6697
    tls: bool = True
    nick: str = "AwayBox"
    username: str = "awaybox"
    realname: str = "AwayBox Persistent Mailbox"
    default_channels: List[str] = field(default_factory=lambda: ["#awaybox"])
    nickserv_enabled: bool = False
    nickserv_command: str = "NickServ@services.dal.net :IDENTIFY {password}"
    chanserv_enabled: bool = False
    chanserv_command: str = "ChanServ@services.dal.net :OP {channel}"
    chanserv_command_template: str = "ChanServ@services.dal.net :OP {channel}"


@dataclass
class AutoReplyConfig:
    enabled: bool = True
    message: str = "I am currently away. Your message has been recorded."
    default_channels: List[str] = field(default_factory=list)
    cooldown_seconds: int = 300


@dataclass
class TelegramConfig:
    allowed_user_id: int
    reply_session_timeout_seconds: int = 120
    log_max_lines: int = 20


@dataclass
class LoggingConfig:
    log_dir: str = "/var/lib/awaybox/logs"


@dataclass
class ReconnectConfig:
    initial_delay_seconds: int = 5
    max_delay_seconds: int = 300
    backoff_multiplier: float = 2.0


@dataclass
class AppConfig:
    irc: IRCConfig
    autoreply: AutoReplyConfig
    telegram: TelegramConfig
    logging: LoggingConfig
    reconnect: ReconnectConfig
    irc_password: Optional[str] = None
    irc_chan_password: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    config_path: Optional[str] = None

    def validate(self) -> None:
        """Strict validation of configuration parameters."""
        if not self.irc.server or not self.irc.server.strip():
            raise ConfigError("IRC server must not be empty.")
        if not (1 <= self.irc.port <= 65535):
            raise ConfigError(f"Invalid IRC port: {self.irc.port}. Must be between 1 and 65535.")
        if not self.irc.nick or not re.match(r"^[A-Za-z0-9_\\\[\]\{\}\^\`\|-]+$", self.irc.nick):
            raise ConfigError(f"Invalid IRC nickname: '{self.irc.nick}'.")
        if self.telegram.allowed_user_id <= 0:
            raise ConfigError(f"Invalid Telegram allowed_user_id: {self.telegram.allowed_user_id}. Must be a positive integer.")
        if self.autoreply.cooldown_seconds < 0:
            raise ConfigError("Auto-reply cooldown cannot be negative.")
        if self.telegram.reply_session_timeout_seconds <= 0:
            raise ConfigError("Telegram reply session timeout must be greater than 0.")
        if self.reconnect.initial_delay_seconds <= 0 or self.reconnect.max_delay_seconds < self.reconnect.initial_delay_seconds:
            raise ConfigError("Invalid reconnect delay configuration.")


def load_config(
    config_file: Optional[str | Path] = None,
    secrets_file: Optional[str | Path] = None,
) -> AppConfig:
    """
    Loads configuration from TOML file and secrets from env / secrets.env file.
    Default paths: /etc/awaybox/config.toml and /etc/awaybox/secrets.env.
    """
    # 1. Resolve paths
    cfg_path = Path(config_file) if config_file else Path(os.environ.get("AWAYBOX_CONFIG", "/etc/awaybox/config.toml"))
    sec_path = Path(secrets_file) if secrets_file else Path(os.environ.get("AWAYBOX_SECRETS", "/etc/awaybox/secrets.env"))

    # Local fallback for development/testing if /etc/awaybox does not exist
    if not cfg_path.is_file():
        local_cfg = Path("config.toml")
        local_example = Path("config.example.toml")
        if local_cfg.is_file():
            cfg_path = local_cfg
        elif local_example.is_file():
            cfg_path = local_example

    raw_cfg: Dict[str, Any] = {}
    if cfg_path.is_file():
        raw_cfg = load_toml(cfg_path)
    elif config_file is not None:
        raise ConfigError(f"Specified configuration file not found: {config_file}")

    # 2. Read secrets
    file_secrets = load_env_file(sec_path)
    irc_password = os.environ.get("IRC_PASSWORD") or file_secrets.get("IRC_PASSWORD")
    irc_chan_password = os.environ.get("IRC_CHAN_PASSWORD") or file_secrets.get("IRC_CHAN_PASSWORD")
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or file_secrets.get("TELEGRAM_BOT_TOKEN")

    # Telegram allowed user ID from env/secrets can override or fallback
    env_user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID") or file_secrets.get("TELEGRAM_ALLOWED_USER_ID")

    # 3. Parse IRC Section
    irc_sec = raw_cfg.get("irc", {})
    irc_cfg = IRCConfig(
        server=irc_sec.get("server", "irc.libera.chat"),
        port=int(irc_sec.get("port", 6697)),
        tls=bool(irc_sec.get("tls", True)),
        nick=str(irc_sec.get("nick", "AwayBox")),
        username=str(irc_sec.get("username", "awaybox")),
        realname=str(irc_sec.get("realname", "AwayBox Persistent Mailbox")),
        default_channels=list(irc_sec.get("default_channels", ["#awaybox"])),
        nickserv_enabled=bool(irc_sec.get("nickserv_enabled", False)),
        nickserv_command=str(irc_sec.get("nickserv_command", "NickServ@services.dal.net :IDENTIFY {password}")),
        chanserv_enabled=bool(irc_sec.get("chanserv_enabled", False)),
        chanserv_command=str(irc_sec.get("chanserv_command", "ChanServ@services.dal.net :OP {channel}")),
        chanserv_command_template=str(irc_sec.get("chanserv_command_template", "ChanServ@services.dal.net :OP {channel}")),
    )

    # 4. Parse Auto-Reply Section
    auto_sec = raw_cfg.get("autoreply", {})
    auto_cfg = AutoReplyConfig(
        enabled=bool(auto_sec.get("enabled", True)),
        message=str(auto_sec.get("message", "I am currently away. Your message has been recorded.")),
        default_channels=list(auto_sec.get("default_channels", [])),
        cooldown_seconds=int(auto_sec.get("cooldown_seconds", 300)),
    )

    # 5. Parse Telegram Section
    tg_sec = raw_cfg.get("telegram", {})
    raw_uid = env_user_id or tg_sec.get("allowed_user_id", 0)
    try:
        allowed_user_id = int(raw_uid)
    except (ValueError, TypeError):
        allowed_user_id = 0

    tg_cfg = TelegramConfig(
        allowed_user_id=allowed_user_id,
        reply_session_timeout_seconds=int(tg_sec.get("reply_session_timeout_seconds", 120)),
        log_max_lines=int(tg_sec.get("log_max_lines", 20)),
    )

    # 6. Parse Logging Section
    log_sec = raw_cfg.get("logging", {})
    log_cfg = LoggingConfig(
        log_dir=str(log_sec.get("log_dir", "/var/lib/awaybox/logs")),
    )

    # 7. Parse Reconnect Section
    rec_sec = raw_cfg.get("reconnect", {})
    rec_cfg = ReconnectConfig(
        initial_delay_seconds=int(rec_sec.get("initial_delay_seconds", 5)),
        max_delay_seconds=int(rec_sec.get("max_delay_seconds", 300)),
        backoff_multiplier=float(rec_sec.get("backoff_multiplier", 2.0)),
    )

    app_config = AppConfig(
        irc=irc_cfg,
        autoreply=auto_cfg,
        telegram=tg_cfg,
        logging=log_cfg,
        reconnect=rec_cfg,
        irc_password=irc_password,
        irc_chan_password=irc_chan_password,
        telegram_bot_token=telegram_token,
        config_path=str(cfg_path) if cfg_path.is_file() else None,
    )

    app_config.validate()
    return app_config
