#!/usr/bin/env python3
"""Telegram-vezérlő a papír-szolgáltatásokhoz (paper sniper + paper carry).

- Rendszeres jelentés (alap: 2 óránként): szolgáltatások állapota, a sniper és a carry
  eredménye az elmúlt ablakban és összesen, figyelmeztetésekkel.
- Riasztás, ha egy szolgáltatás leáll vagy újra elindul.
- Parancsok: lásd HELP_TEXT. Csak a whitelistelt chat ID használhatja.

A jelentés adatait a paper scriptek saját parancsai adják (subprocess), így egy
git pull után mindig a friss kódjuk fut, a vezérlő újraindítása nélkül is.

Izolált sniper (deploy/install_paper_sniper.sh): a sniper saját konfigja a /etc/paper-sniper/paper_sniper.env,
az állapota a /var/lib/paper-sniper; a /set PAPER_SNIPER_* oda ír, az export onnan olvas. A sniper
parancsai a vezérlő környezete (Telegram-token, kulcsok) nélkül futnak.

Konfig (config/live.env, .env):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_CONTROL_ALLOWED_CHAT_IDS   # vesszővel; üres = TELEGRAM_CHAT_ID
    TELEGRAM_REPORT_INTERVAL_HOURS=2    # 0 = nincs rendszeres jelentés
    TELEGRAM_SNIPER_SERVICE=hyperliquid-paper-sniper
    TELEGRAM_CARRY_SERVICE=hyperliquid-paper-carry
    TELEGRAM_CONTROL_SERVICE=hyperliquid-telegram-control
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
STATE_DIR = PROJECT_ROOT / "state"
OFFSET_FILE = STATE_DIR / "telegram_control.offset"
REPORT_STATE_FILE = STATE_DIR / "telegram_control_report.json"
POLL_TIMEOUT_SECONDS = 25
SERVICE_CHECK_SECONDS = 60
MAX_MESSAGE_LEN = 3800
MAX_EXPORT_BYTES = 45 * 1024 * 1024          # a Telegram bot API 50 MB-os feltoltesi korlatja alatt
# csak ezek a fajlok kerulhetnek az exportba (kulcsot nem tartalmaznak; a .env soha)
EXPORT_FILES = [
    "state/paper_sniper_trades.csv",
    "state/paper_sniper_state.json",
    "state/paper_carry_log.csv",
    "state/paper_carry_state.json",
    "state/paper_sniper_migrations.csv",
    "state/paper_sniper_postgrad.csv",
    "state/paper_sniper_curves.csv",
]
WALLET_LOG_REL = "state/paper_sniper_wallet_trades.csv"   # nagy lehet: csak a fejléc + az utolsó sorok
TOKEN_LOG_REL = "state/paper_sniper_tokens.csv"
WALLET_LOG_EXPORT_LINES = 200_000
SNIPER_ENV_FILE = Path("/etc/paper-sniper/paper_sniper.env")   # izolált sniper: saját konfig ...
SNIPER_STATE_DIR = Path("/var/lib/paper-sniper")               # ... és állapot-könyvtár
logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    {"command": "report", "description": "Teljes jelentés most"},
    {"command": "status", "description": "Szolgáltatások állapota"},
    {"command": "sniper", "description": "Sniper: részletes eredmény"},
    {"command": "diag", "description": "Sniper: diagnosztika"},
    {"command": "wallets", "description": "Sniper: okos tárcák viselkedése"},
    {"command": "tokens", "description": "Sniper: nyerő tokenek korai jelei"},
    {"command": "carry", "description": "Carry: állapot és összesítő"},
    {"command": "logs_sniper", "description": "Sniper: utolsó naplósorok"},
    {"command": "logs_carry", "description": "Carry: utolsó naplósorok"},
    {"command": "restart_sniper", "description": "Sniper újraindítása"},
    {"command": "restart_carry", "description": "Carry újraindítása"},
    {"command": "stop_sniper", "description": "Sniper leállítása (megerősítéssel)"},
    {"command": "stop_carry", "description": "Carry leállítása (megerősítéssel)"},
    {"command": "start_sniper", "description": "Sniper indítása"},
    {"command": "start_carry", "description": "Carry indítása"},
    {"command": "pull", "description": "Git pull + papír-szolgáltatások újraindítása"},
    {"command": "set", "description": "Papír-beállítás: /set KULCS=ÉRTÉK"},
    {"command": "unset", "description": "Papír-beállítás törlése: /unset KULCS"},
    {"command": "version", "description": "Futó kódverzió, lemaradás a GitHubhoz képest"},
    {"command": "export", "description": "Adatfájlok + diag egy zip-ben"},
    {"command": "menu", "description": "Gombos menü"},
    {"command": "help", "description": "Parancslista"},
]

HELP_TEXT = """🤖 PAPÍR-BOTOK VEZÉRLÉSE (valódi pénz nélkül)

📊 /report - teljes jelentés most (sniper + carry)
🩺 /status - szolgáltatások állapota
🎯 /sniper - sniper: stratégiánkénti részletes eredmény
🔬 /diag - sniper: diagnosztika (RPC, tárca-követés, bennfentes-klaszterek)
👛 /wallets - okos tárcák viselkedése: mi működik, szerencse vagy tudás
🏆 /tokens - nyerő tokenek korai jelei (tanító / ellenőrző rész)
💰 /carry - carry: állapot és összesítő
📜 /logs_sniper, /logs_carry - utolsó naplósorok
🔄 /restart_sniper, /restart_carry - újraindítás
⏸ /stop_sniper, /stop_carry - leállítás (megerősítéssel)
▶️ /start_sniper, /start_carry - indítás
⬇️ /pull - git pull --rebase + papír-szolgáltatások újraindítása
⚙️ /set - papír-beállítások listája; /set KULCS=ÉRTÉK beírja (sniper: a saját konfigjába, ha izolált; utána /restart_sniper)
   pl. /set PAPER_SNIPER_STRATS=S10_bennfentes,S13_amm,S14_volumen ; /unset KULCS törli
📦 /version - melyik kód fut, van-e telepítetlen PR, kell-e újraindítás
📤 /export - CSV-k, állapotfájlok és diag egy zip-ben (elemzéshez)
🧭 /menu - gombos menü

Rendszeres jelentés: {interval}. Riasztás, ha egy szolgáltatás leáll."""


# ---------------------------------------------------------------- konfig
def load_env() -> None:
    if load_dotenv:
        load_dotenv(PROJECT_ROOT / "config" / "live.env", override=False)
        load_dotenv(PROJECT_ROOT / ".env", override=True)


def services() -> dict[str, str]:
    return {
        "sniper": os.getenv("TELEGRAM_SNIPER_SERVICE", "hyperliquid-paper-sniper"),
        "carry": os.getenv("TELEGRAM_CARRY_SERVICE", "hyperliquid-paper-carry"),
    }


def control_service() -> str:
    return os.getenv("TELEGRAM_CONTROL_SERVICE", "hyperliquid-telegram-control")


def report_interval_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("TELEGRAM_REPORT_INTERVAL_HOURS", "2"))) * 3600
    except ValueError:
        return 7200.0


def report_hours() -> float:
    return report_interval_seconds() / 3600 or 2.0


def allowed_chat_ids() -> set[str]:
    raw = os.getenv("TELEGRAM_CONTROL_ALLOWED_CHAT_IDS", "") or os.getenv("TELEGRAM_CHAT_ID", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


# ---------------------------------------------------------------- Telegram API
def telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def register_bot_commands(token: str) -> None:
    # a régi (grid bot) parancsokat is lecseréli a Telegram menüjében
    requests.post(telegram_api(token, "setMyCommands"), json={"commands": BOT_COMMANDS}, timeout=15).raise_for_status()


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """darabolás sorhatáron, hogy egy szám vagy táblázatsor ne törjön ketté; a limitnél hosszabb sor kemény vágással"""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in text.split("\n"):
        for piece in [line[i : i + limit] for i in range(0, len(line), limit)] or [""]:
            if cur and size + 1 + len(piece) > limit:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            size += len(piece) + (1 if cur else 0)
            cur.append(piece)
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()] or [text]


def send_message(token: str, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
    chunks = split_message(text)
    for idx, chunk in enumerate(chunks):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if reply_markup is not None and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(telegram_api(token, "sendMessage"), json=payload, timeout=15).raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning("sendMessage hiba: %s", type(exc).__name__)
            return False
    return True


def send_document(token: str, chat_id: str, filename: str, data: bytes, caption: str = "") -> bool:
    try:
        requests.post(
            telegram_api(token, "sendDocument"),
            data={"chat_id": chat_id, "caption": caption[:1000]},
            files={"document": (filename, data)},
            timeout=120,
        ).raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        logger.warning("sendDocument hiba: %s", type(exc).__name__)
        return False


def answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(
            telegram_api(token, "answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        ).raise_for_status()
    except requests.exceptions.RequestException:
        return


def main_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📊 Jelentés", "callback_data": "cmd:report"}, {"text": "🩺 Állapot", "callback_data": "cmd:status"}],
            [{"text": "🎯 Sniper", "callback_data": "cmd:sniper"}, {"text": "🔬 Diag", "callback_data": "cmd:diag"}],
            [{"text": "💰 Carry", "callback_data": "cmd:carry"}],
            [{"text": "🔄 Sniper újraindítás", "callback_data": "cmd:restart_sniper"},
             {"text": "🔄 Carry újraindítás", "callback_data": "cmd:restart_carry"}],
            [{"text": "⏸ Sniper stop", "callback_data": "confirm:stop_sniper"},
             {"text": "⏸ Carry stop", "callback_data": "confirm:stop_carry"}],
        ]
    }


def confirm_keyboard(action: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "✅ Megerősítem", "callback_data": f"do:{action}"}, {"text": "❌ Mégse", "callback_data": "cmd:menu"}]
        ]
    }


# ---------------------------------------------------------------- futtatás
def run_cmd(args: list[str], *, timeout: int = 60, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, cwd=PROJECT_ROOT, env=env)
        return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout ({timeout}s)"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def redact_sensitive_output(text: str) -> str:
    redacted = re.sub(
        r"(?i)(telegram[_-]?(?:bot[_-]?)?token|hl[_-]?private[_-]?key|private[_-]?key|secret|password|passphrase|api[_-]?key)\s*([:=])\s*([^\s]+)",
        r"\1\2[REDACTED]",
        text,
    )
    redacted = re.sub(r"(?i)(bot)[0-9]{5,}:[A-Za-z0-9_-]{20,}", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)0x[a-f0-9]{64}", "0x[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(api-key=|api_key=)[A-Za-z0-9-]+", r"\1[REDACTED]", redacted)  # pl. Helius RPC URL
    return redacted


def clip(text: str, max_chars: int = 3000) -> str:
    text = redact_sensitive_output(text.strip()) or "(nincs kimenet)"
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "\n... (levágva)"


def sniper_isolated() -> bool:
    return os.path.isfile(SNIPER_ENV_FILE)


def clean_env() -> dict[str, str]:
    """a sniper a saját konfigját olvassa: a vezérlő környezetét (Telegram-token, .env kulcsai) nem kapja meg"""
    return {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ", "HOME")}


def paper_script(name: str, *args: str, timeout: int = 90) -> str:
    env = clean_env() if name == "paper_sniper" else None
    rc, out = run_cmd([PYTHON, str(PROJECT_ROOT / "scripts" / name), *args], timeout=timeout, env=env)
    if rc != 0:
        return f"❌ {name} {' '.join(args)} hiba (rc={rc}):\n{clip(out, 1500)}"
    return redact_sensitive_output(out)


def service_state(unit: str) -> str:
    _, active = run_cmd(["systemctl", "is-active", unit], timeout=10)
    return (active.splitlines() or ["unknown"])[-1].strip() or "unknown"


def service_since(unit: str) -> str:
    _, out = run_cmd(["systemctl", "show", unit, "-p", "ActiveEnterTimestamp", "--value"], timeout=10)
    out = out.strip()
    if not out or out == "n/a":
        return ""
    try:
        started = datetime.strptime(" ".join(out.split()[1:3]), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    sec = max(0, int((datetime.now() - started).total_seconds()))
    return f"{sec // 86400} nap {sec % 86400 // 3600} óra" if sec >= 86400 else f"{sec // 3600} óra {sec % 3600 // 60} perc"


def state_icon(state: str) -> str:
    return {"active": "🟢", "inactive": "⚪", "failed": "🔴", "activating": "🟡"}.get(state, "🟠")


def status_text() -> str:
    lines = ["🩺 SZOLGÁLTATÁSOK"]
    for label, unit in services().items():
        st = service_state(unit)
        since = service_since(unit) if st == "active" else ""
        lines.append(f"{state_icon(st)} {label} ({unit}): {st}" + (f", fut {since}" if since else ""))
    _, disk = run_cmd(["df", "-h", "--output=pcent", str(PROJECT_ROOT)], timeout=10)
    _, mem = run_cmd(["free", "-m"], timeout=10)
    used = disk.splitlines()[-1].strip() if disk else "?"
    m = re.search(r"Mem:\s+(\d+)\s+(\d+)", mem or "")
    lines.append(f"💾 lemez {used}" + (f", memória {int(m.group(2))}/{int(m.group(1))} MB" if m else ""))
    return "\n".join(lines)


def git_out(*args: str, timeout: int = 30) -> str:
    rc, out = run_cmd(["git", *args], timeout=timeout)
    return out.strip() if rc == 0 else ""


def service_start_epoch(unit: str) -> float | None:
    _, out = run_cmd(["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic", "--value"], timeout=10)
    try:
        mono = int(out.strip()) / 1e6
    except ValueError:
        return None
    if mono <= 0:
        return None
    return time.time() - (time.monotonic() - mono)


SERVICE_SCRIPTS = {"sniper": "scripts/paper_sniper", "carry": "scripts/paper_carry"}


def version_info(fetch: bool = True) -> dict[str, Any]:
    """futó commit, lemaradás az origin/main-hez, helyi commitok, és mely szolgáltatás fut régebbi kóddal"""
    if fetch:
        run_cmd(["git", "fetch", "-q", "origin", "main"], timeout=45)
    info: dict[str, Any] = {
        "head": git_out("log", "-1", "--format=%h %cd %s", "--date=format:%Y-%m-%d %H:%M"),
        "behind": git_out("log", "--format=%h %s", "HEAD..origin/main").splitlines(),
        "local": git_out("log", "--format=%h %s", "origin/main..HEAD").splitlines(),
        "stale": [],
    }
    prs = []
    for line in info["behind"]:
        m = re.search(r"Merge pull request #(\d+)", line)
        if m:
            prs.append(f"#{m.group(1)}")
    info["prs"] = prs
    for label, unit in services().items():
        script = PROJECT_ROOT / SERVICE_SCRIPTS[label]
        started = service_start_epoch(unit) if service_state(unit) == "active" else None
        if started and script.exists() and script.stat().st_mtime > started + 5:
            info["stale"].append(label)
    ctrl_started = service_start_epoch(control_service())
    if ctrl_started and Path(__file__).resolve().stat().st_mtime > ctrl_started + 5:
        info["stale"].append("vezérlő")
    return info


def version_text(fetch: bool = True) -> str:
    v = version_info(fetch)
    lines = ["📦 KÓDVERZIÓ", f"fut: {v['head'] or '?'}"]
    if v["behind"]:
        lines.append(f"⚠️ {len(v['behind'])} új commit a GitHubon" + (f" (PR {', '.join(v['prs'])})" if v["prs"] else "")
                     + " — telepítés: /pull")
        lines += [f"  {c}" for c in v["behind"][:8]]
    else:
        lines.append("✅ naprakész az origin/main-nel")
    if v["local"]:
        lines.append(f"helyi commit (csak a VPS-en): {len(v['local'])}")
        lines += [f"  {c}" for c in v["local"][:3]]
    if v["stale"]:
        lines.append(f"⚠️ régebbi kóddal fut: {', '.join(v['stale'])} — /restart_sniper, /restart_carry vagy /pull")
    return "\n".join(lines)


def version_warning() -> str:
    """egy sor a rendszeres jelentésbe, csak ha van teendő"""
    v = version_info(fetch=True)
    parts = []
    if v["behind"]:
        parts.append(f"{len(v['behind'])} telepítetlen commit" + (f" (PR {', '.join(v['prs'])})" if v["prs"] else "") + " — /pull")
    if v["stale"]:
        parts.append(f"régebbi kóddal fut: {', '.join(v['stale'])}")
    return ("⚠️ " + "; ".join(parts)) if parts else f"📦 kód: {v['head'].split(' ')[0] if v['head'] else '?'}, naprakész"


def data_path(rel: str) -> Path:
    """az export fájljai; az izolált sniperé a saját állapot-könyvtárában (a zipben a név marad state/...)"""
    if rel.startswith("state/paper_sniper_") and sniper_isolated():
        return SNIPER_STATE_DIR / rel.split("/", 1)[1]
    return PROJECT_ROOT / rel


def add_csv_tail(z: zipfile.ZipFile, rel: str, included: list[str]) -> None:
    """nagy CSV: csak a fejléc + az utolsó sorok kerülnek a zipbe"""
    path = data_path(rel)
    if not path.is_file():
        return
    from collections import deque

    with path.open(encoding="utf-8", errors="replace") as f:
        header = f.readline()
        tail = deque(f, maxlen=WALLET_LOG_EXPORT_LINES)
    z.writestr(rel, header + "".join(tail))
    included.append(f"{path.name} (utolsó {len(tail)} sor)")


def build_export() -> tuple[str, bytes, str]:
    """zip a papír-adatokkal + diag/riport szövegekkel; (fájlnév, tartalom, felirat)"""
    buf = io.BytesIO()
    included, skipped = [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in EXPORT_FILES:
            path = data_path(rel)
            if path.is_file():
                # kitakart szoveg, nem a nyers fajl: egy hibauzenetben se maradhasson API-kulcs
                z.writestr(rel, redact_sensitive_output(path.read_text(encoding="utf-8", errors="replace")))
                included.append(rel.split("/")[-1])
            else:
                skipped.append(rel.split("/")[-1])
        for rel in (WALLET_LOG_REL, TOKEN_LOG_REL):
            add_csv_tail(z, rel, included)
        texts = {
            "sniper_report.txt": paper_script("paper_sniper", "report"),
            "sniper_kiszallas.txt": paper_script("paper_sniper", "report", "--kiszallas"),
            "sniper_diag.txt": paper_script("paper_sniper", "diag"),
            "sniper_wallets.txt": paper_script("paper_sniper", "wallets", timeout=180),
            "sniper_tokens.txt": paper_script("paper_sniper", "tokens", timeout=180),
            "carry_report.txt": paper_script("paper_carry", "report"),
            "version.txt": version_text(fetch=False),
        }
        for name, text in texts.items():
            z.writestr(name, redact_sensitive_output(text))
    data = buf.getvalue()
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    caption = f"📤 Papír-export {stamp}: {', '.join(included) or 'nincs adatfájl'} + diag/riportok"
    if skipped:
        caption += f" (hiányzik: {', '.join(skipped)})"
    return f"paper_export_{stamp}.zip", data, caption


def full_report(hours: float) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"📊 PAPÍR-JELENTÉS {now} (ablak: {hours:g} óra)", "", status_text(), version_warning()]
    parts += ["", "🎯 SNIPER (pump.fun)", paper_script("paper_sniper", "report", "--telegram", "--ora", f"{hours:g}")]
    parts += ["", "💰 CARRY (HYPE funding)", paper_script("paper_carry", "telegram", "--ora", f"{hours:g}")]
    return "\n".join(parts)


def logs_text(label: str) -> str:
    unit = services()[label]
    _, out = run_cmd(["journalctl", "-u", unit, "-n", "25", "--no-pager", "-o", "cat"], timeout=15)
    return f"📜 {unit} utolsó sorai:\n{clip(out)}"


def systemctl_action(action: str, label: str) -> str:
    unit = services()[label]
    rc, out = run_cmd(["systemctl", action, unit], timeout=60)
    state = service_state(unit)
    if rc != 0:
        return f"❌ systemctl {action} {unit} hiba (rc={rc}):\n{clip(out, 800)}"
    return f"✅ {label}: {action} kész, állapot: {state_icon(state)} {state}"


def run_pull() -> str:
    """git pull --rebase (a VPS-en helyi commit van), hiba esetén rebase --abort; utána a papír-szolgáltatások újraindítása"""
    rc, out = run_cmd(["git", "pull", "--rebase", "origin", "main"], timeout=90)
    if rc != 0:
        run_cmd(["git", "rebase", "--abort"], timeout=30)
        return f"❌ git pull --rebase hiba (rc={rc}), rebase visszavonva:\n{clip(out, 1500)}"
    lines = [f"✅ git pull kész:\n{clip(out, 1200)}"]
    for label in services():
        lines.append(systemctl_action("restart", label))
    lines.append("🔄 A vezérlő is újraindul.")
    return "\n".join(lines)


# ---------------------------------------------------------------- parancsok
CONFIRM_LABELS = {"stop_sniper": "⏸ A sniper leállítása", "stop_carry": "⏸ A carry leállítása"}


LIVE_ENV_REL = "config/live.env"
SETTABLE_RE = re.compile(r"^(PAPER_SNIPER_[A-Z0-9_]{1,40}|PAPER_CARRY_[A-Z0-9_]{1,40}|CARRY_[A-Z0-9_]{1,40}|TELEGRAM_REPORT_INTERVAL_HOURS)$")
FORBIDDEN_IN_KEY = ("KEY", "TOKEN", "SECRET", "URL", "PRIVATE", "PASS", "WALLET")
VALUE_RE = re.compile(r"^[A-Za-z0-9_.,:\-]{0,200}$")


def settable_key(key: str) -> bool:
    return bool(SETTABLE_RE.match(key)) and not any(f in key for f in FORBIDDEN_IN_KEY)


def env_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def write_env_lines(path: Path, lines: list[str]) -> None:
    """atomikus csere; a jogosultság és a tulajdonos marad (a live.env kulcsokat is tartalmaz: újként 600)"""
    try:
        st = path.stat()
        mode, owner = st.st_mode & 0o777, (st.st_uid, st.st_gid)
    except OSError:
        mode, owner = 0o600, None
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        if owner:
            os.chown(tmp, *owner)                  # pl. root:paper-sniper, hogy a sniper olvashassa
        os.chmod(tmp, mode)
    except OSError:
        pass
    tmp.replace(path)


def env_target(key: str) -> tuple[Path, str]:
    """hová kerül a kulcs: az izolált sniper saját konfigjába, vagy a config/live.env-be"""
    if key.startswith("PAPER_SNIPER_") and sniper_isolated():
        return SNIPER_ENV_FILE, str(SNIPER_ENV_FILE)
    return PROJECT_ROOT / LIVE_ENV_REL, LIVE_ENV_REL


def settable_lines(path: Path, skip_sniper: bool = False) -> list[str]:
    return [ln for ln in env_lines(path) if "=" in ln and settable_key(ln.split("=", 1)[0].strip())
            and not (skip_sniper and ln.strip().startswith("PAPER_SNIPER_"))]


def set_command(text: str) -> str:
    """papír-beállítás a config/live.env-ben (izolált sniper: a saját konfigjában); csak engedélyezett, titkot nem tartalmazó kulcsok"""
    parts = text.strip().split(None, 1)
    command = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    live = PROJECT_ROOT / LIVE_ENV_REL
    if command == "/set" and not arg:
        iso = sniper_isolated()
        out = []
        if iso:
            cur = settable_lines(SNIPER_ENV_FILE)
            out += [f"🎯 Sniper (izolált) — {SNIPER_ENV_FILE}:"] + ([f"  {ln}" for ln in cur] or ["  (nincs)"]) + [""]
        cur = settable_lines(live, skip_sniper=iso)
        out += [f"⚙️ Papír-beállítások a {LIVE_ENV_REL}-ben:"] + ([f"  {ln}" for ln in cur] or ["  (nincs)"])
        old = [ln for ln in env_lines(live) if ln.strip().startswith("PAPER_SNIPER_")]
        if iso and old:
            out.append(f"ℹ️ A {LIVE_ENV_REL} {len(old)} PAPER_SNIPER_* sorát a sniper már nem olvassa (törölhetők).")
        return "\n".join(out) + "\n\nMódosítás: /set KULCS=ÉRTÉK — törlés: /unset KULCS — érvényes: /restart_sniper"
    if command == "/set":
        key, sep, value = arg.partition("=")
        if not sep:
            key, _, value = arg.partition(" ")
        key, value = key.strip().upper(), value.strip()
    else:
        key, value = arg.split()[0].upper() if arg else "", ""
    if not settable_key(key):
        return "❌ Ez a kulcs nem állítható Telegramról (csak PAPER_SNIPER_*, CARRY_*, TELEGRAM_REPORT_INTERVAL_HOURS; kulcs/URL soha)."
    if command == "/set" and not VALUE_RE.match(value):
        return "❌ Érvénytelen érték (csak betű, szám és _ . , : - lehet, legfeljebb 200 karakter)."
    path, label = env_target(key)
    lines = env_lines(path)
    rest = [ln for ln in lines if not ln.strip().startswith(key + "=")]
    if command == "/set":
        rest.append(f"{key}={value}")
        msg = f"✅ Beállítva: {key}={value}"
    else:
        if len(rest) == len(lines):
            return f"ℹ️ Nincs ilyen sor itt: {label}: {key}"
        msg = f"✅ Törölve: {key}"
    write_env_lines(path, rest)
    msg += f" ({label})"
    if path == live and any(ln.strip().startswith(key + "=") for ln in env_lines(PROJECT_ROOT / ".env")):
        msg += "\n⚠️ A .env-ben is van ilyen kulcs, és az ERŐSEBB (felülírja). Onnan kézzel kell törölni."
    return msg + "\nÉrvényes az újraindítás után: /restart_sniper (vagy /restart_carry)"


def handle_command(text: str, *, confirmed: bool = False) -> tuple[str, dict[str, Any] | None]:
    command = text.strip().split()[0].split("@")[0].lower()
    interval = report_interval_seconds()
    if command in {"/help", "/start"}:
        every = f"{interval / 3600:g} óránként" if interval else "kikapcsolva"
        return HELP_TEXT.format(interval=every), main_menu_keyboard()
    if command == "/menu":
        return "🤖 Papír-botok — válassz:", main_menu_keyboard()
    if command == "/report":
        return full_report(report_hours()), None
    if command == "/status":
        return status_text(), None
    if command == "/version":
        return version_text(), None
    if command == "/sniper":
        return "🎯 SNIPER\n" + paper_script("paper_sniper", "report"), None
    if command == "/tokens":
        return "🏆 TOKENEK\n" + paper_script("paper_sniper", "tokens", timeout=180), None
    if command == "/wallets":
        return "👛 TÁRCÁK\n" + paper_script("paper_sniper", "wallets", timeout=180), None
    if command == "/diag":
        return "🔬 SNIPER DIAG\n" + paper_script("paper_sniper", "diag"), None
    if command == "/carry":
        return "💰 CARRY\n" + paper_script("paper_carry", "status") + "\n" + paper_script("paper_carry", "report"), None
    if command in {"/set", "/unset"}:
        return set_command(text), None
    m = re.fullmatch(r"/(logs|restart|stop|start)_(sniper|carry)", command)
    if m:
        action, label = m.groups()
        if action == "logs":
            return logs_text(label), None
        if action == "stop" and not confirmed:
            return f"⚠️ MEGERŐSÍTÉS\n{CONFIRM_LABELS[command[1:]]}", confirm_keyboard(command[1:])
        return systemctl_action(action, label), None
    return "Ismeretlen parancs. /help", None


def handle_callback(data: str) -> tuple[str, dict[str, Any] | None]:
    if data == "cmd:menu":
        return handle_command("/menu")
    kind, _, action = data.partition(":")
    if kind in {"cmd", "confirm"} and re.fullmatch(r"[a-z_]+", action):
        return handle_command(f"/{action}")
    if kind == "do" and action in CONFIRM_LABELS:
        return handle_command(f"/{action}", confirmed=True)
    return "Ismeretlen gomb. /menu", main_menu_keyboard()


# ---------------------------------------------------------------- állapot
def read_offset() -> int | None:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset), encoding="utf-8")


def read_report_state() -> dict[str, Any]:
    try:
        return json.loads(REPORT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_report_state(state: dict[str, Any]) -> None:
    REPORT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def service_alerts(previous: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """állapotváltozás-riasztások; az első ellenőrzés csak felveszi az állapotot"""
    current = {label: service_state(unit) for label, unit in services().items()}
    alerts = []
    for label, st in current.items():
        old = previous.get(label)
        if old is None or old == st:
            continue
        if st != "active":
            alerts.append(f"🚨 {label} ({services()[label]}) leállt: {old} -> {st}\n/logs_{label}")
        elif old != "activating":
            alerts.append(f"✅ {label} újra fut ({old} -> active)")
    return alerts, current


# ---------------------------------------------------------------- fő ciklus
def poll_loop() -> None:
    load_env()
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        raise RuntimeError("Hiányzik a TELEGRAM_BOT_TOKEN vagy a TELEGRAM_CHAT_ID")
    allowed = allowed_chat_ids()
    offset = read_offset()
    try:
        register_bot_commands(token)
    except requests.exceptions.RequestException:
        logger.warning("setMyCommands sikertelen")
    interval = report_interval_seconds()
    send_message(token, chat, f"✅ Papír-vezérlő elindult. Jelentés: {f'{interval / 3600:g} óránként' if interval else 'kikapcsolva'}. /help",
                 main_menu_keyboard())

    last_service_check = 0.0
    svc_state: dict[str, str] = {}
    while True:
        try:
            now = time.time()
            if now - last_service_check >= SERVICE_CHECK_SECONDS:
                last_service_check = now
                alerts, svc_state = service_alerts(svc_state)
                for a in alerts:
                    send_message(token, chat, a)
            rs = read_report_state()
            if interval and now - float(rs.get("last_report", 0)) >= interval:
                write_report_state({"last_report": now})   # előbb jelöljük, hogy hiba esetén se ismételje percenként
                send_message(token, chat, full_report(interval / 3600))

            payload: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            resp = requests.get(telegram_api(token, "getUpdates"), params=payload, timeout=POLL_TIMEOUT_SECONDS + 10)
            resp.raise_for_status()
            for update in resp.json().get("result", []):
                offset = int(update.get("update_id", 0)) + 1
                write_offset(offset)   # a feldolgozás előtt: /pull utáni újraindulásnál ne fusson újra
                handle_update(token, chat, allowed, update)
        except requests.exceptions.RequestException:
            time.sleep(2)
        except Exception as exc:
            logger.exception("vezérlő ciklus hiba")
            send_message(token, chat, f"⚠️ Telegram-vezérlő hiba: {type(exc).__name__}: {clip(str(exc), 300)}")
            time.sleep(10)


def handle_update(token: str, chat: str, allowed: set[str], update: dict[str, Any]) -> None:
    callback = update.get("callback_query") or {}
    if callback:
        chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat_id not in allowed:
            answer_callback(token, str(callback.get("id", "")), "Nincs jogosultság")
            send_message(token, chat, f"⚠️ Jogosulatlan gombnyomás chat_id={chat_id}")
            return
        answer_callback(token, str(callback.get("id", "")))
        reply, markup = handle_callback(str(callback.get("data", "") or ""))
        send_message(token, chat_id, reply, markup)
        return

    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = str(message.get("text", "") or "").strip()
    if not text.startswith("/"):
        return
    if chat_id not in allowed:
        send_message(token, chat, f"⚠️ Jogosulatlan parancs chat_id={chat_id}: {text[:80]}")
        return
    command = text.split()[0].split("@")[0].lower()
    if command == "/pull":
        send_message(token, chat_id, "⏳ git pull --rebase + újraindítás...")
        send_message(token, chat_id, run_pull())
        run_cmd(["systemctl", "restart", "--no-block", control_service()], timeout=15)
        return
    if command == "/export":
        send_message(token, chat_id, "⏳ Export készül...")
        try:
            name, data, caption = build_export()
        except Exception as exc:
            send_message(token, chat_id, f"❌ Export hiba: {type(exc).__name__}: {clip(str(exc), 300)}")
            return
        if len(data) > MAX_EXPORT_BYTES:
            send_message(token, chat_id, f"❌ Az export túl nagy ({len(data) / 1e6:.1f} MB > {MAX_EXPORT_BYTES / 1e6:.0f} MB).")
        elif not send_document(token, chat_id, name, data, caption):
            send_message(token, chat_id, "❌ A fájl küldése nem sikerült (hálózat?).")
        return
    if command == "/report":
        send_message(token, chat_id, "⏳ Jelentés készül...")
    try:
        reply, markup = handle_command(text)
    except Exception as exc:
        reply, markup = f"❌ Parancs hiba: {type(exc).__name__}: {clip(str(exc), 300)}", None
    send_message(token, chat_id, reply, markup)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    poll_loop()
