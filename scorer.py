"""
Token scoring engine — evaluates pump.fun token quality 0-100.

Tuned for fast memecoin sniping on pump.fun bonding curve tokens.
Scores are calibrated so that ~20-30% of new launches pass the threshold
and generate trades. Brand new tokens get fair baseline scores for
factors that can't be measured yet (holders, volume, buy/sell ratio).
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp

import config
import wallet
from logger import setup_logger

log = setup_logger("scorer")


@dataclass
class TokenScore:
    mint: str
    name: str = ""
    symbol: str = ""
    score: int = 0
    timestamp: float = field(default_factory=time.time)

    # Individual factor scores
    liquidity_score: int = 0
    dev_wallet_score: int = 0
    social_score: int = 0
    mint_authority_score: int = 0
    volume_score: int = 0
    holder_score: int = 0
    honeypot_score: int = 0
    buy_sell_ratio_score: int = 0

    # Raw data for dashboard display
    liquidity_usd: float = 0.0
    dev_wallet_pct: float = 0.0
    has_twitter: bool = False
    has_telegram: bool = False
    has_website: bool = False
    mint_authority_revoked: bool = False
    holder_count: int = 0
    volume_usd: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    market_cap: float = 0.0
    is_honeypot: bool = False
    deployer: str = ""
    token_age_seconds: int = 0
    creator_bought_sol: float = 0.0

    # Bonding curve data (for accurate paper pricing)
    v_sol_in_curve: float = 0.0
    v_tokens_in_curve: float = 0.0
    token_price_usd: float = 0.0  # calculated real price at scoring time

    # Decision
    action: str = "SKIP"  # BUY or SKIP
    skip_reason: str = ""
    bet_size_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Blacklist ─────────────────────────────────────────────────────────

_blacklist: set[str] = set()


def load_blacklist():
    global _blacklist
    if config.BLACKLIST_FILE.exists():
        try:
            data = json.loads(config.BLACKLIST_FILE.read_text())
            _blacklist = set(data.get("deployers", []))
            log.info(f"Loaded {len(_blacklist)} blacklisted deployers")
        except Exception:
            _blacklist = set()


def add_to_blacklist(deployer: str, reason: str = ""):
    _blacklist.add(deployer)
    try:
        data = {"deployers": list(_blacklist)}
        config.BLACKLIST_FILE.write_text(json.dumps(data, indent=2))
        log.warning(f"Blacklisted deployer {deployer[:8]}... — {reason}")
    except Exception as e:
        log.error(f"Failed to save blacklist: {e}")


def is_blacklisted(deployer: str) -> bool:
    return deployer in _blacklist


# ── Scoring Functions (tuned for pump.fun) ────────────────────────────

def _score_liquidity(liquidity_sol: float) -> int:
    """
    Score based on SOL in bonding curve.
    pump.fun starts at 30 virtual SOL. More real SOL = more interest.
    30 SOL (base) = 10pts, 35+ = 12, 50+ = 15, 100+ = 20
    """
    if liquidity_sol < 20:
        return 0
    elif liquidity_sol < 30:
        return 5
    elif liquidity_sol < 35:
        return 10  # base pump.fun token
    elif liquidity_sol < 50:
        return 12
    elif liquidity_sol < 100:
        return 15
    else:
        return 20


def _score_dev_buy(sol_amount: float) -> int:
    """
    Score based on how much SOL the creator spent on the initial buy.
    Creator buying their own token = skin in the game.
    0 SOL = suspicious (possible dump setup), 0.5-3 SOL = healthy, 5+ = very committed.
    But too much (10+) = they hold too much supply.
    """
    if sol_amount <= 0:
        return 3  # no initial buy — neutral (they might buy later)
    elif sol_amount < 0.1:
        return 5  # tiny buy — at least something
    elif sol_amount < 1.0:
        return 10  # moderate — good sign
    elif sol_amount < 3.0:
        return 15  # solid commitment
    elif sol_amount < 5.0:
        return 12  # heavy — could dump
    elif sol_amount < 10.0:
        return 8   # very heavy — risky concentration
    else:
        return 3   # whale dev — high dump risk


def _score_socials(has_twitter: bool, has_telegram: bool, has_website: bool) -> int:
    """Social presence — signals community effort. Max 15."""
    points = 0
    if has_twitter:
        points += 7
    if has_telegram:
        points += 5
    if has_website:
        points += 3
    return min(points, 15)


def _score_mint_authority(revoked: bool) -> int:
    """Mint authority revoked = can't inflate supply."""
    return 15 if revoked else 0


def _score_initial_volume(sol_amount: float) -> int:
    """
    Score based on initial buy volume in SOL.
    For brand new tokens, we use the creator's initial buy as a proxy.
    Any initial buy > 0 means there was volume.
    """
    if sol_amount <= 0:
        return 3  # no volume yet — neutral for new tokens
    elif sol_amount < 0.5:
        return 8
    elif sol_amount < 2.0:
        return 12
    else:
        return 15


def _score_holders_new() -> int:
    """
    For brand new tokens we can't know holder count yet.
    Give a neutral baseline so new tokens aren't penalized.
    """
    return 5  # baseline — not penalized, not rewarded


def _score_honeypot(is_honeypot: bool) -> int:
    """Honeypot = can't sell. Instant disqualification."""
    return -50 if is_honeypot else 5


def _score_name_quality(name: str, symbol: str) -> int:
    """
    Bonus points for tokens with names that suggest effort/meme potential.
    Generic garbage names get 0. Recognizable meme references get points.
    """
    name_lower = (name + " " + symbol).lower()

    # Penalize extremely short or generic names
    if len(name.strip()) < 2 or len(symbol.strip()) < 2:
        return -5

    # Bonus for meme-related keywords (high virality potential)
    meme_keywords = [
        "pepe", "doge", "shib", "wojak", "chad", "moon", "elon", "trump",
        "cat", "dog", "frog", "based", "giga", "sigma", "alpha", "ai",
        "agent", "ape", "bull", "pump", "king", "god", "baby", "maga",
    ]
    for kw in meme_keywords:
        if kw in name_lower:
            return 5

    return 0  # neutral — not great, not terrible


# ── API Data Fetching ─────────────────────────────────────────────────

async def _fetch_rugcheck(mint: str) -> dict:
    """Fetch safety data from rugcheck.xyz API."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        log.debug(f"Rugcheck API error for {mint[:8]}: {e}")
    return {}


# ── Main Scoring Function ────────────────────────────────────────────

async def score_token(
    mint: str,
    name: str = "",
    symbol: str = "",
    extra_data: Optional[dict] = None,
) -> TokenScore:
    """
    Score a token 0-100. Returns TokenScore with breakdown.

    Data sources:
    - extra_data: comes from pump.fun WebSocket (has real-time bonding curve data)
    - rugcheck: safety checks (honeypot, freeze authority)

    WebSocket fields we use:
    - vSolInBondingCurve: virtual SOL in bonding curve (liquidity)
    - marketCapSol: market cap in SOL
    - solAmount: SOL the creator spent on initial buy
    - initialBuy: tokens the creator received
    - traderPublicKey: creator wallet
    - name, symbol, uri
    """
    ts = TokenScore(mint=mint, name=name, symbol=symbol)
    data = extra_data or {}

    # Fetch rugcheck in background (don't block on it)
    rugcheck_task = asyncio.create_task(_fetch_rugcheck(mint))

    # ── Extract fields from WebSocket data ────────────────────────

    # Liquidity: SOL in bonding curve
    liquidity_sol = 0.0
    v_tokens = 0.0
    if "vSolInBondingCurve" in data:
        liquidity_sol = float(data["vSolInBondingCurve"])
    elif "virtual_sol_reserves" in data:
        liquidity_sol = float(data["virtual_sol_reserves"]) / 1e9

    if "vTokensInBondingCurve" in data:
        v_tokens = float(data["vTokensInBondingCurve"])
    elif "virtual_token_reserves" in data:
        v_tokens = float(data["virtual_token_reserves"])

    # Store bonding curve data for paper trading price calculation
    ts.v_sol_in_curve = liquidity_sol
    ts.v_tokens_in_curve = v_tokens

    # Get SOL price for USD conversion
    sol_price = await wallet.get_sol_price_usd()
    if sol_price <= 0:
        sol_price = 80.0  # fallback estimate

    ts.liquidity_usd = liquidity_sol * sol_price

    # Calculate real token price from bonding curve: price = (vSol / vTokens) * solPrice
    if liquidity_sol > 0 and v_tokens > 0:
        ts.token_price_usd = (liquidity_sol / v_tokens) * sol_price
    elif ts.market_cap > 0:
        # Fallback: market cap / 1 billion (standard pump.fun supply)
        ts.token_price_usd = ts.market_cap / 1_000_000_000

    # Market cap
    mcap_sol = float(data.get("marketCapSol", 0) or 0)
    ts.market_cap = mcap_sol * sol_price
    if ts.market_cap == 0:
        ts.market_cap = float(data.get("usd_market_cap", 0) or 0)

    # Creator's initial buy
    ts.creator_bought_sol = float(data.get("solAmount", 0) or 0)
    ts.deployer = data.get("traderPublicKey", "") or data.get("creator", "")

    # Dev wallet concentration: estimate from initial buy vs bonding curve
    if liquidity_sol > 0 and ts.creator_bought_sol > 0:
        # Rough estimate: what % of the curve did the dev buy?
        ts.dev_wallet_pct = (ts.creator_bought_sol / liquidity_sol) * 100
    else:
        ts.dev_wallet_pct = 5.0  # assume moderate if unknown

    # Socials: check if URI metadata has socials (from pump.fun API)
    ts.has_twitter = bool(data.get("twitter"))
    ts.has_telegram = bool(data.get("telegram"))
    ts.has_website = bool(data.get("website"))

    # Mint authority: pump.fun tokens always have mint authority revoked
    ts.mint_authority_revoked = True

    # Volume: use creator's initial buy SOL as proxy for new tokens
    ts.volume_usd = ts.creator_bought_sol * sol_price

    # Token age
    ts.token_age_seconds = 0  # brand new from WebSocket

    # ── Rugcheck data (if available) ──────────────────────────────

    ts.is_honeypot = False
    try:
        rugcheck_data = await asyncio.wait_for(rugcheck_task, timeout=3.0)
    except (asyncio.TimeoutError, Exception):
        rugcheck_data = {}

    if rugcheck_data:
        score_label = rugcheck_data.get("score", "")
        if isinstance(score_label, str) and score_label.lower() in ("danger", "rugged"):
            ts.is_honeypot = True

        risks = rugcheck_data.get("risks", [])
        for r in risks:
            risk_name = str(r.get("name", "")).lower()
            if "honeypot" in risk_name or "freeze" in risk_name:
                ts.is_honeypot = True
            if "mint" in risk_name and "authority" in risk_name:
                ts.mint_authority_revoked = False

            # Try to get better dev wallet % from rugcheck
            if "top" in risk_name and "holder" in risk_name:
                desc = str(r.get("description", ""))
                pct_match = re.search(r"(\d+\.?\d*)%", desc)
                if pct_match:
                    ts.dev_wallet_pct = float(pct_match.group(1))

    # ── Calculate scores ──────────────────────────────────────────

    ts.liquidity_score = _score_liquidity(liquidity_sol)
    ts.dev_wallet_score = _score_dev_buy(ts.creator_bought_sol)
    ts.social_score = _score_socials(ts.has_twitter, ts.has_telegram, ts.has_website)
    ts.mint_authority_score = _score_mint_authority(ts.mint_authority_revoked)
    ts.volume_score = _score_initial_volume(ts.creator_bought_sol)
    ts.holder_score = _score_holders_new()
    ts.honeypot_score = _score_honeypot(ts.is_honeypot)
    ts.buy_sell_ratio_score = _score_name_quality(name, symbol)

    # Total score (clamped 0-100)
    raw_score = (
        ts.liquidity_score
        + ts.dev_wallet_score
        + ts.social_score
        + ts.mint_authority_score
        + ts.volume_score
        + ts.holder_score
        + ts.honeypot_score
        + ts.buy_sell_ratio_score
    )
    ts.score = max(0, min(100, raw_score))

    # ── Instant disqualifiers ─────────────────────────────────────

    if ts.is_honeypot:
        ts.score = 0
        ts.action = "SKIP"
        ts.skip_reason = "Honeypot detected"
        log.warning(f"{symbol} ({mint[:8]}) — HONEYPOT, score → 0")
        return ts

    if is_blacklisted(ts.deployer):
        ts.score = 0
        ts.action = "SKIP"
        ts.skip_reason = "Blacklisted deployer"
        log.warning(f"{symbol} ({mint[:8]}) — blacklisted deployer")
        return ts

    # Dev holds way too much (>50% of curve) — likely dump
    if ts.dev_wallet_pct > 50:
        ts.score = min(ts.score, 15)
        ts.skip_reason = "Dev holds >50% of supply"

    # ── Decision ──────────────────────────────────────────────────

    if ts.score >= config.MIN_SCORE_TO_TRADE:
        ts.action = "BUY"
        if ts.score >= 75:
            ts.bet_size_usd = config.BET_HIGH
        elif ts.score >= 55:
            ts.bet_size_usd = config.BET_MED
        else:
            ts.bet_size_usd = config.BET_LOW
    else:
        ts.action = "SKIP"
        if not ts.skip_reason:
            ts.skip_reason = f"Score {ts.score} < {config.MIN_SCORE_TO_TRADE}"

    log.info(
        f"{symbol} ({mint[:8]}) — Score: {ts.score}/100 "
        f"[liq:{ts.liquidity_score} dev:{ts.dev_wallet_score} "
        f"social:{ts.social_score} mint:{ts.mint_authority_score} "
        f"vol:{ts.volume_score} hold:{ts.holder_score} "
        f"hp:{ts.honeypot_score} name:{ts.buy_sell_ratio_score}] "
        f"→ {ts.action}"
        + (f" (${ts.bet_size_usd})" if ts.action == "BUY" else f" ({ts.skip_reason})")
    )

    return ts


# Load blacklist on import
load_blacklist()
