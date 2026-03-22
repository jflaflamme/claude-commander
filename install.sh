#!/usr/bin/env bash
set -euo pipefail

# Claude Commander installer
# Usage: curl -fsSL https://raw.githubusercontent.com/jflaflamme/claude-commander/main/install.sh | bash

REPO="jflaflamme/claude-commander"
INSTALL_DIR="${CLAUDE_COMMANDER_DIR:-$HOME/claude-commander}"

echo "Claude Commander installer"
echo "========================="
echo ""

# Check dependencies
for cmd in python3 git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Error: $cmd is required but not found."
        exit 1
    fi
done

# Check Python version
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "Error: Python 3.11+ required (found $PY_VERSION)"
    exit 1
fi

# Install uv if missing
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Clone or update
if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing installation at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    echo "Cloning to $INSTALL_DIR..."
    git clone "https://github.com/$REPO.git" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Install dependencies
echo "Installing dependencies..."
uv sync

# Setup .env if missing
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from template. You need to fill in:"
    echo "  $INSTALL_DIR/.env"
    echo ""
    echo "  TELEGRAM_BOT_TOKEN  — get from @BotFather on Telegram"
    echo "  ADMIN_CHAT_ID       — your Telegram user ID"
    echo "  ANTHROPIC_API_KEY   — from console.anthropic.com"
    echo ""
fi

echo ""
echo "Installation complete!"
echo ""

# --- systemd user service setup ---
if command -v systemctl &>/dev/null; then
    SERVICE_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SERVICE_DIR/claude-commander.service"
    mkdir -p "$SERVICE_DIR"

    sed "s|{INSTALL_DIR}|$INSTALL_DIR|g" \
        "$INSTALL_DIR/claude-commander.service" \
        > "$SERVICE_FILE"
    chmod 644 "$SERVICE_FILE"

    systemctl --user daemon-reload
    systemctl --user enable claude-commander.service

    echo "systemd service installed and enabled."
    echo ""
    echo "Edit your credentials, then start:"
    echo "  nano $INSTALL_DIR/.env"
    echo "  systemctl --user start claude-commander.service"
    echo ""
    echo "To auto-start at boot without login (run once, needs sudo):"
    echo "  sudo loginctl enable-linger $USER"
    echo ""
    echo "To update later, just send /update to your bot."
    echo ""
else
    echo "To start:"
    echo "  cd $INSTALL_DIR"
    echo "  # Edit .env with your credentials"
    echo "  uv run python bot.py"
    echo ""
fi

echo "Or with Docker:"
echo "  cd $INSTALL_DIR"
echo "  docker compose up --build -d"
echo ""
