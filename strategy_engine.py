"""
Strategy Engine - Core trading logic.
Manages ladder placement, fill tracking, and position management.
"""

import logging
import time
from typing import Dict, List, Optional, Set

from config import LADDER_LEVELS, EXIT_PRICES, ORDER_SIZE, STOP_LOSS_PRICE, STOP_LOSS_ENTRIES, MIN_NOTIONAL_VALUE_USDC
from models import (
    EventContext, OrderSide, OrderType, TrackedOrder,
    Position, CycleResult, StrategyState, MarketPhase
)
from polymarket_client import PolymarketClient
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


class StrategyEngine:
    """
    Mean Reversion Ladder Strategy.
    
    Logic:
    - PRE_MARKET: Place buy orders at LADDER_LEVELS (40-48¢)
    - On buy fill: Place sell at dynamic EXIT_PRICE (47-49¢ based on entry)
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
        
        # Accumulator for partial fills below minimum order size ($1 USDC notional)
        # Key: (slug, side, token_id, exit_price), Value: {size: float, total_entry_value: float}
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
        
        # =================================================================
        # STATE RECOVERY: Check if we already have orders for this event
        # This prevents double-ordering on bot restart
        # =================================================================
        try:
            existing_orders = self.client.get_open_orders()
            # Filter for orders belonging to this event's tokens
            relevant_orders = [
                o for o in existing_orders 
                if o.get("asset_id") in [event.yes_token_id, event.no_token_id]
            ]
            
            if relevant_orders:
                logger.info(f"♻️ STATE RECOVERY: Found {len(relevant_orders)} existing orders for {slug}. Adopting...")
                
                recovered_count = 0
                for o_data in relevant_orders:
                    try:
                        # Reconstruct TrackedOrder from API data
                        token_id = o_data.get("asset_id")
                        
                        # Determine OrderSide (YES/NO) based on token ID
                        if token_id == event.yes_token_id:
                            side = OrderSide.YES
                        else:
                            side = OrderSide.NO
                            
                        # Determine OrderType (BUY/SELL)
                        type_str = o_data.get("side", "").upper()
                        order_type = OrderType.BUY if type_str == "BUY" else OrderType.SELL
                        
                        tracked = TrackedOrder(
                            order_id=o_data.get("id"),
                            token_id=token_id,
                            side=side,
                            order_type=order_type,
                            price=float(o_data.get("price", 0)),
                            size=float(o_data.get("size", 0)), # Use 'size' or 'original_size'? Usually 'size' is remaining? No, 'size' is usually original in API responses often. Let's assume size is correct for now or use original_size.
                            # Poly API typically returns 'size' as original and 'size_matched' as filled. 
                            # TrackedOrder usually wants original size.
                            event_slug=slug
                        )
                        
                        # Add to appropriate list
                        if order_type == OrderType.BUY:
                            self._buy_orders[slug].append(tracked)
                        else:
                            self._sell_orders[slug].append(tracked)
                            
                        recovered_count += 1
                        
                    except Exception as e:
                        logger.error(f"❌ Failed to recover order {o_data.get('id')}: {e}")
                
                logger.info(f"✅ Recovered {recovered_count} orders for {slug}. Skipping new ladder placement.")
                
                # Get current balance for notification (even if we didn't place new orders)
                balance = self.client.get_balance()
                self.notifier.send_message(f"♻️ Bot reiniciado: Recuperadas {recovered_count} órdenes para {slug}")
                
                # Assume initialized and return count
                return recovered_count
                
        except Exception as e:
            logger.error(f"❌ State recovery check failed: {e}")

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
    
    def check_fills(self, event: EventContext, open_order_ids: Optional[Set[str]] = None) -> Optional[Set[str]]:
        """
        Check for filled orders and process them.
        Now checks PARTIAL fills on OPEN orders too (critical fix).
        
        Args:
            event: The event context
            open_order_ids: Pre-fetched set of open order IDs (from main.py)
        """
        slug = event.slug
        
        if slug not in self._states:
            return open_order_ids  # Return the set even if not initialized
        
        # Use provided open_order_ids or fetch (fallback)
        if open_order_ids is None:
            open_orders = self.client.get_open_orders()
            open_order_ids = {o.get("id") for o in open_orders}
        
        # =================================================================
        # CHECK BUY ORDERS (OPTIMIZED: Priority check + smart filtering)
        # =================================================================
        # Sort by price DESC (48¢ first - most likely to fill)
        buy_orders = self._buy_orders.get(slug, [])
        active_buys = [o for o in buy_orders if o.order_id not in self._known_filled]
        active_buys_sorted = sorted(active_buys, key=lambda o: o.price, reverse=True)
        
        for order in active_buys_sorted:
            # OPTIMIZATION: Only call get_order() if:
            # 1. Order disappeared from open_order_ids (likely filled/cancelled), OR
            # 2. Order is at high price (48¢+) - check every cycle for fast response
            is_high_priority = order.price >= 0.46  # 46¢+ orders checked every cycle
            order_missing = order.order_id not in open_order_ids
            
            if not (order_missing or is_high_priority):
                continue  # Skip low-priority orders that are still open
            
            try:
                order_data = self.client.get_order(order.order_id)
                
                if not order_data:
                    continue
                
                size_matched = float(order_data.get("size_matched") or order_data.get("sizeMatched") or 0)
                status = order_data.get("status", "").upper()
                
                # Process any NEW fills (delta from last check)
                if size_matched > 0:
                    delta_fill = size_matched - order.processed_size
                    
                    if delta_fill > 0.000001:  # Floating point tolerance
                        logger.info(f"✅ BUY fill detected: +{delta_fill:.4f} shares (Total: {size_matched})")
                        
                        # Process the fill IMMEDIATELY
                        safe_delta = round(delta_fill, 6)
                        self._process_buy_fill(order, event, fill_amount=safe_delta)
                        order.processed_size = size_matched
                    
                    # Mark complete if fully filled
                    api_original_size = float(order_data.get("original_size") or order_data.get("originalSize") or order.size)
                    if size_matched >= api_original_size or status in ["MATCHED", "CANCELLED"]:
                        self._known_filled.add(order.order_id)
                
                elif status in ["CANCELLED", "INVALID", "EXPIRED", "REJECTED"]:
                    # Order is dead with 0 fills - stop tracking
                    logger.debug(f"🗑️ BUY order {order.order_id[:10]} is {status} (0 fills). Removed.")
                    self._known_filled.add(order.order_id)
                    
            except Exception as e:
                if order.order_id not in open_order_ids:
                    logger.debug(f"Order {order.order_id[:10]} not found (likely filled): {e}")
                else:
                    logger.error(f"❌ Error checking order {order.order_id[:10]}: {e}")

        
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
                            # PARTIAL FILL: Log info, order stays open for remaining
                            logger.info(f"📊 PARTIAL SELL: {size_matched}/{original_size} shares filled. Waiting...")
                    
                    elif status in ["CANCELED", "CANCELLED", "INVALID", "EXPIRED", "REJECTED"]:
                        # 🗑️ Order is dead and has 0 fills. Stop tracking it.
                        logger.debug(f"🗑️ SELL order {order.order_id[:10]} is {status} (0 fills). Removed.")
                        self._known_filled.add(order.order_id)
                         
                except Exception as e:
                    logger.error(f"❌ Error verifying sell fill for {order.order_id}: {e}")

        # NOTE: Pending sells are processed once per cycle in main.py, not per-event
        
        # =========================================================================
        # STOP-LOSS MONITOR (Client-Side)
        # Only for 48¢ entries: If market drops to 18¢, dump at market price
        # =========================================================================
        self._check_stop_loss(event, open_order_ids)
        
        # Return cached IDs for reuse in check_completion (avoids extra API call)
        return open_order_ids
    
    def process_pending_sells(self) -> None:
        """
        Retry placing sell orders that failed previously.
        IMPORTANT: Call this ONCE per cycle from main.py, not per-event!
        
        CRITICAL: Validates 1 USDC minimum notional value before retry.
        """
        if not self._pending_sells:
            return
        
        still_pending = []
        
        for pending in self._pending_sells:
            # ⚠️ DUST VALIDATION: Check if order meets minimum notional value
            notional_value = pending['size'] * pending['exit_price']
            
            if notional_value < MIN_NOTIONAL_VALUE_USDC:
                min_shares_needed = MIN_NOTIONAL_VALUE_USDC / pending['exit_price']
                logger.error(
                    f"💀 DUST REJECTED: {pending['size']:.4f} shares @ {int(pending['exit_price']*100)}¢ "
                    f"= ${notional_value:.4f} < ${MIN_NOTIONAL_VALUE_USDC}. Need {min_shares_needed:.2f} shares. "
                    f"⚠️ Cannot sell - will expire worthless!"
                )
                # Don't retry, it will always fail
                continue
            
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
                
                # Notify via Telegram
                self.notifier.send_sell_placed(
                    side_name=pending['side'].display_name,
                    entry_price=pending['entry_price'],
                    exit_price=pending['exit_price'],
                    size=pending['size'],
                    slug=slug
                )
            else:
                # Still failing - increment attempts
                pending['attempts'] += 1
                
                # SMART RETRY: Check actual balance on EVERY failed attempt (not just after 5)
                try:
                    actual_balance = self.client.get_token_balance(pending['token_id'])
                    
                    if actual_balance == 0:
                        # Settlement delay - tokens not yet on-chain
                        # Keep trying but cap at 60 attempts (~30s at 0.5s poll)
                        if pending['attempts'] <= 60:
                            logger.debug(f"⏳ Settlement delay (bal=0) for {pending['slug']}. Attempt {pending['attempts']}/60")
                            still_pending.append(pending)
                        else:
                            # After 30 seconds, something is wrong
                            logger.error(f"❌ Settlement timeout after 60 attempts for {pending['slug']}")
                            self.notifier.send_message(
                                f"⚠️ ALERTA: Settlement timeout para {pending['side'].display_name}. "
                                f"Verifica manualmente."
                            )
                        continue
                        
                    elif 0 < actual_balance < pending['size']:
                        # FLOAT PRECISION: Actual balance is less than requested
                        # Use the REAL balance (truncated to avoid over-selling)
                        adjusted_size = float(int(actual_balance * 1000000)) / 1000000  # Truncate to 6 decimals
                        logger.warning(
                            f"📉 Float precision fix: {pending['size']:.6f} -> {adjusted_size:.6f} "
                            f"for {pending['slug']}"
                        )
                        pending['size'] = adjusted_size
                        pending['attempts'] = 0  # Reset for new size
                        still_pending.append(pending)
                        continue
                        
                    elif actual_balance >= pending['size']:
                        # We have enough balance but order still failed - API issue
                        if pending['attempts'] <= 10:
                            still_pending.append(pending)
                            logger.warning(
                                f"⚠️ SELL retry {pending['attempts']}/10 (balance OK): "
                                f"{pending['side'].display_name} @ {int(pending['exit_price']*100)}¢"
                            )
                        else:
                            logger.error(
                                f"❌ GAVE UP on SELL after 10 attempts (balance was OK): "
                                f"{pending['side'].display_name}"
                            )
                            self.notifier.send_message(
                                f"⚠️ ALERTA CRÍTICA: No se pudo colocar venta después de 10 intentos. "
                                f"Revisa: {pending['side'].display_name} @ {int(pending['exit_price']*100)}¢"
                            )
                        continue
                        
                except Exception as e:
                    logger.error(f"❌ Error checking balance for retry: {e}")
                    if pending['attempts'] <= 10:
                        still_pending.append(pending)
        
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
                    # No sleep needed - cancel is synchronous
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
    
    def _flush_accumulator_for_event(self, event: EventContext) -> None:
        """
        Flush any accumulated shares for an event when transitioning to LIVE.
        
        CRITICAL: Polymarket enforces Precio × Cantidad ≥ 1 USDC.
        If dust doesn't meet minimum, it gets LOCKED until market expiration.
        We try to sell anyway and log if rejected.
        """
        slug = event.slug
        
        # Find all accumulator keys for this event
        keys_to_flush = [k for k in self._fill_accumulator.keys() if k[0] == slug]
        
        for acc_key in keys_to_flush:
            acc = self._fill_accumulator[acc_key]
            
            if acc['size'] > 0.001:  # Only if there's meaningful size
                _, side, token_id, exit_price = acc_key
                sell_size = acc['size']
                avg_entry = acc['total_entry_value'] / acc['size'] if acc['size'] > 0 else 0
                
                # Check if meets minimum notional value
                notional_value = sell_size * exit_price
                min_shares_required = MIN_NOTIONAL_VALUE_USDC / exit_price
                
                if notional_value < MIN_NOTIONAL_VALUE_USDC:
                    logger.error(
                        f"💀 DUST LOCKED: {sell_size:.4f} shares @ {int(exit_price*100)}¢ "
                        f"= ${notional_value:.4f} < ${MIN_NOTIONAL_VALUE_USDC} (API will reject). "
                        f"Need {min_shares_required:.2f} shares minimum. "
                        f"⚠️ These shares will be LOCKED until market expiration!"
                    )
                    # Clear accumulator anyway (nothing we can do)
                    self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
                    
                    # Notify Telegram about locked dust
                    self.notifier.send_message(
                        f"💀 DUST LOCKED ({slug})\n"
                        f"{side.display_name}: {sell_size:.4f} shares @ {int(exit_price*100)}¢\n"
                        f"Value: ${notional_value:.4f} < ${MIN_NOTIONAL_VALUE_USDC} min\n"
                        f"⚠️ Cannot sell - will expire worthless!"
                    )
                    continue  # Skip this dust, cannot sell
                
                logger.warning(
                    f"📦 FLUSH ACCUMULATOR: {sell_size:.4f} shares @ exit {int(exit_price*100)}¢ "
                    f"(${notional_value:.4f} meets ${MIN_NOTIONAL_VALUE_USDC} minimum)"
                )
                
                # 🛡️ PRECISION SAFETY: Use actual balance (truncated)
                try:
                    actual_balance = self.client.get_token_balance(token_id)
                    sell_size = float(int(actual_balance * 1000000)) / 1000000
                    
                    if sell_size * exit_price < MIN_NOTIONAL_VALUE_USDC:
                        logger.error(f"💀 DUST in flush: ${sell_size * exit_price:.4f} < ${MIN_NOTIONAL_VALUE_USDC}")
                        self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
                        continue
                        
                except Exception as e:
                    logger.warning(f"⚠️ Balance check failed in flush: {e}")
                
                # Add to pending sells (will be processed immediately or retried)
                pending = {
                    'token_id': token_id,
                    'side': side,
                    'exit_price': exit_price,
                    'size': sell_size,
                    'slug': slug,
                    'entry_price': avg_entry,
                    'attempts': 0
                }
                self._pending_sells.append(pending)
                
                # Clear this accumulator
                self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
    
    def _process_buy_fill(self, order: TrackedOrder, event: EventContext, fill_amount: Optional[float] = None) -> None:
        """Handle a buy order fill."""
        slug = event.slug
        entry_price = order.price
        exit_price = self._get_exit_price(entry_price)
        
        # Use provided fill_amount (processed delta) or fallback to order.size
        # The mutation of order.size is dangerous, so explicit arg is better.
        actual_size = fill_amount if fill_amount is not None else order.size
        
        logger.info(
            f"✅ BUY FILLED: {order.side.display_name} @ {int(entry_price*100)}¢ "
            f"→ Exit target: {int(exit_price*100)}¢"
        )
        
        # Notify Telegram
        telegram_msg = (
            f"✅ BUY FILLED: {order.side.display_name} @ {int(entry_price*100)}¢ ({actual_size:.2f} shares)\n"
            f"🎯 Target: {int(exit_price*100)}¢"
        )
        success = self.notifier.send_message(telegram_msg)
        if not success:
            logger.warning(f"⚠️ Failed to send BUY notification to Telegram")
        
        # Record position
        position = Position(
            side=order.side,
            entry_price=entry_price,
            size=actual_size,
            token_id=order.token_id,
            event_slug=slug
        )
        self._positions[slug].append(position)
        
        # Record in results
        if order.side == OrderSide.YES:
            self._results[slug].fills_yes.append(entry_price)
        else:
            self._results[slug].fills_no.append(entry_price)
        
        # =====================================================================
        # POLYMARKET MINIMUM ORDER SIZE (Dynamic)
        # API enforces: Precio × Cantidad ≥ 1 USDC (valor nocional)
        # Ref: CLOB API error INVALID_ORDER_MIN_SIZE
        # =====================================================================
        
        # Calculate minimum shares needed at exit price
        # Add 1% margin to avoid rejections due to rounding
        min_shares_required = (MIN_NOTIONAL_VALUE_USDC / exit_price) * 1.01
        
        # Accumulate fills BY EXIT PRICE to preserve the EXIT_PRICES strategy
        # Key includes exit_price so 47¢→48¢ and 48¢→49¢ entries are tracked separately
        acc_key = (slug, order.side, order.token_id, exit_price)
        
        if acc_key not in self._fill_accumulator:
            self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
        
        acc = self._fill_accumulator[acc_key]
        acc['size'] += actual_size
        acc['total_entry_value'] += actual_size * entry_price
        
        logger.info(
            f"📦 Accumulated for exit @{int(exit_price*100)}¢: {acc['size']:.2f} shares "
            f"(need {min_shares_required:.2f} = ${MIN_NOTIONAL_VALUE_USDC}/{exit_price:.2f})"
        )
        
        # Only place sell when we have enough shares for this specific exit price
        # Use 99% threshold to handle partial fills
        SELL_THRESHOLD = min_shares_required * 0.99
        if acc['size'] >= SELL_THRESHOLD:
            avg_entry = acc['total_entry_value'] / acc['size']
            
            # 🛡️ PRECISION SAFETY: Always use actual balance (truncated)
            # This avoids "insufficient balance" errors from float imprecision
            try:
                actual_balance = self.client.get_token_balance(order.token_id)
                # Truncate to 6 decimals
                sell_size = float(int(actual_balance * 1000000)) / 1000000
                
                # Validate minimum notional ($1 USDC)
                if sell_size * exit_price < MIN_NOTIONAL_VALUE_USDC:
                    logger.error(
                        f"💀 DUST: {sell_size:.6f} shares @ {int(exit_price*100)}¢ "
                        f"= ${sell_size * exit_price:.4f} < ${MIN_NOTIONAL_VALUE_USDC}"
                    )
                    self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
                    return
                    
            except Exception as e:
                logger.warning(f"⚠️ Balance check failed, using accumulator: {e}")
                sell_size = acc['size']
            
            # Reset accumulator
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
                
                # Notify via Telegram (critical for monitoring)
                self.notifier.send_sell_placed(
                    side_name=order.side.display_name,
                    entry_price=avg_entry,
                    exit_price=exit_price,
                    size=sell_size,
                    slug=slug
                )
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
        
    def audit_cancelled_orders(self, order_ids: List[str], event: EventContext) -> None:
        """
        Audit a list of BUY orders that were just cancelled.
        If we find they actually filled (fully or partially) during the cancellation race,
        we treat them as filled and place the corresponding sell orders.
        """
        if not order_ids:
            return
            
        logger.info(f"🕵️ Auditing {len(order_ids)} cancelled orders for hidden fills...")
        
        # We need to find the TrackedOrder objects for these IDs
        # They should still be in _buy_orders
        orders_to_audit = []
        for order in self._buy_orders.get(event.slug, []):
            if order.order_id in order_ids:
                orders_to_audit.append(order)
        
        for order in orders_to_audit:
            try:
                # Fetch final status from API
                order_data = self.client.get_order(order.order_id)
                
                # Safety: Skip if API returned None
                if not order_data:
                    logger.debug(f"⏳ Order {order.order_id[:10]}... not found in API during audit")
                    continue
                
                # Check if it has any matched size
                size_matched = float(order_data.get("size_matched") or order_data.get("sizeMatched") or 0)
                original_size = float(order_data.get("original_size") or order_data.get("originalSize") or order.size)
                
                if size_matched > 0:
                    
                    # LOGIC:
                    # Uses DELTA logic to prevent double counting if partial fill was already seen.
                    delta_fill = size_matched - order.processed_size
                    
                    if delta_fill > 0.000001:
                        logger.warning(
                            f"⚠️ RACE CONDITION AUDIT: Order {order.order_id[:10]} found with +{delta_fill:.4f} hidden shares! "
                            f"(Total: {size_matched}/{original_size})"
                        )
                        
                        # SAFETY: Pass delta explicitely
                        safe_delta = round(delta_fill, 6)
                        self._process_buy_fill(order, event, fill_amount=safe_delta)
                        
                        # Mark as processed
                        order.processed_size = size_matched
                        
                    # If fully filled now, mark as known
                    if size_matched >= original_size:
                        self._known_filled.add(order.order_id)
                        
            except Exception as e:
                logger.error(f"❌ Failed to audit order {order.order_id}: {e}")
                        

    
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
        
        # =========================================================================
        # 🛡️ RACE CONDITION AUDIT
        # Check immediately if any "cancelled" orders actually filled
        # =========================================================================
        if order_ids_to_cancel:
            logger.info(f"⏳ Auditing {len(order_ids_to_cancel)} cancelled orders...")
            # No sleep needed - API is fast enough
            self.audit_cancelled_orders(order_ids_to_cancel, event)
        
        # =========================================================================
        # 📦 FLUSH ACCUMULATOR: Process any remaining accumulated shares
        # Sell if meets $1 USDC minimum, otherwise mark as dust
        # =========================================================================
        self._flush_accumulator_for_event(event)
            
        self._states[slug] = StrategyState.EXITING
        
        logger.info(f"🔴 LIVE MODE: {slug} | Cancelled {cancelled} buys (batch)")
        self.notifier.send_phase_transition(event, cancelled)
        
        return cancelled
    
    def check_completion(self, event: EventContext, cached_open_ids: set = None) -> bool:
        """
        Check if strategy is complete for an event.
        Complete when in EXITING state and no open sell orders.
        
        Args:
            event: The event context
            cached_open_ids: Optional set of open order IDs (avoids extra API call)
        """
        slug = event.slug
        
        if self._states.get(slug) != StrategyState.EXITING:
            return False
        
        # Use cached IDs if provided, otherwise fetch
        if cached_open_ids is None:
            open_orders = self.client.get_open_orders()
            open_ids = {o.get("id") for o in open_orders}
        else:
            open_ids = cached_open_ids
        
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
