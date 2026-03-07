"""
Solana wallet interactions — balance checks, keypair loading, transaction signing.
"""

import asyncio
import base64
import struct
import time
from typing import Optional

import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import config
from logger import setup_logger

log = setup_logger("wallet")

# Module-level keypair (loaded once)
_keypair: Optional[Keypair] = None
_paper_balance_sol: float = 0.59  # starting paper balance (~50 EUR)


def get_keypair() -> Keypair:
    global _keypair
    if _keypair is None:
        if config.PAPER_MODE:
            # Generate a throwaway keypair for paper mode
            _keypair = Keypair()
            log.info("Paper mode — using throwaway keypair")
        else:
            raw = config.WALLET_PRIVATE_KEY
            if not raw:
                raise ValueError("WALLET_PRIVATE_KEY not set")
            # Support both base58 and byte-array formats
            try:
                _keypair = Keypair.from_base58_string(raw)
            except Exception:
                try:
                    import json
                    byte_list = json.loads(raw)
                    _keypair = Keypair.from_bytes(bytes(byte_list))
                except Exception as e:
                    raise ValueError(f"Could not parse WALLET_PRIVATE_KEY: {e}")
            log.info(f"Wallet loaded: {_keypair.pubkey()}")
    return _keypair


def get_pubkey() -> Pubkey:
    return get_keypair().pubkey()


def get_pubkey_str() -> str:
    return str(get_pubkey())


# ── RPC helpers with failover ─────────────────────────────────────────

_rpc_index = 0
_rpc_urls = [config.SOLANA_RPC_URL] + config.BACKUP_RPC_URLS


async def _rpc_call(method: str, params: list, retries: int = 3) -> dict:
    """Make a JSON-RPC call with automatic failover to backup RPCs."""
    global _rpc_index

    last_error = None
    for attempt in range(retries):
        url = _rpc_urls[_rpc_index % len(_rpc_urls)]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if "error" in data:
                        raise Exception(f"RPC error: {data['error']}")
                    return data.get("result", {})
        except Exception as e:
            last_error = e
            log.warning(f"RPC call {method} failed (attempt {attempt+1}): {e}")
            _rpc_index += 1
            # Exponential backoff
            await asyncio.sleep(min(2 ** attempt, 8))

    raise ConnectionError(f"All RPC attempts failed for {method}: {last_error}")


# ── Balance ───────────────────────────────────────────────────────────

async def get_sol_balance() -> float:
    """Get SOL balance in SOL (not lamports)."""
    if config.PAPER_MODE:
        return _paper_balance_sol

    result = await _rpc_call("getBalance", [get_pubkey_str()])
    lamports = result.get("value", 0)
    return lamports / 1e9


async def get_token_balance(mint: str) -> float:
    """Get SPL token balance for a given mint."""
    if config.PAPER_MODE:
        return 0.0

    result = await _rpc_call(
        "getTokenAccountsByOwner",
        [
            get_pubkey_str(),
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    )
    accounts = result.get("value", [])
    if not accounts:
        return 0.0

    info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
    return float(info.get("uiAmount", 0))


_cached_sol_price: float = 0.0
_cached_sol_price_time: float = 0.0
_SOL_PRICE_CACHE_TTL = 30  # cache for 30 seconds


async def get_sol_price_usd() -> float:
    """Fetch current SOL/USD price with caching and multiple fallbacks."""
    global _cached_sol_price, _cached_sol_price_time

    # Return cached price if fresh
    if _cached_sol_price > 0 and (time.time() - _cached_sol_price_time) < _SOL_PRICE_CACHE_TTL:
        return _cached_sol_price

    # Try CoinGecko (free, no auth)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = float(data["solana"]["usd"])
                    _cached_sol_price = price
                    _cached_sol_price_time = time.time()
                    return price
    except Exception as e:
        log.debug(f"CoinGecko SOL price failed: {e}")

    # Fallback: Binance public API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "SOLUSDT"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = float(data["price"])
                    _cached_sol_price = price
                    _cached_sol_price_time = time.time()
                    return price
    except Exception as e:
        log.debug(f"Binance SOL price failed: {e}")

    # Return last known price if all APIs fail
    if _cached_sol_price > 0:
        log.warning(f"Using stale SOL price: ${_cached_sol_price:.2f}")
        return _cached_sol_price

    log.warning("All SOL price sources failed")
    return 0.0


# ── Paper mode helpers ────────────────────────────────────────────────

def paper_deduct(sol_amount: float):
    global _paper_balance_sol
    _paper_balance_sol -= sol_amount
    log.info(f"[PAPER] Deducted {sol_amount:.4f} SOL — balance: {_paper_balance_sol:.4f}")


def paper_credit(sol_amount: float):
    global _paper_balance_sol
    _paper_balance_sol += sol_amount
    log.info(f"[PAPER] Credited {sol_amount:.4f} SOL — balance: {_paper_balance_sol:.4f}")


def paper_get_balance() -> float:
    return _paper_balance_sol


# ── Transaction sending ──────────────────────────────────────────────

async def send_transaction(tx_bytes: bytes) -> str:
    """Send a signed serialized transaction and return the signature."""
    if config.PAPER_MODE:
        return "PAPER_TX_" + str(int(time.time()))

    encoded = base64.b64encode(tx_bytes).decode("utf-8")

    result = await _rpc_call(
        "sendTransaction",
        [
            encoded,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 3,
            },
        ],
    )
    return result


async def confirm_transaction(sig: str, timeout: int = 30) -> bool:
    """Wait for transaction confirmation."""
    if config.PAPER_MODE:
        return True

    start = time.time()
    while time.time() - start < timeout:
        try:
            result = await _rpc_call(
                "getSignatureStatuses",
                [[sig]],
            )
            statuses = result.get("value", [])
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    if status.get("err") is None:
                        return True
                    else:
                        log.error(f"Transaction failed: {status['err']}")
                        return False
        except Exception:
            pass
        await asyncio.sleep(1)

    log.error(f"Transaction {sig} timed out after {timeout}s")
    return False
