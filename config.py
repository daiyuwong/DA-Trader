"""
Configuration loader — reads .env and validates all settings.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)


def _get(key: str, default=None, cast=str, required=False):
    val = os.getenv(key, default)
    if required and val is None:
        print(f"[FATAL] Missing required env var: {key}")
        sys.exit(1)
    if val is None:
        return None
    try:
        return cast(val)
    except (ValueError, TypeError):
        print(f"[FATAL] Invalid value for {key}: {val}")
        sys.exit(1)


def _bool(v):
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


# ── Solana ────────────────────────────────────────────────────────────
SOLANA_RPC_URL = _get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_WS_URL = _get("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")
WALLET_PRIVATE_KEY = _get("WALLET_PRIVATE_KEY", required=False)  # not required in paper mode

# Backup RPCs for failover
BACKUP_RPC_URLS = [
    url.strip()
    for url in _get("BACKUP_RPC_URLS", "").split(",")
    if url.strip()
]

# ── Trading Rules ─────────────────────────────────────────────────────
MAX_POSITIONS = _get("MAX_POSITIONS", 3, int)
STOP_LOSS_PCT = _get("STOP_LOSS_PCT", 0.50, float)         # -50% hard stop
TRAILING_STOP_PCT = _get("TRAILING_STOP_PCT", 0.30, float) # 30% trailing from peak
TAKE_PROFIT_1 = _get("TAKE_PROFIT_1", 2.0, float)          # 2x → sell 50%
TAKE_PROFIT_2 = _get("TAKE_PROFIT_2", 5.0, float)          # 5x → sell remaining
MIN_SCORE_TO_TRADE = _get("MIN_SCORE_TO_TRADE", 40, int)
DAILY_LOSS_LIMIT_PCT = _get("DAILY_LOSS_LIMIT_PCT", 0.40, float)
COOLDOWN_AFTER_LOSSES = _get("COOLDOWN_AFTER_LOSSES", 3, int)
COOLDOWN_MINUTES = _get("COOLDOWN_MINUTES", 30, int)
MAX_POSITION_PCT = _get("MAX_POSITION_PCT", 0.30, float)   # max 30% of balance per trade

# ── Bet Sizing (USD equivalent in SOL) ────────────────────────────────
BET_HIGH = _get("BET_HIGH", 9.0, float)
BET_MED = _get("BET_MED", 5.0, float)
BET_LOW = _get("BET_LOW", 2.50, float)

# ── Scanner Settings ──────────────────────────────────────────────────
MOMENTUM_SCAN_INTERVAL = _get("MOMENTUM_SCAN_INTERVAL", 60, int)   # seconds
MOMENTUM_MIN_AGE = _get("MOMENTUM_MIN_AGE", 300, int)              # 5 min
MOMENTUM_MAX_AGE = _get("MOMENTUM_MAX_AGE", 3600, int)             # 60 min
MOMENTUM_PRICE_CHANGE_MIN = _get("MOMENTUM_PRICE_CHANGE_MIN", 0.20, float)  # +20%
MOMENTUM_PRICE_CHANGE_MAX = _get("MOMENTUM_PRICE_CHANGE_MAX", 0.50, float)  # +50%
MOMENTUM_MAX_MCAP = _get("MOMENTUM_MAX_MCAP", 500_000, float)      # $500K

# ── Monitor Settings ──────────────────────────────────────────────────
PRICE_CHECK_INTERVAL = _get("PRICE_CHECK_INTERVAL", 10, int)       # seconds
RUG_LIQUIDITY_DROP_PCT = _get("RUG_LIQUIDITY_DROP_PCT", 0.70, float)
RUG_LIQUIDITY_WINDOW = _get("RUG_LIQUIDITY_WINDOW", 60, int)       # seconds
MAX_POSITION_AGE_MINUTES = _get("MAX_POSITION_AGE_MINUTES", 15, int)  # force-exit after N min if <+10%

# ── Capital Reserve ────────────────────────────────────────────────────
RESERVE_BALANCE_SOL = _get("RESERVE_BALANCE_SOL", 0.10, float)     # minimum SOL to keep undeployed

# ── Anti-MEV ──────────────────────────────────────────────────────────
SLIPPAGE_BPS = _get("SLIPPAGE_BPS", 300, int)          # 3% default slippage
PRIORITY_FEE_LAMPORTS = _get("PRIORITY_FEE_LAMPORTS", 50000, int)  # priority fee
JITO_TIP_LAMPORTS = _get("JITO_TIP_LAMPORTS", 0, int)  # optional Jito tip

# ── Dashboard ─────────────────────────────────────────────────────────
DASHBOARD_PORT = _get("DASHBOARD_PORT", 5000, int)
DASHBOARD_HOST = _get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PASSWORD = _get("DASHBOARD_PASSWORD", "changeme")

# ── Mode ──────────────────────────────────────────────────────────────
PAPER_MODE = _get("PAPER_MODE", "true", _bool)
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()

# ── Paths ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
TRADES_FILE = DATA_DIR / "trades.json"
BLACKLIST_FILE = DATA_DIR / "blacklist.json"

# Ensure dirs exist
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ── Jupiter API ───────────────────────────────────────────────────────
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

# ── Pump.fun ──────────────────────────────────────────────────────────
PUMPFUN_WS_URL = "wss://pumpportal.fun/api/data"
PUMPFUN_API_URL = "https://frontend-api.pump.fun"

# ── Token Addresses ───────────────────────────────────────────────────
SOL_MINT = "So11111111111111111111111111111111111111112"
WSOL_MINT = SOL_MINT  # wrapped SOL

# ── Validation ────────────────────────────────────────────────────────
if not PAPER_MODE and not WALLET_PRIVATE_KEY:
    print("[FATAL] WALLET_PRIVATE_KEY required when PAPER_MODE=false")
    sys.exit(1)

if STOP_LOSS_PCT <= 0 or STOP_LOSS_PCT >= 1:
    print("[FATAL] STOP_LOSS_PCT must be between 0 and 1")
    sys.exit(1)
