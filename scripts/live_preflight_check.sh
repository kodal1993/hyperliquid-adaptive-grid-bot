#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "LIVE PREFLIGHT FAIL: $1" >&2
  exit 1
}

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

ENV_PROFILE="${ENV_PROFILE:-live}"
PROFILE_FILE="config/${ENV_PROFILE}.env"
if [[ -f "$PROFILE_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROFILE_FILE"
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    fail "python interpreter not found"
  fi
fi

[[ "${ENV_PROFILE:-}" == "live" ]] || fail "ENV_PROFILE must be live (current: ${ENV_PROFILE:-unset})"
[[ "${HL_NETWORK:-}" == "mainnet" ]] || fail "HL_NETWORK must be mainnet (current: ${HL_NETWORK:-unset})"
[[ "${PAPER_MODE:-}" == "false" ]] || fail "PAPER_MODE must be false (current: ${PAPER_MODE:-unset})"
[[ "${ENABLE_LIVE_TRADING:-}" == "true" ]] || fail "ENABLE_LIVE_TRADING must be true (current: ${ENABLE_LIVE_TRADING:-unset})"

LIVE_EXECUTION_ENABLED_VALUE="${LIVE_EXECUTION_ENABLED:-false}"
echo "LIVE_EXECUTION_ENABLED=${LIVE_EXECUTION_ENABLED_VALUE}"
[[ "$LIVE_EXECUTION_ENABLED_VALUE" == "true" ]] || fail "LIVE_EXECUTION_ENABLED must be true for micro-live"

STOP_FILE="${EMERGENCY_STOP_FILE:-state/STOP_LIVE}"
[[ ! -e "$STOP_FILE" ]] || fail "STOP_LIVE/emergency stop file exists at ${STOP_FILE}"

[[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || fail "TELEGRAM_BOT_TOKEN is missing"
[[ -n "${TELEGRAM_CHAT_ID:-}" ]] || fail "TELEGRAM_CHAT_ID is missing"
echo "telegram_credentials_present=true"
echo "python_bin=${PYTHON_BIN}"

"$PYTHON_BIN" - <<'PY'
import os
import sys
from src.hyperliquid_client import HyperliquidClient

symbol = os.getenv("HL_DEFAULT_SYMBOL", "BTC")
client = HyperliquidClient(
    private_key=os.getenv("HL_PRIVATE_KEY", ""),
    account_address=os.getenv("HL_ACCOUNT_ADDRESS", ""),
    network=os.getenv("HL_NETWORK", "mainnet"),
)
client.connect()
summary = client.get_account_summary()
open_orders = client.get_open_orders(symbol)
account_value = float(summary.get("accountValue", 0.0))
withdrawable = float(summary.get("withdrawable", 0.0))
print(f"accountValue={account_value}")
print(f"withdrawable={withdrawable}")
print(f"open_orders_count={len(open_orders)}")
print(f"assetPositions_count={summary.get('assetPositions_count', 0)}")
if account_value <= 0:
    print("LIVE PREFLIGHT FAIL: accountValue must be > 0", file=sys.stderr)
    sys.exit(1)
if withdrawable <= 0:
    print("LIVE PREFLIGHT FAIL: withdrawable must be > 0", file=sys.stderr)
    sys.exit(1)
PY

echo "LIVE PREFLIGHT OK"
