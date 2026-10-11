import os
import io
import gzip
import time
import random
import hashlib
import hmac
import json
import asyncio
from urllib.parse import parse_qsl
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, MenuButtonWebApp, BotCommand, BotCommandScopeDefault,
    LinkPreviewOptions, LabeledPrice, PreCheckoutQuery, InputFile,
)
from telegram.ext import PreCheckoutQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
PORT = int(os.environ.get("PORT", 10000))
_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(_DATA_DIR, exist_ok=True)
CARDS_MEDIA_DIR = os.path.join(_DATA_DIR, "cards")
os.makedirs(CARDS_MEDIA_DIR, exist_ok=True)
DB = os.environ.get("DB_PATH", os.path.join(_DATA_DIR, "casino.db"))
AUTH_MAX_AGE = 86400
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ═══════════════════════════════════════════════════════════
# TELEGRAM STORAGE (kostenlose Persistenz)
# ═══════════════════════════════════════════════════════════
STORAGE_CHAT_ID = os.environ.get("STORAGE_CHAT_ID", "").strip()
DB_BACKUP_INTERVAL = int(os.environ.get("DB_BACKUP_INTERVAL", "300"))  # Sekunden

ADMIN_IDS = [6098272278]

CARDS_IMAGE_BASE = os.environ.get("CARDS_IMAGE_BASE", "").strip().rstrip("/")
CARDS_IMAGE_EXT = os.environ.get("CARDS_IMAGE_EXT", "png").lstrip(".")

SHOP_PACKS = {
    "100":   {"coins": 100,   "stars": 1,  "title": "100 Coins"},
    "1000":  {"coins": 1000,  "stars": 5,  "title": "1.000 Coins"},
    "5000":  {"coins": 5000,  "stars": 15, "title": "5.000 Coins"},
    "10000": {"coins": 10000, "stars": 25, "title": "10.000 Coins"},
}

BOT_INSTANCE = None
bj_sessions = {}
crash_sessions = {}
_db_dirty = False


def mark_db_dirty():
    global _db_dirty
    _db_dirty = True


def default_card_image_url(card_id: int) -> str:
    if not CARDS_IMAGE_BASE or "CHANGE_ME" in CARDS_IMAGE_BASE:
        return ""
    return f"{CARDS_IMAGE_BASE}/{card_id}.{CARDS_IMAGE_EXT}"


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        coins INTEGER DEFAULT 1000,
        last_daily INTEGER DEFAULT 0,
        max_bet INTEGER DEFAULT 100,
        self_excluded_until INTEGER DEFAULT 0,
        total_wagered INTEGER DEFAULT 0,
        total_won INTEGER DEFAULT 0,
        win_streak INTEGER DEFAULT 0,
        best_streak INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        added_at INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS wins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, game TEXT, amount INTEGER, created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS referrals (
        referred_id INTEGER PRIMARY KEY,
        referrer_id INTEGER, created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS user_cards (
        user_id INTEGER NOT NULL,
        card_id INTEGER NOT NULL,
        vip INTEGER DEFAULT 0,
        acquired_at INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, card_id)
    );
    CREATE TABLE IF NOT EXISTS card_media (
        card_id INTEGER PRIMARY KEY,
        image_url TEXT NOT NULL DEFAULT '',
        creator_link TEXT DEFAULT '',
        paypal_link TEXT DEFAULT '',
        updated_at INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS user_state (
        user_id INTEGER PRIMARY KEY,
        data TEXT DEFAULT '{}',
        updated_at INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS globals (
        key TEXT PRIMARY KEY,
        value INTEGER DEFAULT 0
    );
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    for col, ddl in [("win_streak", "INTEGER DEFAULT 0"), ("best_streak", "INTEGER DEFAULT 0")]:
        if col not in cols:
            try: con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except Exception: pass
    media_cols = [r[1] for r in con.execute("PRAGMA table_info(card_media)").fetchall()]
    for col, ddl in [
        ("creator_link", "TEXT DEFAULT ''"),
        ("paypal_link", "TEXT DEFAULT ''"),
        ("tg_file_id", "TEXT DEFAULT ''"),
    ]:
        if col not in media_cols:
            try: con.execute(f"ALTER TABLE card_media ADD COLUMN {col} {ddl}")
            except Exception: pass

    for aid in ADMIN_IDS:
        try: con.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)",
                         (int(aid), int(time.time())))
        except Exception: pass

    raw_vip = os.environ.get("VIP_IMAGES", "").strip()
    if raw_vip:
        try:
            vip_map = json.loads(raw_vip)
            for cid_s, val in vip_map.items():
                try:
                    cid = int(cid_s)
                    if isinstance(val, dict):
                        url = str(val.get("image_url", "")).strip()
                        clink = str(val.get("creator_link", "")).strip()
                        plink = str(val.get("paypal_link", "")).strip()
                    else:
                        url = str(val).strip(); clink = ""; plink = ""
                    if cid in range(1, 31):
                        con.execute(
                            "INSERT INTO card_media (card_id, image_url, creator_link, paypal_link, updated_at) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(card_id) DO UPDATE SET image_url=excluded.image_url, "
                            "creator_link=excluded.creator_link, paypal_link=excluded.paypal_link, "
                            "updated_at=excluded.updated_at",
                            (cid, url, clink, plink, int(time.time())),
                        )
                except Exception: pass
            print(f"VIP_IMAGES seeded: {len(vip_map)} entries")
        except Exception as e:
            print(f"VIP_IMAGES parse error: {e}")

    con.commit(); con.close()
    try: load_media_backup()
    except Exception as e: print(f"load_media_backup: {e}")


# ═══════════════════════════════════════════════════════════
# TELEGRAM STORAGE HELPERS
# ═══════════════════════════════════════════════════════════
async def tg_send_document(content: bytes, filename: str, caption: str = "") -> Optional[dict]:
    """Sendet Bytes als Dokument an den Storage-Channel. Gibt {file_id, message_id} zurück."""
    if not STORAGE_CHAT_ID or BOT_INSTANCE is None:
        return None
    try:
        bio = io.BytesIO(content)
        bio.name = filename
        msg = await BOT_INSTANCE.send_document(
            chat_id=STORAGE_CHAT_ID,
            document=InputFile(bio, filename=filename),
            caption=caption or None,
        )
        if msg.document:
            return {"file_id": msg.document.file_id, "message_id": msg.message_id}
        return None
    except Exception as e:
        print(f"tg_send_document error: {e}")
        return None


async def tg_get_document_content(file_id: str) -> Optional[bytes]:
    if BOT_INSTANCE is None:
        return None
    try:
        f = await BOT_INSTANCE.get_file(file_id)
        bio = io.BytesIO()
        await f.download_to_memory(bio)
        return bio.getvalue()
    except Exception as e:
        print(f"tg_get_document_content error: {e}")
        return None


async def tg_save_db_backup(force: bool = False):
    global _db_dirty
    if not STORAGE_CHAT_ID or BOT_INSTANCE is None:
        return False
    if not force and not _db_dirty:
        return False
    try:
        if not os.path.isfile(DB):
            return False
        with open(DB, "rb") as f:
            data = f.read()
        if len(data) < 100:
            return False
        gz = gzip.compress(data, compresslevel=6)
        ts = int(time.time())
        res = await tg_send_document(
            gz, f"casino-{ts}.db.gz",
            f"🗄️ DB-Backup · {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} · {len(data)} B → {len(gz)} B"
        )
        if res:
            try:
                await BOT_INSTANCE.pin_chat_message(
                    chat_id=STORAGE_CHAT_ID,
                    message_id=res["message_id"],
                    disable_notification=True,
                )
            except Exception as e:
                print(f"pin message: {e}")
            print(f"✅ DB-Backup hochgeladen: msg_id={res['message_id']}, {len(gz)} bytes")
            _db_dirty = False
            return True
    except Exception as e:
        print(f"tg_save_db_backup error: {e}")
    return False


async def tg_restore_db():
    """Beim Start: Wenn DB fehlt oder leer ist → vom gepinnten Backup wiederherstellen."""
    if not STORAGE_CHAT_ID or BOT_INSTANCE is None:
        return False
    if os.path.isfile(DB) and os.path.getsize(DB) > 8192:
        print(f"DB vorhanden ({os.path.getsize(DB)} B) – kein Restore nötig")
        return False
    try:
        chat = await BOT_INSTANCE.get_chat(STORAGE_CHAT_ID)
        pinned = getattr(chat, "pinned_message", None)
        if not pinned or not pinned.document:
            print("Kein gepinntes DB-Backup gefunden")
            return False
        file_id = pinned.document.file_id
        content = await tg_get_document_content(file_id)
        if not content:
            return False
        raw = gzip.decompress(content)
        if not raw.startswith(b"SQLite format 3"):
            print("⚠️ Backup ist keine gültige SQLite-Datei")
            return False
        # Backup der aktuellen (leeren) DB zur Sicherheit
        if os.path.isfile(DB):
            try: os.rename(DB, DB + ".old")
            except Exception: pass
        with open(DB, "wb") as f:
            f.write(raw)
        print(f"✅ DB vom Telegram-Backup wiederhergestellt: {len(raw)} bytes")
        return True
    except Exception as e:
        print(f"tg_restore_db error: {e}")
        return False


async def db_backup_loop():
    print(f"DB-Backup-Loop gestartet (Intervall {DB_BACKUP_INTERVAL}s)")
    while True:
        try:
            await asyncio.sleep(DB_BACKUP_INTERVAL)
            await tg_save_db_backup(force=False)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"backup loop: {e}")


# ═══════════════════════════════════════════════════════════
# CARD HELPERS
# ═══════════════════════════════════════════════════════════
def get_local_card_file(card_id: int):
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        p = os.path.join(CARDS_MEDIA_DIR, f"{card_id}.{ext}")
        if os.path.isfile(p):
            return p
    return None


def _catalog_version() -> int:
    con = db()
    row = con.execute("SELECT value FROM globals WHERE key='catalog_version'").fetchone()
    con.close()
    return int(row["value"]) if row else 0


def bump_catalog_version() -> int:
    con = db()
    row = con.execute("SELECT value FROM globals WHERE key='catalog_version'").fetchone()
    new = (int(row["value"]) if row else 0) + 1
    con.execute(
        "INSERT INTO globals (key, value) VALUES ('catalog_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (new,),
    )
    con.commit(); con.close()
    return new


def user_has_card(uid: int, card_id: int) -> bool:
    con = db()
    row = con.execute("SELECT 1 FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id)).fetchone()
    con.close()
    return bool(row)


def grant_card(uid: int, card_id: int) -> bool:
    if card_id < 1 or card_id > 30:
        return False
    con = db()
    existing = con.execute("SELECT 1 FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id)).fetchone()
    if existing:
        con.close(); return False
    con.execute(
        "INSERT INTO user_cards (user_id, card_id, vip, acquired_at) VALUES (?,?,0,?)",
        (uid, card_id, int(time.time())),
    )
    con.commit(); con.close()
    mark_db_dirty()
    return True


def get_card_vip_info(card_id: int) -> dict:
    con = db()
    try:
        row = con.execute(
            "SELECT COALESCE(image_url,'') AS image_url, "
            "COALESCE(creator_link,'') AS creator_link, "
            "COALESCE(paypal_link,'') AS paypal_link, "
            "COALESCE(tg_file_id,'') AS tg_file_id FROM card_media WHERE card_id=?",
            (card_id,)
        ).fetchone()
    except Exception:
        row = None
    con.close()

    default_paypal = os.environ.get("PAYPAL_VIP_LINK", "").strip()
    local_exists = get_local_card_file(card_id) is not None

    if not row:
        if not local_exists:
            return {
                "is_vip_card": False,
                "image_url": default_card_image_url(card_id),
                "creator_link": "",
                "paypal_link": "",
                "tg_file_id": "",
            }
        return {
            "is_vip_card": True,
            "image_url": f"/api/media/{card_id}",
            "creator_link": "",
            "paypal_link": default_paypal,
            "tg_file_id": "",
        }

    img = (row["image_url"] or "").strip()
    if not img and not row["tg_file_id"]:
        img = default_card_image_url(card_id)

    return {
        "is_vip_card": True,
        "image_url": img,
        "creator_link": (row["creator_link"] or "").strip(),
        "paypal_link": (row["paypal_link"] or "").strip() or default_paypal,
        "tg_file_id": (row["tg_file_id"] or "").strip(),
    }


def get_user_collection(uid: int) -> list:
    con = db()
    rows = con.execute(
        "SELECT card_id, vip FROM user_cards WHERE user_id=? ORDER BY card_id",
        (uid,),
    ).fetchall()
    con.close()
    adm = is_admin(uid)
    out = []
    for r in rows:
        cid = r["card_id"]
        info = get_card_vip_info(cid)
        vip_unlocked = bool(r["vip"])
        out.append({
            "card_id": cid,
            "owned": True,
            "vip": vip_unlocked,
            "is_vip_card": info["is_vip_card"],
            "image_url": info["image_url"],
            "creator_link": info["creator_link"] if ((vip_unlocked or adm) and info["is_vip_card"]) else "",
            "paypal_link": info["paypal_link"] if info["is_vip_card"] else "",
        })
    return out


def get_media_rows() -> dict:
    con = db()
    try:
        rows = con.execute(
            "SELECT card_id, COALESCE(image_url,'') AS image_url, "
            "COALESCE(creator_link,'') AS creator_link, "
            "COALESCE(paypal_link,'') AS paypal_link FROM card_media"
        ).fetchall()
    except Exception:
        rows = []
    con.close()
    out = {}
    for r in rows:
        cid = r["card_id"]
        if cid < 1 or cid > 30:
            continue
        out[cid] = {
            "image_url": (r["image_url"] or "").strip(),
            "creator_link": (r["creator_link"] or "").strip(),
            "paypal_link": (r["paypal_link"] or "").strip(),
        }
    return out


def media_backup_path():
    return os.path.join(_DATA_DIR, "card_media.json")


def save_media_backup():
    try:
        media = get_media_rows()
        payload = {str(k): {
            "image_url": v["image_url"],
            "creator_link": v.get("creator_link") or "",
            "paypal_link": v.get("paypal_link") or "",
        } for k, v in media.items()}
        with open(media_backup_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"media backup write: {e}")


def load_media_backup():
    p = media_backup_path()
    if not os.path.isfile(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        con = db()
        n = 0
        for cid_s, val in (data or {}).items():
            try:
                cid = int(cid_s)
                if isinstance(val, dict):
                    url = str(val.get("image_url") or "").strip()
                    clink = str(val.get("creator_link") or "").strip()
                    plink = str(val.get("paypal_link") or "").strip()
                else:
                    url = str(val).strip(); clink = ""; plink = ""
                if cid < 1 or cid > 30:
                    continue
                con.execute(
                    "INSERT INTO card_media (card_id, image_url, creator_link, paypal_link, updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(card_id) DO UPDATE SET image_url=excluded.image_url, "
                    "creator_link=excluded.creator_link, paypal_link=excluded.paypal_link, "
                    "updated_at=excluded.updated_at",
                    (cid, url, clink, plink, int(time.time())),
                )
                n += 1
            except Exception:
                pass
        con.commit(); con.close()
        if n: print(f"card_media.json restored: {n}")
        return n
    except Exception as e:
        print(f"media backup load: {e}")
        return 0


def get_user(uid):
    con = db()
    row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        con.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, int(time.time())))
        con.commit()
        row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        mark_db_dirty()
    con.close()
    return dict(row)


def update_user(uid, **f):
    if not f:
        return
    con = db()
    clause = ", ".join(f"{k}=?" for k in f)
    con.execute(f"UPDATE users SET {clause} WHERE user_id=?", list(f.values()) + [uid])
    con.commit(); con.close()
    mark_db_dirty()


def is_admin(uid):
    con = db()
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)


def calc_level(wagered: int):
    W = max(0, wagered)
    level = 1 + int((W / 100) ** 0.6)
    def thresh(l): return int(100 * (l - 1) ** 1.667)
    cur = thresh(level); nxt = thresh(level + 1)
    pct = int(((W - cur) / max(1, nxt - cur)) * 100) if nxt > cur else 0
    return level, max(0, min(100, pct))


# ═══════════════════════════════════════════════════════════
# HOUSE-EDGE ENGINE
# ═══════════════════════════════════════════════════════════
HOUSE_EDGE = 0.15
TARGET_RTP = 1.0 - HOUSE_EDGE
RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}

def player_heat(u):
    streak = max(0, u.get("win_streak") or 0)
    wagered = max(1, u.get("total_wagered") or 1)
    won = max(0, u.get("total_won") or 0)
    ratio = won / wagered
    heat = 0.0
    heat += min(0.5, streak * 0.08)
    heat += min(0.5, max(0.0, (ratio - 0.7) * 1.2))
    return max(0.0, min(1.0, heat))

def effective_rtp(u):
    h = player_heat(u)
    return TARGET_RTP - h * 0.13 + (1.0 - h) * 0.05

def biased_random(u):
    r = random.random()
    h = player_heat(u)
    if h > 0.3 and random.random() < h * 0.55:
        r = 0.55 + r * 0.45
    return r

def decide_win(u, fair_p):
    rtp = effective_rtp(u)
    p = fair_p * (rtp / 0.95)
    p = max(0.05, min(0.48, p))
    return random.random() < p

def crash_multiplier(u):
    r = biased_random(u)
    edge = 0.92 - player_heat(u) * 0.08
    if r < 0.04: return 1.00
    mult = max(1.01, edge / (1.0 - r))
    mult = min(mult, 100.0)
    if mult > 2.5 and random.random() < 0.35 + player_heat(u) * 0.25:
        mult = 1.05 + random.random() * 0.30
    return round(mult, 2)

def slots_outcome(u):
    symbols = ["🍒","🍋","🔔","💎","7️⃣"]
    weights_base = [32,28,22,12,6]
    heat = player_heat(u)
    weights = list(weights_base)
    weights[3] = max(4, int(12 - heat * 6))
    weights[4] = max(2, int(6 - heat * 3))
    def pick():
        total = sum(weights); x = random.randrange(total); acc = 0
        for s, w in zip(symbols, weights):
            acc += w
            if x < acc: return s
        return symbols[0]
    a, b, c = pick(), pick(), pick()
    if a == b and c != a and random.random() < 0.45:
        pass
    elif random.random() < 0.22:
        a = b = pick()
        others = [s for s in symbols if s != a]
        c = random.choice(others)
    if a == b == c:
        mult = 15 if a == "7️⃣" else 8 if a == "💎" else 4
        return [a, b, c], mult
    if a == b or b == c or a == c:
        return [a, b, c], 1.2
    return [a, b, c], 0

def coin_outcome(u, choice):
    choice = (choice or "").lower()
    want_win = decide_win(u, 0.42)
    if want_win and choice in ("kopf","zahl"):
        side = choice; won = True
    else:
        side = ("zahl" if choice == "kopf" else "kopf") if choice in ("kopf","zahl") else random.choice(["kopf","zahl"])
        won = False
    return side, won

def bowl_outcome(u):
    rtp = effective_rtp(u)
    p_strike = max(0.08, min(0.18, rtp / 5.0))
    r = random.random()
    if r < p_strike: return 6
    if r < p_strike + 0.18: return 5
    return random.randint(1, 4)

def roulette_spin(u, bet_type, number=None):
    heat = player_heat(u)
    force_lose = random.random() < (0.08 + heat * 0.12)
    n = random.randint(0, 36)
    if force_lose:
        for _ in range(40):
            n = random.randint(0, 36)
            color = "green" if n == 0 else "red" if n in RED else "black"
            hit = (bet_type == "single" and number == n) or \
                  (bet_type == "red" and color == "red") or \
                  (bet_type == "black" and color == "black") or \
                  (bet_type == "even" and n != 0 and n % 2 == 0) or \
                  (bet_type == "odd" and n % 2 == 1) or \
                  (bet_type == "1-18" and 1 <= n <= 18) or \
                  (bet_type == "19-36" and 19 <= n <= 36)
            if not hit: break
    color = "green" if n == 0 else "red" if n in RED else "black"
    return n, color

def record_outcome(uid, won, game, win_amount):
    u = get_user(uid)
    if won:
        s = u["win_streak"] + 1
        b = max(u["best_streak"], s)
        update_user(uid, win_streak=s, best_streak=b)
    else:
        update_user(uid, win_streak=0)
    if win_amount >= 300:
        con = db()
        con.execute("INSERT INTO wins (user_id, game, amount, created_at) VALUES (?,?,?,?)",
                    (uid, game, win_amount, int(time.time())))
        con.commit(); con.close()
        mark_db_dirty()


# ═══════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════
def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "initData fehlt")
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "hash fehlt")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise HTTPException(401, "Signatur ungültig")
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except Exception:
        raise HTTPException(401, "auth_date ungültig")
    if time.time() - auth_date > AUTH_MAX_AGE:
        raise HTTPException(401, "initData abgelaufen")
    try:
        user = json.loads(parsed.get("user", "{}"))
    except Exception:
        raise HTTPException(401, "User ungültig")
    if not user.get("id"):
        raise HTTPException(401, "User fehlt")
    return user


def auth(x_init_data: str = Header(None, alias="X-Init-Data")):
    return validate_init_data(x_init_data or "")["id"]


def require_admin(uid):
    if not is_admin(uid):
        raise HTTPException(403, "Kein Admin")


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/")
@app.head("/")
def root():
    return HTMLResponse("<h1>Casino backend läuft.</h1>", status_code=200)


@app.get("/api/me")
def me(uid: int = Depends(auth)):
    u = get_user(uid)
    level, xp_pct = calc_level(u["total_wagered"])
    return {
        "coins": u["coins"],
        "max_bet": max(u["max_bet"], u["coins"]),
        "wagered": u["total_wagered"],
        "won": u["total_won"],
        "excluded_until": u["self_excluded_until"],
        "streak": u["win_streak"],
        "best_streak": u["best_streak"],
        "level": level,
        "xp_pct": xp_pct,
        "is_admin": is_admin(uid),
        "owned_cards": len(get_user_collection(uid)),
    }


@app.post("/api/reset")
def reset_session(uid: int = Depends(auth)):
    update_user(uid, coins=1000, total_wagered=0, total_won=0,
                win_streak=0, best_streak=0, last_daily=0)
    return {"ok": True, "coins": 1000}


class PlayReq(BaseModel):
    game: str
    bet: int
    choice: Optional[str] = None


@app.post("/api/play")
def play(req: PlayReq, uid: int = Depends(auth)):
    u = get_user(uid)
    now = int(time.time())
    if u["self_excluded_until"] > now:
        raise HTTPException(403, "Selbstausgeschlossen")
    if req.bet < 1:
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Coins")

    payout = 0
    result = {}

    if req.game == "coin":
        side, won = coin_outcome(u, req.choice or "")
        payout = int(req.bet * 1.9) if won else 0
        result = {"outcome": side, "won": won, "near_miss": False}
    elif req.game in ("dice", "bowl"):
        val = bowl_outcome(u)
        won = val == 6
        payout = req.bet * 5 if won else 0
        result = {"outcome": val, "won": won, "near_miss": (val == 5 and not won)}
    elif req.game == "slots":
        reels, mult = slots_outcome(u)
        payout = int(req.bet * mult) if mult else 0
        a, b, c = reels[0], reels[1], reels[2]
        nm = (a == b or b == c or a == c) and mult < 2
        result = {"outcome": reels, "won": payout > 0, "near_miss": nm, "mult": mult}
    else:
        raise HTTPException(400, "Unbekanntes Spiel")

    delta = payout - req.bet
    new_coins = max(0, u["coins"] + delta)
    update_user(uid, coins=new_coins,
                total_wagered=u["total_wagered"] + req.bet,
                total_won=u["total_won"] + payout)
    record_outcome(uid, payout > req.bet, req.game, payout)
    return {"result": result, "payout": payout, "delta": delta, "coins": new_coins}


@app.post("/api/daily")
def daily(uid: int = Depends(auth)):
    u = get_user(uid)
    now = int(time.time())
    if now - u["last_daily"] < 86400:
        return {"ok": False, "remaining": 86400 - (now - u["last_daily"])}
    new = u["coins"] + 1000
    update_user(uid, coins=new, last_daily=now)
    return {"ok": True, "amount": 1000, "coins": new}


class BonusReq(BaseModel):
    amount: int


@app.post("/api/bonus")
def bonus(req: BonusReq, uid: int = Depends(auth)):
    if not (1 <= req.amount <= 1000000):
        raise HTTPException(400, "Ungültiger Betrag")
    u = get_user(uid)
    new_coins = u["coins"] + req.amount
    update_user(uid, coins=new_coins)
    return {"ok": True, "amount": req.amount, "coins": new_coins}


class WagerReq(BaseModel):
    amount: int


@app.post("/api/wager")
def wager(req: WagerReq, uid: int = Depends(auth)):
    if req.amount < 1:
        raise HTTPException(400, "Ungültiger Betrag")
    u = get_user(uid)
    if req.amount > u["coins"]:
        raise HTTPException(400, "Nicht genug Coins")
    new_coins = u["coins"] - req.amount
    update_user(uid, coins=new_coins, total_wagered=u["total_wagered"] + req.amount)
    return {"ok": True, "coins": new_coins}


class PayoutReq(BaseModel):
    amount: int


@app.post("/api/payout")
def payout_endpoint(req: PayoutReq, uid: int = Depends(auth)):
    if not (0 <= req.amount <= 10000000):
        raise HTTPException(400, "Ungültiger Betrag")
    u = get_user(uid)
    new_coins = u["coins"] + req.amount
    update_user(uid, coins=new_coins, total_won=u["total_won"] + req.amount)
    if req.amount > 0:
        record_outcome(uid, True, "crash", req.amount)
    return {"ok": True, "coins": new_coins}


class ReferralReq(BaseModel):
    ref_id: int


@app.post("/api/referral")
def register_referral(req: ReferralReq, uid: int = Depends(auth)):
    ref_id = req.ref_id
    if ref_id == uid or ref_id <= 0:
        return {"ok": False, "reason": "invalid"}
    try:
        get_user(ref_id)
    except Exception:
        return {"ok": False, "reason": "no_referrer"}

    con = db()
    existing = con.execute("SELECT referrer_id FROM referrals WHERE referred_id=?", (uid,)).fetchone()
    if existing:
        con.close(); return {"ok": False, "reason": "already_referred"}
    con.execute("INSERT INTO referrals (referred_id, referrer_id, created_at) VALUES (?,?,?)",
                (uid, ref_id, int(time.time())))
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (ref_id,)).fetchone()[0]
    con.close()
    mark_db_dirty()

    reward = 500
    milestone = None
    if count in (1, 3, 5, 10, 25):
        milestone = count
        reward += {1: 500, 3: 2000, 5: 5000, 10: 15000, 25: 50000}[count]

    ref_u = get_user(ref_id)
    update_user(ref_id, coins=ref_u["coins"] + reward)

    welcome = 500
    new_u = get_user(uid)
    update_user(uid, coins=new_u["coins"] + welcome)

    return {"ok": True, "welcome_bonus": welcome, "referrer_reward": reward,
            "referrer_count": count, "milestone": milestone}


@app.get("/api/referrals")
def get_referrals(uid: int = Depends(auth)):
    con = db()
    count = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
    con.close()
    return {"count": count}


# ═══════════════════════════════════════════════════════════
# USER STATE
# ═══════════════════════════════════════════════════════════
class UserStateReq(BaseModel):
    data: dict


@app.get("/api/user_state")
def get_user_state(uid: int = Depends(auth)):
    con = db()
    row = con.execute("SELECT data FROM user_state WHERE user_id=?", (uid,)).fetchone()
    con.close()
    if not row:
        return {"data": {}}
    try:
        return {"data": json.loads(row["data"] or "{}")}
    except Exception:
        return {"data": {}}


@app.post("/api/user_state")
def set_user_state(req: UserStateReq, uid: int = Depends(auth)):
    con = db()
    row = con.execute("SELECT data FROM user_state WHERE user_id=?", (uid,)).fetchone()
    existing = {}
    if row:
        try: existing = json.loads(row["data"] or "{}")
        except Exception: existing = {}
    existing.update(req.data or {})
    payload = json.dumps(existing, separators=(",", ":"))
    con.execute(
        "INSERT INTO user_state (user_id, data, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (uid, payload, int(time.time())),
    )
    con.commit(); con.close()
    mark_db_dirty()
    return {"ok": True, "data": existing}


# ═══════════════════════════════════════════════════════════
# GLOBAL JACKPOT + CATALOG VERSION
# ═══════════════════════════════════════════════════════════
def _bump_jackpot(amount: int) -> int:
    con = db()
    row = con.execute("SELECT value FROM globals WHERE key='jackpot'").fetchone()
    cur = int(row["value"]) if row else 0
    new = cur + int(amount)
    con.execute(
        "INSERT INTO globals (key, value) VALUES ('jackpot', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (new,),
    )
    con.commit(); con.close()
    mark_db_dirty()
    return new


@app.get("/api/jackpot")
def get_jackpot():
    con = db()
    row = con.execute("SELECT value FROM globals WHERE key='jackpot'").fetchone()
    con.close()
    return {"jackpot": int(row["value"]) if row else 0}


@app.get("/api/catalog_version")
def get_catalog_version():
    return {"version": _catalog_version()}


# ═══════════════════════════════════════════════════════════
# BLACKJACK
# ═══════════════════════════════════════════════════════════
def new_deck():
    ranks = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
    suits = ["♠","♥","♦","♣"]
    deck = [{"r": r, "s": s} for r in ranks for s in suits]
    random.shuffle(deck)
    return deck


def hand_value(hand):
    total = 0; aces = 0
    for c in hand:
        r = c["r"]
        if r == "A": aces += 1; total += 11
        elif r in ("J","Q","K"): total += 10
        else: total += int(r)
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total


@app.post("/api/bj/start")
def bj_start(req: PlayReq, uid: int = Depends(auth)):
    u = get_user(uid)
    if u["self_excluded_until"] > int(time.time()):
        raise HTTPException(403, "Selbstausgeschlossen")
    if req.bet < 1:
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Coins")

    deck = new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    bj_sessions[uid] = {"deck": deck, "player": player, "dealer": dealer, "bet": req.bet}
    pv = hand_value(player); dv = hand_value(dealer)

    if pv == 21 and dv == 21:
        payout = req.bet; delta = 0; new_coins = u["coins"]
        update_user(uid, total_wagered=u["total_wagered"] + req.bet,
                    total_won=u["total_won"] + payout)
        del bj_sessions[uid]
        return {"player": player, "dealer": dealer, "player_value": pv, "dealer_value": dv,
                "state": "push", "payout": payout, "delta": delta, "coins": new_coins}
    elif pv == 21:
        payout = int(req.bet * 2.5); delta = payout - req.bet
        new_coins = max(0, u["coins"] + delta)
        update_user(uid, coins=new_coins,
                    total_wagered=u["total_wagered"] + req.bet,
                    total_won=u["total_won"] + payout)
        record_outcome(uid, True, "blackjack", payout)
        del bj_sessions[uid]
        return {"player": player, "dealer": dealer, "player_value": pv, "dealer_value": dv,
                "state": "blackjack", "payout": payout, "delta": delta, "coins": new_coins}

    return {"player": player, "dealer": [dealer[0], {"r": "?", "s": "?"}],
            "player_value": pv, "dealer_value": hand_value([dealer[0]]), "state": "continue"}


@app.post("/api/bj/hit")
def bj_hit(uid: int = Depends(auth)):
    s = bj_sessions.get(uid)
    if not s:
        raise HTTPException(400, "Keine Session")
    s["player"].append(s["deck"].pop())
    pv = hand_value(s["player"])
    if pv > 21:
        u = get_user(uid)
        new_coins = max(0, u["coins"] - s["bet"])
        update_user(uid, coins=new_coins, total_wagered=u["total_wagered"] + s["bet"])
        record_outcome(uid, False, "blackjack", 0)
        del bj_sessions[uid]
        return {"player": s["player"], "player_value": pv, "state": "bust", "coins": new_coins}
    return {"player": s["player"], "player_value": pv, "state": "continue"}


@app.post("/api/bj/stand")
def bj_stand(uid: int = Depends(auth)):
    s = bj_sessions.get(uid)
    if not s:
        raise HTTPException(400, "Keine Session")
    while hand_value(s["dealer"]) < 17:
        s["dealer"].append(s["deck"].pop())
    pv = hand_value(s["player"]); dv = hand_value(s["dealer"])
    u = get_user(uid)
    if dv > 21 or pv > dv: payout = s["bet"] * 2
    elif pv == dv: payout = s["bet"]
    else: payout = 0
    delta = payout - s["bet"]
    new_coins = max(0, u["coins"] + delta)
    update_user(uid, coins=new_coins,
                total_wagered=u["total_wagered"] + s["bet"],
                total_won=u["total_won"] + payout)
    record_outcome(uid, payout > s["bet"], "blackjack", payout)
    del bj_sessions[uid]
    return {"player": s["player"], "dealer": s["dealer"], "player_value": pv, "dealer_value": dv,
            "payout": payout, "delta": delta, "coins": new_coins,
            "state": "win" if payout > s["bet"] else "push" if payout == s["bet"] else "lose"}


# ═══════════════════════════════════════════════════════════
# ROULETTE
# ═══════════════════════════════════════════════════════════
class RouletteReq(BaseModel):
    bet: int
    bet_type: str
    number: Optional[int] = None


@app.post("/api/roulette")
def roulette(req: RouletteReq, uid: int = Depends(auth)):
    u = get_user(uid)
    if u["self_excluded_until"] > int(time.time()):
        raise HTTPException(403, "Selbstausgeschlossen")
    if req.bet < 1:
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Coins")

    n, color = roulette_spin(u, req.bet_type, req.number)
    payout = 0
    if req.bet_type == "single" and req.number == n: payout = req.bet * 36
    elif req.bet_type == "red" and color == "red": payout = req.bet * 2
    elif req.bet_type == "black" and color == "black": payout = req.bet * 2
    elif req.bet_type == "even" and n != 0 and n % 2 == 0: payout = req.bet * 2
    elif req.bet_type == "odd" and n % 2 == 1: payout = req.bet * 2
    elif req.bet_type == "1-18" and 1 <= n <= 18: payout = req.bet * 2
    elif req.bet_type == "19-36" and 19 <= n <= 36: payout = req.bet * 2

    delta = payout - req.bet
    new_coins = max(0, u["coins"] + delta)
    update_user(uid, coins=new_coins,
                total_wagered=u["total_wagered"] + req.bet,
                total_won=u["total_won"] + payout)
    record_outcome(uid, payout > req.bet, "roulette", payout)
    return {"number": n, "color": color, "payout": payout, "delta": delta, "coins": new_coins}


# ═══════════════════════════════════════════════════════════
# CRASH
# ═══════════════════════════════════════════════════════════
class CrashStartReq(BaseModel):
    bet: int


@app.post("/api/crash/start")
def crash_start(req: CrashStartReq, uid: int = Depends(auth)):
    u = get_user(uid)
    if u["self_excluded_until"] > int(time.time()):
        raise HTTPException(403, "Selbstausgeschlossen")
    if req.bet < 1:
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Coins")
    new_coins = u["coins"] - req.bet
    update_user(uid, coins=new_coins, total_wagered=u["total_wagered"] + req.bet)
    point = crash_multiplier(u)
    crash_sessions[uid] = {"bet": req.bet, "point": point, "cashed": False}
    return {"ok": True, "coins": new_coins, "crash_point": point}


class CrashCashoutReq(BaseModel):
    multiplier: float


@app.post("/api/crash/cashout")
def crash_cashout(req: CrashCashoutReq, uid: int = Depends(auth)):
    s = crash_sessions.get(uid)
    if not s or s.get("cashed"):
        raise HTTPException(400, "Keine Crash-Session")
    point = float(s["point"]); mult = float(req.multiplier)
    u = get_user(uid)

    if mult >= point:
        s["cashed"] = True
        record_outcome(uid, False, "crash", 0)
        del crash_sessions[uid]
        return {"ok": False, "crashed": True, "coins": u["coins"], "payout": 0}

    mult = max(1.0, min(mult, max(1.0, point - 0.01)))
    payout = int(s["bet"] * mult)
    new_coins = u["coins"] + payout
    update_user(uid, coins=new_coins, total_won=u["total_won"] + payout)
    record_outcome(uid, True, "crash", payout)
    s["cashed"] = True
    bet_val = s["bet"]
    del crash_sessions[uid]
    _bump_jackpot(int(bet_val * 0.05))
    return {"ok": True, "coins": new_coins, "payout": payout}


@app.post("/api/crash/loss")
def crash_loss(uid: int = Depends(auth)):
    s = crash_sessions.get(uid)
    if s:
        record_outcome(uid, False, "crash", 0)
        del crash_sessions[uid]
    u = get_user(uid)
    return {"ok": True, "coins": u["coins"]}


# ═══════════════════════════════════════════════════════════
# CARDS / COLLECTION
# ═══════════════════════════════════════════════════════════
CARD_PRICES = {"common": 400, "rare": 1200, "epic": 3500, "legendary": 9000}
VIP_PRICES  = {"common": 800, "rare": 2500, "epic": 6000, "legendary": 15000}
CARD_RARITY = {i: ("common" if i <= 18 else "rare" if i <= 26 else "epic" if i <= 29 else "legendary")
               for i in range(1, 31)}


class BuyCardReq(BaseModel):
    card_id: int


@app.post("/api/buy_card")
def buy_card(req: BuyCardReq, uid: int = Depends(auth)):
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
    info = get_card_vip_info(cid)
    if info["is_vip_card"]:
        raise HTTPException(400, "VIP-Karte – nur per PayPal freischaltbar")
    if user_has_card(uid, cid):
        raise HTTPException(400, "Karte bereits vorhanden")
    rarity = CARD_RARITY[cid]
    price = CARD_PRICES[rarity]
    u = get_user(uid)
    if u["coins"] < price:
        raise HTTPException(400, "Nicht genug Coins")
    new_coins = u["coins"] - price
    update_user(uid, coins=new_coins)
    grant_card(uid, cid)
    return {"ok": True, "card_id": cid, "price": price, "rarity": rarity, "coins": new_coins}


class UnlockVipReq(BaseModel):
    card_id: int


@app.post("/api/unlock_vip")
def unlock_vip(req: UnlockVipReq, uid: int = Depends(auth)):
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
    info = get_card_vip_info(cid)
    if not info["is_vip_card"]:
        raise HTTPException(400, "Diese Karte ist keine VIP-Karte")
    grant_card(uid, cid)
    con = db()
    row = con.execute("SELECT vip FROM user_cards WHERE user_id=? AND card_id=?", (uid, cid)).fetchone()
    if row and row["vip"]:
        con.close()
        return {"ok": True, "already": True, "card_id": cid,
                "image_url": info["image_url"],
                "creator_link": info["creator_link"] or None,
                "coins": get_user(uid)["coins"]}
    con.execute("UPDATE user_cards SET vip=1 WHERE user_id=? AND card_id=?", (uid, cid))
    con.commit(); con.close()
    mark_db_dirty()
    return {"ok": True, "card_id": cid,
            "image_url": info["image_url"],
            "creator_link": info["creator_link"] or None,
            "coins": get_user(uid)["coins"], "via": "paypal"}


@app.get("/api/media/{card_id}")
async def media_proxy(card_id: int):
    import urllib.request
    if card_id < 1 or card_id > 30:
        raise HTTPException(404, "Nicht gefunden")

    info = get_card_vip_info(card_id)

    # 1) Telegram-Storage hat Vorrang (falls hochgeladen)
    tg_fid = info.get("tg_file_id") or ""
    if tg_fid:
        data = await tg_get_document_content(tg_fid)
        if data:
            # MIME aus magic bytes
            mime = "image/jpeg"
            if data[:8] == b"\x89PNG\r\n\x1a\n": mime = "image/png"
            elif data[:6] in (b"GIF87a", b"GIF89a"): mime = "image/gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP": mime = "image/webp"
            return Response(
                content=data, media_type=mime,
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "Access-Control-Allow-Origin": "*",
                },
            )

    # 2) Lokale Datei (falls vorhanden)
    local = get_local_card_file(card_id)
    if local:
        ext = local.rsplit(".", 1)[-1].lower()
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif",
        }.get(ext, "image/jpeg")
        return FileResponse(
            local, media_type=mime,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # 3) Remote-URL aus DB durchreichen
    url = info.get("image_url") or ""
    if not url or url.startswith("/api/media/"):
        raise HTTPException(404, "Kein Bild konfiguriert")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 CasinoHuntBot/1.0", "Accept": "image/*,*/*"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type") or "image/jpeg"
            if not ctype.startswith("image/"): ctype = "image/jpeg"
            return Response(
                content=data, media_type=ctype,
                headers={
                    "Cache-Control": "public, max-age=1800",
                    "Access-Control-Allow-Origin": "*",
                },
            )
    except Exception as e:
        raise HTTPException(502, f"Bild konnte nicht geladen werden: {e}")


class GrantCardReq(BaseModel):
    card_id: int


@app.post("/api/grant_card")
def grant_card_api(req: GrantCardReq, uid: int = Depends(auth)):
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
    is_new = grant_card(uid, cid)
    return {"ok": True, "card_id": cid, "is_new": is_new}


class SyncCollectionReq(BaseModel):
    card_ids: list


@app.post("/api/collection/sync")
def sync_collection(req: SyncCollectionReq, uid: int = Depends(auth)):
    added = 0
    for cid in req.card_ids or []:
        try: cid = int(cid)
        except Exception: continue
        if grant_card(uid, cid): added += 1
    return {"ok": True, "added": added, "collection": get_user_collection(uid)}


@app.get("/api/collection")
def api_collection(uid: int = Depends(auth)):
    col = get_user_collection(uid)
    owned_ids = {c["card_id"] for c in col}
    adm = is_admin(uid)
    catalog = []
    for cid in range(1, 31):
        rarity = CARD_RARITY.get(cid, "common")
        info = get_card_vip_info(cid)
        owned = cid in owned_ids
        entry = next((c for c in col if c["card_id"] == cid), None)
        vip_unlocked = bool(entry and entry["vip"])
        catalog.append({
            "card_id": cid,
            "rarity": rarity,
            "owned": owned,
            "vip": vip_unlocked,
            "is_vip_card": info["is_vip_card"],
            "image_url": info["image_url"],
            "creator_link": info["creator_link"] if ((vip_unlocked or adm) and info["is_vip_card"]) else "",
            "paypal_link": info["paypal_link"] if info["is_vip_card"] else "",
            "buy_price": CARD_PRICES.get(rarity, 400),
            "vip_price": VIP_PRICES.get(rarity, 800),
        })
    return {
        "collection": col,
        "catalog": catalog,
        "owned_count": len(owned_ids),
        "total": 30,
        "is_admin": is_admin(uid),
        "version": _catalog_version(),
    }


# ═══════════════════════════════════════════════════════════
# ADMIN: Karten-Bild per URL setzen
# ═══════════════════════════════════════════════════════════
class AdminImageReq(BaseModel):
    card_id: int
    image_url: Optional[str] = ""
    creator_link: Optional[str] = None
    paypal_link: Optional[str] = None


def _remove_local_card_files(card_id: int):
    for e in ("png", "jpg", "jpeg", "webp", "gif"):
        p = os.path.join(CARDS_MEDIA_DIR, f"{card_id}.{e}")
        if os.path.isfile(p):
            try: os.remove(p)
            except Exception: pass


@app.post("/api/admin/card_image")
async def admin_card_image(req: AdminImageReq, uid: int = Depends(auth)):
    require_admin(uid)
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")

    url = (req.image_url or "").strip()
    if url:
        if url.startswith("//"): url = "https:" + url
        if not (url.startswith("https://") or url.startswith("http://")):
            raise HTTPException(400, "Bild-URL muss mit http(s):// beginnen")

    clink = (req.creator_link or "").strip()
    if clink and not (clink.startswith("https://t.me/") or clink.startswith("https://telegram.me/")):
        raise HTTPException(400, "creator_link muss https://t.me/... sein")

    plink = (req.paypal_link or "").strip()
    if plink and not plink.startswith("https://"):
        raise HTTPException(400, "paypal_link muss https:// sein")

    if url and not url.startswith("/api/media/"):
        _remove_local_card_files(cid)

    con = db()
    con.execute(
        "INSERT INTO card_media (card_id, image_url, creator_link, paypal_link, tg_file_id, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(card_id) DO UPDATE SET image_url=excluded.image_url, "
        "creator_link=excluded.creator_link, paypal_link=excluded.paypal_link, "
        "tg_file_id='', updated_at=excluded.updated_at",
        (cid, url, clink, plink, "", int(time.time())),
    )
    con.commit(); con.close()
    save_media_backup()
    bump_catalog_version()
    mark_db_dirty()
    asyncio.create_task(tg_save_db_backup(force=True))
    return {"ok": True, "card_id": cid, "is_vip_card": True,
            "image_url": url, "creator_link": clink, "paypal_link": plink,
            "version": _catalog_version()}


# ═══════════════════════════════════════════════════════════
# ADMIN: Karten-Bild als Datei hochladen (→ Telegram Storage)
# ═══════════════════════════════════════════════════════════
@app.post("/api/admin/card_image_upload")
async def admin_upload_image(
    card_id: int = Form(...),
    creator_link: str = Form(""),
    paypal_link: str = Form(""),
    file: UploadFile = File(...),
    uid: int = Depends(auth),
):
    require_admin(uid)
    if card_id not in CARD_RARITY:
        raise HTTPException(400, "Ungültige Karte")

    content = await file.read()
    if len(content) > 6 * 1024 * 1024:
        raise HTTPException(400, "Bild zu groß (max 6 MB)")
    if len(content) < 100:
        raise HTTPException(400, "Datei zu klein / leer")

    # Echte Format-Erkennung via Magic Bytes
    ext = None
    magic = content[:12]
    if magic[:8] == b"\x89PNG\r\n\x1a\n": ext = "png"
    elif magic[:3] == b"\xff\xd8\xff": ext = "jpg"
    elif magic[:6] in (b"GIF87a", b"GIF89a"): ext = "gif"
    elif magic[:4] == b"RIFF" and magic[8:12] == b"WEBP": ext = "webp"
    if not ext:
        raise HTTPException(400, "Kein gültiges Bild (PNG/JPG/GIF/WebP)")

    clink = (creator_link or "").strip()
    if clink and not (clink.startswith("https://t.me/") or clink.startswith("https://telegram.me/")):
        raise HTTPException(400, "creator_link muss https://t.me/... sein")
    plink = (paypal_link or "").strip()
    if plink and not plink.startswith("https://"):
        raise HTTPException(400, "paypal_link muss https:// sein")

    # Alte Dateien entfernen
    _remove_local_card_files(card_id)

    # 1) Upload zu Telegram-Storage (falls konfiguriert)
    tg_file_id = ""
    if STORAGE_CHAT_ID and BOT_INSTANCE is not None:
        ts = int(time.time())
        res = await tg_send_document(
            content, f"card-{card_id}-{ts}.{ext}",
            f"🃏 VIP Karte {card_id} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}"
        )
        if res:
            tg_file_id = res["file_id"]
            print(f"✅ VIP-Bild Karte {card_id} zu Telegram hochgeladen: {tg_file_id[:30]}…")
        else:
            print("⚠️ Telegram-Upload fehlgeschlagen – Fallback auf lokalen Speicher")
    else:
        print("ℹ️ STORAGE_CHAT_ID nicht gesetzt – lokaler Speicher")

    # 2) Fallback: lokal speichern (falls Telegram fehlschlägt oder nicht konfiguriert)
    local_path = None
    if not tg_file_id:
        local_path = os.path.join(CARDS_MEDIA_DIR, f"{card_id}.{ext}")
        with open(local_path, "wb") as f:
            f.write(content)

    ts = int(time.time())
    image_url = f"/api/media/{card_id}?v={ts}"

    con = db()
    con.execute(
        "INSERT INTO card_media (card_id, image_url, creator_link, paypal_link, tg_file_id, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(card_id) DO UPDATE SET image_url=excluded.image_url, "
        "creator_link=excluded.creator_link, paypal_link=excluded.paypal_link, "
        "tg_file_id=excluded.tg_file_id, updated_at=excluded.updated_at",
        (card_id, image_url, clink, plink, tg_file_id, ts),
    )
    con.commit(); con.close()
    save_media_backup()
    bump_catalog_version()
    mark_db_dirty()
    asyncio.create_task(tg_save_db_backup(force=True))

    return {
        "ok": True, "card_id": card_id,
        "image_url": image_url,
        "creator_link": clink, "paypal_link": plink,
        "storage": "telegram" if tg_file_id else "local",
        "version": _catalog_version(),
    }


@app.delete("/api/admin/card_image/{card_id}")
async def admin_delete_image(card_id: int, uid: int = Depends(auth)):
    require_admin(uid)
    if card_id < 1 or card_id > 30:
        raise HTTPException(400, "Ungültige Karte")
    _remove_local_card_files(card_id)
    con = db()
    con.execute("DELETE FROM card_media WHERE card_id=?", (card_id,))
    con.commit(); con.close()
    save_media_backup()
    bump_catalog_version()
    mark_db_dirty()
    asyncio.create_task(tg_save_db_backup(force=True))
    return {"ok": True, "card_id": card_id, "is_vip_card": False,
            "version": _catalog_version()}


# ═══════════════════════════════════════════════════════════
# ADMIN: manueller DB-Backup-Trigger
# ═══════════════════════════════════════════════════════════
@app.post("/api/admin/backup")
async def admin_backup_now(uid: int = Depends(auth)):
    require_admin(uid)
    if not STORAGE_CHAT_ID:
        raise HTTPException(400, "STORAGE_CHAT_ID nicht gesetzt")
    if BOT_INSTANCE is None:
        raise HTTPException(503, "Bot nicht bereit")
    ok = await tg_save_db_backup(force=True)
    if not ok:
        raise HTTPException(500, "Backup fehlgeschlagen – siehe Logs")
    return {"ok": True, "message": "DB-Backup in Telegram-Channel hochgeladen"}


@app.get("/api/admin/storage_status")
async def admin_storage_status(uid: int = Depends(auth)):
    require_admin(uid)
    return {
        "storage_chat_id": STORAGE_CHAT_ID or None,
        "bot_ready": BOT_INSTANCE is not None,
        "db_size_bytes": os.path.getsize(DB) if os.path.isfile(DB) else 0,
        "db_dirty": _db_dirty,
        "backup_interval_s": DB_BACKUP_INTERVAL,
    }


# ═══════════════════════════════════════════════════════════
# SHOP
# ═══════════════════════════════════════════════════════════
@app.get("/api/shop/packs")
def shop_packs(uid: int = Depends(auth)):
    return {"packs": [{"id": k, "coins": v["coins"], "stars": v["stars"], "title": v["title"]}
                      for k, v in SHOP_PACKS.items()]}


class ShopInvoiceReq(BaseModel):
    pack_id: str


@app.post("/api/shop/invoice")
async def shop_invoice(req: ShopInvoiceReq, uid: int = Depends(auth)):
    pack = SHOP_PACKS.get(req.pack_id)
    if not pack:
        raise HTTPException(400, "Unbekanntes Pack")
    if BOT_INSTANCE is None:
        raise HTTPException(503, "Bot nicht bereit")
    payload = f"stars:{uid}:{req.pack_id}:{int(time.time())}"
    if len(payload) > 128: payload = payload[:128]
    try:
        link = await BOT_INSTANCE.create_invoice_link(
            title=pack["title"],
            description=f"{pack['coins']} Spiel-Coins für Casino Hunt (kein Echtgeld-Gewinn).",
            payload=payload, currency="XTR",
            prices=[LabeledPrice(label=pack["title"], amount=pack["stars"])],
        )
    except Exception as e:
        raise HTTPException(500, f"Invoice-Fehler: {e}")
    return {"ok": True, "invoice_url": link, "pack_id": req.pack_id,
            "coins": pack["coins"], "stars": pack["stars"]}


@app.get("/api/leaderboard")
def leaderboard(uid: int = Depends(auth)):
    con = db()
    rows = con.execute("SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10").fetchall()
    con.close()
    entries = [{"user_id": r["user_id"], "coins": r["coins"], "is_me": r["user_id"] == uid} for r in rows]
    idx = next((i for i, e in enumerate(entries) if e["is_me"]), None)
    return {"entries": entries, "my_rank": idx + 1 if idx is not None else None}


@app.get("/api/feed")
def feed(uid: int = Depends(auth)):
    con = db()
    rows = con.execute("SELECT user_id, game, amount FROM wins ORDER BY id DESC LIMIT 12").fetchall()
    con.close()
    return {"entries": [{"user_id": r["user_id"], "game": r["game"],
                         "amount": r["amount"], "is_me": r["user_id"] == uid} for r in rows]}


@app.get("/api/admin/stats")
def admin_stats(uid: int = Depends(auth)):
    require_admin(uid)
    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    coins = con.execute("SELECT COALESCE(SUM(coins),0) FROM users").fetchone()[0]
    wag = con.execute("SELECT COALESCE(SUM(total_wagered),0) FROM users").fetchone()[0]
    won = con.execute("SELECT COALESCE(SUM(total_won),0) FROM users").fetchone()[0]
    con.close()
    return {"users": users, "coins_in_circulation": coins, "total_wagered": wag,
            "total_won": won, "house_edge": wag - won}


# ═══════════════════════════════════════════════════════════
# TELEGRAM BOT
# ═══════════════════════════════════════════════════════════
WELCOME = (
    "🎰 <b>Virtual Casino</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Das beste Casino auf Telegram.\n"
    "Tippe den Button unten, um zu starten.\n\n"
    "⚠️ VORSICHT · 18+"
)


def bot_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎮 Casino öffnen", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=bot_kb(),
                                    parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎰 <b>Virtual Casino</b>\nAlle Spiele laufen in der Mini App.\n⚠️ Kein Echtgeld · 18+",
        parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Zeigt die Chat-ID – funktioniert im Channel und bei Weiterleitung."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg:
        return
    lines = []
    if msg.forward_from_chat:
        fc = msg.forward_from_chat
        lines.append(f"📨 Weitergeleitet von: <b>{fc.title or fc.id}</b>")
        lines.append(f"📌 Typ: <code>{fc.type}</code>")
        lines.append(f"🆔 Channel-ID: <code>{fc.id}</code>")
    else:
        lines.append(f"📌 Chat: <b>{chat.title or chat.full_name or chat.id}</b>")
        lines.append(f"📌 Typ: <code>{chat.type}</code>")
        lines.append(f"🆔 ID: <code>{chat.id}</code>")
    lines.append("")
    lines.append("💡 Diese ID als <code>STORAGE_CHAT_ID</code> in Render eintragen.")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manueller DB-Backup-Trigger – nur für Admins."""
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in ADMIN_IDS:
        await update.message.reply_text("⛔ Kein Admin")
        return
    if not STORAGE_CHAT_ID:
        await update.message.reply_text("❌ STORAGE_CHAT_ID nicht gesetzt")
        return
    await update.message.reply_text("⏳ Backup läuft…")
    ok = await tg_save_db_backup(force=True)
    if ok:
        await update.message.reply_text("✅ DB-Backup in Storage-Channel hochgeladen")
    else:
        await update.message.reply_text("❌ Backup fehlgeschlagen – siehe Logs")


async def on_precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    try:
        await q.answer(ok=True)
    except Exception as e:
        print(f"precheckout error: {e}")
        try: await q.answer(ok=False, error_message="Zahlung abgelehnt")
        except Exception: pass


async def on_successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.successful_payment: return
    sp = msg.successful_payment
    payload = sp.invoice_payload or ""
    parts = payload.split(":")
    if len(parts) < 3 or parts[0] != "stars": return
    try:
        uid = int(parts[1]); pack_id = parts[2]
    except Exception: return
    pack = SHOP_PACKS.get(pack_id)
    if not pack: return
    u = get_user(uid)
    update_user(uid, coins=u["coins"] + pack["coins"])
    print(f"Stars payment OK uid={uid} +{pack['coins']}")
    try:
        await msg.reply_text(f"✅ +{pack['coins']} Coins gutgeschrieben!")
    except Exception: pass


async def post_init(bot_app):
    await bot_app.bot.set_my_commands(
        [
            BotCommand("start", "Casino öffnen"),
            BotCommand("help", "Hilfe"),
            BotCommand("chatid", "Chat-ID anzeigen"),
            BotCommand("backup", "DB-Backup (Admin)"),
        ],
        scope=BotCommandScopeDefault(),
    )
    try:
        await bot_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎮 Casino", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception as e:
        print(f"Menu button: {e}")


async def main():
    global BOT_INSTANCE
    enable_polling = os.environ.get("ENABLE_BOT_POLLING", "1").strip() not in ("0","false","False","no")
    bot_app = None

    if enable_polling:
        try:
            bot_app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
            bot_app.add_handler(CommandHandler("start", cmd_start))
            bot_app.add_handler(CommandHandler("help", cmd_help))
            bot_app.add_handler(CommandHandler("chatid", cmd_chatid))
            bot_app.add_handler(CommandHandler("backup", cmd_backup))
            bot_app.add_handler(PreCheckoutQueryHandler(on_precheckout))
            bot_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
            await bot_app.initialize()
            BOT_INSTANCE = bot_app.bot
            try:
                await bot_app.bot.delete_webhook(drop_pending_updates=True)
                print("Webhook gelöscht")
            except Exception as e:
                print(f"delete_webhook: {e}")
            await bot_app.start()
            try:
                await bot_app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                )
                print("Bot-Polling aktiv")
            except Exception as e:
                print(f"Polling nicht gestartet ({e})")
        except Exception as e:
            print(f"Bot-Init übersprungen: {e}")
            bot_app = None
    else:
        print("ENABLE_BOT_POLLING=0 → nur API")

    # ── Telegram-Storage: DB bei Bedarf wiederherstellen ──
    if BOT_INSTANCE is not None and STORAGE_CHAT_ID:
        print(f"Telegram-Storage aktiv: chat_id={STORAGE_CHAT_ID}")
        # Kleiner Delay, damit Bot die API-Verbindung aufgebaut hat
        await asyncio.sleep(1.0)
        try:
            await tg_restore_db()
        except Exception as e:
            print(f"tg_restore_db: {e}")
        # Backup-Loop starten
        asyncio.create_task(db_backup_loop())
    elif STORAGE_CHAT_ID and BOT_INSTANCE is None:
        print("⚠️ STORAGE_CHAT_ID gesetzt, aber Bot nicht bereit – Storage deaktiviert")
    else:
        print("ℹ️ STORAGE_CHAT_ID nicht gesetzt – keine Telegram-Persistenz")

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Server startet auf Port {PORT}")
    print(f"WebApp URL: {WEBAPP_URL}")
    print(f"DATA_DIR: {_DATA_DIR}")
    print(f"STORAGE: {'Telegram (' + STORAGE_CHAT_ID + ')' if STORAGE_CHAT_ID else 'lokal (flüchtig)'}")

    try:
        await server.serve()
    finally:
        # Letztes Backup vor dem Beenden
        if BOT_INSTANCE and STORAGE_CHAT_ID:
            try:
                print("Beende – letztes DB-Backup…")
                await tg_save_db_backup(force=True)
            except Exception as e:
                print(f"final backup: {e}")
        if bot_app is not None:
            try:
                if bot_app.updater.running: await bot_app.updater.stop()
            except Exception: pass
            try:
                await bot_app.stop(); await bot_app.shutdown()
            except Exception: pass


if __name__ == "__main__":
    asyncio.run(main())
