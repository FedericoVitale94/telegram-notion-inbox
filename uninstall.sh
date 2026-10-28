#!/bin/sh
# Removes the service (LaunchAgent on macOS, systemd on Linux). Code, instance folder, .env, cache and logs stay.
# Usage: ./uninstall.sh [label]   (default com.telegram-notion-inbox; see the label install.sh printed)
LABEL="${1:-com.telegram-notion-inbox}"
if [ "$(uname)" = Linux ]; then
    sudo systemctl disable --now "$LABEL" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$LABEL.service"
    sudo systemctl daemon-reload
else
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
fi
echo "Uninstalled $LABEL."
