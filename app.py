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

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
PORT = int(os.environ.get("PORT", 10000))
DB = "/tmp/casino.db"
AUTH_MAX_AGE = 86400
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ═══════════════════════════════════════════════════════════
# ADMIN-IDs (werden bei jedem Start automatisch eingetragen)
# ═══════════════════════════════════════════════════════════
ADMIN_IDS = [6098272278]


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
    CREATE TABLE IF NOT EXISTS referrals (
        referred_id INTEGER PRIMARY KEY,
        referrer_id INTEGER,
        created_at INTEGER
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
        image_url TEXT NOT NULL,
        updated_at INTEGER DEFAULT 0
    );
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    for col, ddl in [("win_streak", "INTEGER DEFAULT 0"), ("best_streak", "INTEGER DEFAULT 0")]:
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except Exception:
                pass

    # ── Admins dauerhaft eintragen ──
    for aid in ADMIN_IDS:
        try:
            con.execute(
                "INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)",
                (int(aid), int(time.time())),
            )
        except Exception:
            pass

    con.commit()
    con.close()


def user_has_card(uid: int, card_id: int) -> bool:
    con = db()
    row = con.execute(
        "SELECT 1 FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id)
    ).fetchone()
    con.close()
    return bool(row)


def grant_card(uid: int, card_id: int) -> bool:
    """Gibt Karte, returns True wenn neu."""
    if card_id < 1 or card_id > 30:
        return False
    con = db()
    existing = con.execute(
        "SELECT 1 FROM user_cards WHERE user_id=? AND card_id=?", (uid, card_id)
    ).fetchone()
    if existing:
        con.close()
        return False
    con.execute(
        "INSERT INTO user_cards (user_id, card_id, vip, acquired_at) VALUES (?,?,0,?)",
        (uid, card_id, int(time.time())),
    )
    con.commit()
    con.close()
    return True


def get_user_collection(uid: int) -> list:
    con = db()
    rows = con.execute(
        "SELECT card_id, vip FROM user_cards WHERE user_id=? ORDER BY card_id",
        (uid,),
    ).fetchall()
    media = {
        r["card_id"]: r["image_url"]
        for r in con.execute("SELECT card_id, image_url FROM card_media").fetchall()
    }
    con.close()
    out = []
    for r in rows:
        cid = r["card_id"]
        out.append({
            "card_id": cid,
            "vip": bool(r["vip"]),
            "image_url": media.get(cid) if r["vip"] else None,
            "has_vip_media": cid in media,
        })
    return out


def get_all_media() -> dict:
    con = db()
    rows = con.execute("SELECT card_id, image_url FROM card_media").fetchall()
    con.close()
    return {r["card_id"]: r["image_url"] for r in rows}


def get_user(uid):
    con = db()
    row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        con.execute(
            "INSERT INTO users (user_id, created_at) VALUES (?, ?)",
            (uid, int(time.time())),
        )
        con.commit()
        row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return dict(row)


def update_user(uid, **f):
    if not f:
        return
    con = db()
    clause = ", ".join(f"{k}=?" for k in f)
    con.execute(
        f"UPDATE users SET {clause} WHERE user_id=?",
        list(f.values()) + [uid],
    )
    con.commit()
    con.close()


def is_admin(uid):
    con = db()
    row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return bool(row)


def calc_level(wagered: int):
    W = max(0, wagered)
    level = 1 + int((W / 100) ** 0.6)

    def thresh(l):
        return int(100 * (l - 1) ** 1.667)

    cur = thresh(level)
    nxt = thresh(level + 1)
    pct = int(((W - cur) / max(1, nxt - cur)) * 100) if nxt > cur else 0
    return level, max(0, min(100, pct))



# ═══════════════════════════════════════════════════════════
# HOUSE-EDGE / WARN-APP ENGINE
# Ziel: Spieler fühlt sich oft als Gewinner, verliert aber langfristig.
# Ziel-RTP ca. 82–88 % (House Edge 12–18 %).
# ═══════════════════════════════════════════════════════════

HOUSE_EDGE = 0.15          # 15 % Bankvorteil im Schnitt
TARGET_RTP = 1.0 - HOUSE_EDGE  # 0.85

RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

def player_heat(u: dict) -> float:
    """
    0.0 = kalt (viele Verluste) → etwas großzügiger
    1.0 = heiß (viele Gewinne / hohe Streak) → härter
    """
    streak = max(0, u.get("win_streak") or 0)
    wagered = max(1, u.get("total_wagered") or 1)
    won = max(0, u.get("total_won") or 0)
    ratio = won / wagered  # >1 = im Plus
    heat = 0.0
    heat += min(0.5, streak * 0.08)          # Streak drückt
    heat += min(0.5, max(0.0, (ratio - 0.7) * 1.2))  # wenn RTP des Spielers hoch
    return max(0.0, min(1.0, heat))


def effective_rtp(u: dict) -> float:
    """Dynamische RTP: bei Hitze runter, bei Kälte etwas rauf (aber nie fair)."""
    h = player_heat(u)
    # Basis 0.85, bei Hitze bis ~0.72, bei Kälte bis ~0.90
    return TARGET_RTP - h * 0.13 + (1.0 - h) * 0.05


def biased_random(u: dict) -> float:
    """Zufallszahl 0..1, bei Hitze eher höhere Werte (schlechter für Spieler bei 1/x Formeln)."""
    r = random.random()
    h = player_heat(u)
    if h > 0.3 and random.random() < h * 0.55:
        # Pull toward higher values (worse for player in inverse formulas)
        r = 0.55 + r * 0.45
    return r


def decide_win(u: dict, fair_p: float) -> bool:
    """
    fair_p = faire Gewinnwahrscheinlichkeit.
    Wir skalieren mit effective_rtp / erwartetem Fair-Return.
    """
    rtp = effective_rtp(u)
    # grobe Skalierung der Win-Chance
    p = fair_p * (rtp / 0.95)  # 0.95 ≈ typische "fast faire" Basis
    p = max(0.05, min(0.48, p))
    return random.random() < p


def crash_multiplier(u: dict) -> float:
    """
    Crash-Punkt mit House Edge.
    Klassische Formel: crash = max(1.0, edge / (1 - r))
    Mit edge < 1 entsteht House Edge.
    Near-Miss: oft zwischen 1.01–1.20 crashen lassen.
    """
    r = biased_random(u)
    edge = 0.92 - player_heat(u) * 0.08  # 0.84–0.92
    if r < 0.04:  # Instant crash ~4 %
        return 1.00
    mult = max(1.01, edge / (1.0 - r))
    mult = min(mult, 100.0)
    # Near-miss Cluster: häufiger im 1.05–1.35 Bereich
    if mult > 2.5 and random.random() < 0.35 + player_heat(u) * 0.25:
        mult = 1.05 + random.random() * 0.30
    return round(mult, 2)


def slots_outcome(u: dict):
    """
    Viele Near-Misses (2 gleich), seltene echte Hits.
    Erwarteter RTP ~83–87 %.
    """
    symbols = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
    weights_base = [32, 28, 22, 12, 6]  # 7️⃣ selten

    heat = player_heat(u)
    # Bei Hitze noch seltener Jackpot-Symbole
    weights = list(weights_base)
    weights[3] = max(4, int(12 - heat * 6))
    weights[4] = max(2, int(6 - heat * 3))

    def pick():
        total = sum(weights)
        x = random.randrange(total)
        acc = 0
        for s, w in zip(symbols, weights):
            acc += w
            if x < acc:
                return s
        return symbols[0]

    a, b, c = pick(), pick(), pick()

    # Near-miss: oft 2 gleiche, drittes knapp daneben
    if a == b and c != a and random.random() < 0.45:
        pass  # already near miss
    elif random.random() < 0.22:  # force near-miss look
        a = b = pick()
        # third different
        others = [s for s in symbols if s != a]
        c = random.choice(others)

    if a == b == c:
        if a == "7️⃣":
            mult = 15  # war 20 – leicht gesenkt
        elif a == "💎":
            mult = 8
        else:
            mult = 4
        return [a, b, c], mult
    if a == b or b == c or a == c:
        return [a, b, c], 1.2  # kleine "Tröst"-Auszahlung, unter Break-even bei Häufigkeit
    return [a, b, c], 0


def coin_outcome(u: dict, choice: str):
    """Münzwurf mit House Edge. Multi 1.9 → bei p≈0.42 RTP ≈ 0.80."""
    choice = (choice or "").lower()
    want_win = decide_win(u, 0.42)
    if want_win and choice in ("kopf", "zahl"):
        side = choice
        won = True
    else:
        if choice in ("kopf", "zahl"):
            side = "zahl" if choice == "kopf" else "kopf"
        else:
            side = random.choice(["kopf", "zahl"])
        won = False
    return side, won


def bowl_outcome(u: dict):
    """
    Wert 1–6. Strike (6) zahlt 5x.
    Faire RTP bei p=1/6 *5 ≈ 0.83.
    Wir steuern p(Strike) und streuen Near-Miss (Wert 5).
    """
    rtp = effective_rtp(u)
    # Ziel: p_strike * 5 ≈ rtp  → p ≈ rtp/5
    p_strike = max(0.08, min(0.18, rtp / 5.0))
    r = random.random()
    if r < p_strike:
        return 6
    if r < p_strike + 0.18:  # Near miss: 5er
        return 5
    return random.randint(1, 4)


def roulette_spin(u: dict, bet_type: str, number=None):
    """
    Europäische Roulette-Zahlen 0–36, aber mit leicht verzerrter Verteilung
    gegen die Wette des Spielers (House Edge spürbar höher als 2.7 %).
    """
    heat = player_heat(u)
    # Mit steigender Hitze: höhere Chance, dass die Wette verliert
    force_lose = random.random() < (0.08 + heat * 0.12)

    n = random.randint(0, 36)
    if force_lose:
        # Wähle eine Zahl die die Wette nicht trifft
        for _ in range(40):
            n = random.randint(0, 36)
            color = "green" if n == 0 else "red" if n in RED else "black"
            hit = False
            if bet_type == "single" and number == n:
                hit = True
            elif bet_type == "red" and color == "red":
                hit = True
            elif bet_type == "black" and color == "black":
                hit = True
            elif bet_type == "even" and n != 0 and n % 2 == 0:
                hit = True
            elif bet_type == "odd" and n % 2 == 1:
                hit = True
            elif bet_type == "1-18" and 1 <= n <= 18:
                hit = True
            elif bet_type == "19-36" and 19 <= n <= 36:
                hit = True
            if not hit:
                break
    color = "green" if n == 0 else "red" if n in RED else "black"
    return n, color



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


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        "max_bet": u["max_bet"],
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
    update_user(
        uid,
        coins=1000,
        total_wagered=0,
        total_won=0,
        win_streak=0,
        best_streak=0,
        last_daily=0,
    )
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
    if not (1 <= req.bet <= u["max_bet"]):
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Münzen")

    payout = 0
    result = {}

    if req.game == "coin":
        side, won = coin_outcome(u, req.choice or "")
        payout = int(req.bet * 1.9) if won else 0
        result = {"outcome": side, "won": won}
    elif req.game in ("dice", "bowl"):
        val = bowl_outcome(u)
        won = val == 6
        payout = req.bet * 5 if won else 0
        result = {"outcome": val, "won": won}
    elif req.game == "slots":
        reels, mult = slots_outcome(u)
        payout = int(req.bet * mult) if mult else 0
        result = {"outcome": reels, "won": payout > 0}
    else:
        raise HTTPException(400, "Unbekanntes Spiel")

    delta = payout - req.bet
    new_coins = max(0, u["coins"] + delta)
    update_user(
        uid,
        coins=new_coins,
        total_wagered=u["total_wagered"] + req.bet,
        total_won=u["total_won"] + payout,
    )
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
    if not (1 <= req.amount <= 100000):
        raise HTTPException(400, "Ungültiger Betrag")
    u = get_user(uid)
    if req.amount > u["coins"]:
        raise HTTPException(400, "Nicht genug Coins")
    new_coins = u["coins"] - req.amount
    update_user(
        uid,
        coins=new_coins,
        total_wagered=u["total_wagered"] + req.amount,
    )
    return {"ok": True, "coins": new_coins}


class PayoutReq(BaseModel):
    amount: int


@app.post("/api/payout")
def payout_endpoint(req: PayoutReq, uid: int = Depends(auth)):
    """Gutschrift nach bereits abgezogenem Einsatz (z.B. Crash Cashout)."""
    if not (0 <= req.amount <= 10000000):
        raise HTTPException(400, "Ungültiger Betrag")
    u = get_user(uid)
    new_coins = u["coins"] + req.amount
    update_user(
        uid,
        coins=new_coins,
        total_won=u["total_won"] + req.amount,
    )
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
    existing = con.execute(
        "SELECT referrer_id FROM referrals WHERE referred_id=?", (uid,)
    ).fetchone()
    if existing:
        con.close()
        return {"ok": False, "reason": "already_referred"}

    con.execute(
        "INSERT INTO referrals (referred_id, referrer_id, created_at) VALUES (?,?,?)",
        (uid, ref_id, int(time.time())),
    )
    con.commit()
    count = con.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (ref_id,)
    ).fetchone()[0]
    con.close()

    reward = 500
    milestone = None
    if count == 1:
        reward += 500
        milestone = 1
    elif count == 3:
        reward += 2000
        milestone = 3
    elif count == 5:
        reward += 5000
        milestone = 5
    elif count == 10:
        reward += 15000
        milestone = 10
    elif count == 25:
        reward += 50000
        milestone = 25

    ref_u = get_user(ref_id)
    update_user(ref_id, coins=ref_u["coins"] + reward)

    welcome = 500
    new_u = get_user(uid)
    update_user(uid, coins=new_u["coins"] + welcome)

    return {
        "ok": True,
        "welcome_bonus": welcome,
        "referrer_reward": reward,
        "referrer_count": count,
        "milestone": milestone,
    }


@app.get("/api/referrals")
def get_referrals(uid: int = Depends(auth)):
    con = db()
    count = con.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)
    ).fetchone()[0]
    con.close()
    return {"count": count}


# ── Blackjack ──────────────────────────────────────────────
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
    bj_sessions[uid] = {
        "deck": deck,
        "player": player,
        "dealer": dealer,
        "bet": req.bet,
    }
    pv = hand_value(player)
    dv = hand_value(dealer)

    # Natural Blackjack / Push sofort abrechnen
    if pv == 21 and dv == 21:
        payout = req.bet
        delta = 0
        new_coins = u["coins"]
        update_user(
            uid,
            total_wagered=u["total_wagered"] + req.bet,
            total_won=u["total_won"] + payout,
        )
        del bj_sessions[uid]
        return {
            "player": player,
            "dealer": dealer,
            "player_value": pv,
            "dealer_value": dv,
            "state": "push",
            "payout": payout,
            "delta": delta,
            "coins": new_coins,
        }
    elif pv == 21:
        payout = int(req.bet * 2.5)
        delta = payout - req.bet
        new_coins = max(0, u["coins"] + delta)
        update_user(
            uid,
            coins=new_coins,
            total_wagered=u["total_wagered"] + req.bet,
            total_won=u["total_won"] + payout,
        )
        record_outcome(uid, True, "blackjack", payout)
        del bj_sessions[uid]
        return {
            "player": player,
            "dealer": dealer,
            "player_value": pv,
            "dealer_value": dv,
            "state": "blackjack",
            "payout": payout,
            "delta": delta,
            "coins": new_coins,
        }

    return {
        "player": player,
        "dealer": [dealer[0], {"r": "?", "s": "?"}],
        "player_value": pv,
        "dealer_value": hand_value([dealer[0]]),
        "state": "continue",
    }


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
        update_user(
            uid,
            coins=new_coins,
            total_wagered=u["total_wagered"] + s["bet"],
        )
        record_outcome(uid, False, "blackjack", 0)
        del bj_sessions[uid]
        return {
            "player": s["player"],
            "player_value": pv,
            "state": "bust",
            "coins": new_coins,
        }
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
    update_user(
        uid,
        coins=new_coins,
        total_wagered=u["total_wagered"] + s["bet"],
        total_won=u["total_won"] + payout,
    )
    record_outcome(uid, payout > s["bet"], "blackjack", payout)
    del bj_sessions[uid]
    return {
        "player": s["player"],
        "dealer": s["dealer"],
        "player_value": pv,
        "dealer_value": dv,
        "payout": payout,
        "delta": delta,
        "coins": new_coins,
        "state": "win" if payout > s["bet"] else "push" if payout == s["bet"] else "lose",
    }


# ── Roulette ───────────────────────────────────────────────
# RED already defined



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

    n, color = roulette_spin(u, req.bet_type, req.number)
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
    update_user(
        uid,
        coins=new_coins,
        total_wagered=u["total_wagered"] + req.bet,
        total_won=u["total_won"] + payout,
    )
    record_outcome(uid, payout > req.bet, "roulette", payout)
    return {
        "number": n,
        "color": color,
        "payout": payout,
        "delta": delta,
        "coins": new_coins,
    }



class CrashStartReq(BaseModel):
    bet: int


@app.post("/api/crash/start")
def crash_start(req: CrashStartReq, uid: int = Depends(auth)):
    """Einsatz abziehen + serverseitigen Crash-Punkt erzeugen (House Edge)."""
    u = get_user(uid)
    if u["self_excluded_until"] > int(time.time()):
        raise HTTPException(403, "Selbstausgeschlossen")
    if not (1 <= req.bet <= u["max_bet"]):
        raise HTTPException(400, "Einsatz ungültig")
    if req.bet > u["coins"]:
        raise HTTPException(400, "Nicht genug Münzen")

    new_coins = u["coins"] - req.bet
    update_user(
        uid,
        coins=new_coins,
        total_wagered=u["total_wagered"] + req.bet,
    )
    point = crash_multiplier(u)
    # Session merken (einfach in-memory)
    crash_sessions[uid] = {"bet": req.bet, "point": point, "cashed": False}
    return {"ok": True, "coins": new_coins, "crash_point": point}


class CrashCashoutReq(BaseModel):
    multiplier: float


@app.post("/api/crash/cashout")
def crash_cashout(req: CrashCashoutReq, uid: int = Depends(auth)):
    s = crash_sessions.get(uid)
    if not s or s.get("cashed"):
        raise HTTPException(400, "Keine Crash-Session")
    point = float(s["point"])
    mult = float(req.multiplier)
    if mult >= point:
        # Zu spät – bereits gecrasht
        s["cashed"] = True
        record_outcome(uid, False, "crash", 0)
        del crash_sessions[uid]
        u = get_user(uid)
        return {"ok": False, "crashed": True, "coins": u["coins"], "payout": 0}

    payout = int(s["bet"] * mult)
    u = get_user(uid)
    new_coins = u["coins"] + payout
    update_user(uid, coins=new_coins, total_won=u["total_won"] + payout)
    record_outcome(uid, True, "crash", payout)
    s["cashed"] = True
    del crash_sessions[uid]
    return {"ok": True, "coins": new_coins, "payout": payout}


@app.post("/api/crash/loss")
def crash_loss(uid: int = Depends(auth)):
    s = crash_sessions.get(uid)
    if s:
        record_outcome(uid, False, "crash", 0)
        del crash_sessions[uid]
    u = get_user(uid)
    return {"ok": True, "coins": u["coins"]}


crash_sessions = {}



# Karten-Preise nach Seltenheit (Warn-App: teuer, damit man spielt)
CARD_PRICES = {
    "common": 400,
    "rare": 1200,
    "epic": 3500,
    "legendary": 9000,
}

VIP_PRICES = {
    "common": 800,
    "rare": 2500,
    "epic": 6000,
    "legendary": 15000,
}

# Kartendefinition serverseitig (muss mit Frontend übereinstimmen)
CARD_RARITY = {
    1: "common", 2: "common", 3: "common", 4: "common", 5: "common",
    6: "common", 7: "common", 8: "common", 9: "common", 10: "common",
    11: "common", 12: "common", 13: "common", 14: "common", 15: "common",
    16: "common", 17: "common", 18: "common",
    19: "rare", 20: "rare", 21: "rare", 22: "rare", 23: "rare",
    24: "rare", 25: "rare", 26: "rare",
    27: "epic", 28: "epic", 29: "epic",
    30: "legendary",
}


class BuyCardReq(BaseModel):
    card_id: int


@app.post("/api/buy_card")
def buy_card(req: BuyCardReq, uid: int = Depends(auth)):
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
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
    return {
        "ok": True,
        "card_id": cid,
        "price": price,
        "rarity": rarity,
        "coins": new_coins,
    }


class UnlockVipReq(BaseModel):
    card_id: int


@app.post("/api/unlock_vip")
def unlock_vip(req: UnlockVipReq, uid: int = Depends(auth)):
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
    if not user_has_card(uid, cid):
        raise HTTPException(400, "Karte nicht im Besitz")
    con = db()
    row = con.execute(
        "SELECT vip FROM user_cards WHERE user_id=? AND card_id=?", (uid, cid)
    ).fetchone()
    media = con.execute(
        "SELECT image_url FROM card_media WHERE card_id=?", (cid,)
    ).fetchone()
    if not media:
        con.close()
        raise HTTPException(400, "Kein VIP-Bild für diese Karte")
    if row and row["vip"]:
        con.close()
        return {
            "ok": True,
            "already": True,
            "card_id": cid,
            "image_url": media["image_url"],
            "coins": get_user(uid)["coins"],
        }
    rarity = CARD_RARITY[cid]
    price = VIP_PRICES.get(rarity, 800)
    u = get_user(uid)
    if u["coins"] < price:
        con.close()
        raise HTTPException(400, "Nicht genug Coins")
    new_coins = u["coins"] - price
    update_user(uid, coins=new_coins)
    con.execute(
        "UPDATE user_cards SET vip=1 WHERE user_id=? AND card_id=?", (uid, cid)
    )
    con.commit()
    con.close()
    return {
        "ok": True,
        "card_id": cid,
        "price": price,
        "image_url": media["image_url"],
        "coins": new_coins,
    }


class GrantCardReq(BaseModel):
    card_id: int


@app.post("/api/grant_card")
def grant_card_api(req: GrantCardReq, uid: int = Depends(auth)):
    """Karte gutschreiben (Drops, Level, Rad) – kostenlos, einmalig."""
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
    is_new = grant_card(uid, cid)
    return {"ok": True, "card_id": cid, "is_new": is_new}


class SyncCollectionReq(BaseModel):
    card_ids: list


@app.post("/api/collection/sync")
def sync_collection(req: SyncCollectionReq, uid: int = Depends(auth)):
    """Migriert lokale CloudStorage-Karten einmalig auf den Server."""
    added = 0
    for cid in req.card_ids or []:
        try:
            cid = int(cid)
        except Exception:
            continue
        if grant_card(uid, cid):
            added += 1
    return {"ok": True, "added": added, "collection": get_user_collection(uid)}


@app.get("/api/collection")
def api_collection(uid: int = Depends(auth)):
    media = get_all_media()
    col = get_user_collection(uid)
    owned_ids = {c["card_id"] for c in col}
    catalog = []
    for cid in range(1, 31):
        rarity = CARD_RARITY.get(cid, "common")
        owned = cid in owned_ids
        entry = next((c for c in col if c["card_id"] == cid), None)
        catalog.append({
            "card_id": cid,
            "rarity": rarity,
            "owned": owned,
            "vip": bool(entry and entry["vip"]),
            "has_vip_media": cid in media,
            "image_url": (entry["image_url"] if entry and entry["vip"] else None),
            "buy_price": CARD_PRICES.get(rarity, 400),
            "vip_price": VIP_PRICES.get(rarity, 800),
        })
    return {
        "collection": col,
        "catalog": catalog,
        "owned_count": len(owned_ids),
        "total": 30,
        "is_admin": is_admin(uid),
    }


class AdminImageReq(BaseModel):
    card_id: int
    image_url: str


@app.post("/api/admin/card_image")
def admin_card_image(req: AdminImageReq, uid: int = Depends(auth)):
    require_admin(uid)
    cid = req.card_id
    if cid not in CARD_RARITY:
        raise HTTPException(400, "Unbekannte Karte")
    url = (req.image_url or "").strip()
    if not url.startswith("https://"):
        raise HTTPException(400, "Nur https:// URLs erlaubt")
    if len(url) > 2000:
        raise HTTPException(400, "URL zu lang")
    con = db()
    con.execute(
        "INSERT INTO card_media (card_id, image_url, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(card_id) DO UPDATE SET image_url=excluded.image_url, updated_at=excluded.updated_at",
        (cid, url, int(time.time())),
    )
    con.commit()
    con.close()
    return {"ok": True, "card_id": cid, "image_url": url}


@app.delete("/api/admin/card_image/{card_id}")
def admin_delete_image(card_id: int, uid: int = Depends(auth)):
    require_admin(uid)
    con = db()
    con.execute("DELETE FROM card_media WHERE card_id=?", (card_id,))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/leaderboard")
def leaderboard(uid: int = Depends(auth)):
    con = db()
    rows = con.execute(
        "SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10"
    ).fetchall()
    con.close()
    entries = [
        {"user_id": r["user_id"], "coins": r["coins"], "is_me": r["user_id"] == uid}
        for r in rows
    ]
    idx = next((i for i, e in enumerate(entries) if e["is_me"]), None)
    return {"entries": entries, "my_rank": idx + 1 if idx is not None else None}


@app.get("/api/feed")
def feed(uid: int = Depends(auth)):
    con = db()
    rows = con.execute(
        "SELECT user_id, game, amount FROM wins ORDER BY id DESC LIMIT 12"
    ).fetchall()
    con.close()
    return {
        "entries": [
            {
                "user_id": r["user_id"],
                "game": r["game"],
                "amount": r["amount"],
                "is_me": r["user_id"] == uid,
            }
            for r in rows
        ]
    }


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
    return {
        "users": users,
        "coins_in_circulation": coins,
        "total_wagered": wag,
        "total_won": won,
        "house_edge": wag - won,
    }


# ── Telegram Bot ───────────────────────────────────────────
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
    await update.message.reply_text(
        WELCOME,
        reply_markup=bot_kb(),
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎰 <b>Virtual Casino</b>\nAlle Spiele laufen in der Mini App.\n"
        "⚠️ Kein Echtgeld · 18+",
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
    )


async def post_init(bot_app):
    await bot_app.bot.set_my_commands(
        [
            BotCommand("start", "Casino öffnen"),
            BotCommand("help", "Hilfe"),
        ],
        scope=BotCommandScopeDefault(),
    )
    try:
        await bot_app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🎮 Casino", web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
    except Exception as e:
        print(f"Menu button konnte nicht gesetzt werden: {e}")


async def main():
    """
    API ist immer primär. Bot-Polling ist optional und darf den Server nie killen.
    ENABLE_BOT_POLLING=0 → nur API (empfohlen wenn Conflict bestehen bleibt).
    """
    enable_polling = os.environ.get("ENABLE_BOT_POLLING", "1").strip() not in ("0", "false", "False", "no")

    bot_app = None
    if enable_polling:
        try:
            bot_app = (
                ApplicationBuilder()
                .token(BOT_TOKEN)
                .post_init(post_init)
                .build()
            )
            bot_app.add_handler(CommandHandler("start", cmd_start))
            bot_app.add_handler(CommandHandler("help", cmd_help))
            await bot_app.initialize()
            try:
                await bot_app.bot.delete_webhook(drop_pending_updates=True)
                print("Webhook gelöscht")
            except Exception as e:
                print(f"delete_webhook: {e}")
            await bot_app.start()
            try:
                await bot_app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message", "callback_query"],
                )
                print("Bot-Polling aktiv")
            except Exception as e:
                print(f"Polling nicht gestartet ({e}) – API läuft trotzdem")
        except Exception as e:
            print(f"Bot-Init übersprungen: {e}")
            bot_app = None
    else:
        print("ENABLE_BOT_POLLING=0 → nur API-Modus")

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Server startet auf Port {PORT}")
    print(f"WebApp URL: {WEBAPP_URL}")
    try:
        await server.serve()
    finally:
        if bot_app is not None:
            try:
                if bot_app.updater.running:
                    await bot_app.updater.stop()
            except Exception:
                pass
            try:
                await bot_app.stop()
                await bot_app.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
