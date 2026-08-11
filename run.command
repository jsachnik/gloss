#!/bin/bash
# Start Gloss.  Double-click this file in Finder.
#
# Opening a .command from Finder runs it in a login shell, so ~/.zshrc and
# ~/.zprofile are sourced and any API key exported there comes across. Settings
# also come from the .env file in this folder, which install.command writes.

cd "$(dirname "$0")"

VENV="$HOME/.venvs/gloss"

if [ ! -x "$VENV/bin/python" ]; then
    echo "No environment found at $VENV"
    echo "Double-click install.command first."
    echo
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

echo "Starting Gloss..."
echo

# app.py reports the configured provider and any problem with it at startup, so
# there's no provider-specific check to duplicate here.  It also detects a copy
# already running and reopens that one instead of starting a second server --
# which is why the "leave this window open" notice is printed there, once it
# knows whether this window is the one actually serving.
exec "$VENV/bin/python" app.py
