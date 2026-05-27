#!/usr/bin/env bash
set -euo pipefail

PAPER_DIR="/root/hyperliquid-adaptive-grid-bot-paper"
LIVE_DIR="/root/hyperliquid-adaptive-grid-bot-live"
REPO_URL="https://github.com/kodal1993/hyperliquid-adaptive-grid-bot.git"
SYSTEMD_DIR="/etc/systemd/system"
INSTALL_SYSTEMD="${1:-}"

setup_repo() {
  local dir="$1"

  if [ ! -d "$dir/.git" ]; then
    mkdir -p "$dir"
    git clone "$REPO_URL" "$dir"
  else
    git -C "$dir" fetch --all --prune
    git -C "$dir" pull --ff-only
  fi

  mkdir -p "$dir/state" "$dir/logs" "$dir/data"

  if [ ! -d "$dir/.venv" ]; then
    python3 -m venv "$dir/.venv"
  fi

  "$dir/.venv/bin/pip" install --upgrade pip
  "$dir/.venv/bin/pip" install -r "$dir/requirements.txt"
}

setup_repo "$PAPER_DIR"
setup_repo "$LIVE_DIR"

cp -f "$LIVE_DIR/config/live.env.example" "$LIVE_DIR/config/live.env.example"
if [ -f "$LIVE_DIR/config/live.env" ]; then
  echo "Existing $LIVE_DIR/config/live.env detected; leaving untouched."
else
  echo "No live config created automatically. Use config/live.env.example as template."
fi

if [ "$INSTALL_SYSTEMD" = "--install-systemd" ]; then
  cp -f "$LIVE_DIR/deploy/systemd/hyperliquid-grid-bot-paper.service" "$SYSTEMD_DIR/hyperliquid-grid-bot-paper.service"
  cp -f "$LIVE_DIR/deploy/systemd/hyperliquid-grid-bot-live.service" "$SYSTEMD_DIR/hyperliquid-grid-bot-live.service"
  systemctl daemon-reload
  systemctl enable hyperliquid-grid-bot-paper.service
  echo "Paper service enabled."
  echo "Live service installed but not enabled automatically."
fi

echo "Live config is prepared but live trading remains disabled. Do not set ENABLE_LIVE_TRADING=true until manual approval."
