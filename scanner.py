"""
Token scanner — two modes:
1. New Launch Sniper: WebSocket listener for pump.fun new token events
2. Momentum Scanner: periodic polling for tokens showing breakout signals

Improvements:
- Reconnection logic with exponential backoff for WebSocket
- Deduplication to avoid scoring the same token twice
- Rate limiting to avoid RPC throttling
"""

import asyncio
import json
import time
from typing import Callable, Optional, Awaitable

import aiohttp

import config
from logger import setup_logger
from scorer import score_token, TokenScore

log = setup_logger("scanner")

# Track tokens we've already evaluated (avoid re-scoring)
_seen_tokens: dict[str, float] = {}  # mint -> timestamp
_SEEN_TTL = 3600  # forget after 1 hour

# Track symbols we've already bought recently (avoid buying same ticker twice)
_seen_symbols: dict[str, float] = {}  # symbol_upper -> timestamp
_SYMBOL_COOLDOWN = 300  # 5 minutes before same symbol is allowed again


def _cleanup_seen():
    """Remove expired entries from seen cache."""
    now = time.time()
    expired = [k for k, v in _seen_tokens.items() if now - v > _SEEN_TTL]
    for k in expired:
        del _seen_tokens[k]


def _mark_seen(mint: str):
    _seen_tokens[mint] = time.time()


def _is_seen(mint: str) -> bool:
    _cleanup_seen()
    return mint in _seen_tokens


def _is_symbol_on_cooldown(symbol: str) -> bool:
    key = symbol.upper().strip()
    last = _seen_symbols.get(key)
    return last is not None and (time.time() - last) < _SYMBOL_COOLDOWN


def _mark_symbol_seen(symbol: str):
    _seen_symbols[symbol.upper().strip()] = time.time()


# Callback type for when a token passes scoring
OnTokenScored = Callable[[TokenScore], Awaitable[None]]


# ── Mode 1: New Launch Sniper ─────────────────────────────────────────

class PumpFunSniper:
    """
    Connects to pump.fun WebSocket and listens for new token creation events.
    Scores each token and calls the callback if it passes.
    """

    def __init__(self, on_scored: OnTokenScored):
        self.on_scored = on_scored
        self._running = False
        self._ws = None

    async def start(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._connect()
                backoff = 1  # reset on successful connection
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"pump.fun WebSocket error: {e}")
                if not self._running:
                    break
                wait = min(backoff, 30)
                log.info(f"Reconnecting in {wait}s...")
                await asyncio.sleep(wait)
                backoff *= 2

    async def _connect(self):
        log.info("Connecting to pump.fun WebSocket...")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                config.PUMPFUN_WS_URL,
                timeout=aiohttp.ClientTimeout(total=30),
                heartbeat=15,
            ) as ws:
                self._ws = ws
                log.info("Connected to pump.fun WebSocket")

                # Subscribe to new token creation events
                await ws.send_json({
                    "method": "subscribeNewToken",
                })
                log.info("Subscribed to new token events")

                async for msg in ws:
                    if not self._running:
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            await self._handle_message(json.loads(msg.data))
                        except Exception as e:
                            log.error(f"Error processing message: {e}")

                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        log.warning(f"WebSocket closed: {msg.type}")
                        break

    async def _handle_message(self, data: dict):
        """Process a pump.fun WebSocket message."""
        # New token creation event
        if not isinstance(data, dict):
            return

        mint = data.get("mint")
        if not mint:
            return

        if _is_seen(mint):
            return

        _mark_seen(mint)

        name = data.get("name", "Unknown")
        symbol = data.get("symbol", "???")

        # Skip if we've recently seen/bought this symbol (different mint, same name)
        if _is_symbol_on_cooldown(symbol):
            log.debug(f"Symbol {symbol} on cooldown, skipping {mint[:8]}")
            return

        _mark_symbol_seen(symbol)
        log.info(f"New token detected: {symbol} ({name}) — {mint[:12]}...")

        # Score it
        try:
            token_score = await score_token(
                mint=mint,
                name=name,
                symbol=symbol,
                extra_data=data,
            )

            # Always notify (dashboard needs to see skipped tokens too)
            await self.on_scored(token_score)

        except Exception as e:
            log.error(f"Error scoring {symbol}: {e}")

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())


# ── Mode 2: Momentum Scanner ─────────────────────────────────────────

class MomentumScanner:
    """
    Periodically scans recently created tokens for momentum signals.
    Looks for tokens that are 5-60 minutes old with strong buy momentum.
    """

    def __init__(self, on_scored: OnTokenScored):
        self.on_scored = on_scored
        self._running = False

    async def start(self):
        self._running = True
        log.info(f"Momentum scanner started (interval: {config.MOMENTUM_SCAN_INTERVAL}s)")

        while self._running:
            try:
                await self._scan_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Momentum scan error: {e}")

            if self._running:
                await asyncio.sleep(config.MOMENTUM_SCAN_INTERVAL)

    async def _scan_cycle(self):
        """One scan cycle — fetch trending tokens and score them."""
        tokens = await self._fetch_trending()

        if not tokens:
            return

        log.info(f"Momentum scan: evaluating {len(tokens)} candidates")

        for token in tokens:
            if not self._running:
                break

            mint = token.get("mint", "")
            if not mint or _is_seen(mint):
                continue

            # Check age filter
            created = token.get("created_timestamp", 0)
            if created:
                if created > 1e12:
                    created = created / 1000
                age = time.time() - created
                if age < config.MOMENTUM_MIN_AGE or age > config.MOMENTUM_MAX_AGE:
                    continue

            # Check market cap filter
            mcap = float(token.get("usd_market_cap", 0) or 0)
            if mcap > config.MOMENTUM_MAX_MCAP:
                continue

            _mark_seen(mint)

            name = token.get("name", "Unknown")
            symbol = token.get("symbol", "???")

            if _is_symbol_on_cooldown(symbol):
                continue
            _mark_symbol_seen(symbol)

            try:
                token_score = await score_token(
                    mint=mint,
                    name=name,
                    symbol=symbol,
                    extra_data=token,
                )
                await self.on_scored(token_score)
            except Exception as e:
                log.error(f"Error scoring momentum token {symbol}: {e}")

            # Small delay to avoid rate limits
            await asyncio.sleep(0.5)

    async def _fetch_trending(self) -> list[dict]:
        """Fetch recently created tokens from pump.fun sorted by activity."""
        try:
            async with aiohttp.ClientSession() as session:
                # Fetch recently created tokens sorted by creation time
                async with session.get(
                    f"{config.PUMPFUN_API_URL}/coins",
                    params={
                        "offset": 0,
                        "limit": 50,
                        "sort": "created_timestamp",
                        "order": "DESC",
                        "includeNsfw": "false",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            return data
                        return data.get("coins", data.get("data", []))
        except Exception as e:
            log.warning(f"Failed to fetch trending tokens: {e}")
        return []

    def stop(self):
        self._running = False
