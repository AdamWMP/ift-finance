#!/usr/bin/env zsh
# Cron wrapper for the v2 sync. Sources zshrc so OP_API_KEY / IFT_SALES_BOARD_*
# env vars are visible to the cron environment, then runs the sync end-to-end.
set -e

source "$HOME/.zshrc" 2>/dev/null || true
DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Image Fitness Training /Finance/v2"
LOG="$DIR/sync.log"

cd "$DIR"
echo "--- $(date '+%Y-%m-%d %H:%M:%S') sync start ---" >> "$LOG"
"$HOME/.venvs/ift-finance/bin/python3" -m app.sync >> "$LOG" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') sync end ---"   >> "$LOG"
