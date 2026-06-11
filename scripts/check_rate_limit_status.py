"""Report the address's Hyperliquid request budget and fill activity.

Reads HL_ACCOUNT_ADDRESS (and HL_NETWORK) from the environment or
config/live.env and queries the public info API — no private key needed.

Usage on the VPS:
    python scripts/check_rate_limit_status.py
    python scripts/check_rate_limit_status.py --address 0x... --days 14
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


def _api_base() -> str:
    network = os.getenv("HL_NETWORK", "mainnet").lower()
    if network == "testnet":
        return "https://api.hyperliquid-testnet.xyz"
    return "https://api.hyperliquid.xyz"


def _info(payload: dict) -> object:
    resp = requests.post(f"{_api_base()}/info", json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="", help="Account address (defaults to HL_ACCOUNT_ADDRESS)")
    parser.add_argument("--days", type=int, default=14, help="How many days of fills to summarize")
    args = parser.parse_args()

    for env_path in (Path("config/live.env"), Path(".env")):
        if env_path.exists():
            load_dotenv(env_path, override=False)
    address = args.address or os.getenv("HL_ACCOUNT_ADDRESS", "")
    if not address:
        raise SystemExit("No address: pass --address or set HL_ACCOUNT_ADDRESS")

    rate = _info({"type": "userRateLimit", "user": address})
    cum_volume = float(rate.get("cumVlm", 0.0) or 0.0)
    used = float(rate.get("nRequestsUsed", 0.0) or 0.0)
    cap = float(rate.get("nRequestsCap", 0.0) or 0.0)
    deficit = max(used - cap, 0.0)
    print("=== Address request budget (userRateLimit) ===")
    print(f"cumulative_volume_usd: {cum_volume:,.2f}")
    print(f"requests_used:         {used:,.0f}")
    print(f"requests_cap:          {cap:,.0f}  (10,000 + 1 per traded USDC)")
    if deficit > 0:
        print(f"DEFICIT:               {deficit:,.0f} requests -> needs ${deficit:,.0f} more traded volume")
        print("status:                RATE LIMITED (~1 action / 10s trickle)")
    else:
        print(f"headroom:              {cap - used:,.0f} requests")
        print("status:                OK")
    if cum_volume > 0:
        print(f"requests_per_usdc:     {used / cum_volume:.2f}  (sustainable: <= 1.00)")

    fills = _info({"type": "userFills", "user": address})
    if not isinstance(fills, list):
        fills = []
    now = datetime.now(timezone.utc)
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0, "volume": 0.0, "fees": 0.0, "maker": 0.0})
    total_volume = 0.0
    for fill in fills:
        ts = datetime.fromtimestamp(float(fill.get("time", 0)) / 1000.0, tz=timezone.utc)
        if (now - ts).days > args.days:
            continue
        day = ts.strftime("%Y-%m-%d")
        notional = float(fill.get("px", 0.0)) * float(fill.get("sz", 0.0))
        by_day[day]["count"] += 1
        by_day[day]["volume"] += notional
        by_day[day]["fees"] += float(fill.get("fee", 0.0) or 0.0)
        if not bool(fill.get("crossed", False)):
            by_day[day]["maker"] += 1
        total_volume += notional

    print(f"\n=== Fills, last {args.days} days (API returns the most recent 2000) ===")
    print(f"{'day':<12}{'fills':>7}{'volume_usd':>14}{'maker_pct':>11}{'fees_usd':>10}")
    for day in sorted(by_day):
        d = by_day[day]
        maker_pct = 100.0 * d["maker"] / d["count"] if d["count"] else 0.0
        print(f"{day:<12}{d['count']:>7.0f}{d['volume']:>14,.2f}{maker_pct:>10.0f}%{d['fees']:>10.4f}")
    print(f"{'total':<12}{sum(d['count'] for d in by_day.values()):>7.0f}{total_volume:>14,.2f}")
    if fills:
        last_ts = datetime.fromtimestamp(float(fills[0].get("time", 0)) / 1000.0, tz=timezone.utc)
        print(f"\nlast_fill: {last_ts.isoformat()}  ({(now - last_ts).total_seconds() / 3600.0:.1f}h ago)")
    else:
        print("\nlast_fill: none returned")
    if deficit > 0 and by_day:
        daily_volume = total_volume / max(len(by_day), 1)
        if daily_volume > 0:
            print(f"deficit_repayment_eta: ~{deficit / daily_volume:.1f} days at the recent ${daily_volume:,.0f}/day fill volume")


if __name__ == "__main__":
    main()
