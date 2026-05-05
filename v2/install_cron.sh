#!/usr/bin/env zsh
# Installs (idempotent) a cron entry that runs the v2 sync every 10 minutes.
# Tag is used to find + replace the entry on re-runs.
set -e

DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance/v2"
TAG="# ift-finance-v2-sync"
ENTRY="*/10 * * * * \"$DIR/run_sync.sh\"  $TAG"

chmod +x "$DIR/run_sync.sh"

# Pull current crontab (no error if empty), strip any prior entry, append new.
( crontab -l 2>/dev/null | grep -v -F "$TAG" ; echo "$ENTRY" ) | crontab -
echo "Installed cron:"
crontab -l | grep -F "$TAG"
echo
echo "Reminder: cron needs Full Disk Access to read iCloud — see HANDOFF.md"
