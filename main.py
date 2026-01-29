"""
Main Entry Point - Production Bot for Polymarket.
Runs on Railway with HTTP health check.
"""

import asyncio
import logging
import os
import sys
import time
import traceback

from aiohttp import web

from config import (
    LOG_LEVEL, POLL_INTERVAL_SECONDS, SCANNER_INTERVAL_SECONDS,
    HEARTBEAT_INTERVAL, MAX_CONCURRENT_EVENTS
)
from polymarket_client import get_client
from event_scanner import EventScanner
from strategy_engine import StrategyEngine
from telegram_notifier import get_notifier
from models import MarketPhase

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


class ProductionBot:
    """
    Main bot orchestrator.
    KISS: Connect, scan, trade, repeat.
    """
    
    def __init__(self):
        self.client = get_client()
        self.scanner = EventScanner(max_events=MAX_CONCURRENT_EVENTS)
        self.strategy: StrategyEngine = None
        self.notifier = get_notifier()
        self._running = False
    
    async def start(self) -> bool:
        """Initialize and start the bot."""
        logger.info("=" * 60)
        logger.info("🚀 PRODUCTION BOT STARTING")
        logger.info("=" * 60)
        
        # Connect to Polymarket
        if not self.client.connect():
            logger.error("❌ Failed to connect to Polymarket")
            self.notifier.send_error("Failed to connect to Polymarket API")
            return False
        
        # Initialize strategy
        self.strategy = StrategyEngine(self.client)
        
        # Get and log balance
        balance = self.client.get_balance()
        logger.info(f"💰 USDC Balance: ${balance:.2f}")
        
        # Send startup notification
        self.notifier.send_startup(balance)
        
        self._running = True
        return True
    
    async def stop(self):
        """Gracefully stop the bot."""
        logger.info("🛑 Stopping bot...")
        self._running = False
        
        # Cancel all open orders for safety
        cancelled = self.client.cancel_all_orders()
        logger.info(f"❌ Cancelled {cancelled} orders on shutdown")
    
    async def run(self):
        """Main bot loop."""
        if not await self.start():
            return
        
        last_scan = 0
        last_heartbeat = 0
        
        try:
            while self._running:
                now = time.time()
                
                # Scan for new events periodically
                if now - last_scan >= SCANNER_INTERVAL_SECONDS:
                    last_scan = now
                    new_events = self.scanner.scan_for_events()
                    
                    for event in new_events:
                        self.notifier.send_event_discovered(event)
                        
                        # CRITICAL: Only place ladder orders if in PRE_MARKET phase!
                        # Never place BUY orders on LIVE events - that's gambling
                        if event.phase == MarketPhase.PRE_MARKET:
                            self.strategy.initialize_event(event)
                            logger.info(f"🪜 Initialized strategy for PRE_MARKET event: {event.slug}")
                        else:
                            logger.warning(
                                f"⚠️ SKIPPING event {event.slug} - already in {event.phase.name} phase. "
                                f"No orders placed to avoid losses."
                            )
                
                # Update phases and handle transitions
                transitioned = self.scanner.update_phases()
                for event in transitioned:
                    self.strategy.transition_to_live(event)
                
                # Check fills for active events
                for event in self.scanner.get_active_events():
                    # 🔍 UPDATE LIVE PRICES (for Stop-Loss)
                    try:
                        # Fetch YES Orderbook
                        yes_ob = self.client._client.get_order_book(event.yes_token_id)
                        if yes_ob and yes_ob.bids:
                            # Find the HIGHEST bid (best exit price)
                            # Bids may NOT be sorted, so find max
                            best_yes_bid = max(float(b.price) for b in yes_ob.bids)
                            # Sanity check: ignore spam bids below 10¢
                            if best_yes_bid >= 0.10:
                                event.yes_bid = best_yes_bid
                        
                        # Fetch NO Orderbook
                        no_ob = self.client._client.get_order_book(event.no_token_id)
                        if no_ob and no_ob.bids:
                            best_no_bid = max(float(b.price) for b in no_ob.bids)
                            if best_no_bid >= 0.10:
                                event.no_bid = best_no_bid
                            
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update prices for {event.slug}: {e}")

                    self.strategy.check_fills(event)
                    
                    # Check completion
                    if self.strategy.check_completion(event):
                        self.scanner.remove_event(event.slug)
                
                # Process pending sells ONCE per cycle (not per-event!)
                self.strategy.process_pending_sells()
                
                # Heartbeat
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    last_heartbeat = now
                    self._log_heartbeat()
                
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                
        except asyncio.CancelledError:
            logger.info("🛑 Bot cancelled")
        except Exception as e:
            logger.error(f"❌ Fatal error: {e}")
            logger.error(traceback.format_exc())
            self.notifier.send_error(f"Fatal error: {e}")
        finally:
            await self.stop()
    
    def _log_heartbeat(self):
        """Log status heartbeat."""
        events = self.scanner.get_active_events()
        pending = self.strategy.get_pending_count() if self.strategy else 0
        
        if events:
            # Find next event to go LIVE
            next_live = min(events, key=lambda e: e.time_until_start())
            time_left = next_live.time_until_start()
            
            if time_left > 0:
                mins = int(time_left / 60)
                logger.info(
                    f"💓 Heartbeat: {len(events)} events | "
                    f"{pending} orders | "
                    f"Next LIVE: {next_live.slug} in {mins}m"
                )
            else:
                logger.info(
                    f"💓 Heartbeat: {len(events)} events | "
                    f"{pending} orders | "
                    f"LIVE: {next_live.slug}"
                )
        else:
            logger.info(f"💓 Heartbeat: No active events | {pending} orders")


# ===========================================
# HTTP Health Check for Railway
# ===========================================

async def health_check(request):
    """Health check endpoint for Railway."""
    return web.Response(text="OK", status=200)


async def run_health_server():
    """Run minimal HTTP server for health checks."""
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    logger.info(f"🌐 Health server running on port {port}")
    return runner


# ===========================================
# Entry Point
# ===========================================

async def main():
    """Main entry point with health server."""
    logger.info("=" * 50)
    logger.info("🚀 STARTING APPLICATION...")
    logger.info("=" * 50)
    
    # Start health server FIRST (Railway needs this quickly)
    try:
        health_runner = await run_health_server()
        logger.info("✅ Health server ready")
    except Exception as e:
        logger.error(f"❌ Failed to start health server: {e}")
        raise
    
    try:
        # Run the bot (may take time to connect)
        bot = ProductionBot()
        await bot.run()
    except Exception as e:
        logger.error(f"❌ Bot error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # Keep health server running even if bot fails
        await asyncio.sleep(60)  # Give time to see logs
    finally:
        await health_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Shutdown by user")
