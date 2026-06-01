"""
KAYO — Solana Alpha Bot (Final Build)
======================================
New features:
  - Menu button (tap menu, see all commands)
  - Weekly leaderboard (auto-posts Sunday midnight)
  - /stop <address> — lock profit, protect from dips
  - Refresh button on every scan
  - TradingView deep-link button on every scan
  - Momentum detector — detects unusual buy activity before big moves
  - Auto new coin scanner — finds fresh potential coins
  - Auto runner alerts every 5 min

Setup:
  pip install python-telegram-bot==20.7 aiohttp python-dotenv anthropic
  python kayo_final.py
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, MenuButtonCommands,
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters,
)

# ─── PASTE YOUR TOKENS HERE ──────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
ALERT_CHAT_ID = int(os.environ.get("ALERT_CHAT_ID", "0"))
# ─────────────────────────────────────────────────────────────────────────────

ai_client = None
try:
    if ANTHROPIC_KEY and "PASTE" not in ANTHROPIC_KEY:
        import anthropic
        ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        print("AI enabled")
    else:
        print("AI disabled — add Anthropic key to enable")
except Exception as e:
    print(f"AI disabled: {e}")

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════════════════════════
active_calls:      dict = {}  # uid -> {addr -> call_data}
stopped_calls:     dict = {}  # uid -> {addr -> final_data}  (locked profits)
posted_runners:    set  = set()
posted_new_coins:  set  = set()
watchlists:        dict = {}
reminders:         list = []
user_xp:           dict = {}
group_points:      dict = {}
custom_prompts:    dict = {}
scan_history:      dict = {}
settings:          dict = {}
momentum_baseline: dict = {}  # addr -> {txns_1h, vol_1h, timestamp}
weekly_lb_cache:   dict = {}  # uid -> {symbol, profit_pct, x}

MILESTONES   = [2, 5, 10, 20, 50, 100]
SOLANA_RE    = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')

KNOWN_TOKENS = {
    "So11111111111111111111111111111111111111112":   "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So":  "mSOL",
}

RUNNER_FILTERS = {
    "min_liquidity_usd":    10_000,
    "max_liquidity_usd":   500_000,
    "min_volume_24h":       50_000,
    "max_fdv_usd":       5_000_000,
    "min_price_change_1h":     5.0,
    "min_txns_1h":              50,
    "min_age_minutes":          30,
    "max_age_hours":            48,
}

NEW_COIN_FILTERS = {
    "min_liquidity_usd":  5_000,
    "max_age_minutes":       60,   # Under 1 hour old
    "min_txns_1h":           20,
}

MOMENTUM_THRESHOLDS = {
    "txn_spike_pct":   150,   # 150% increase in txns = momentum signal
    "vol_spike_pct":   200,   # 200% increase in volume = momentum signal
    "min_txns_1h":      30,
}

KAYO_SYSTEM = """
You are Kayo — a sharp, witty Solana on-chain alpha bot.
Personality: British-Nigerian energy, degen-aware, confident, never boring.
Short punchy replies. Verdict: APE / WATCH / AVOID. Sign off — Kayo 🦅
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_price(p: float) -> str:
    if p == 0: return "$0"
    if p < 0.000001: return f"${p:.10f}"
    if p < 0.01: return f"${p:.8f}"
    if p < 1: return f"${p:.6f}"
    return f"${p:,.4f}"

def fmt_usd(v: float) -> str:
    if v >= 1_000_000_000: return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000: return f"${v/1_000:.1f}K"
    return f"${v:.2f}"

def fmt_pct(v) -> str:
    try:
        v = float(v)
        return f"+{v:.1f}%" if v > 0 else f"{v:.1f}%"
    except: return "N/A"

def x_emoji(x: float) -> str:
    if x >= 10: return "🚀🚀🚀"
    if x >= 5:  return "🚀🚀"
    if x >= 2:  return "🚀"
    if x >= 1:  return "📈"
    return "📉"

def rug_label(s: int) -> str:
    if s >= 80: return "SAFU ✅"
    if s >= 50: return "MODERATE RISK 🟡"
    if s >= 20: return "HIGH RISK 🟠"
    return "DANGER 🔴"

def compute_rug_score(s: dict) -> int:
    score = 100
    if s.get("is_honeypot") == "1":          score -= 60
    if s.get("cannot_sell_all") == "1":      score -= 40
    if s.get("is_blacklisted") == "1":       score -= 30
    if s.get("is_proxy") == "1":             score -= 20
    if s.get("owner_change_balance") == "1": score -= 20
    if float(s.get("buy_tax", 0) or 0) > 10 or float(s.get("sell_tax", 0) or 0) > 10:
        score -= 20
    if s.get("lp_locked") == "1":            score += 10
    return max(0, min(100, score))

def add_xp(uid: int, amount: int = 5):
    user_xp.setdefault(uid, {"xp": 0, "last_claim": None})
    user_xp[uid]["xp"] += amount

def add_group_point(chat_id: int, uid: int):
    group_points.setdefault(chat_id, {})
    group_points[chat_id][uid] = group_points[chat_id].get(uid, 0) + 1

def save_scan(uid: int, address: str):
    scan_history.setdefault(uid, [])
    if address not in scan_history[uid]:
        scan_history[uid].insert(0, address)
    scan_history[uid] = scan_history[uid][:20]

def get_setting(chat_id: int, key: str, default=True):
    return settings.get(chat_id, {}).get(key, default)

def tv_link(symbol: str, address: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol=CRYPTO:{symbol}USDT"

def dex_chart_link(address: str) -> str:
    return f"https://dexscreener.com/solana/{address}"


# ═══════════════════════════════════════════════════════════════════════════════
#  AI
# ═══════════════════════════════════════════════════════════════════════════════

async def kayo_think(prompt: str, context: str = "", system_override: str = "") -> str:
    if not ai_client:
        return "AI offline. Add Anthropic key to enable. — Kayo 🦅"
    try:
        sys = system_override or KAYO_SYSTEM
        full = f"{context}\n\n{prompt}".strip() if context else prompt
        msg = ai_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=sys,
            messages=[{"role": "user", "content": full}],
        )
        return msg.content[0].text
    except:
        return "Brain fried rn. Try again. — Kayo 🦅"

async def kayo_token_take(pair: dict, security: dict, rug_score: int) -> str:
    base = pair.get("baseToken", {})
    ctx = (
        f"Token: {base.get('name')} (${base.get('symbol')})\n"
        f"Price: {pair.get('priceUsd',0)} MCap: {pair.get('fdv',0)}\n"
        f"Liq: {pair.get('liquidity',{}).get('usd',0)} Vol24h: {pair.get('volume',{}).get('h24',0)}\n"
        f"1h: {pair.get('priceChange',{}).get('h1',0)}% 24h: {pair.get('priceChange',{}).get('h24',0)}%\n"
        f"Buys/Sells 1h: {pair.get('txns',{}).get('h1',{}).get('buys',0)}/{pair.get('txns',{}).get('h1',{}).get('sells',0)}\n"
        f"Rug: {rug_score}/100 Honeypot: {security.get('is_honeypot','0')} LP: {security.get('lp_locked','0')}"
    )
    return await kayo_think("Alpha take. Verdict: APE/WATCH/AVOID. Max 5 lines.", ctx)


# ═══════════════════════════════════════════════════════════════════════════════
#  API
# ═══════════════════════════════════════════════════════════════════════════════

async def dex_token(session: aiohttp.ClientSession, address: str) -> Optional[dict]:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return None
            data = await r.json()
            pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
            if not pairs: return None
            pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            return pairs[0]
    except: return None

async def dex_search(session: aiohttp.ClientSession, query: str) -> list:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/search?q={query}",
            timeout=aiohttp.ClientTimeout(total=12)
        ) as r:
            if r.status != 200: return []
            data = await r.json()
            return [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
    except: return []

async def goplus(session: aiohttp.ClientSession, address: str) -> dict:
    try:
        async with session.get(
            f"https://api.gopluslabs.io/api/v1/token_security/solana?contract_addresses={address}",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200: return {}
            data = await r.json()
            result = data.get("result", {})
            return result.get(address.lower(), result.get(address, {}))
    except: return {}

async def fetch_runners(session: aiohttp.ClientSession) -> list:
    pairs = await dex_search(session, "solana")
    now_ms = time.time() * 1000
    f = RUNNER_FILTERS
    runners = []
    for p in pairs:
        liq   = float(p.get("liquidity", {}).get("usd", 0) or 0)
        vol24 = float(p.get("volume", {}).get("h24", 0) or 0)
        fdv   = float(p.get("fdv", 0) or 0)
        ch1h  = float(p.get("priceChange", {}).get("h1", 0) or 0)
        txns  = int(p.get("txns", {}).get("h1", {}).get("buys", 0) or 0) + \
                int(p.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
        created = p.get("pairCreatedAt", 0) or 0
        age_m   = (now_ms - created) / 60_000 if created else 99999
        if (f["min_liquidity_usd"] <= liq <= f["max_liquidity_usd"]
            and vol24 >= f["min_volume_24h"]
            and (fdv == 0 or fdv <= f["max_fdv_usd"])
            and ch1h >= f["min_price_change_1h"]
            and txns >= f["min_txns_1h"]
            and f["min_age_minutes"] <= age_m <= f["max_age_hours"] * 60):
            runners.append(p)
    runners.sort(key=lambda x: float(x.get("priceChange", {}).get("h1", 0) or 0), reverse=True)
    return runners[:10]

async def fetch_new_coins(session: aiohttp.ClientSession) -> list:
    """Find brand new coins under 1 hour old with activity."""
    pairs = await dex_search(session, "solana")
    now_ms = time.time() * 1000
    f = NEW_COIN_FILTERS
    new_coins = []
    for p in pairs:
        liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
        created = p.get("pairCreatedAt", 0) or 0
        txns    = int(p.get("txns", {}).get("h1", {}).get("buys", 0) or 0) + \
                  int(p.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
        if created == 0: continue
        age_m = (now_ms - created) / 60_000
        if (liq >= f["min_liquidity_usd"]
            and age_m <= f["max_age_minutes"]
            and txns >= f["min_txns_1h"]):
            new_coins.append(p)
    new_coins.sort(key=lambda x: x.get("pairCreatedAt", 0), reverse=True)
    return new_coins[:5]

async def coingecko_lookup(session: aiohttp.ClientSession, coin_id: str) -> Optional[dict]:
    try:
        async with session.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&community_data=false",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            return await r.json() if r.status == 200 else None
    except: return None

async def coingecko_global(session: aiohttp.ClientSession) -> dict:
    try:
        async with session.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            return (await r.json()).get("data", {}) if r.status == 200 else {}
    except: return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  TOKEN CARD + KEYBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def build_card(pair: dict, security: dict = {}, rug_score: int = -1) -> str:
    base    = pair.get("baseToken", {})
    name    = base.get("name", "Unknown")
    symbol  = base.get("symbol", "???")
    address = base.get("address", "N/A")
    price   = float(pair.get("priceUsd", 0) or 0)
    liq     = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    fdv     = float(pair.get("fdv", 0) or 0)
    mcap    = float(pair.get("marketCap", 0) or 0) or fdv
    vol24   = float(pair.get("volume", {}).get("h24", 0) or 0)
    vol1h   = float(pair.get("volume", {}).get("h1", 0) or 0)
    ch5m    = pair.get("priceChange", {}).get("m5", "N/A")
    ch1h    = pair.get("priceChange", {}).get("h1", "N/A")
    ch24h   = pair.get("priceChange", {}).get("h24", "N/A")
    buys1h  = pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0
    sells1h = pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{address}")
    created = pair.get("pairCreatedAt", 0)
    age_str = "Unknown"
    if created:
        age = datetime.utcnow() - datetime.utcfromtimestamp(created / 1000)
        age_str = f"{int(age.total_seconds()//3600)}h {int((age.total_seconds()%3600)//60)}m"

    msg = (
        f"🦅 KAYO SCAN — {name} (${symbol})\n"
        f"{address}\n\n"
        f"Price:     {fmt_price(price)}\n"
        f"MCap:      {fmt_usd(mcap)}\n"
        f"Liquidity: {fmt_usd(liq)}\n"
        f"Vol 24h:   {fmt_usd(vol24)} | 1h: {fmt_usd(vol1h)}\n"
        f"Change:    5m {fmt_pct(ch5m)} | 1h {fmt_pct(ch1h)} | 24h {fmt_pct(ch24h)}\n"
        f"Txns 1h:   {buys1h} buys / {sells1h} sells\n"
        f"Age:       {age_str}\n"
    )
    if rug_score >= 0:
        hp = "YES 🚨" if security.get("is_honeypot") == "1" else "No ✅"
        lp = "Yes 🔒" if security.get("lp_locked") == "1" else "No ⚠️"
        msg += (
            f"\nSafety:    {rug_score}/100 — {rug_label(rug_score)}\n"
            f"Honeypot:  {hp}\n"
            f"LP Locked: {lp}\n"
            f"Tax:       {security.get('buy_tax','?')}% buy / {security.get('sell_tax','?')}% sell\n"
        )
    return msg

def scan_keyboard(address: str, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📞 Call", callback_data=f"call:{address}"),
            InlineKeyboardButton("⭐ Watch", callback_data=f"watch:{address}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{address}"),
        ],
        [
            InlineKeyboardButton("📊 DexScreener", url=dex_chart_link(address)),
            InlineKeyboardButton("📈 TradingView", url=tv_link(symbol, address)),
        ],
    ])


# ═══════════════════════════════════════════════════════════════════════════════
#  LEADERBOARD HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def record_weekly_profit(uid: int, symbol: str, profit_pct: float, mult: float):
    """Record a stopped call's profit for weekly leaderboard."""
    weekly_lb_cache.setdefault(uid, [])
    weekly_lb_cache[uid].append({
        "symbol": symbol,
        "profit_pct": profit_pct,
        "mult": mult,
        "stopped_at": datetime.utcnow().isoformat(),
    })

def build_weekly_leaderboard() -> str:
    """Build weekly leaderboard from stopped calls."""
    if not weekly_lb_cache:
        return "No stopped calls this week yet. Use /stop <address> to lock profits."

    # Per user best call
    user_best = {}
    for uid, calls in weekly_lb_cache.items():
        if calls:
            best = max(calls, key=lambda x: x["profit_pct"])
            user_best[uid] = best

    sorted_users = sorted(user_best.items(), key=lambda x: x[1]["profit_pct"], reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 KAYO WEEKLY LEADERBOARD\n{'─'*28}\n"]
    for i, (uid, best) in enumerate(sorted_users[:10], 1):
        medal = medals[i-1] if i <= 3 else f"{i}."
        sign = "+" if best["profit_pct"] >= 0 else ""
        lines.append(
            f"{medal} User {uid}\n"
            f"   Best Call: ${best['symbol']}\n"
            f"   Return: {sign}{best['profit_pct']:.1f}% ({best['mult']:.2f}x)\n"
        )
    lines.append("\nUse /stop <address> to lock your profits before a dip!")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════════════════════

async def auto_scan_loop(app: Application):
    """Every 5 min: post new early runners to group."""
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                runners = await fetch_runners(session)
            new = [r for r in runners
                   if r.get("baseToken", {}).get("address", "") not in posted_runners]
            if new and ALERT_CHAT_ID:
                for p in new[:3]:
                    base    = p.get("baseToken", {})
                    symbol  = base.get("symbol", "???")
                    address = base.get("address", "")
                    price   = float(p.get("priceUsd", 0) or 0)
                    fdv     = float(p.get("fdv", 0) or 0)
                    liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
                    ch1h    = p.get("priceChange", {}).get("h1", 0)
                    async with aiohttp.ClientSession() as s2:
                        sec = await goplus(s2, address)
                    rs   = compute_rug_score(sec) if sec else 50
                    take = await kayo_token_take(p, sec, rs)
                    msg = (
                        f"🦅 KAYO EARLY RUNNER\n{'─'*24}\n"
                        f"${symbol}\n"
                        f"Price: {fmt_price(price)} | MCap: {fmt_usd(fdv)}\n"
                        f"Liq: {fmt_usd(liq)} | 1h: {fmt_pct(ch1h)}\n"
                        f"Safety: {rs}/100\n\n"
                        f"Kayo's Take:\n{take}"
                    )
                    await app.bot.send_message(
                        ALERT_CHAT_ID, msg,
                        reply_markup=scan_keyboard(address, symbol)
                    )
                    posted_runners.add(address)
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Auto-scan: {e}")
        await asyncio.sleep(300)


async def new_coin_scanner_loop(app: Application):
    """Every 3 min: scan for brand new coins under 1h old."""
    await asyncio.sleep(90)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                new_coins = await fetch_new_coins(session)
            fresh = [c for c in new_coins
                     if c.get("baseToken", {}).get("address", "") not in posted_new_coins]
            if fresh and ALERT_CHAT_ID:
                for p in fresh[:2]:
                    base    = p.get("baseToken", {})
                    symbol  = base.get("symbol", "???")
                    address = base.get("address", "")
                    price   = float(p.get("priceUsd", 0) or 0)
                    fdv     = float(p.get("fdv", 0) or 0)
                    liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
                    txns    = int(p.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
                    created = p.get("pairCreatedAt", 0)
                    age_m   = int((time.time() * 1000 - created) / 60_000) if created else 0
                    msg = (
                        f"🆕 NEW COIN ALERT — Kayo\n{'─'*24}\n"
                        f"${symbol} — {base.get('name','')}\n"
                        f"Age: {age_m} minutes old\n"
                        f"Price: {fmt_price(price)}\n"
                        f"MCap: {fmt_usd(fdv)} | Liq: {fmt_usd(liq)}\n"
                        f"Buys (1h): {txns}\n\n"
                        f"DYOR — very early, very risky. — Kayo 🦅"
                    )
                    await app.bot.send_message(
                        ALERT_CHAT_ID, msg,
                        reply_markup=scan_keyboard(address, symbol)
                    )
                    posted_new_coins.add(address)
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"New coin scanner: {e}")
        await asyncio.sleep(180)


async def momentum_detector_loop(app: Application):
    """
    Every 4 min: detect unusual buy activity spikes on existing coins.
    Compares current txns/volume to baseline from last check.
    No external API needed — uses DexScreener free data.
    """
    await asyncio.sleep(120)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                pairs = await dex_search(session, "solana")

            now = time.time()
            alerts = []

            for p in pairs:
                base    = p.get("baseToken", {})
                address = base.get("address", "")
                symbol  = base.get("symbol", "???")
                if not address: continue

                txns_1h = int(p.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
                vol_1h  = float(p.get("volume", {}).get("h1", 0) or 0)
                fdv     = float(p.get("fdv", 0) or 0)
                liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
                ch1h    = float(p.get("priceChange", {}).get("h1", 0) or 0)
                price   = float(p.get("priceUsd", 0) or 0)

                # Skip very low liquidity
                if liq < 5000: continue
                # Skip if already massively moved
                if ch1h > 50: continue

                baseline = momentum_baseline.get(address)

                if baseline:
                    prev_txns = baseline["txns_1h"]
                    prev_vol  = baseline["vol_1h"]

                    if prev_txns > 0 and prev_vol > 0:
                        txn_spike = ((txns_1h - prev_txns) / prev_txns) * 100
                        vol_spike = ((vol_1h - prev_vol) / prev_vol) * 100

                        t = MOMENTUM_THRESHOLDS
                        if (txn_spike >= t["txn_spike_pct"]
                            and vol_spike >= t["vol_spike_pct"]
                            and txns_1h >= t["min_txns_1h"]):
                            alerts.append({
                                "pair": p,
                                "txn_spike": txn_spike,
                                "vol_spike": vol_spike,
                                "txns_1h": txns_1h,
                                "price": price,
                                "fdv": fdv,
                                "liq": liq,
                                "symbol": symbol,
                                "address": address,
                            })

                # Update baseline
                momentum_baseline[address] = {
                    "txns_1h": txns_1h,
                    "vol_1h": vol_1h,
                    "timestamp": now,
                }

            # Post top 2 momentum alerts
            if alerts and ALERT_CHAT_ID:
                alerts.sort(key=lambda x: x["txn_spike"], reverse=True)
                for a in alerts[:2]:
                    msg = (
                        f"⚡ MOMENTUM DETECTED — Kayo\n{'─'*24}\n"
                        f"${a['symbol']}\n"
                        f"Buy Txns spike: +{a['txn_spike']:.0f}%\n"
                        f"Volume spike:   +{a['vol_spike']:.0f}%\n"
                        f"Current txns/h: {a['txns_1h']}\n"
                        f"Price: {fmt_price(a['price'])}\n"
                        f"MCap: {fmt_usd(a['fdv'])} | Liq: {fmt_usd(a['liq'])}\n\n"
                        f"People are buying. Could be early. DYOR. — Kayo 🦅"
                    )
                    await app.bot.send_message(
                        ALERT_CHAT_ID, msg,
                        reply_markup=scan_keyboard(a["address"], a["symbol"])
                    )
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Momentum detector: {e}")
        await asyncio.sleep(240)


async def milestone_loop(app: Application):
    """Every 3 min: check active calls for 2x/5x/10x milestones."""
    await asyncio.sleep(60)
    while True:
        try:
            all_calls = [(uid, addr, d)
                         for uid, calls in active_calls.items()
                         for addr, d in calls.items()]
            if all_calls:
                async with aiohttp.ClientSession() as session:
                    for uid, address, data in all_calls:
                        pair = await dex_token(session, address)
                        if not pair: continue
                        cur    = float(pair.get("priceUsd", 0) or 0)
                        called = data["called_price"]
                        if cur == 0 or called == 0: continue
                        mult = cur / called
                        hits = data.setdefault("milestones_hit", [])
                        for m in MILESTONES:
                            if mult >= m and m not in hits:
                                hits.append(m)
                                sym     = data["symbol"]
                                dex_url = data.get("dex_url", "")
                                emoji   = "🚀🚀🚀" if m >= 10 else "🚀🚀" if m >= 5 else "🚀"
                                comment = await kayo_think(
                                    f"${sym} just hit {m}x. Price: {fmt_price(cur)}. 2-line hype comment."
                                )
                                alert = (
                                    f"{emoji} {m}X MILESTONE — ${sym}\n\n"
                                    f"Called at: {fmt_price(called)}\n"
                                    f"Now:       {fmt_price(cur)}\n"
                                    f"Return:    {mult:.2f}x\n\n"
                                    f"{comment}\n\nChart: {dex_url}"
                                )
                                try: await app.bot.send_message(uid, alert)
                                except: pass
                                if ALERT_CHAT_ID:
                                    try: await app.bot.send_message(ALERT_CHAT_ID, alert)
                                    except: pass
                        await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Milestone: {e}")
        await asyncio.sleep(180)


async def reminder_loop(app: Application):
    await asyncio.sleep(10)
    while True:
        now = datetime.utcnow()
        fired = [r for r in reminders if now >= r["fire_at"]]
        for r in fired:
            try: await app.bot.send_message(r["chat_id"], f"⏰ Reminder: {r['text']}")
            except: pass
            reminders.remove(r)
        await asyncio.sleep(30)


async def weekly_leaderboard_loop(app: Application):
    """Post leaderboard every Sunday at midnight UTC."""
    while True:
        now = datetime.utcnow()
        # Calculate seconds until next Sunday midnight
        days_until_sunday = (6 - now.weekday()) % 7
        next_sunday = (now + timedelta(days=days_until_sunday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if next_sunday <= now:
            next_sunday += timedelta(weeks=1)
        wait_seconds = (next_sunday - now).total_seconds()
        logger.info(f"Weekly leaderboard in {wait_seconds/3600:.1f}h")
        await asyncio.sleep(wait_seconds)
        try:
            board = build_weekly_leaderboard()
            if ALERT_CHAT_ID:
                await app.bot.send_message(ALERT_CHAT_ID, board)
            # Clear weekly cache after posting
            weekly_lb_cache.clear()
        except Exception as e:
            logger.error(f"Weekly LB: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦅 KAYO — Solana Alpha Intelligence\n\n"
        "Tap the Menu button below for all commands.\n\n"
        "Quick start:\n"
        "/scan <address> — Full token scan\n"
        "/call <address> — Register a call\n"
        "/stop <address> — Lock your profit\n"
        "/mycalls        — Live P&L tracker\n"
        "/runners        — Early runners now\n"
        "/leaderboard    — Weekly top calls\n"
        "/momentum       — Coins with unusual buys\n"
        "/newcoins       — Brand new coins\n"
        "/kayo <q>       — Ask me anything\n\n"
        "— Kayo 🦅"
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /scan <token_address>")
        return
    address = context.args[0].strip()
    uid = update.effective_user.id
    wait = await update.message.reply_text("🔍 Scanning...")
    async with aiohttp.ClientSession() as session:
        pair, sec = await asyncio.gather(dex_token(session, address), goplus(session, address))
    if not pair:
        await wait.edit_text("❌ Token not found on Solana.")
        return
    rs   = compute_rug_score(sec) if sec else -1
    card = build_card(pair, sec, rs)
    take = await kayo_token_take(pair, sec, rs)
    sym  = pair.get("baseToken", {}).get("symbol", "???")
    save_scan(uid, address)
    add_xp(uid, 2)
    add_group_point(update.effective_chat.id, uid)
    await wait.edit_text(
        card + f"\n\nKayo's Take:\n{take}",
        reply_markup=scan_keyboard(address, sym),
        disable_web_page_preview=True
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lock in profit on a call — protects leaderboard score from future dips."""
    if not context.args:
        await update.message.reply_text("Usage: /stop <token_address>")
        return
    address = context.args[0].strip()
    uid = update.effective_user.id
    calls = active_calls.get(uid, {})
    if address not in calls:
        await update.message.reply_text("No active call found for that address. Use /call first.")
        return
    wait = await update.message.reply_text("🔒 Locking in profit...")
    async with aiohttp.ClientSession() as session:
        pair = await dex_token(session, address)
    call_data = calls[address]
    called_price = call_data["called_price"]
    symbol = call_data["symbol"]
    if pair:
        cur_price = float(pair.get("priceUsd", 0) or 0)
    else:
        cur_price = called_price
    mult       = cur_price / called_price if called_price > 0 else 1
    profit_pct = (mult - 1) * 100
    sign       = "+" if profit_pct >= 0 else ""
    # Move to stopped calls
    stopped_calls.setdefault(uid, {})[address] = {
        **call_data,
        "stopped_price": cur_price,
        "stopped_at": datetime.utcnow().isoformat(),
        "final_mult": mult,
        "final_pct": profit_pct,
    }
    del active_calls[uid][address]
    # Record for weekly leaderboard
    record_weekly_profit(uid, symbol, profit_pct, mult)
    add_xp(uid, 20)
    emoji = x_emoji(mult)
    await wait.edit_text(
        f"🔒 PROFIT LOCKED — ${symbol}\n\n"
        f"Entry:  {fmt_price(called_price)}\n"
        f"Exit:   {fmt_price(cur_price)}\n"
        f"Return: {sign}{profit_pct:.1f}% — {mult:.2f}x {emoji}\n\n"
        f"Locked into weekly leaderboard. Dips won't affect your score now.\n"
        f"— Kayo 🦅"
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current weekly leaderboard."""
    board = build_weekly_leaderboard()
    await update.message.reply_text(board)


async def call_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = None
    if context.args:
        address = context.args[0].strip()
    elif update.callback_query:
        address = update.callback_query.data.split(":", 1)[1]
        await update.callback_query.answer()
    if not address:
        await update.effective_message.reply_text("Usage: /call <token_address>")
        return
    wait = await update.effective_message.reply_text("📡 Locking in entry...")
    async with aiohttp.ClientSession() as session:
        pair = await dex_token(session, address)
    if not pair:
        await wait.edit_text("❌ Can't fetch token data.")
        return
    price  = float(pair.get("priceUsd", 0) or 0)
    mcap   = float(pair.get("fdv", 0) or pair.get("marketCap", 0) or 0)
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    if price == 0:
        await wait.edit_text("❌ No price data.")
        return
    uid = update.effective_user.id
    active_calls.setdefault(uid, {})[address] = {
        "symbol": symbol, "called_price": price, "called_mcap": mcap,
        "called_at": datetime.utcnow().isoformat(),
        "address": address,
        "dex_url": pair.get("url", f"https://dexscreener.com/solana/{address}"),
        "milestones_hit": [],
    }
    add_xp(uid, 10)
    comment = await kayo_think(f"Just called ${symbol} at {fmt_price(price)}, MCap {fmt_usd(mcap)}. 2-line degen entry comment.")
    await wait.edit_text(
        f"📞 CALL LOCKED — ${symbol}\n\n"
        f"Entry: {fmt_price(price)}\n"
        f"MCap:  {fmt_usd(mcap)}\n"
        f"Time:  {datetime.utcnow().strftime('%H:%M UTC')}\n\n"
        f"{comment}\n\n"
        f"Use /stop <address> to lock profits. — Kayo 🦅"
    )


async def my_calls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    calls = active_calls.get(uid, {})
    if not calls:
        await update.message.reply_text("No active calls. Use /call <address>. — Kayo 🦅")
        return
    wait = await update.message.reply_text("📊 Fetching live data...")
    async with aiohttp.ClientSession() as session:
        pairs = await asyncio.gather(*[dex_token(session, a) for a in calls])
    results = []
    for (address, data), pair in zip(calls.items(), pairs):
        symbol = data["symbol"]
        cp = data["called_price"]
        if not pair:
            results.append(f"⚠️ ${symbol} — unavailable")
            continue
        cur  = float(pair.get("priceUsd", 0) or 0)
        if cur == 0:
            results.append(f"⚠️ ${symbol} — no price")
            continue
        mult = cur / cp
        pct  = (mult - 1) * 100
        sign = "+" if pct >= 0 else ""
        age  = datetime.utcnow() - datetime.fromisoformat(data["called_at"])
        age_str = f"{int(age.total_seconds()//3600)}h {int((age.total_seconds()%3600)//60)}m"
        results.append(
            f"{x_emoji(mult)} ${symbol}\n"
            f"   Entry: {fmt_price(cp)}\n"
            f"   Now:   {fmt_price(cur)}\n"
            f"   P&L:   {sign}{pct:.1f}% — {mult:.2f}x\n"
            f"   Age:   {age_str}\n"
            f"   /stop {address}"
        )
    await wait.edit_text(
        f"📋 Kayo Call Tracker — {len(calls)} active\n{'─'*26}\n\n" + "\n\n".join(results)
    )


async def cmd_momentum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show coins with detected momentum spikes."""
    wait = await update.message.reply_text("⚡ Scanning for momentum...")
    async with aiohttp.ClientSession() as session:
        pairs = await dex_search(session, "solana")
    now = time.time()
    alerts = []
    for p in pairs:
        base    = p.get("baseToken", {})
        address = base.get("address", "")
        symbol  = base.get("symbol", "???")
        if not address: continue
        txns_1h = int(p.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
        vol_1h  = float(p.get("volume", {}).get("h1", 0) or 0)
        liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
        ch1h    = float(p.get("priceChange", {}).get("h1", 0) or 0)
        price   = float(p.get("priceUsd", 0) or 0)
        fdv     = float(p.get("fdv", 0) or 0)
        if liq < 5000 or ch1h > 50: continue
        baseline = momentum_baseline.get(address)
        if baseline:
            prev_txns = baseline["txns_1h"]
            prev_vol  = baseline["vol_1h"]
            if prev_txns > 0 and prev_vol > 0:
                txn_spike = ((txns_1h - prev_txns) / prev_txns) * 100
                vol_spike = ((vol_1h - prev_vol) / prev_vol) * 100
                t = MOMENTUM_THRESHOLDS
                if txn_spike >= t["txn_spike_pct"] and vol_spike >= t["vol_spike_pct"]:
                    alerts.append({
                        "symbol": symbol, "address": address,
                        "txn_spike": txn_spike, "vol_spike": vol_spike,
                        "txns_1h": txns_1h, "price": price,
                        "fdv": fdv, "liq": liq,
                        "dex_url": p.get("url", f"https://dexscreener.com/solana/{address}"),
                    })
        momentum_baseline[address] = {"txns_1h": txns_1h, "vol_1h": vol_1h, "timestamp": now}
    if not alerts:
        await wait.edit_text(
            "No momentum spikes detected right now.\n"
            "Kayo monitors this every 4 minutes automatically. — Kayo 🦅"
        )
        return
    alerts.sort(key=lambda x: x["txn_spike"], reverse=True)
    lines = [f"⚡ Momentum Detected ({len(alerts)} coins)\n{'─'*24}\n"]
    for a in alerts[:5]:
        lines.append(
            f"${a['symbol']}\n"
            f"   Txn spike: +{a['txn_spike']:.0f}% | Vol spike: +{a['vol_spike']:.0f}%\n"
            f"   Price: {fmt_price(a['price'])} | MCap: {fmt_usd(a['fdv'])}\n"
            f"   {a['dex_url']}"
        )
    await wait.edit_text("\n\n".join(lines), disable_web_page_preview=True)


async def cmd_newcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show brand new coins under 1 hour old."""
    wait = await update.message.reply_text("🆕 Finding new coins...")
    async with aiohttp.ClientSession() as session:
        coins = await fetch_new_coins(session)
    if not coins:
        await wait.edit_text("No new coins found right now. Try again in a few minutes. — Kayo 🦅")
        return
    lines = [f"🆕 New Coins (Under 1h Old)\n{'─'*24}\n"]
    for p in coins:
        base    = p.get("baseToken", {})
        symbol  = base.get("symbol", "???")
        address = base.get("address", "")
        price   = float(p.get("priceUsd", 0) or 0)
        fdv     = float(p.get("fdv", 0) or 0)
        liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
        created = p.get("pairCreatedAt", 0)
        age_m   = int((time.time() * 1000 - created) / 60_000) if created else 0
        txns    = int(p.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
        dex_url = p.get("url", f"https://dexscreener.com/solana/{address}")
        lines.append(
            f"${symbol} — {age_m}m old\n"
            f"   Price: {fmt_price(price)} | MCap: {fmt_usd(fdv)}\n"
            f"   Liq: {fmt_usd(liq)} | Buys: {txns}\n"
            f"   {dex_url}"
        )
    lines.append("\nVery early = very risky. DYOR always. — Kayo 🦅")
    await wait.edit_text("\n\n".join(lines), disable_web_page_preview=True)


async def cmd_runners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = await update.message.reply_text("🔭 Finding today's runners...")
    async with aiohttp.ClientSession() as session:
        runners = await fetch_runners(session)
    if not runners:
        await wait.edit_text("Nothing matching right now. Try again soon. — Kayo 🦅")
        return
    lines = [f"🚀 Today's Runners ({len(runners)})\n{'─'*24}\n"]
    for i, p in enumerate(runners, 1):
        base    = p.get("baseToken", {})
        sym     = base.get("symbol", "???")
        addr    = base.get("address", "")
        price   = float(p.get("priceUsd", 0) or 0)
        fdv     = float(p.get("fdv", 0) or 0)
        liq     = float(p.get("liquidity", {}).get("usd", 0) or 0)
        ch1h    = p.get("priceChange", {}).get("h1", 0)
        dex_url = p.get("url", f"https://dexscreener.com/solana/{addr}")
        lines.append(
            f"{i}. ${sym}\n"
            f"   {fmt_price(price)} | MCap: {fmt_usd(fdv)} | Liq: {fmt_usd(liq)}\n"
            f"   1h: {fmt_pct(ch1h)}\n"
            f"   {dex_url}"
        )
    await wait.edit_text("\n\n".join(lines), disable_web_page_preview=True)


async def ask_kayo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /kayo <question>")
        return
    question = " ".join(context.args)
    wait = await update.message.reply_text("🦅 Thinking...")
    uid = update.effective_user.id
    sys = custom_prompts.get(uid, KAYO_SYSTEM)
    reply = await kayo_think(question, system_override=sys)
    await wait.edit_text(reply)


async def cmd_narrative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = await update.message.reply_text("📰 Building today's playbook...")
    async with aiohttp.ClientSession() as session:
        pairs = await dex_search(session, "solana")
    top20   = sorted(pairs, key=lambda x: float(x.get("volume", {}).get("h24", 0) or 0), reverse=True)[:20]
    avg_ch  = sum(float(p.get("priceChange", {}).get("h24", 0) or 0) for p in top20) / max(len(top20), 1)
    top5    = [(p.get("baseToken", {}).get("symbol", "?"), float(p.get("volume", {}).get("h24", 0) or 0)) for p in top20[:5]]
    sentiment = "BULLISH 🟢" if avg_ch > 5 else "NEUTRAL 🟡" if avg_ch > -5 else "BEARISH 🔴"
    kayo_read = await kayo_think(
        f"Solana today: {sentiment}, avg 24h {avg_ch:+.1f}%, top: {', '.join([s for s,_ in top5])}. "
        f"4-line degen playbook."
    )
    now = datetime.utcnow()
    await wait.edit_text(
        f"📰 Kayo's Playbook — {now.strftime('%d %b %H:%M UTC')}\n\n"
        f"Sentiment: {sentiment} | Avg 24h: {avg_ch:+.1f}%\n"
        f"Top Vol: {', '.join([f'${s}' for s,_ in top5])}\n\n"
        f"{kayo_read}\n\n— Kayo 🦅"
    )


async def cmd_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /verify <token_address>")
        return
    address = context.args[0].strip()
    wait = await update.message.reply_text("🕵️ Verifying...")
    if address in KNOWN_TOKENS:
        await wait.edit_text(f"✅ VERIFIED — Official {KNOWN_TOKENS[address]} contract.\n{address}")
        return
    async with aiohttp.ClientSession() as session:
        pair, sec = await asyncio.gather(dex_token(session, address), goplus(session, address))
    if not pair:
        await wait.edit_text("❌ Not found on Solana.")
        return
    base   = pair.get("baseToken", {})
    symbol = base.get("symbol", "").upper()
    known_syms = {v: k for k, v in KNOWN_TOKENS.items()}
    flags = []
    if symbol in known_syms:
        flags.append(f"⚠️ ${symbol} matches known token but DIFFERENT address")
    if sec.get("is_honeypot") == "1": flags.append("🚨 HONEYPOT")
    if sec.get("is_blacklisted") == "1": flags.append("🚨 BLACKLISTED")
    rs = compute_rug_score(sec) if sec else 50
    verdict = "🔴 LIKELY SCAM" if (symbol in known_syms or rs < 30) else "🟡 SUSPICIOUS" if rs < 60 else "🟢 No red flags"
    await wait.edit_text(
        f"🕵️ VERIFY — ${symbol}\n{address}\n\n"
        f"Verdict: {verdict}\nSafety: {rs}/100\n\n" +
        ("\n".join(flags) if flags else "No flags found.")
    )


async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_xp.setdefault(uid, {"xp": 0, "last_claim": None})
    xp   = user_xp[uid]["xp"]
    last = user_xp[uid]["last_claim"]
    can_claim = not last or (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() > 86400
    if can_claim:
        user_xp[uid]["xp"] += 50
        user_xp[uid]["last_claim"] = datetime.utcnow().isoformat()
        xp += 50
        claim_msg = "✅ Daily XP claimed! +50 XP"
    else:
        next_claim = datetime.fromisoformat(last) + timedelta(days=1)
        mins = int((next_claim - datetime.utcnow()).total_seconds() // 60)
        claim_msg = f"Next claim in {mins}m"
    rank = "🥉 Rookie" if xp < 100 else "🥈 Degen" if xp < 500 else "🥇 Alpha" if xp < 2000 else "💎 Chad"
    await update.message.reply_text(f"⭐ Your Rank\n\nXP: {xp}\nRank: {rank}\n{claim_msg}")


async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    await update.message.reply_text(
        f"🕐 World Timezones\n\n"
        f"UTC:      {now.strftime('%H:%M')}\n"
        f"London:   {(now+timedelta(hours=1)).strftime('%H:%M')} BST\n"
        f"New York: {(now-timedelta(hours=4)).strftime('%H:%M')} EDT\n"
        f"LA:       {(now-timedelta(hours=7)).strftime('%H:%M')} PDT\n"
        f"Dubai:    {(now+timedelta(hours=4)).strftime('%H:%M')} GST\n"
        f"Lagos:    {(now+timedelta(hours=1)).strftime('%H:%M')} WAT\n"
        f"Tokyo:    {(now+timedelta(hours=9)).strftime('%H:%M')} JST"
    )


async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = await update.message.reply_text("📊 Fetching macro data...")
    async with aiohttp.ClientSession() as session:
        btc = await coingecko_lookup(session, "bitcoin")
        eth = await coingecko_lookup(session, "ethereum")
        sol = await coingecko_lookup(session, "solana")
        glob = await coingecko_global(session)
    def gp(d): return d.get("market_data",{}).get("current_price",{}).get("usd",0) if d else 0
    def gc(d): return d.get("market_data",{}).get("price_change_percentage_24h",0) if d else 0
    btc_dom = glob.get("market_cap_percentage",{}).get("btc",0)
    total_mcap = glob.get("total_market_cap",{}).get("usd",0)
    await wait.edit_text(
        f"🌍 Macro\n\n"
        f"BTC: {fmt_price(gp(btc))} | {fmt_pct(gc(btc))}\n"
        f"ETH: {fmt_price(gp(eth))} | {fmt_pct(gc(eth))}\n"
        f"SOL: {fmt_price(gp(sol))} | {fmt_pct(gc(sol))}\n\n"
        f"Total MCap: {fmt_usd(total_mcap)}\n"
        f"BTC Dom: {btc_dom:.1f}%"
    )


async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    wait = await update.message.reply_text(f"📊 Top coins (page {page})...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=10&page={page}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                coins = await r.json() if r.status == 200 else []
        except: coins = []
    if not coins:
        await wait.edit_text("❌ Couldn't fetch data.")
        return
    lines = [f"📊 Top Coins (Page {page})\n"]
    for c in coins:
        lines.append(f"#{c.get('market_cap_rank','?')} ${c.get('symbol','').upper()} — {fmt_price(c.get('current_price',0))} | {fmt_pct(c.get('price_change_percentage_24h',0))}")
    lines.append(f"\n/index {page+1} for next page")
    await wait.edit_text("\n".join(lines))


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    wl = watchlists.get(uid, [])
    if not wl:
        await update.message.reply_text("Watchlist empty. Use /f <address> to add tokens.")
        return
    wait = await update.message.reply_text("⭐ Loading watchlist...")
    async with aiohttp.ClientSession() as session:
        pairs = await asyncio.gather(*[dex_token(session, a) for a in wl])
    lines = [f"⭐ Watchlist ({len(wl)})\n"]
    for address, pair in zip(wl, pairs):
        if pair:
            base  = pair.get("baseToken", {})
            price = float(pair.get("priceUsd", 0) or 0)
            ch1h  = pair.get("priceChange", {}).get("h1", "N/A")
            lines.append(f"${base.get('symbol')} — {fmt_price(price)} | 1h: {fmt_pct(ch1h)}")
        else:
            lines.append(f"{address[:12]}... — unavailable")
    await wait.edit_text("\n".join(lines))


async def cmd_f(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        uid = update.effective_user.id
        await update.message.reply_text(f"⭐ {len(watchlists.get(uid,[]))} tokens in watchlist. Use /watchlist to view.")
        return
    address = context.args[0].strip()
    uid = update.effective_user.id
    watchlists.setdefault(uid, [])
    if address in watchlists[uid]:
        await update.message.reply_text("Already in watchlist.")
    else:
        watchlists[uid].insert(0, address)
        watchlists[uid] = watchlists[uid][:20]
        await update.message.reply_text(f"⭐ Added! {len(watchlists[uid])} tokens saved.")


async def cmd_remindme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /remindme 1h30m <message>")
        return
    match = re.match(r'(?:(\d+)h)?(?:(\d+)m)?', context.args[0])
    if not match or not any(match.groups()):
        await update.message.reply_text("Invalid format. Use: 1h30m, 30m, 2h")
        return
    delta = timedelta(hours=int(match.group(1) or 0), minutes=int(match.group(2) or 0))
    text  = " ".join(context.args[1:])
    reminders.append({"chat_id": update.effective_user.id, "text": text, "fire_at": datetime.utcnow() + delta})
    await update.message.reply_text(f"⏰ Reminder set for {context.args[0]}: {text}")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = settings.get(chat_id, {})
    await update.message.reply_text(
        f"⚙️ Settings\n\n"
        f"Buttons:       {'ON ✅' if s.get('buttons', True) else 'OFF'}\n"
        f"Auto-responder:{'ON ✅' if s.get('autoresponder', True) else 'OFF'}\n\n"
        f"/buttons — toggle scan buttons\n"
        f"/autoresponder — toggle auto address scan"
    )


async def cmd_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings.setdefault(chat_id, {})
    current = settings[chat_id].get("buttons", True)
    settings[chat_id]["buttons"] = not current
    await update.message.reply_text(f"Scan buttons: {'ON ✅' if not current else 'OFF ❌'}")


async def cmd_autoresponder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings.setdefault(chat_id, {})
    current = settings[chat_id].get("autoresponder", True)
    settings[chat_id]["autoresponder"] = not current
    await update.message.reply_text(f"Auto address scanner: {'ON ✅' if not current else 'OFF ❌'}")


async def cmd_gp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    points = group_points.get(chat_id, {})
    if not points:
        await update.message.reply_text("No group points yet!")
        return
    sorted_p = sorted(points.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = ["🏆 Group Leaderboard\n"]
    medals = ["🥇","🥈","🥉"]
    for i, (uid, pts) in enumerate(sorted_p, 1):
        m = medals[i-1] if i <= 3 else f"{i}."
        lines.append(f"{m} User {uid} — {pts} pts")
    await update.message.reply_text("\n".join(lines))


# ── Callback handler ──────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("call:"):
        await call_token(update, context)

    elif data.startswith("watch:"):
        address = data.split(":", 1)[1]
        uid = update.effective_user.id
        watchlists.setdefault(uid, [])
        if address not in watchlists[uid]:
            watchlists[uid].insert(0, address)
            await query.message.reply_text("⭐ Added to watchlist!")
        else:
            await query.message.reply_text("Already in watchlist.")

    elif data.startswith("refresh:"):
        address = data.split(":", 1)[1]
        async with aiohttp.ClientSession() as session:
            pair, sec = await asyncio.gather(dex_token(session, address), goplus(session, address))
        if not pair:
            await query.message.reply_text("❌ Couldn't refresh — token data unavailable.")
            return
        rs   = compute_rug_score(sec) if sec else -1
        card = build_card(pair, sec, rs)
        sym  = pair.get("baseToken", {}).get("symbol", "???")
        take = await kayo_token_take(pair, sec, rs)
        try:
            await query.message.edit_text(
                card + f"\n\nKayo's Take:\n{take}\n\nRefreshed: {datetime.utcnow().strftime('%H:%M UTC')}",
                reply_markup=scan_keyboard(address, sym),
                disable_web_page_preview=True
            )
        except: pass


# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    if not msg: return
    text = msg.text or ""
    chat_id = update.effective_chat.id
    bot_username = context.bot.username or ""

    if get_setting(chat_id, "autoresponder", True):
        addresses = SOLANA_RE.findall(text)
        valid = [a for a in addresses if len(a) >= 32]
        if valid:
            address = valid[0]
            uid = update.effective_user.id
            wait = await msg.reply_text("👀 Spotted an address — scanning...")
            async with aiohttp.ClientSession() as session:
                pair, sec = await asyncio.gather(dex_token(session, address), goplus(session, address))
            if pair:
                rs   = compute_rug_score(sec) if sec else -1
                card = build_card(pair, sec, rs)
                take = await kayo_token_take(pair, sec, rs)
                sym  = pair.get("baseToken", {}).get("symbol", "???")
                save_scan(uid, address)
                add_xp(uid, 2)
                kb = scan_keyboard(address, sym) if get_setting(chat_id, "buttons", True) else None
                await wait.edit_text(
                    card + f"\n\nKayo's Take:\n{take}",
                    reply_markup=kb,
                    disable_web_page_preview=True
                )
            else:
                await wait.edit_text("❌ Token not found on Solana.")
            return

    is_mention = f"@{bot_username}".lower() in text.lower()
    is_reply   = (msg.reply_to_message and msg.reply_to_message.from_user
                  and msg.reply_to_message.from_user.username == bot_username)
    if is_mention or is_reply:
        clean = text.replace(f"@{bot_username}", "").strip()
        if not clean: return
        uid = update.effective_user.id
        sys = custom_prompts.get(uid, KAYO_SYSTEM)
        wait = await msg.reply_text("🦅 ...")
        reply = await kayo_think(clean, system_override=sys)
        await wait.edit_text(reply)


# ═══════════════════════════════════════════════════════════════════════════════
#  MENU BUTTON + STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

BOT_COMMANDS = [
    BotCommand("start",        "Show welcome message"),
    BotCommand("scan",         "Full token scan"),
    BotCommand("call",         "Register a call"),
    BotCommand("stop",         "Lock profit from a call"),
    BotCommand("mycalls",      "Live P&L on all calls"),
    BotCommand("leaderboard",  "Weekly top calls"),
    BotCommand("runners",      "Today's early runners"),
    BotCommand("newcoins",     "Brand new coins under 1h"),
    BotCommand("momentum",     "Coins with unusual buy activity"),
    BotCommand("narrative",    "Daily Solana playbook"),
    BotCommand("verify",       "Check if token is fake"),
    BotCommand("watchlist",    "Your saved tokens"),
    BotCommand("f",            "Add token to watchlist"),
    BotCommand("macro",        "BTC ETH SOL overview"),
    BotCommand("index",        "Top 10 coins"),
    BotCommand("kayo",         "Ask Kayo anything"),
    BotCommand("rank",         "Your XP and rank"),
    BotCommand("gp",           "Group leaderboard"),
    BotCommand("remindme",     "Set a personal reminder"),
    BotCommand("tz",           "World timezones"),
    BotCommand("settings",     "Group settings"),
    BotCommand("buttons",      "Toggle scan buttons"),
    BotCommand("autoresponder","Toggle auto address scan"),
]


async def post_init(app: Application):
    # Set menu button so all commands show in Telegram menu
    await app.bot.set_my_commands(BOT_COMMANDS)
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    # Start background tasks
    asyncio.create_task(auto_scan_loop(app))
    asyncio.create_task(new_coin_scanner_loop(app))
    asyncio.create_task(momentum_detector_loop(app))
    asyncio.create_task(milestone_loop(app))
    asyncio.create_task(reminder_loop(app))
    asyncio.create_task(weekly_leaderboard_loop(app))
    logger.info("🦅 Kayo background tasks started.")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    handlers = [
        ("start",         start),
        ("help",          start),
        ("scan",          scan),
        ("call",          call_token),
        ("stop",          cmd_stop),
        ("mycalls",       my_calls),
        ("leaderboard",   cmd_leaderboard),
        ("runners",       cmd_runners),
        ("trending",      cmd_runners),
        ("newcoins",      cmd_newcoins),
        ("momentum",      cmd_momentum),
        ("narrative",     cmd_narrative),
        ("verify",        cmd_verify),
        ("watchlist",     cmd_watchlist),
        ("f",             cmd_f),
        ("macro",         cmd_macro),
        ("markets",       cmd_macro),
        ("index",         cmd_index),
        ("kayo",          ask_kayo),
        ("rank",          cmd_rank),
        ("gp",            cmd_gp),
        ("remindme",      cmd_remindme),
        ("tz",            cmd_tz),
        ("settings",      cmd_settings),
        ("buttons",       cmd_buttons),
        ("autoresponder", cmd_autoresponder),
    ]

    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🦅 Kayo is live.")
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"BOT CRASHED: {e}")


if __name__ == "__main__":
    main()
