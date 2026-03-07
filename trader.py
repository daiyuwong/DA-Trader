"""
Trade execution — buys and sells via Jupiter Aggregator API.

Improvements:
- Anti-MEV: randomized slippage within a range + priority fees
- Retry logic with exponential backoff
- Paper mode simulation with realistic price tracking
- Transaction confirmation with timeout
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config
import wallet
from logger import setup_logger

log = setup_logger("trader")


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIAL_EXIT = "partial_exit"   # hit TP1, sold 50%
    CLOSED = "closed"
    FAILED = "failed"


class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Position:
    mint: str
    symbol: str
    name: str
    entry_price_usd: float = 0.0
    entry_sol: float = 0.0
    entry_usd: float = 0.0
    current_price_usd: float = 0.0
    highest_price_usd: float = 0.0   # for trailing stop
    token_amount: float = 0.0
    remaining_pct: float = 1.0       # 1.0 = full, 0.5 = half (after TP1)
    status: TradeStatus = TradeStatus.OPEN
    score: int = 0
    entry_time: float = field(default_factory=time.time)
    exit_time: float = 0.0
    exit_price_usd: float = 0.0
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""
    tx_buy: str = ""
    tx_sell: str = ""

    # Liquidity tracking for rug detection
    entry_liquidity: float = 0.0
    current_liquidity: float = 0.0
    liquidity_history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def update_price(self, price: float):
        self.current_price_usd = price
        if price > self.highest_price_usd:
            self.highest_price_usd = price
        if self.entry_price_usd > 0:
            self.pnl_pct = (price - self.entry_price_usd) / self.entry_price_usd
            self.pnl_usd = (price - self.entry_price_usd) * self.token_amount * self.remaining_pct


@dataclass
class TradeRecord:
    mint: str
    symbol: str
    action: str
    amount_sol: float
    amount_usd: float
    price_usd: float
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    tx_sig: str = ""
    score: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Trade History Persistence ─────────────────────────────────────────

_trade_history: list[TradeRecord] = []
_positions: dict[str, Position] = {}  # mint -> Position


def _load_history():
    global _trade_history
    if config.TRADES_FILE.exists():
        try:
            data = json.loads(config.TRADES_FILE.read_text())
            _trade_history = [TradeRecord(**r) for r in data]
            log.info(f"Loaded {len(_trade_history)} historical trades")
        except Exception as e:
            log.warning(f"Failed to load trade history: {e}")
            _trade_history = []


def _save_history():
    try:
        config.TRADES_FILE.write_text(
            json.dumps([r.to_dict() for r in _trade_history], indent=2)
        )
    except Exception as e:
        log.error(f"Failed to save trade history: {e}")


def get_positions() -> dict[str, Position]:
    return _positions


def get_open_positions() -> list[Position]:
    return [p for p in _positions.values() if p.status in (TradeStatus.OPEN, TradeStatus.PARTIAL_EXIT)]


def get_trade_history() -> list[TradeRecord]:
    return _trade_history


def get_today_trades() -> list[TradeRecord]:
    """Get trades from today only."""
    import datetime
    today = datetime.date.today()
    return [
        t for t in _trade_history
        if datetime.date.fromtimestamp(t.timestamp) == today
    ]


def get_stats() -> dict:
    """Calculate all-time trading statistics."""
    total = len(_trade_history)
    sells = [t for t in _trade_history if t.action == "SELL"]
    wins = [t for t in sells if t.pnl_pct > 0]
    losses = [t for t in sells if t.pnl_pct <= 0]

    total_pnl_usd = sum(t.pnl_usd for t in sells)
    best_trade = max(sells, key=lambda t: t.pnl_pct) if sells else None
    worst_trade = min(sells, key=lambda t: t.pnl_pct) if sells else None

    return {
        "total_trades": total,
        "total_sells": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(sells) * 100) if sells else 0,
        "total_pnl_usd": round(total_pnl_usd, 2),
        "best_trade": {
            "symbol": best_trade.symbol,
            "pnl_pct": round(best_trade.pnl_pct * 100, 1),
        } if best_trade else None,
        "worst_trade": {
            "symbol": worst_trade.symbol,
            "pnl_pct": round(worst_trade.pnl_pct * 100, 1),
        } if worst_trade else None,
    }


# ── Jupiter API Swap ──────────────────────────────────────────────────

async def _get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int,
) -> Optional[dict]:
    """Get a swap quote from Jupiter."""
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                config.JUPITER_QUOTE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    log.error(f"Jupiter quote error {resp.status}: {text}")
                    return None
    except Exception as e:
        log.error(f"Jupiter quote request failed: {e}")
        return None


async def _execute_swap(quote: dict) -> Optional[str]:
    """Execute a swap using Jupiter swap API and sign with our wallet."""
    try:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet.get_pubkey_str(),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": config.PRIORITY_FEE_LAMPORTS,
            "dynamicComputeUnitLimit": True,
        }

        if config.JITO_TIP_LAMPORTS > 0:
            payload["jitoTipLamports"] = config.JITO_TIP_LAMPORTS

        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.JUPITER_SWAP_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"Jupiter swap error {resp.status}: {text}")
                    return None

                data = await resp.json()
                swap_tx_b64 = data.get("swapTransaction")
                if not swap_tx_b64:
                    log.error("No swapTransaction in response")
                    return None

        # Deserialize, sign, send
        import base64
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)

        # Sign with our keypair
        kp = wallet.get_keypair()
        signed_tx = VersionedTransaction(tx.message, [kp])

        # Send
        sig = await wallet.send_transaction(bytes(signed_tx))
        log.info(f"Transaction sent: {sig}")

        # Confirm
        confirmed = await wallet.confirm_transaction(sig)
        if confirmed:
            log.info(f"Transaction confirmed: {sig}")
            return sig
        else:
            log.error(f"Transaction not confirmed: {sig}")
            return None

    except Exception as e:
        log.error(f"Swap execution failed: {e}")
        return None


# ── Real Price Fetch ─────────────────────────────────────────────────

async def _fetch_real_token_price(mint: str, sol_price: float) -> float:
    """
    Fetch the real current price of a token from pump.fun bonding curve.
    Uses the formula: price = vSolInBondingCurve / vTokensInBondingCurve * sol_price
    Falls back to DexScreener if pump.fun API is unavailable.
    """
    # Try pump.fun API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config.PUMPFUN_API_URL}/coins/{mint}",
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    vsol = float(data.get("virtual_sol_reserves", 0) or 0)
                    vtokens = float(data.get("virtual_token_reserves", 0) or 0)
                    if vsol > 0 and vtokens > 0:
                        # Real bonding curve price formula
                        price_in_sol = vsol / vtokens
                        return price_in_sol * sol_price
                    # Fallback: market cap / supply
                    mcap = float(data.get("usd_market_cap", 0) or 0)
                    supply = float(data.get("total_supply", 0) or 0)
                    if mcap > 0 and supply > 0:
                        return mcap / supply
    except Exception as e:
        log.debug(f"pump.fun price fetch error for {mint[:8]}: {e}")

    # Try DexScreener
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        return float(pairs[0].get("priceUsd", 0) or 0)
    except Exception as e:
        log.debug(f"DexScreener price fetch error for {mint[:8]}: {e}")

    return 0.0


# ── Buy / Sell ────────────────────────────────────────────────────────

async def buy_token(
    mint: str,
    symbol: str,
    name: str,
    amount_usd: float,
    score: int,
    sol_price: float,
    token_price_usd: float = 0.0,
) -> Optional[Position]:
    """
    Buy a token. Returns a Position if successful.
    token_price_usd: real price from WebSocket bonding curve (for paper mode).
    """
    amount_sol = amount_usd / sol_price if sol_price > 0 else 0
    if amount_sol <= 0:
        log.error(f"Invalid buy amount: ${amount_usd} at SOL=${sol_price}")
        return None

    # Check balance
    balance = await wallet.get_sol_balance()
    max_allowed = balance * config.MAX_POSITION_PCT
    if amount_sol > max_allowed:
        amount_sol = max_allowed
        amount_usd = amount_sol * sol_price
        log.warning(f"Capped buy to {config.MAX_POSITION_PCT*100}% of balance: {amount_sol:.4f} SOL")

    if amount_sol > balance - 0.01:  # keep 0.01 SOL for fees
        log.warning(f"Insufficient balance: {balance:.4f} SOL, need {amount_sol:.4f}")
        return None

    log.info(f"BUYING {symbol}: {amount_sol:.4f} SOL (${amount_usd:.2f})")

    if config.PAPER_MODE:
        # Use the real price from WebSocket bonding curve data (already calculated by scorer)
        # Fall back to API fetch only if not provided
        real_price = token_price_usd
        if real_price <= 0:
            real_price = await _fetch_real_token_price(mint, sol_price)
        if real_price <= 0:
            log.warning(f"[PAPER] No price available for {symbol}, skipping")
            return None

        # Simulate realistic slippage (3% default + random variance)
        slippage_pct = (config.SLIPPAGE_BPS / 10000) + random.uniform(0, 0.01)
        # We pay MORE than market price when buying (slippage works against us)
        effective_entry_price = real_price * (1 + slippage_pct)

        # Calculate realistic token amount we'd receive
        token_amount = amount_usd / effective_entry_price

        wallet.paper_deduct(amount_sol)

        position = Position(
            mint=mint,
            symbol=symbol,
            name=name,
            entry_price_usd=effective_entry_price,
            current_price_usd=effective_entry_price,
            highest_price_usd=effective_entry_price,
            entry_sol=amount_sol,
            entry_usd=amount_usd,
            token_amount=token_amount,
            status=TradeStatus.OPEN,
            score=score,
            tx_buy=f"PAPER_BUY_{int(time.time())}",
        )
        _positions[mint] = position

        record = TradeRecord(
            mint=mint,
            symbol=symbol,
            action="BUY",
            amount_sol=amount_sol,
            amount_usd=amount_usd,
            price_usd=effective_entry_price,
            score=score,
            tx_sig=position.tx_buy,
        )
        _trade_history.append(record)
        _save_history()

        log.info(
            f"[PAPER] Bought {symbol} — "
            f"price: ${effective_entry_price:.8f} "
            f"(+{slippage_pct*100:.1f}% slippage) "
            f"tokens: {token_amount:,.0f} "
            f"cost: {amount_sol:.4f} SOL"
        )
        return position

    # Real mode — execute via Jupiter
    amount_lamports = int(amount_sol * 1e9)

    # Anti-MEV: randomize slippage slightly
    slippage = config.SLIPPAGE_BPS + random.randint(-50, 100)

    quote = await _get_quote(
        input_mint=config.SOL_MINT,
        output_mint=mint,
        amount_lamports=amount_lamports,
        slippage_bps=slippage,
    )

    if not quote:
        log.error(f"Failed to get quote for {symbol}")
        return None

    # Get expected output
    out_amount = int(quote.get("outAmount", 0))
    if out_amount <= 0:
        log.error(f"Zero output amount for {symbol}")
        return None

    sig = await _execute_swap(quote)
    if not sig:
        log.error(f"Failed to execute buy for {symbol}")
        return None

    # Calculate entry price from quote
    # outAmount is in token base units, inAmount is in lamports
    in_lamports = int(quote.get("inAmount", amount_lamports))
    in_sol = in_lamports / 1e9
    in_usd = in_sol * sol_price

    # We need to know decimals to convert out_amount to actual token count
    # For now estimate — monitor will correct with actual balance
    entry_price = in_usd / out_amount if out_amount > 0 else 0

    position = Position(
        mint=mint,
        symbol=symbol,
        name=name,
        entry_price_usd=entry_price,
        entry_sol=in_sol,
        entry_usd=in_usd,
        token_amount=out_amount,
        status=TradeStatus.OPEN,
        score=score,
        tx_buy=sig,
    )
    _positions[mint] = position

    record = TradeRecord(
        mint=mint,
        symbol=symbol,
        action="BUY",
        amount_sol=in_sol,
        amount_usd=in_usd,
        price_usd=entry_price,
        score=score,
        tx_sig=sig,
    )
    _trade_history.append(record)
    _save_history()

    log.info(f"Bought {symbol}: {in_sol:.4f} SOL — tx: {sig}")
    return position


async def sell_token(
    position: Position,
    pct: float,
    reason: str,
    sol_price: float,
) -> bool:
    """
    Sell a position (or part of it).
    pct: 0.0-1.0 (e.g., 0.5 = sell half)
    """
    if position.status == TradeStatus.CLOSED:
        log.warning(f"{position.symbol} already closed")
        return False

    sell_amount = int(position.token_amount * pct * position.remaining_pct)
    if sell_amount <= 0:
        log.warning(f"Nothing to sell for {position.symbol}")
        return False

    log.info(f"SELLING {pct*100:.0f}% of {position.symbol} — reason: {reason}")

    if config.PAPER_MODE:
        # Simulate realistic slippage on sell (we receive LESS than market price)
        slippage_pct = (config.SLIPPAGE_BPS / 10000) + random.uniform(0, 0.01)
        effective_sell_price = position.current_price_usd * (1 - slippage_pct)

        sell_usd = effective_sell_price * sell_amount
        sell_sol = sell_usd / sol_price if sol_price > 0 else 0
        wallet.paper_credit(sell_sol)

        pnl_pct = (effective_sell_price - position.entry_price_usd) / position.entry_price_usd
        pnl_usd = (effective_sell_price - position.entry_price_usd) * sell_amount

        if pct >= 1.0 or position.remaining_pct * (1 - pct) < 0.01:
            position.status = TradeStatus.CLOSED
            position.exit_time = time.time()
            position.exit_price_usd = position.current_price_usd
            position.exit_reason = reason
            position.remaining_pct = 0
        else:
            position.status = TradeStatus.PARTIAL_EXIT
            position.remaining_pct *= (1 - pct)

        record = TradeRecord(
            mint=position.mint,
            symbol=position.symbol,
            action="SELL",
            amount_sol=sell_sol,
            amount_usd=sell_usd,
            price_usd=position.current_price_usd,
            pnl_pct=pnl_pct,
            pnl_usd=pnl_usd,
            reason=reason,
            tx_sig=f"PAPER_SELL_{int(time.time())}",
            score=position.score,
        )
        _trade_history.append(record)
        _save_history()

        log.info(
            f"[PAPER] Sold {pct*100:.0f}% of {position.symbol} — "
            f"PnL: {pnl_pct*100:+.1f}% (${pnl_usd:+.2f}) — {reason}"
        )
        return True

    # Real mode
    slippage = config.SLIPPAGE_BPS + random.randint(-50, 100)

    quote = await _get_quote(
        input_mint=position.mint,
        output_mint=config.SOL_MINT,
        amount_lamports=sell_amount,
        slippage_bps=slippage,
    )

    if not quote:
        log.error(f"Failed to get sell quote for {position.symbol}")
        return False

    sig = await _execute_swap(quote)
    if not sig:
        log.error(f"Failed to execute sell for {position.symbol}")
        return False

    # Calculate PnL
    out_lamports = int(quote.get("outAmount", 0))
    out_sol = out_lamports / 1e9
    out_usd = out_sol * sol_price

    pnl_usd = out_usd - (position.entry_usd * pct * position.remaining_pct)
    pnl_pct = position.pnl_pct

    if pct >= 1.0 or position.remaining_pct * (1 - pct) < 0.01:
        position.status = TradeStatus.CLOSED
        position.exit_time = time.time()
        position.exit_price_usd = position.current_price_usd
        position.exit_reason = reason
        position.remaining_pct = 0
    else:
        position.status = TradeStatus.PARTIAL_EXIT
        position.remaining_pct *= (1 - pct)

    record = TradeRecord(
        mint=position.mint,
        symbol=position.symbol,
        action="SELL",
        amount_sol=out_sol,
        amount_usd=out_usd,
        price_usd=position.current_price_usd,
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        reason=reason,
        tx_sig=sig,
        score=position.score,
    )
    _trade_history.append(record)
    _save_history()

    log.info(
        f"Sold {pct*100:.0f}% of {position.symbol} — "
        f"PnL: {pnl_pct*100:+.1f}% (${pnl_usd:+.2f}) — {reason} — tx: {sig}"
    )
    return True


# Load history on import
_load_history()
