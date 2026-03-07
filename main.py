"""
DA Trader — Entry point.
Orchestrates the bot loop, scanner, monitor, and dashboard together.
"""

import asyncio
import signal
import sys
import threading

import config
import wallet
import trader
import risk
from scanner import PumpFunSniper, MomentumScanner
from monitor import PositionMonitor
from scorer import TokenScore
from dashboard.app import start_dashboard_thread, set_bot_controller, set_last_scored
from logger import setup_logger

log = setup_logger("main")


class BotController:
    """Controls the bot lifecycle — start, pause, stop."""

    def __init__(self):
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._sniper: PumpFunSniper | None = None
        self._momentum: MomentumScanner | None = None
        self._monitor: PositionMonitor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def is_running(self) -> bool:
        return self._running

    def start(self):
        """Start the bot (called from dashboard or main)."""
        if self._running:
            log.warning("Bot is already running")
            return
        self._running = True
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._run_bot(), self._loop)
        log.info("Bot start requested")

    def stop(self):
        """Stop the bot (does NOT sell open positions)."""
        self._running = False
        if self._sniper:
            self._sniper.stop()
        if self._momentum:
            self._momentum.stop()
        if self._monitor:
            self._monitor.stop()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        log.info("Bot stopped")

    async def _on_token_scored(self, token_score: TokenScore):
        """Callback when scanner scores a token."""
        # Update dashboard display
        set_last_scored(token_score)

        # Check if we should trade
        if token_score.action != "BUY":
            return

        # Risk check
        sol_price = await wallet.get_sol_price_usd()
        if sol_price <= 0:
            log.warning("Cannot trade — SOL price unavailable")
            return

        allowed, reason = await risk.can_open_position(token_score.bet_size_usd, sol_price)
        if not allowed:
            log.info(f"Skipping {token_score.symbol}: {reason}")
            return

        # Execute buy
        log.info(
            f"Opening position: {token_score.symbol} "
            f"(score {token_score.score}, bet ${token_score.bet_size_usd})"
        )

        position = await trader.buy_token(
            mint=token_score.mint,
            symbol=token_score.symbol,
            name=token_score.name,
            amount_usd=token_score.bet_size_usd,
            score=token_score.score,
            sol_price=sol_price,
            token_price_usd=token_score.token_price_usd,
        )

        if position:
            log.info(f"Position opened: {token_score.symbol}")
        else:
            log.error(f"Failed to open position: {token_score.symbol}")

    async def _run_bot(self):
        """Main bot loop."""
        log.info("=" * 50)
        log.info("DA TRADER starting up")
        log.info(f"Mode: {'PAPER' if config.PAPER_MODE else 'LIVE'}")
        log.info(f"Max positions: {config.MAX_POSITIONS}")
        log.info(f"Min score: {config.MIN_SCORE_TO_TRADE}")
        log.info(f"Stop-loss: -{config.STOP_LOSS_PCT*100:.0f}%")
        log.info(f"TP1: {config.TAKE_PROFIT_1}x | TP2: {config.TAKE_PROFIT_2}x")
        log.info(f"Trailing stop: -{config.TRAILING_STOP_PCT*100:.0f}% from peak")
        log.info("=" * 50)

        # Log wallet info
        try:
            balance = await wallet.get_sol_balance()
            sol_price = await wallet.get_sol_price_usd()
            log.info(f"Wallet: {wallet.get_pubkey_str()[:12]}...")
            log.info(f"Balance: {balance:.4f} SOL (${balance * sol_price:.2f})")
        except Exception as e:
            log.error(f"Wallet error: {e}")

        # Create components
        self._sniper = PumpFunSniper(on_scored=self._on_token_scored)
        self._momentum = MomentumScanner(on_scored=self._on_token_scored)
        self._monitor = PositionMonitor()

        # Launch all as concurrent tasks
        self._tasks = [
            asyncio.create_task(self._sniper.start()),
            asyncio.create_task(self._momentum.start()),
            asyncio.create_task(self._monitor.start()),
        ]

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            log.info("Bot tasks cancelled")
        except Exception as e:
            log.error(f"Bot error: {e}")
        finally:
            self._running = False
            log.info("Bot loop ended")


async def main():
    """Main async entry point."""
    controller = BotController()
    controller._loop = asyncio.get_event_loop()

    # Register controller with dashboard
    set_bot_controller(controller)

    # Start dashboard in background thread
    dashboard_thread = start_dashboard_thread()

    # Handle shutdown gracefully
    def shutdown_handler(sig, frame):
        log.info("Shutdown signal received")
        controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Print startup info
    log.info("=" * 50)
    log.info("DA TRADER")
    log.info(f"Dashboard: http://localhost:{config.DASHBOARD_PORT}")
    log.info(f"Mode: {'PAPER' if config.PAPER_MODE else 'LIVE'}")
    log.info("=" * 50)

    # Auto-start the bot
    controller._running = True
    await controller._run_bot()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
    except Exception as e:
        log.error(f"Fatal error: {e}")
        raise
