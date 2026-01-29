"""
Strategy Engine - Core trading logic.
Manages ladder placement, fill tracking, and position management.
"""

import logging
import time
from typing import Dict, List, Optional, Set

from config import LADDER_LEVELS, EXIT_PRICES, ORDER_SIZE, STOP_LOSS_PRICE, STOP_LOSS_ENTRIES
from models import (
    EventContext, OrderSide, OrderType, TrackedOrder,
    Position, CycleResult, StrategyState, MarketPhase
)
from polymarket_client import PolymarketClient, get_client
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


class StrategyEngine:
    """
    Mean Reversion Ladder Strategy.
    
    Logic:
    - PRE_MARKET: Place buy orders at LADDER_LEVELS (40-48¢)
    - On buy fill: Place sell at EXIT_PRICE (49¢)
    - On sell fill in PRE_MARKET: Reload (re-place buy at entry price)
    - On LIVE: Cancel all buys, only exits remain
    """
    
    def __init__(self, client: PolymarketClient):
        self.client = client
        self.notifier = get_notifier()
        
        # State tracking per event
        self._states: Dict[str, StrategyState] = {}
        self._positions: Dict[str, List[Position]] = {}
        self._results: Dict[str, CycleResult] = {}
        
        # Track our orders
        self._buy_orders: Dict[str, List[TrackedOrder]] = {}  # event -> orders
        self._sell_orders: Dict[str, List[TrackedOrder]] = {}
        self._stop_loss_orders: Dict[str, List[TrackedOrder]] = {}  # Stop-loss orders
        
        # Track which orders we've seen as filled (order IDs)
        self._known_filled: Set[str] = set()
        
        # Queue for sells that failed to place (will retry each cycle)
        self._pending_sells: List[Dict] = []  # [{token_id, side, exit_price, size, slug, entry_price, attempts}]
        
        # Accumulator for partial fills below minimum order size (5 shares)
        # Key: (slug, side, token_id), Value: {size: float, value: float (size*entry for weighted avg)}
        self._fill_accumulator: Dict[tuple, Dict] = {}
    
    def _get_exit_price(self, entry_price: float) -> float:
        """
        Get the appropriate exit price for a given entry.
        
        Rules:
        - 48¢ entry → 49¢ exit
        - 46-47¢ entry → 48¢ exit
        - 40-45¢ entry → 47¢ exit
        """
        # Round to avoid float precision issues
        entry_rounded = round(entry_price, 2)
        return EXIT_PRICES.get(entry_rounded, 0.49)  # Default to 49¢ if not mapped
    
    def _needs_stop_loss(self, entry_price: float) -> bool:
        """Check if an entry price needs a stop-loss order."""
        entry_rounded = round(entry_price, 2)
        return entry_rounded in STOP_LOSS_ENTRIES
    
    def initialize_event(self, event: EventContext) -> int:
        """
        Initialize strategy for a new event.
        Places the ladder of buy orders.
        
        CRITICAL: Only call this for PRE_MARKET events!
        
        Returns:
            Number of orders placed
        """
        slug = event.slug
        
        if slug in self._states:
            return 0  # Already initialized
        
        # DEFENSIVE CHECK: Reject LIVE or ENDED events
        if event.phase != MarketPhase.PRE_MARKET:
            logger.error(
                f"❌ REJECTED: Cannot initialize {slug} - event is {event.phase.name}. "
                f"Only PRE_MARKET events allowed!"
            )
            return 0
        
        self._states[slug] = StrategyState.ACCUMULATING
        self._positions[slug] = []
        self._results[slug] = CycleResult(event_slug=slug, start_time=time.time())
        self._buy_orders[slug] = []
        self._sell_orders[slug] = []
        self._stop_loss_orders[slug] = []
        
        orders_placed = 0
        
        # Place ladder on both YES and NO
        for side, token_id in [
            (OrderSide.YES, event.yes_token_id),
            (OrderSide.NO, event.no_token_id)
        ]:
            for price in LADDER_LEVELS:
                order = self.client.place_limit_order(
                    token_id=token_id,
                    side=side,
                    order_type=OrderType.BUY,
                    price=price,
                    size=ORDER_SIZE,
                    event_slug=slug
                )
                
                if order:
                    self._buy_orders[slug].append(order)
                    orders_placed += 1
        
        logger.info(f"🪜 Ladder placed for {slug}: {orders_placed} orders")
        
        # Get current balance for notification
        balance = self.client.get_balance()
        self.notifier.send_ladder_placed(slug, orders_placed, balance)
        
        return orders_placed
    
    def check_fills(self, event: EventContext) -> None:
        """
        Check for filled orders and process them.
        Uses the CLOB API to check order status.
        """
        slug = event.slug
        
        if slug not in self._states:
            return
        
        # Get current open orders from API
        open_orders = self.client.get_open_orders()
        open_order_ids = {o.get("id") for o in open_orders}
        
        # Check buy orders
        for order in self._buy_orders.get(slug, []):
            if order.order_id in self._known_filled:
                continue
            
            # Use 'if not in open_orders' only as a trigger to CHECK status
            if order.order_id not in open_order_ids:
                try:
                    # 🛡️ SAFETY CHECK: Verify it actually filled
                    order_data = self.client.get_order(order.order_id)
                    
                    # Log the raw status for debugging
                    logger.info(f"🕵️ Checking missing order {order.order_id}: {order_data}")

                    # Determine if it's truly filled (API returns snake_case 'size_matched')
                    size_matched = float(order_data.get("size_matched") or order_data.get("sizeMatched") or 0)
                    status = order_data.get("status", "").upper()

                    if size_matched > 0:
                        # Update size to actual filled amount (handles partial fills)
                        original_size = order.size
                        order.size = size_matched 
                        
                        logger.info(f"✅ Order confirmed filled: {size_matched}/{original_size}")
                        self._process_buy_fill(order, event)
                        
                        # Only mark as fully known if fully filled or cancelled (or MATCHED)
                        if size_matched == original_size or status == "CANCELLED" or status == "MATCHED":
                            self._known_filled.add(order.order_id)
                    
                    elif status == "CANCELLED":
                        logger.warning(f"⚠️ Order {order.order_id} was CANCELLED (not filled). Ignoring.")
                        self._known_filled.add(order.order_id)
                        
                    else:
                        logger.warning(f"⚠️ Order {order.order_id} missing from Open Orders but status is {status}. Waiting... (size_matched={size_matched})")
                        
                except Exception as e:
                    logger.error(f"❌ Error verifying fill for {order.order_id}: {e}")

        
        # Check sell orders (take-profit)
        for order in self._sell_orders.get(slug, []):
            if order.order_id in self._known_filled:
                continue
            
            if order.order_id not in open_order_ids:
                try:
                    # 🛡️ SAFETY CHECK
                    order_data = self.client.get_order(order.order_id)
                    
                    # Skip if API returned None (order not found yet)
                    if order_data is None:
                        logger.debug(f"⏳ Order {order.order_id[:10]}... not found in API yet, will retry")
                        continue
                    
                    size_matched = float(order_data.get("size_matched") or order_data.get("sizeMatched") or 0)
                    original_size = float(order_data.get("original_size") or order_data.get("originalSize") or order.size)
                    status = order_data.get("status", "").upper()
                    
                    if size_matched > 0:
                        # Update size to actual filled amount
                        order.size = size_matched
                        self._process_sell_fill(order, event, is_stop_loss=False)
                        
                        # Only mark complete if FULLY filled or explicitly done
                        if size_matched >= original_size or status == "MATCHED":
                            self._known_filled.add(order.order_id)
                        else:
                            # PARTIAL FILL: Log warning, order stays open for remaining
                            logger.warning(f"⚠️ PARTIAL SELL FILL: {size_matched}/{original_size} shares. Remaining on book.")
                            
                    elif status == "CANCELLED":
                         self._known_filled.add(order.order_id)
                         
                except Exception as e:
                    logger.error(f"❌ Error verifying sell fill for {order.order_id}: {e}")

        # NOTE: Pending sells are processed once per cycle in main.py, not per-event
        
        # =========================================================================
        # STOP-LOSS MONITOR (Client-Side)
        # Only for 48¢ entries: If market drops to 18¢, dump at market price
        # =========================================================================
        self._check_stop_loss(event, open_order_ids)
    
    def process_pending_sells(self) -> None:
        """
        Retry placing sell orders that failed previously.
        IMPORTANT: Call this ONCE per cycle from main.py, not per-event!
        """
        if not self._pending_sells:
            return
        
        still_pending = []
        
        for pending in self._pending_sells:
            sell_order = self.client.place_limit_order(
                token_id=pending['token_id'],
                side=pending['side'],
                order_type=OrderType.SELL,
                price=pending['exit_price'],
                size=pending['size'],
                event_slug=pending['slug']
            )
            
            if sell_order:
                sell_order.entry_price = pending['entry_price']
                slug = pending['slug']
                if slug in self._sell_orders:
                    self._sell_orders[slug].append(sell_order)
                else:
                    self._sell_orders[slug] = [sell_order]
                    
                logger.info(
                    f"✅ PENDING SELL placed (attempt {pending['attempts']+1}): "
                    f"{pending['side'].display_name} @ {int(pending['exit_price']*100)}¢ x{pending['size']}"
                )
            else:
                # Still failing, increment attempts and keep in queue
                pending['attempts'] += 1
                if pending['attempts'] <= 10:  # Max 10 attempts (=50 seconds if 5s poll interval)
                    still_pending.append(pending)
                    logger.warning(
                        f"⚠️ PENDING SELL retry failed (attempt {pending['attempts']}): "
                        f"{pending['side'].display_name} @ {int(pending['exit_price']*100)}¢"
                    )
                else:
                    # Give up after 10 attempts
                    logger.error(
                        f"❌ GAVE UP on SELL after 10 attempts: "
                        f"{pending['side'].display_name} @ {int(pending['exit_price']*100)}¢ x{pending['size']}"
                    )
                    self.notifier.send_message(
                        f"⚠️ ALERTA CRÍTICA: No se pudo colocar orden de venta después de 10 intentos. "
                        f"Revisa manualmente: {pending['side'].display_name} @ {int(pending['exit_price']*100)}¢"
                    )
        
        self._pending_sells = still_pending
    
    def _check_stop_loss(self, event: EventContext, open_order_ids: set) -> None:
        """
        Monitor sell orders from high-risk entries (48¢) for stop-loss.
        If market price drops to STOP_LOSS_PRICE or below, dump at market.
        """
        slug = event.slug
        
        # Get current best bids from event context (populated in main loop)
        current_bids = {
            OrderSide.YES: event.yes_bid,
            OrderSide.NO: event.no_bid
        }
        
        for order in self._sell_orders.get(slug, []):
            # Skip if already processed
            if order.order_id in self._known_filled:
                continue
            
            # Skip if order is no longer open (already filled)
            if order.order_id not in open_order_ids:
                continue
            
            # Only check stop-loss for high-risk entries (48¢)
            entry_price = order.entry_price or 0
            if not self._needs_stop_loss(entry_price):
                continue
            
            # Get current market price (best bid)
            current_market_price = current_bids.get(order.side)
            
            # Safety: Skip if no price data or price below minimum threshold (spam)
            if current_market_price is None or current_market_price < 0.10:
                continue
            
            # TRIGGER STOP-LOSS if price drops to threshold
            if current_market_price <= STOP_LOSS_PRICE:
                logger.warning(
                    f"🔻 STOP-LOSS TRIGGERED: {order.side.display_name} @ {int(current_market_price*100)}¢ "
                    f"<= {int(STOP_LOSS_PRICE*100)}¢. Dumping position!"
                )
                
                # 1. Cancel the Take-Profit Order to unlock tokens
                try:
                    logger.info(f"🔓 Cancelling TP order {order.order_id[:8]}...")
                    self.client.cancel_order(order.order_id)
                    time.sleep(1.0)  # Wait for cancellation
                    self._known_filled.add(order.order_id)  # Mark as handled
                except Exception as e:
                    logger.error(f"❌ Failed to cancel TP for SL: {e}")
                    continue
                
                # 2. Execute Market Sell (limit sell at 1¢ to hit any bid)
                logger.warning(f"📉 Executing MARKET SELL for {order.size} shares...")
                dump_order = self.client.place_limit_order(
                    token_id=order.token_id,
                    side=order.side,
                    order_type=OrderType.SELL,
                    price=0.01,  # Market sell (crosses any bid)
                    size=order.size,
                    event_slug=slug
                )
                
                if dump_order:
                    logger.warning(f"✅ STOP-LOSS EXECUTED: Sold {order.size} shares at market")
                    self.notifier.send_message(
                        f"🔴 STOP-LOSS EJECUTADO: Vendido {order.size} {order.side.display_name} "
                        f"a mercado (precio cayó a {int(current_market_price*100)}¢)"
                    )
                else:
                    logger.error(f"❌ Failed to execute stop-loss market sell!")
                    self.notifier.send_message(
                        f"⚠️ ALERTA: Stop-loss no se pudo ejecutar. Intervención manual requerida."
                    )
    
    def _process_buy_fill(self, order: TrackedOrder, event: EventContext) -> None:
        """Handle a buy order fill."""
        slug = event.slug
        entry_price = order.price
        exit_price = self._get_exit_price(entry_price)
        
        logger.info(
            f"✅ BUY FILLED: {order.side.display_name} @ {int(entry_price*100)}¢ "
            f"→ Exit target: {int(exit_price*100)}¢"
        )
        
        # Record position
        position = Position(
            side=order.side,
            entry_price=entry_price,
            size=order.size,
            token_id=order.token_id,
            event_slug=slug
        )
        self._positions[slug].append(position)
        
        # Record in results
        if order.side == OrderSide.YES:
            self._results[slug].fills_yes.append(entry_price)
        else:
            self._results[slug].fills_no.append(entry_price)
        
        # Minimum order size for Polymarket
        MIN_ORDER_SIZE = 5.0
        
        # Accumulate fills BY EXIT PRICE to preserve the EXIT_PRICES strategy
        # Key includes exit_price so 47¢→48¢ and 48¢→49¢ entries are tracked separately
        acc_key = (slug, order.side, order.token_id, exit_price)
        
        if acc_key not in self._fill_accumulator:
            self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
        
        acc = self._fill_accumulator[acc_key]
        acc['size'] += order.size
        acc['total_entry_value'] += order.size * entry_price
        
        logger.info(f"📦 Accumulated for exit @{int(exit_price*100)}¢: {acc['size']:.2f} shares (need {MIN_ORDER_SIZE} to sell)")
        
        # Only place sell when we have enough shares for this specific exit price
        # Use 99% threshold (4.95) to handle partial fills like 4.986
        SELL_THRESHOLD = MIN_ORDER_SIZE * 0.99  # 4.95 instead of 5.0
        if acc['size'] >= SELL_THRESHOLD:
            sell_size = acc['size']
            avg_entry = acc['total_entry_value'] / acc['size']
            
            # Reset accumulator for this exit price
            self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
            
            # No delay - pending queue handles retries if tokens not settled
            
            sell_order = self.client.place_limit_order(
                token_id=order.token_id,
                side=order.side,
                order_type=OrderType.SELL,
                price=exit_price,  # Use the correct exit price for this entry level
                size=sell_size,
                event_slug=slug
            )
            
            if sell_order:
                sell_order.entry_price = avg_entry
                self._sell_orders[slug].append(sell_order)
                logger.info(f"✅ SELL order placed: {order.side.display_name} @ {int(exit_price*100)}¢ x{sell_size}")
            else:
                # Add to pending queue
                pending = {
                    'token_id': order.token_id,
                    'side': order.side,
                    'exit_price': exit_price,
                    'size': sell_size,
                    'slug': slug,
                    'entry_price': avg_entry,
                    'attempts': 1
                }
                self._pending_sells.append(pending)
                logger.warning(f"⚠️ SELL failed, queued for retry: {order.side.display_name} @ {int(exit_price*100)}¢ x{sell_size}")
        
        self.notifier.send_fill(order)
    
    def _process_sell_fill(self, order: TrackedOrder, event: EventContext, is_stop_loss: bool = False) -> None:
        """
        Handle a sell order fill (take-profit or stop-loss).
        
        When one fires, we cancel the opposing order (OCO behavior).
        """
        slug = event.slug
        
        # Calculate PnL
        entry_price = order.entry_price or 0
        pnl = (order.price - entry_price) * order.size
        self._results[slug].total_pnl += pnl
        
        # Log appropriately based on order type
        if is_stop_loss:
            logger.warning(
                f"🛑 STOP-LOSS HIT: {order.side.display_name} "
                f"{int(entry_price*100)}¢ → {int(order.price*100)}¢ | Loss: ${abs(pnl):.2f}"
            )
        else:
            logger.info(
                f"✅ TAKE-PROFIT: {order.side.display_name} "
                f"{int(entry_price*100)}¢ → {int(order.price*100)}¢ | PnL: ${pnl:.2f}"
            )
        
        # OCO (One-Cancels-Other) logic for 48¢ entries:
        # If take-profit fires, cancel the stop-loss and vice versa
        if self._needs_stop_loss(entry_price):
            if is_stop_loss:
                # Stop-loss fired - cancel the take-profit
                for sell in self._sell_orders.get(slug, []):
                    if (sell.entry_price and abs(sell.entry_price - entry_price) < 0.001 
                        and sell.side == order.side
                        and sell.order_id not in self._known_filled):
                        self.client.cancel_order(sell.order_id)
                        self._known_filled.add(sell.order_id)
                        logger.info(f"🔄 OCO: Cancelled take-profit for closed position")
                        break
            else:
                # Take-profit fired - cancel the stop-loss
                for stop in self._stop_loss_orders.get(slug, []):
                    if (stop.entry_price and abs(stop.entry_price - entry_price) < 0.001
                        and stop.side == order.side
                        and stop.order_id not in self._known_filled):
                        self.client.cancel_order(stop.order_id)
                        self._known_filled.add(stop.order_id)
                        logger.info(f"🔄 OCO: Cancelled stop-loss for closed position")
                        break
        
        # Remove position
        positions = self._positions.get(slug, [])
        for pos in positions:
            if pos.side == order.side and abs(pos.entry_price - entry_price) < 0.001:
                positions.remove(pos)
                break
        
        self.notifier.send_fill(order, pnl=pnl)
        
        # RELOAD LOGIC: Re-place buy if in pre-market (only for take-profit exits)
        # Don't reload on stop-loss - that would be chasing losses
        if self._states.get(slug) == StrategyState.ACCUMULATING and not is_stop_loss:
            token_id = event.yes_token_id if order.side == OrderSide.YES else event.no_token_id
            
            reload_order = self.client.place_limit_order(
                token_id=token_id,
                side=order.side,
                order_type=OrderType.BUY,
                price=entry_price,
                size=order.size,
                event_slug=slug
            )
            
            if reload_order:
                self._buy_orders[slug].append(reload_order)
                logger.info(f"♻️ RELOAD: Replenished buy @ {int(entry_price*100)}¢")
    
    def transition_to_live(self, event: EventContext) -> int:
        """
        Handle event going LIVE.
        Cancel all buy orders in batch (faster), keep sells active.
        
        Returns:
            Number of orders cancelled
        """
        slug = event.slug
        
        if self._states.get(slug) != StrategyState.ACCUMULATING:
            return 0
        
        # Collect all unfilled buy order IDs for batch cancellation
        order_ids_to_cancel = [
            order.order_id
            for order in self._buy_orders.get(slug, [])
            if order.order_id not in self._known_filled
        ]
        
        # Batch cancel (one API call instead of many)
        cancelled = self.client.cancel_orders_batch(order_ids_to_cancel)
        
        self._states[slug] = StrategyState.EXITING
        
        logger.info(f"🔴 LIVE MODE: {slug} | Cancelled {cancelled} buys (batch)")
        self.notifier.send_phase_transition(event, cancelled)
        
        return cancelled
    
    def check_completion(self, event: EventContext) -> bool:
        """
        Check if strategy is complete for an event.
        Complete when in EXITING state and no open sell orders.
        """
        slug = event.slug
        
        if self._states.get(slug) != StrategyState.EXITING:
            return False
        
        # Check if any sell or stop-loss orders are still open
        open_orders = self.client.get_open_orders()
        open_ids = {o.get("id") for o in open_orders}
        
        has_pending_sells = any(
            o.order_id in open_ids
            for o in self._sell_orders.get(slug, [])
        )
        
        has_pending_stops = any(
            o.order_id in open_ids
            for o in self._stop_loss_orders.get(slug, [])
        )
        
        if not has_pending_sells and not has_pending_stops:
            self._states[slug] = StrategyState.COMPLETED
            self._results[slug].end_time = time.time()
            
            result = self._results[slug]
            logger.info(f"✅ COMPLETE: {slug} | PnL: ${result.total_pnl:.2f}")
            self.notifier.send_cycle_report(result)
            
            return True
        
        return False
    
    def get_state(self, slug: str) -> Optional[StrategyState]:
        """Get strategy state for an event."""
        return self._states.get(slug)
    
    def get_result(self, slug: str) -> Optional[CycleResult]:
        """Get cycle result for an event."""
        return self._results.get(slug)
    
    def get_pending_count(self, slug: str = None) -> int:
        """Get count of pending orders."""
        if slug:
            buys = len([o for o in self._buy_orders.get(slug, []) if o.order_id not in self._known_filled])
            sells = len([o for o in self._sell_orders.get(slug, []) if o.order_id not in self._known_filled])
            stops = len([o for o in self._stop_loss_orders.get(slug, []) if o.order_id not in self._known_filled])
            return buys + sells + stops
        
        total = 0
        for s in self._states:
            total += self.get_pending_count(s)
        return total
