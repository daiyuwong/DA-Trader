"""
Position monitor — watches open positions and triggers exits.

Exit triggers:
1. Take-profit 1: 2x → sell 50%
2. Take-profit 2: 5x → sell remaining
3. Stop-loss: -50% → sell all
4. Trailing stop: -30% from peak (activated after 1.5x) → sell all
5. Rug detection: liquidity drops 70%+ in 60s → emergency sell
"""

import asyncio
import time

import aiohttp

import config
import trader
import wallet
from logger import setup_logger
from scorer import add_to_blacklist

log = setup_logger("monitor")


class PositionMonitor:
    def __init__(self):
        self._running = False

    async def start(self):
        self._running = True
        log.info(f"Position monitor started (check interval: {config.PRICE_CHECK_INTERVAL}s)")

        while self._running:
            try:
                await self._check_all_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Monitor error: {e}")

            if self._running:
                await asyncio.sleep(config.PRICE_CHECK_INTERVAL)

    def stop(self):
        self._running = False

    async def _check_all_positions(self):
        """Check all open positions for exit conditions."""
        positions = trader.get_open_positions()
        if not positions:
            return

        sol_price = await wallet.get_sol_price_usd()
        if sol_price <= 0:
            log.warning("Could not get SOL price, skipping monitor cycle")
            return

        for position in positions:
            if not self._running:
                break

            try:
                await self._check_position(position, sol_price)
            except Exception as e:
                log.error(f"Error checking {position.symbol}: {e}")

            # Small delay between position checks
            await asyncio.sleep(0.5)

    async def _check_position(self, position: trader.Position, sol_price: float):
        """Check a single position for exit triggers."""

        # Fetch current token data
        price, liquidity = await self._get_token_price_and_liquidity(position.mint)

        if price is None or price <= 0:
            # If we can't get price, log but don't exit — could be temporary
            log.debug(f"Could not fetch price for {position.symbol}")
            return

        # Update position
        position.update_price(price)
        position.current_liquidity = liquidity or 0

        # Track liquidity history for rug detection
        position.liquidity_history.append({
            "time": time.time(),
            "liquidity": liquidity or 0,
        })
        # Keep only last 60 seconds of history
        cutoff = time.time() - config.RUG_LIQUIDITY_WINDOW
        position.liquidity_history = [
            h for h in position.liquidity_history if h["time"] > cutoff
        ]

        pnl_pct = position.pnl_pct
        multiplier = 1 + pnl_pct  # e.g., 2x = pnl_pct of 1.0

        # ── 1. Rug Detection (highest priority) ──────────────────
        if self._detect_rug(position):
            log.warning(f"RUG DETECTED for {position.symbol}! Emergency sell")
            await trader.sell_token(position, 1.0, "RUG DETECTED", sol_price)
            # Blacklist the deployer
            if position.mint in trader.get_positions():
                add_to_blacklist(
                    trader.get_positions()[position.mint].name,  # deployer if available
                    f"Rug pull on {position.symbol}",
                )
            return

        # ── 2. Take-profit 2: 5x → sell remaining ────────────────
        if multiplier >= config.TAKE_PROFIT_2:
            log.info(f"{position.symbol} hit {config.TAKE_PROFIT_2}x! Selling remaining")
            await trader.sell_token(position, 1.0, f"TP2 ({config.TAKE_PROFIT_2}x)", sol_price)
            return

        # ── 3. Take-profit 1: 2x → sell 50% ──────────────────────
        if (
            multiplier >= config.TAKE_PROFIT_1
            and position.status == trader.TradeStatus.OPEN
        ):
            log.info(f"{position.symbol} hit {config.TAKE_PROFIT_1}x! Selling 50%")
            await trader.sell_token(position, 0.5, f"TP1 ({config.TAKE_PROFIT_1}x)", sol_price)
            return

        # ── 4. Trailing Stop (activated after 1.5x) ──────────────
        if multiplier > 1.5 and position.highest_price_usd > 0:
            drop_from_peak = (
                (position.highest_price_usd - price)
                / position.highest_price_usd
            )
            if drop_from_peak >= config.TRAILING_STOP_PCT:
                log.info(
                    f"{position.symbol} trailing stop hit: "
                    f"peak ${position.highest_price_usd:.8f} → "
                    f"current ${price:.8f} (-{drop_from_peak*100:.1f}%)"
                )
                await trader.sell_token(position, 1.0, f"Trailing stop (-{drop_from_peak*100:.0f}% from peak)", sol_price)
                return

        # ── 5. Hard Stop-loss ─────────────────────────────────────
        if pnl_pct <= -config.STOP_LOSS_PCT:
            log.info(f"{position.symbol} hit stop-loss ({pnl_pct*100:+.1f}%)")
            await trader.sell_token(position, 1.0, f"Stop-loss ({pnl_pct*100:+.1f}%)", sol_price)
            return

        # ── 6. Time-based exit: free capital from stagnant positions ─
        age_minutes = (time.time() - position.entry_time) / 60
        if age_minutes >= config.MAX_POSITION_AGE_MINUTES and pnl_pct < 0.10:
            log.info(
                f"{position.symbol} time exit: held {age_minutes:.0f}min, "
                f"PnL {pnl_pct*100:+.1f}% — freeing capital"
            )
            await trader.sell_token(
                position, 1.0, f"Time exit ({age_minutes:.0f}min, {pnl_pct*100:+.1f}%)", sol_price
            )
            return

        # Log position status
        log.debug(
            f"{position.symbol}: ${price:.8f} ({pnl_pct*100:+.1f}%) "
            f"peak: ${position.highest_price_usd:.8f}"
        )

    def _detect_rug(self, position: trader.Position) -> bool:
        """
        Detect rug pull by checking liquidity drop over time window.
        Returns True if liquidity dropped 70%+ in the configured window.
        """
        history = position.liquidity_history
        if len(history) < 2:
            return False

        # Compare earliest and latest liquidity in the window
        oldest = history[0]["liquidity"]
        newest = history[-1]["liquidity"]

        if oldest <= 0:
            return False

        drop_pct = (oldest - newest) / oldest

        if drop_pct >= config.RUG_LIQUIDITY_DROP_PCT:
            log.warning(
                f"Liquidity crash for {position.symbol}: "
                f"${oldest:.0f} → ${newest:.0f} (-{drop_pct*100:.0f}%)"
            )
            return True

        return False

    async def _get_token_price_and_liquidity(self, mint: str) -> tuple:
        """
        Get current price and liquidity for a token.
        Uses pump.fun API (primary) and DexScreener (fallback).
        Returns (price_usd, liquidity_usd) or (None, None).
        """
        # Primary: pump.fun API
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{config.PUMPFUN_API_URL}/coins/{mint}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        mcap = float(data.get("usd_market_cap", 0) or 0)
                        supply = float(data.get("total_supply", 1) or 1)
                        price = mcap / supply if supply > 0 else 0
                        reserves = float(data.get("virtual_sol_reserves", 0) or 0) / 1e9
                        sol_price = await wallet.get_sol_price_usd()
                        liquidity = reserves * sol_price
                        return price, liquidity
        except Exception as e:
            log.debug(f"pump.fun price error for {mint[:8]}: {e}")

        # Fallback: DexScreener API (free, no auth)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs", [])
                        if pairs:
                            pair = pairs[0]
                            price = float(pair.get("priceUsd", 0) or 0)
                            liq = pair.get("liquidity", {})
                            liquidity = float(liq.get("usd", 0) or 0)
                            return price, liquidity
        except Exception as e:
            log.debug(f"DexScreener price error for {mint[:8]}: {e}")

        return None, None

    async def _get_liquidity(self, mint: str) -> float:
        """Get liquidity estimate for a token."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{config.PUMPFUN_API_URL}/coins/{mint}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        reserves = float(data.get("virtual_sol_reserves", 0) or 0) / 1e9
                        return reserves * 25  # rough USD conversion
        except Exception:
            pass
        return 0.0
