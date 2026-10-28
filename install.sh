#!/bin/sh
# Installs the bot as a service that keeps listening: a LaunchAgent on macOS (starts at login),
# a systemd service on Linux, e.g. a Google Cloud VM (starts at boot; needs sudo).
# Usage: ./install.sh                         this folder is the instance (config.toml, .env here)
#        ./install.sh <instance folder> [label] e.g. ./install.sh instances/mine com.mine.inbox
# Installing again with the same label replaces the previous service.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$(cd "${1:-$ROOT}" && pwd)"
if [ -n "$2" ]; then LABEL="$2"
elif [ "$HOME_DIR" = "$ROOT" ]; then LABEL=com.telegram-notion-inbox
else LABEL="com.telegram-notion-inbox.$(basename "$HOME_DIR")"; fi
[ -x "$ROOT/.venv/bin/python" ] || { echo "Missing .venv: see README (Setup)"; exit 1; }
[ -f "$HOME_DIR/.env" ] || { echo "Missing $HOME_DIR/.env: see README (Setup)"; exit 1; }
[ -f "$HOME_DIR/config.toml" ] || { echo "Missing $HOME_DIR/config.toml: see README (Setup)"; exit 1; }
mkdir -p "$HOME_DIR/logs"
if [ "$(uname)" = Linux ]; then
    DEST="/etc/systemd/system/$LABEL.service"
    sed -e "s|__LABEL__|$LABEL|g" -e "s|__USER__|$(id -un)|g" -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME_DIR|g" \
        "$ROOT/systemd/telegram-notion-inbox.service" | sudo tee "$DEST" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable "$LABEL"
    sudo systemctl restart "$LABEL"
else
    DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__LABEL__|$LABEL|g" -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME_DIR|g" \
        "$ROOT/launchd/com.telegram-notion-inbox.plist" > "$DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$DEST"
fi
echo "Installed $LABEL: the bot is listening. Log: $HOME_DIR/logs/bot.log"
