#!/usr/bin/env bash
# ==============================================================================
# AwayBox Installer & Setup Script
# Idempotent: Can be run multiple times safely without overwriting configs/secrets
# ==============================================================================

set -euo pipefail

APP_DIR="/opt/awaybox"
CONF_DIR="/etc/awaybox"
LOG_DIR="/var/lib/awaybox/logs"
SERVICE_NAME="awaybox"
SERVICE_USER="awaybox"
SERVICE_FILE="/etc/systemd/system/awaybox.service"

# Visual helpers
COLOR_GREEN='\033[0;32m'
COLOR_BLUE='\033[0;34m'
COLOR_YELLOW='\033[1;33m'
COLOR_RED='\033[0;31m'
COLOR_NC='\033[0m'

log_info() {
    echo -e "${COLOR_BLUE}[INFO]${COLOR_NC} $1"
}

log_success() {
    echo -e "${COLOR_GREEN}[SUCCESS]${COLOR_NC} $1"
}

log_warn() {
    echo -e "${COLOR_YELLOW}[WARN]${COLOR_NC} $1"
}

log_error() {
    echo -e "${COLOR_RED}[ERROR]${COLOR_NC} $1"
}

# 1. Root privilege check
if [ "$EUID" -ne 0 ]; then
    log_error "Setup script must be run as root (or via sudo)."
    exit 1
fi

log_info "Starting AwayBox installation / update..."

# 2. Check Python 3.10+
if ! command -v python3 &>/dev/null; then
    log_error "Python 3 is required but not installed."
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log_info "Detected Python $PY_VER"

# 3. Create service user if not present
if id "$SERVICE_USER" &>/dev/null; then
    log_info "Service user '$SERVICE_USER' already exists."
else
    log_info "Creating non-root system user '$SERVICE_USER'..."
    if command -v useradd &>/dev/null; then
        useradd --system --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
    else
        adduser --system --group --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    log_success "User '$SERVICE_USER' created."
fi

# 4. Create directory structure
log_info "Ensuring directory layout..."
mkdir -p "$APP_DIR"
mkdir -p "$CONF_DIR"
mkdir -p "$LOG_DIR/channels"
mkdir -p "$LOG_DIR/private"

# 5. Copy application source
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log_info "Installing AwayBox code to $APP_DIR..."
cp -r "$SCRIPT_DIR/awaybox" "$APP_DIR/"
cp "$SCRIPT_DIR/pyproject.toml" "$APP_DIR/"
if [ -f "$SCRIPT_DIR/README.md" ]; then
    cp "$SCRIPT_DIR/README.md" "$APP_DIR/"
fi

# 6. Set up Python virtual environment
VENV_DIR="$APP_DIR/venv"
if [ ! -d "$VENV_DIR" ]; then
    log_info "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

log_info "Installing/updating Python dependencies in venv..."
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel --quiet 2>/dev/null || true
"$VENV_DIR/bin/pip" install -e "$APP_DIR" --quiet 2>/dev/null || "$VENV_DIR/bin/pip" install "$APP_DIR" --quiet

# 7. Initialize configuration (IDEMPOTENT: Never overwrite existing config)
CONFIG_TARGET="$CONF_DIR/config.toml"
if [ -f "$CONFIG_TARGET" ]; then
    log_info "Existing configuration preserved at $CONFIG_TARGET"
else
    log_info "Deploying initial configuration template to $CONFIG_TARGET..."
    if [ -f "$SCRIPT_DIR/config.example.toml" ]; then
        cp "$SCRIPT_DIR/config.example.toml" "$CONFIG_TARGET"
    else
        log_warn "config.example.toml not found, creating blank config."
        touch "$CONFIG_TARGET"
    fi
    log_success "Created $CONFIG_TARGET"
fi

# 8. Initialize secrets (IDEMPOTENT: Never overwrite existing secrets)
SECRETS_TARGET="$CONF_DIR/secrets.env"
if [ -f "$SECRETS_TARGET" ]; then
    log_info "Existing secrets file preserved at $SECRETS_TARGET"
else
    log_info "Creating initial secrets template at $SECRETS_TARGET..."
    cat << 'EOF' > "$SECRETS_TARGET"
# AwayBox Secrets
# WARNING: Keep permissions restricted (chmod 600)
IRC_PASSWORD="change_me_to_your_irc_password"
TELEGRAM_BOT_TOKEN="change_me_to_your_bot_token"
TELEGRAM_ALLOWED_USER_ID=123456789
EOF
    log_success "Created secrets template at $SECRETS_TARGET"
fi

# 9. Install systemd service unit
log_info "Configuring systemd service..."
if [ -f "$SCRIPT_DIR/systemd/awaybox.service" ]; then
    cp "$SCRIPT_DIR/systemd/awaybox.service" "$SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    log_success "Systemd service installed and enabled."
else
    log_warn "awaybox.service unit file not found in systemd/ directory."
fi

# 10. Set strict ownership and permissions
log_info "Applying security permissions..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONF_DIR"

chmod 750 "$APP_DIR"
chmod 750 "$CONF_DIR"
chmod 750 "$LOG_DIR"
chmod 640 "$CONFIG_TARGET"
chmod 600 "$SECRETS_TARGET"

log_success "=========================================================="
log_success "AwayBox installation completed successfully!"
log_success "=========================================================="
echo ""
echo "Next Steps:"
echo "1. Edit your secrets in: $SECRETS_TARGET"
echo "   sudo nano $SECRETS_TARGET"
echo ""
echo "2. Review and adjust your settings in: $CONFIG_TARGET"
echo "   sudo nano $CONFIG_TARGET"
echo ""
echo "3. Start and check AwayBox service:"
echo "   sudo systemctl start $SERVICE_NAME"
echo "   sudo systemctl status $SERVICE_NAME"
echo ""
echo "4. Follow real-time logs:"
echo "   journalctl -u $SERVICE_NAME -f"
echo "   tail -f $LOG_DIR/recorder.log"
echo ""
