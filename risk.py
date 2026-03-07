"""
Risk management engine — circuit breakers and position limits.

Rules:
1. Max open positions: configurable (default 3)
2. Max per trade: 30% of total balance
3. Daily loss limit: -40% → pause bot
4. Consecutive stop-loss cooldown: 3 losses → pause 30 min
5. Per-token exposure cap
"""

import time
from dataclasses import dataclass
from typing import Optional

import config
import trader
import wallet
from logger import setup_logger

log = setup_logger("risk")


@dataclass
class RiskState:
    consecutive_losses: int = 0
    daily_pnl_usd: float = 0.0
    daily_trades: int = 0
    cooldown_until: float = 0.0
    paused: bool = False
    pause_reason: str = ""
    last_reset_day: str = ""


_state = RiskState()


def _today_str() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _reset_daily_if_needed():
    """Reset daily counters at midnight."""
    today = _today_str()
    if _state.last_reset_day != today:
        _state.daily_pnl_usd = 0.0
        _state.daily_trades = 0
        _state.consecutive_losses = 0
        _state.last_reset_day = today
        _state.paused = False
        _state.pause_reason = ""
        log.info("Daily risk counters reset")


def get_risk_state() -> dict:
    """Get current risk state for dashboard."""
    _reset_daily_if_needed()
    return {
        "consecutive_losses": _state.consecutive_losses,
        "daily_pnl_usd": round(_state.daily_pnl_usd, 2),
        "daily_trades": _state.daily_trades,
        "paused": _state.paused,
        "pause_reason": _state.pause_reason,
        "cooldown_until": _state.cooldown_until,
        "in_cooldown": time.time() < _state.cooldown_until,
        "cooldown_remaining_s": max(0, int(_state.cooldown_until - time.time())),
    }


async def can_open_position(bet_usd: float, sol_price: float) -> tuple[bool, str]:
    """
    Check all risk rules before opening a new position.
    Returns (allowed: bool, reason: str).
    """
    _reset_daily_if_needed()

    # 1. Check if bot is paused
    if _state.paused:
        return False, f"Bot paused: {_state.pause_reason}"

    # 2. Check cooldown
    if time.time() < _state.cooldown_until:
        remaining = int(_state.cooldown_until - time.time())
        return False, f"In cooldown ({remaining}s remaining after {config.COOLDOWN_AFTER_LOSSES} consecutive losses)"

    # 3. Check max open positions (0 = unlimited)
    if config.MAX_POSITIONS > 0:
        open_positions = trader.get_open_positions()
        if len(open_positions) >= config.MAX_POSITIONS:
            return False, f"Max positions reached ({len(open_positions)}/{config.MAX_POSITIONS})"

    # 4. Check daily loss limit
    balance_usd = (await wallet.get_sol_balance()) * sol_price
    if balance_usd > 0:
        daily_loss_pct = abs(_state.daily_pnl_usd) / balance_usd if _state.daily_pnl_usd < 0 else 0
        if daily_loss_pct >= config.DAILY_LOSS_LIMIT_PCT:
            _state.paused = True
            _state.pause_reason = f"Daily loss limit hit ({daily_loss_pct*100:.1f}%)"
            log.warning(f"RISK: {_state.pause_reason}")
            return False, _state.pause_reason

    # 5. Check position sizing vs balance
    balance_sol = await wallet.get_sol_balance()
    bet_sol = bet_usd / sol_price if sol_price > 0 else 0
    max_sol = balance_sol * config.MAX_POSITION_PCT

    if bet_sol > max_sol:
        return False, f"Bet size {bet_sol:.4f} SOL exceeds max {max_sol:.4f} SOL ({config.MAX_POSITION_PCT*100}% of balance)"

    if bet_sol > balance_sol - 0.01:
        return False, f"Insufficient balance: {balance_sol:.4f} SOL"

    # 6. Capital reserve — always keep minimum SOL undeployed
    if balance_sol - bet_sol < config.RESERVE_BALANCE_SOL:
        return False, (
            f"Capital reserve: balance after trade ({balance_sol - bet_sol:.4f} SOL) "
            f"would drop below floor ({config.RESERVE_BALANCE_SOL} SOL)"
        )

    return True, "OK"


def record_trade_result(pnl_usd: float, is_stop_loss: bool):
    """
    Record a trade result for risk tracking.
    Call this after every sell.
    """
    _reset_daily_if_needed()

    _state.daily_pnl_usd += pnl_usd
    _state.daily_trades += 1

    if is_stop_loss:
        _state.consecutive_losses += 1
        log.info(f"Consecutive losses: {_state.consecutive_losses}/{config.COOLDOWN_AFTER_LOSSES}")

        if _state.consecutive_losses >= config.COOLDOWN_AFTER_LOSSES:
            _state.cooldown_until = time.time() + (config.COOLDOWN_MINUTES * 60)
            log.warning(
                f"RISK: {config.COOLDOWN_AFTER_LOSSES} consecutive stop-losses → "
                f"cooldown for {config.COOLDOWN_MINUTES} minutes"
            )
            _state.consecutive_losses = 0  # reset counter
    else:
        # A winning trade resets the consecutive loss counter
        if pnl_usd > 0:
            _state.consecutive_losses = 0


def force_pause(reason: str):
    """Manually pause the bot."""
    _state.paused = True
    _state.pause_reason = reason
    log.warning(f"Bot manually paused: {reason}")


def resume():
    """Resume the bot from a pause."""
    _state.paused = False
    _state.pause_reason = ""
    _state.cooldown_until = 0
    log.info("Bot resumed")


def is_paused() -> bool:
    """Check if bot is paused or in cooldown."""
    _reset_daily_if_needed()
    if _state.paused:
        return True
    if time.time() < _state.cooldown_until:
        return True
    return False
