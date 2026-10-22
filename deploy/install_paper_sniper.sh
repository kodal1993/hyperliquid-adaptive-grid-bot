#!/usr/bin/env bash
# A paper sniper izolalt telepitese (ujrafuttathato):
#   - kulon felhasznalo: paper-sniper (shell es home nelkul)
#   - kulon allapot- es munkakonyvtar: /var/lib/paper-sniper (ide kerulnek at a state/paper_sniper_* fajlok)
#   - kulon konfig: /etc/paper-sniper/paper_sniper.env (a mostani config/live.env + .env PAPER_SNIPER_* es
#     Solana/PumpPortal URL soraibol; privat kulcs es Telegram-token NEM kerul bele)
#   - systemd unit: a szolgaltatas a repobol csak a scripts/ es a .venv/ konyvtarat latja
# Futtatas a VPS-en, a repo gyokereben:  sudo bash deploy/install_paper_sniper.sh
# Visszaallitas a regi (root, repo) futtatasra:  sudo bash deploy/install_paper_sniper.sh --vissza
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_USER=paper-sniper
ETC_DIR=/etc/paper-sniper
ENV_FILE="$ETC_DIR/paper_sniper.env"
STATE=/var/lib/paper-sniper
UNIT=hyperliquid-paper-sniper.service
PY="$REPO/.venv/bin/python"

[ "$(id -u)" = 0 ] || { echo "HIBA: root kell (sudo bash $0)"; exit 1; }

if [ "${1:-}" = "--vissza" ]; then
  # visszaallitas a regi futtatasra (root, a repo state/ konyvtara, config/live.env + .env)
  systemctl stop "$UNIT" 2>/dev/null || true
  shopt -s nullglob
  for f in "$STATE"/paper_sniper_*; do
    [ -e "$REPO/state/$(basename "$f")" ] || mv "$f" "$REPO/state/"
  done
  [ -f "$ENV_FILE" ] && mv "$ENV_FILE" "$ENV_FILE.disabled.$(date +%s)"
  cat > "/etc/systemd/system/$UNIT" <<EOF
[Unit]
Description=pump.fun PAPER sniper (valodi penz nelkul)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO
ExecStart=$PY scripts/paper_sniper run
Restart=always
RestartSec=15
User=root

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "/etc/systemd/system/$UNIT"
  systemctl daemon-reload
  systemctl restart "$UNIT"
  echo "Visszaallitva: root-kent fut a repobol; a sniper konfigja ujra a config/live.env + .env"
  echo "(a $ENV_FILE.disabled.* fajlban a korabbi izolalt beallitasok megvannak)."
  exit 0
fi
[ -x "$PY" ] || { echo "HIBA: nincs venv: $PY"; exit 1; }
[ -f "$REPO/scripts/paper_sniper" ] || { echo "HIBA: nincs $REPO/scripts/paper_sniper"; exit 1; }

echo "== 1. felhasznalo: $SVC_USER"
if id -u "$SVC_USER" >/dev/null 2>&1; then
  echo "   mar letezik"
else
  useradd --system --user-group --no-create-home --home-dir "$STATE" --shell /usr/sbin/nologin "$SVC_USER"
  echo "   letrehozva"
fi

echo "== 2. probafuttatas az izolalt kornyezetben (a futo szolgaltatas meg nem valtozik)"
BINDS="$REPO/scripts $REPO/.venv"
if ! systemd-run --quiet --wait --pipe --collect -p User="$SVC_USER" -p ProtectHome=tmpfs -p BindReadOnlyPaths="$BINDS" \
     -p ProtectSystem=strict -p PrivateTmp=yes -p NoNewPrivileges=yes \
     "$PY" -I -c "import os, sys, websockets, dotenv
leak = [p for p in ('$REPO/.env', '$REPO/config/live.env') if os.path.exists(p)]
if leak:
    sys.exit('a kulcsfajlok lathatok: ' + ', '.join(leak))
print('   ok: a venv mukodik, a .env es a config/live.env nem latszik')"; then
  echo "HIBA: a probafuttatas nem sikerult, semmi nem valtozott (a felhasznalo maradhat)."
  exit 1
fi

echo "== 3. konfig: $ENV_FILE"
install -d -m 0750 -o root -g "$SVC_USER" "$ETC_DIR"
if [ -f "$ENV_FILE" ]; then
  echo "   mar letezik, nem irom felul"
else
  "$PY" -I - "$REPO" "$ENV_FILE.tmp" <<'PYEOF'
import re, sys
from pathlib import Path
from dotenv import dotenv_values

repo, out = Path(sys.argv[1]), Path(sys.argv[2])
vals = {}
for f in (repo / "config" / "live.env", repo / ".env"):      # mint eddig: a .env az erosebb
    if f.is_file():
        vals.update({k: v for k, v in dotenv_values(f).items() if v is not None})
keep = re.compile(r"^(PAPER_SNIPER_[A-Z0-9_]+|SOLANA_RPC_URL|SOLANA_WS_URL|PUMPPORTAL_WS_URL)$")
secret = ("KEY", "TOKEN", "SECRET", "PRIVATE", "PASS", "WALLET")
keys = [k for k in vals if keep.match(k) and not (k.startswith("PAPER_SNIPER_") and any(s in k for s in secret))]
lines = []
for k in keys:
    v = vals[k].replace("\n", " ").strip()
    lines.append(f'{k}="{v}"' if re.search(r"[\s#\"']", v) else f"{k}={v}")
out.write_text("# a paper sniper sajat konfigja (deploy/install_paper_sniper.sh); a Telegram /set PAPER_SNIPER_* ide ir\n"
               + "\n".join(lines) + "\n")
print("   atvett kulcsok (ertek nelkul): " + (", ".join(keys) or "-"))
PYEOF
  install -m 0640 -o root -g "$SVC_USER" "$ENV_FILE.tmp" "$ENV_FILE"
  rm -f "$ENV_FILE.tmp"
fi

echo "== 4. a szolgaltatas leallitasa es az allapot athelyezese: $STATE"
systemctl stop "$UNIT" 2>/dev/null || true
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE"
shopt -s nullglob
moved=0
for f in "$REPO"/state/paper_sniper_*; do
  dst="$STATE/$(basename "$f")"
  if [ -e "$dst" ]; then
    echo "   mar ott van, kihagyva: $(basename "$f")"
    continue
  fi
  mv "$f" "$dst"
  moved=$((moved + 1))
done
echo "   athelyezve: $moved fajl"
if [ -e "$REPO/state/STOP_SNIPER" ]; then
  echo "   FIGYELEM: $REPO/state/STOP_SNIPER letezik; az uj helye $STATE/STOP_SNIPER (a regit a sniper mar nem nezi)"
fi
chown -R "$SVC_USER:$SVC_USER" "$STATE"
chmod -R g-w,o-rwx "$STATE"

echo "== 5. systemd unit"
sed "s#/root/hyperliquid-adaptive-grid-bot-live#$REPO#g" "$REPO/deploy/systemd/$UNIT" > "/etc/systemd/system/$UNIT"
chmod 0644 "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1 || true
systemctl restart "$UNIT"
sleep 8
if systemctl is-active --quiet "$UNIT"; then
  echo "   fut: $(systemctl show "$UNIT" -p User --value) felhasznaloval"
else
  echo "HIBA: a szolgaltatas nem fut. Naplo: journalctl -u $UNIT -n 50"
fi
journalctl -u "$UNIT" -n 6 --no-pager -o cat || true
echo
echo "Kesz. Telegramon: /diag ('futas: izolalt'), /set (a sniper beallitasai mostantol $ENV_FILE)."
echo "A config/live.env PAPER_SNIPER_* sorait a sniper mar nem olvassa (torolhetok)."
