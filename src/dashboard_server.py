from __future__ import annotations

import argparse
import csv
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATUS_PATHS = [Path('data/status.json'), Path('state/status.json')]
TRADES_CSV = Path('logs/trades.csv')
TRADES_JSONL = Path('logs/trades.jsonl')


def read_status() -> dict:
    for status_path in STATUS_PATHS:
        if status_path.exists():
            return json.loads(status_path.read_text(encoding='utf-8'))
    return {}


def read_trades(limit: int = 30) -> list[dict]:
    rows: list[dict] = []
    if TRADES_CSV.exists():
        with TRADES_CSV.open(encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    elif TRADES_JSONL.exists():
        with TRADES_JSONL.open(encoding='utf-8') as f:
            rows = [json.loads(line) for line in f if line.strip()]
    return rows[-limit:][::-1]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = read_status()
        trades = read_trades()
        html = f"""<html><head><meta http-equiv='refresh' content='5'><title>Bot Dashboard</title>
<style>body{{font-family:Arial;padding:20px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:6px;font-size:12px}}</style></head><body>
<h2>Hyperliquid Adaptive Grid Bot</h2>
<ul>
<li>equity: {status.get('equity')}</li><li>realized_pnl: {status.get('realized_pnl')}</li><li>unrealized_pnl: {status.get('unrealized_pnl')}</li><li>fees_paid: {status.get('fees_paid')}</li>
<li>cash: {status.get('cash')}</li><li>position_size: {status.get('position_size')}</li><li>position_notional: {status.get('position_notional')}</li>
<li>symbol: {status.get('symbol')}</li><li>price: {status.get('price')}</li><li>regime: {status.get('regime')}</li><li>mode: {status.get('mode')}</li>
<li>risk_state: {status.get('risk_state')}</li><li>pause_reason: {status.get('pause_reason') or 'none'}</li><li>updated: {status.get('timestamp')}</li>
</ul>
<h3>Last Trades</h3><table><tr>{''.join(f'<th>{k}</th>' for k in (trades[0].keys() if trades else []))}</tr>
{''.join('<tr>' + ''.join(f'<td>{v}</td>' for v in t.values()) + '</tr>' for t in trades)}
</table></body></html>"""
        b = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    server = HTTPServer((args.host, args.port), Handler)
    print(f'Dashboard serving at http://{args.host}:{args.port}')
    server.serve_forever()


if __name__ == '__main__':
    main()
