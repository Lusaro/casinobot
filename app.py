import os
import time
import random
import hashlib
import hmac
import json
import asyncio
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, MenuButtonWebApp, BotCommand, BotCommandScopeDefault,
    LinkPreviewOptions,
)
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ============================================================
# KONFIGURATION – AUS RENDER ENVIRONMENT VARIABLES
# ============================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
PORT = int(os.environ.get("PORT", 10000))
DB = "/tmp/casino.db"
AUTH_MAX_AGE = 86400
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ============================================================
# DATENBANK
# ============================================================
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
        user_id INTEGER,
        game TEXT,
        amount INTEGER,
        created_at INTEGER
    );
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    for col, ddl in [("win_streak", "INTEGER DEFAULT 0"), ("best_streak", "INTEGER DEFAULT 0")]:
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except Exception:
                pass
    con.commit()
    con.close()

def get_user(uid):
    con = db()
    row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        con.execute("INSERT INTO users (user_id, created_at) VALUES (?, ?)",
                    (uid, int(time.time())))
        con.commit()
        row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return dict(row)

def update_user(uid, **f):
    if not f:
        return
    con = db()
    clause = ", ".join(f"{k}=?" for k in f)
    con.execute(f"UPDATE users SET {clause} WHERE user_id=?",
                list(f.values()) + [uid])
    con.commit()
    con.close()

def is_admin(uid):
    con = db()
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)

def calc_level(wagered: int):
    level = 1 + int((max(0, wagered) / 500) ** 0.5)
    cur = 500 * (level - 1) ** 2
    nxt = 500 * level ** 2
    pct = int(((wagered - cur) / max(1, nxt - cur)) * 100) if nxt > cur else 0
    return level, max(0, min(100, pct))

def record_outcome(uid: int, won: bool, game: str, win_amount: int):
    u = get_user(uid)
    if won:
        s = u["win_streak"] + 1
        b = max(u["best_streak"], s)
        update_user(uid, win_streak=s, best_streak=b)
    else:
        update_user(uid, win_streak=0)
    if win_amount >= 300:
        con = db()
        con.execute(
            "INSERT INTO wins (user_id, game, amount, created_at) VALUES (?,?,?,?)",
            (uid, game, win_amount, int(time.time())),
        )
        con.commit()
        con.close()

# ============================================================
# TELEGRAM AUTH
# ============================================================
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
    auth_date = int(parsed.get("auth_date", "0"))
    if time.time() - auth_date > AUTH_MAX_AGE:
        raise HTTPException(401, "initData abgelaufen")
    user = json.loads(parsed.get("user", "{}"))
    if not user.get("id"):
        raise HTTPException(401, "User fehlt")
    return user

def auth(x_init_data: str = Header(None, alias="X-Init-Data")):
    user = validate_init_data(x_init_data or "")
    return user["id"]

# ============================================================
# FASTAPI
# ============================================================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def root():
    return "<h1>Casino backend läuft.</h1>"

@app.get("/api/me")
def me(uid: int = Depends(auth)):
    u = get_user(uid)
    level, xp_pct = calc_level(u["total_wagered"])
    return {
        "coins": u["coins"], "max_bet": u["max_bet"],
        "wagered": u["total_wagered"], "won": u["total_won"],
        "excluded_until": u["self_excluded_until"],
        "streak": u["win_streak"], "best_streak": u["best_streak"],
        "level": level, "xp_pct": xp_pct,
    }

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
    if not (1 <= req.bet <= u["max_bet"]):
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Münzen")

    payout = 0
    result = {}
    if req.game == "coin":
        side = random.choice(["kopf", "zahl"])
        won = side == (req.choice or "")
        payout = int(req.bet * 1.9) if won else 0
        result = {"outcome": side, "won": won}
    elif req.game in ("dice", "bowl"):
        val = random.randint(1, 6)
        won = val == 6
        payout = req.bet * 5 if won else 0
        result = {"outcome": val, "won": won}
    elif req.game == "slots":
        reels = [random.choice(["🍒", "🍋", "🔔", "💎", "7️⃣"]) for _ in range(3)]
        if reels[0] == reels[1] == reels[2]:
            mult = 20 if reels[0] == "7️⃣" else 10 if reels[0] == "💎" else 5
            payout = req.bet * mult
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            payout = int(req.bet * 1.5)
        result = {"outcome": reels, "won": payout > 0}
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

class LimitReq(BaseModel):
    max_bet: int

@app.post("/api/limit")
def set_limit(req: LimitReq, uid: int = Depends(auth)):
    if not (1 <= req.max_bet <= 1000):
        raise HTTPException(400, "1–1000")
    update_user(uid, max_bet=req.max_bet)
    return {"ok": True, "max_bet": req.max_bet}

class ExcludeReq(BaseModel):
    hours: int

@app.post("/api/exclude")
def set_exclude(req: ExcludeReq, uid: int = Depends(auth)):
    if not (1 <= req.hours <= 720):
        raise HTTPException(400, "1–720")
    update_user(uid, self_excluded_until=int(time.time()) + req.hours * 3600)
    return {"ok": True}

# ============================================================
# BLACKJACK
# ============================================================
bj_sessions = {}

def new_deck():
    ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    suits = ["♠", "♥", "♦", "♣"]
    deck = [{"r": r, "s": s} for r in ranks for s in suits]
    random.shuffle(deck)
    return deck

def hand_value(hand):
    total = 0
    aces = 0
    for c in hand:
        r = c["r"]
        if r == "A":
            aces += 1
            total += 11
        elif r in ("J", "Q", "K"):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

@app.post("/api/bj/start")
def bj_start(req: PlayReq, uid: int = Depends(auth)):
    u = get_user(uid)
    if u["self_excluded_until"] > int(time.time()):
        raise HTTPException(403, "Selbstausgeschlossen")
    if not (1 <= req.bet <= u["max_bet"]):
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Münzen")
    deck = new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    bj_sessions[uid] = {"deck": deck, "player": player,
                        "dealer": dealer, "bet": req.bet}
    pv = hand_value(player)
    dv = hand_value(dealer)
    if pv == 21 and dv == 21:
        state = "push"
    elif pv == 21:
        state = "blackjack"
    else:
        state = "continue"
    return {"player": player,
            "dealer": [dealer[0], {"r": "?", "s": "?"}],
            "player_value": pv,
            "dealer_value": hand_value([dealer[0]]),
            "state": state}

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
        update_user(uid, coins=new_coins,
                    total_wagered=u["total_wagered"] + s["bet"])
        record_outcome(uid, False, "blackjack", 0)
        del bj_sessions[uid]
        return {"player": s["player"], "player_value": pv,
                "state": "bust", "coins": new_coins}
    return {"player": s["player"], "player_value": pv, "state": "continue"}

@app.post("/api/bj/stand")
def bj_stand(uid: int = Depends(auth)):
    s = bj_sessions.get(uid)
    if not s:
        raise HTTPException(400, "Keine Session")
    while hand_value(s["dealer"]) < 17:
        s["dealer"].append(s["deck"].pop())
    pv = hand_value(s["player"])
    dv = hand_value(s["dealer"])
    u = get_user(uid)
    if dv > 21 or pv > dv:
        payout = s["bet"] * 2
    elif pv == dv:
        payout = s["bet"]
    else:
        payout = 0
    delta = payout - s["bet"]
    new_coins = max(0, u["coins"] + delta)
    update_user(uid, coins=new_coins,
                total_wagered=u["total_wagered"] + s["bet"],
                total_won=u["total_won"] + payout)
    record_outcome(uid, payout > s["bet"], "blackjack", payout)
    del bj_sessions[uid]
    return {"player": s["player"], "dealer": s["dealer"],
            "player_value": pv, "dealer_value": dv,
            "payout": payout, "delta": delta, "coins": new_coins,
            "state": "win" if payout > s["bet"] else "push" if payout == s["bet"] else "lose"}

# ============================================================
# ROULETTE
# ============================================================
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

class RouletteReq(BaseModel):
    bet: int
    bet_type: str
    number: Optional[int] = None

@app.post("/api/roulette")
def roulette(req: RouletteReq, uid: int = Depends(auth)):
    u = get_user(uid)
    if u["self_excluded_until"] > int(time.time()):
        raise HTTPException(403, "Selbstausgeschlossen")
    if not (1 <= req.bet <= u["max_bet"]):
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Münzen")

    n = random.randint(0, 36)
    color = "green" if n == 0 else "red" if n in RED else "black"
    payout = 0
    if req.bet_type == "single" and req.number == n:
        payout = req.bet * 36
    elif req.bet_type == "red" and color == "red":
        payout = req.bet * 2
    elif req.bet_type == "black" and color == "black":
        payout = req.bet * 2
    elif req.bet_type == "even" and n != 0 and n % 2 == 0:
        payout = req.bet * 2
    elif req.bet_type == "odd" and n % 2 == 1:
        payout = req.bet * 2
    elif req.bet_type == "1-18" and 1 <= n <= 18:
        payout = req.bet * 2
    elif req.bet_type == "19-36" and 19 <= n <= 36:
        payout = req.bet * 2

    delta = payout - req.bet
    new_coins = max(0, u["coins"] + delta)
    update_user(uid, coins=new_coins,
                total_wagered=u["total_wagered"] + req.bet,
                total_won=u["total_won"] + payout)
    record_outcome(uid, payout > req.bet, "roulette", payout)
    return {"number": n, "color": color, "payout": payout,
            "delta": delta, "coins": new_coins}

# ============================================================
# LEADERBOARD / FEED / ADMIN
# ============================================================
@app.get("/api/leaderboard")
def leaderboard(uid: int = Depends(auth)):
    con = db()
    rows = con.execute(
        "SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10"
    ).fetchall()
    con.close()
    entries = [{"user_id": r["user_id"], "coins": r["coins"],
                "is_me": r["user_id"] == uid} for r in rows]
    idx = next((i for i, e in enumerate(entries) if e["is_me"]), None)
    return {"entries": entries, "my_rank": idx + 1 if idx is not None else None}

@app.get("/api/feed")
def feed(uid: int = Depends(auth)):
    con = db()
    rows = con.execute(
        "SELECT user_id, game, amount FROM wins ORDER BY id DESC LIMIT 12"
    ).fetchall()
    con.close()
    return {"entries": [
        {"user_id": r["user_id"], "game": r["game"], "amount": r["amount"],
         "is_me": r["user_id"] == uid}
        for r in rows
    ]}

def require_admin(uid):
    if not is_admin(uid):
        raise HTTPException(403, "Kein Admin")

@app.get("/api/admin/stats")
def admin_stats(uid: int = Depends(auth)):
    require_admin(uid)
    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    coins = con.execute("SELECT COALESCE(SUM(coins),0) FROM users").fetchone()[0]
    wag = con.execute("SELECT COALESCE(SUM(total_wagered),0) FROM users").fetchone()[0]
    won = con.execute("SELECT COALESCE(SUM(total_won),0) FROM users").fetchone()[0]
    con.close()
    return {"users": users, "coins_in_circulation": coins,
            "total_wagered": wag, "total_won": won,
            "house_edge": wag - won}

# ============================================================
# BOT
# ============================================================
WELCOME = (
    "🎰 <b>Virtual Casino</b>\n"
    "<i>Nur Spielgeld · keine Einzahlungen · keine Käufe</i>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Das gesamte Casino läuft in der Mini App.\n"
    "Tippe den Button unten, um zu starten.\n\n"
    "⚠️ Kein Echtgeld · 18+"
)

def bot_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Casino öffnen",
                             web_app=WebAppInfo(url=WEBAPP_URL))
    ]])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=bot_kb(),
                                    parse_mode=ParseMode.HTML,
                                    link_preview_options=NO_PREVIEW)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎰 <b>Virtual Casino</b>\nAlle Spiele laufen in der Mini App.\n"
        "⚠️ Kein Echtgeld · 18+",
        parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

async def post_init(bot_app):
    await bot_app.bot.set_my_commands([
        BotCommand("start", "Casino öffnen"),
        BotCommand("help", "Hilfe"),
    ], scope=BotCommandScopeDefault())
    try:
        await bot_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎮 Casino",
                                         web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        print(f"Menu button konnte nicht gesetzt werden: {e}")

# ============================================================
# START
# ============================================================
async def main():
    bot_app = (ApplicationBuilder()
               .token(BOT_TOKEN)
               .post_init(post_init)
               .build())
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Server startet auf Port {PORT}")
    print(f"WebApp URL: {WEBAPP_URL}")
    await server.serve()

    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
