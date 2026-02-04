"""
Strategy Engine - Core trading logic.
Manages ladder placement, fill tracking, and position management.
"""

import logging
import time
from typing import Dict, List, Optional, Set

from config import LADDER_LEVELS, EXIT_PRICES, ORDER_SIZE, STOP_LOSS_PRICE, STOP_LOSS_ENTRIES, MIN_SHARES
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
        
        # Accumulator for partial fills below minimum order size (6 shares)
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
        exit_price = EXIT_PRICES.get(entry_rounded)
        
        if exit_price is None:
            # DIAGNOSTIC: Log when using default (potential issue)
            logger.warning(
                f"⚠️ Entry price {entry_price:.6f} (rounded: {entry_rounded}) "
                f"NOT in EXIT_PRICES map! Using default 49¢. "
                f"Available keys: {sorted(EXIT_PRICES.keys())}"
            )
            return 0.49
        
        return exit_price
    
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
                    # IMPROVEMENT: Track API failures to detect phantom fills
                    if not hasattr(order, 'api_fail_count'):
                        order.api_fail_count = 0
                    order.api_fail_count += 1
                    
                    if order.api_fail_count >= 20:  # ~10 seconds of failures
                        logger.error(
                            f"⚠️ API failing consistently for {order.order_id[:10]} "
                            f"(x{order.api_fail_count}). Possible phantom fill!"
                        )
                        self.notifier.send_message(
                            f"⚠️ ALERTA: API no responde para orden {order.order_id[:10]}. "
                            f"Verificar manualmente si hay tokens comprados."
                        )
                        order.api_fail_count = 0  # Reset to avoid spam
                    continue
                
                # Reset fail counter on success
                if hasattr(order, 'api_fail_count'):
                    order.api_fail_count = 0
                
                size_matched = float(order_data.get("size_matched") or order_data.get("sizeMatched") or 0)
                status = order_data.get("status", "").upper()
                
                # Process any NEW fills (delta from last check)
                if size_matched > 0:
                    delta_fill = size_matched - order.processed_size
                    
                    if delta_fill > 0.000001:  # Floating point tolerance
                        logger.info(f"✅ BUY fill: +{delta_fill:.2f} shares @ {order.price:.2f}¢ → Total: {size_matched:.2f}")
                        
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
                    logger.error(f"❌ Error verifying sell fill for {order.order_id[:10]}: {e}")
                    # Track API failures for this order
                    if not hasattr(order, 'verify_fail_count'):
                        order.verify_fail_count = 0
                    order.verify_fail_count += 1
                    
                    if order.verify_fail_count >= 3:  # FAST recovery: only 3 attempts
                        logger.error(
                            f"⚠️ SELL order {order.order_id[:10]} desapareció! Recuperación RÁPIDA..."
                        )
                        
                        # RESILIENCE: Check actual token balance to decide action
                        try:
                            actual_balance = self.client.get_token_balance(order.token_id)
                            
                            if actual_balance >= order.size * 0.99:  # We still have the tokens
                                logger.warning(
                                    f"🔄 RECOVERY RÁPIDA: Tokens en wallet ({actual_balance:.2f} shares). "
                                    f"Recolocando venta en <3 segundos..."
                                )
                                # Add to pending sells for retry
                                pending = {
                                    'token_id': order.token_id,
                                    'side': order.side,
                                    'exit_price': order.price,
                                    'size': order.size,
                                    'slug': slug,
                                    'entry_price': order.entry_price or 0,
                                    'attempts': 0
                                }
                                self._pending_sells.append(pending)
                                self._known_filled.add(order.order_id)  # Stop tracking the old order
                                order.verify_fail_count = 0  # Reset on success
                                
                                self.notifier.send_message(
                                    f"🔄 RECOVERY RÁPIDA (<3s):\n"
                                    f"Venta {order.price:.2f}¢ recolocada automáticamente\n"
                                    f"{order.size:.0f} shares | {slug}"
                                )
                            else:
                                # Tokens not found - likely sold or error
                                logger.warning(
                                    f"✅ RECOVERY RÁPIDA: Tokens vendidos (balance={actual_balance:.2f}). "
                                    f"Procesando como venta ejecutada en <3s."
                                )
                                self._known_filled.add(order.order_id)
                                order.verify_fail_count = 0  # Reset on success
                                
                                # Try to process as sell fill (PnL might be off but better than losing track)
                                if order.entry_price and order.entry_price > 0:
                                    self._process_sell_fill(order, event, is_stop_loss=False)
                                
                        except Exception as balance_err:
                            logger.error(f"❌ Recovery attempt #{order.verify_fail_count} failed: {balance_err}")
                            # NO resetear contador - seguir intentando en próximos ciclos
                            # Enviar alerta solo cada 10 intentos para no spamear
                            if order.verify_fail_count % 10 == 0:
                                self.notifier.send_message(
                                    f"⚠️ API CAÍDA (intento {order.verify_fail_count}):\n"
                                    f"No se puede verificar orden {order.order_id[:10]}.\n"
                                    f"El bot seguirá intentando automáticamente."
                                )
                        else:
                            # Success: reset counter and stop tracking this order
                            order.verify_fail_count = 0

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
        
        CRITICAL: Validates minimum 6 shares before retry.
        """
        if not self._pending_sells:
            return
        
        still_pending = []
        
        for pending in self._pending_sells:
            # ⚠️ DUST VALIDATION: Check if order meets minimum shares
            if pending['size'] < MIN_SHARES:
                logger.error(
                    f"💀 DUST REJECTED: {pending['size']:.2f} shares < {MIN_SHARES} min. "
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
                
                # SMART RETRY: Check actual balance AND open orders
                try:
                    actual_balance = self.client.get_token_balance(pending['token_id'])
                    
                    # ⚠️ CRITICAL: Check if tokens are already locked in open sell orders
                    # Polymarket locks tokens when you have an open sell order
                    open_orders = self.client.get_open_orders()
                    locked_in_sells = sum(
                        float(o.get("size", 0)) - float(o.get("size_matched", 0) or o.get("sizeMatched", 0))
                        for o in open_orders
                        if o.get("asset_id") == pending['token_id'] 
                        and o.get("side", "").upper() == "SELL"
                    )
                    
                    available_balance = actual_balance - locked_in_sells
                    
                    if available_balance <= 0:
                        # Tokens are locked in existing sell orders - no need to retry
                        logger.warning(
                            f"🔒 Tokens locked: {actual_balance:.2f} total, {locked_in_sells:.2f} in open sells. "
                            f"Skipping retry for {pending['side'].display_name}"
                        )
                        # Check if we already have a sell order for this - avoid duplicates
                        existing_sell = any(
                            o.get("asset_id") == pending['token_id'] 
                            and o.get("side", "").upper() == "SELL"
                            and abs(float(o.get("price", 0)) - pending['exit_price']) < 0.001
                            for o in open_orders
                        )
                        if existing_sell:
                            logger.info(f"✅ Sell order already exists for this position - removing from pending")
                            continue  # Don't retry, order already exists
                        # If no matching order exists but balance is locked, keep trying briefly
                        if pending['attempts'] <= 5:
                            still_pending.append(pending)
                        continue
                    
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
                        
                    elif 0 < available_balance < pending['size']:
                        # Available balance is less than requested - adjust to exact balance
                        adjusted_size = available_balance
                        
                        # Validate minimum shares
                        if adjusted_size < MIN_SHARES:
                            logger.error(
                                f"💀 DUST after adjustment: {adjusted_size:.2f} shares < {MIN_SHARES} min"
                            )
                            continue  # Can't sell dust
                        
                        logger.warning(
                            f"📉 Balance adjustment: {pending['size']:.6f} -> {adjusted_size:.6f} "
                            f"(available: {available_balance:.6f}, locked: {locked_in_sells:.6f})"
                        )
                        pending['size'] = adjusted_size
                        pending['attempts'] = 0  # Reset for new size
                        still_pending.append(pending)
                        continue
                        
                    elif available_balance >= pending['size']:
                        # We have enough available balance but order still failed - API issue
                        if pending['attempts'] <= 10:
                            still_pending.append(pending)
                            logger.warning(
                                f"⚠️ SELL retry {pending['attempts']}/10 (avail={available_balance:.2f}): "
                                f"{pending['side'].display_name} @ {int(pending['exit_price']*100)}¢"
                            )
                        else:
                            logger.error(
                                f"❌ GAVE UP on SELL after 10 attempts (avail={available_balance:.2f}): "
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
                cancel_success = False
                try:
                    logger.info(f"🔓 Cancelling TP order {order.order_id[:8]}...")
                    self.client.cancel_order(order.order_id)
                    cancel_success = True
                except Exception as e:
                    logger.error(f"❌ Failed to cancel TP for SL: {e}")
                    # CRITICAL FIX: Verify if order was actually cancelled (timeout scenario)
                    try:
                        order_status = self.client.get_order(order.order_id)
                        if order_status is None:
                            logger.warning("📋 Order not found - likely cancelled. Proceeding with SL...")
                            cancel_success = True
                        elif order_status.get("status", "").upper() in ["CANCELLED", "CANCELED", "MATCHED"]:
                            logger.warning(f"📋 Order status: {order_status.get('status')}. Proceeding with SL...")
                            cancel_success = True
                        else:
                            logger.error(f"❌ Order still active: {order_status.get('status')}. Cannot proceed.")
                    except Exception as e2:
                        logger.error(f"❌ Failed to verify order status: {e2}")
                
                if not cancel_success:
                    continue  # Really failed, retry next cycle
                
                self._known_filled.add(order.order_id)  # Mark as handled
                
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
                
                # Check if meets minimum shares
                if sell_size < MIN_SHARES:
                    logger.error(
                        f"💀 DUST LOCKED: {sell_size:.2f} shares < {MIN_SHARES} min. "
                        f"⚠️ These shares will be LOCKED until market expiration!"
                    )
                    # Clear accumulator anyway (nothing we can do)
                    self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
                    
                    # Notify Telegram about locked dust
                    self.notifier.send_message(
                        f"💀 DUST LOCKED ({slug})\n"
                        f"{side.display_name}: {sell_size:.2f} shares < {MIN_SHARES} min\n"
                        f"⚠️ Cannot sell - will expire worthless!"
                    )
                    continue  # Skip this dust, cannot sell
                
                logger.warning(
                    f"📦 FLUSH ACCUMULATOR: {sell_size:.0f} shares @ exit {int(exit_price*100)}¢ "
                    f"(meets {MIN_SHARES} shares minimum)"
                )
                
                # ⚠️ CRITICAL: Keep sell_size from accumulator, only reduce if necessary
                # sell_size is already set from acc['size'] above
                
                # 🛡️ SAFETY: Verify we have enough available balance
                try:
                    actual_balance = self.client.get_token_balance(token_id)
                    
                    # Check tokens locked in open sell orders
                    open_orders = self.client.get_open_orders()
                    locked_in_sells = sum(
                        float(o.get("size", 0)) - float(o.get("size_matched", 0) or o.get("sizeMatched", 0))
                        for o in open_orders
                        if o.get("asset_id") == token_id 
                        and o.get("side", "").upper() == "SELL"
                    )
                    
                    available_balance = actual_balance - locked_in_sells
                    
                    # Only reduce if available is LESS than what we want to sell
                    if available_balance < sell_size:
                        if available_balance <= 0:
                            logger.warning(
                                f"🔒 All tokens locked in flush ({locked_in_sells:.2f} in sells). Skipping."
                            )
                            self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
                            continue
                        
                        sell_size = available_balance  # Use exact balance from Polymarket
                    
                    if sell_size < MIN_SHARES:
                        logger.error(f"💀 DUST in flush: {sell_size:.2f} shares < {MIN_SHARES} min")
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
        
        # DIAGNOSTIC: Log exact prices to detect float precision issues
        logger.info(
            f"✅ BUY FILLED: {order.side.display_name} @ {entry_price:.2f}¢ → Exit: {exit_price:.2f}¢ ({actual_size:.0f} shares)"
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
        # POLYMARKET MINIMUM ORDER SIZE: 6 shares
        # API enforces: minimum 5 shares per order
        # We use 6 to avoid floating point edge cases (4.999 rejected)
        # =====================================================================
        
        # Accumulate fills BY EXIT PRICE to preserve the EXIT_PRICES strategy
        # Key includes exit_price so 47¢→48¢ and 48¢→49¢ entries are tracked separately
        acc_key = (slug, order.side, order.token_id, exit_price)
        
        if acc_key not in self._fill_accumulator:
            self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
        
        acc = self._fill_accumulator[acc_key]
        acc['size'] += actual_size
        acc['total_entry_value'] += actual_size * entry_price
        
        logger.info(
            f"📦 Accumulated: {acc['size']:.0f} shares @ exit {exit_price:.2f}¢ "
            f"(need {MIN_SHARES} for min)"
        )
        
        # Only place sell when we have enough shares for this specific exit price
        if acc['size'] >= MIN_SHARES:
            avg_entry = acc['total_entry_value'] / acc['size']
            
            # ⚠️ CRITICAL: Use accumulator size, NOT total balance!
            # The accumulator tracks exactly how many shares we need to sell for THIS fill
            sell_size = acc['size']
            
            # 🛡️ SAFETY: Verify we have enough available balance
            try:
                actual_balance = self.client.get_token_balance(order.token_id)
                
                # Check tokens locked in open sell orders
                open_orders = self.client.get_open_orders()
                locked_in_sells = sum(
                    float(o.get("size", 0)) - float(o.get("size_matched", 0) or o.get("sizeMatched", 0))
                    for o in open_orders
                    if o.get("asset_id") == order.token_id 
                    and o.get("side", "").upper() == "SELL"
                )
                
                available_balance = actual_balance - locked_in_sells
                
                # Only reduce sell_size if available is LESS than what we want to sell
                if available_balance < sell_size:
                    if available_balance <= 0:
                        logger.warning(
                            f"🔒 All tokens locked in open sells ({locked_in_sells:.2f}). "
                            f"Will retry when orders fill/cancel."
                        )
                        # Don't clear accumulator - keep tracking
                        return
                    
                    # Use exact available balance from Polymarket
                    sell_size = available_balance
                    logger.warning(
                        f"📉 Adjusted sell size: {acc['size']:.2f} → {sell_size:.2f} "
                        f"(available: {available_balance:.2f}, locked: {locked_in_sells:.2f})"
                    )
                
                # Validate minimum shares (6)
                if sell_size < MIN_SHARES:
                    logger.error(
                        f"💀 DUST: {sell_size:.2f} shares < {MIN_SHARES} min"
                    )
                    self._fill_accumulator[acc_key] = {'size': 0.0, 'total_entry_value': 0.0}
                    return
                    
            except Exception as e:
                logger.warning(f"⚠️ Balance check failed, using accumulator size: {e}")
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
                logger.info(f"✅ SELL placed: {order.side.display_name} @ {exit_price:.2f}¢ x{sell_size:.0f}")
                
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
                logger.warning(f"⚠️ SELL queued for retry: {order.side.display_name} @ {exit_price:.2f}¢ x{sell_size:.0f}")
        
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
        
        IMPROVED: Detects sells that disappeared without filling (event resolution).
        
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
        
        # Track sells that are still open vs disappeared
        pending_sells = []
        disappeared_sells = []
        
        for o in self._sell_orders.get(slug, []):
            if o.order_id in self._known_filled:
                continue  # Already processed
            
            if o.order_id in open_ids:
                pending_sells.append(o)
            else:
                # Order disappeared - check if it was filled or just cancelled
                try:
                    order_data = self.client.get_order(o.order_id)
                    if order_data:
                        size_matched = float(order_data.get("size_matched") or order_data.get("sizeMatched") or 0)
                        if size_matched > 0:
                            # Was filled - process it
                            o.size = size_matched
                            self._process_sell_fill(o, event, is_stop_loss=False)
                            self._known_filled.add(o.order_id)
                        else:
                            # Disappeared with 0 fills = cancelled by event resolution
                            disappeared_sells.append(o)
                            self._known_filled.add(o.order_id)
                    else:
                        # API returned None - assume cancelled
                        disappeared_sells.append(o)
                        self._known_filled.add(o.order_id)
                except Exception as e:
                    logger.warning(f"⚠️ Could not verify sell {o.order_id[:10]}: {e}")
                    disappeared_sells.append(o)
                    self._known_filled.add(o.order_id)
        
        # Alert about sells that didn't execute
        if disappeared_sells:
            # RESILIENCE RÁPIDA: For each disappeared sell, check if tokens still exist
            recovered = 0
            for sell_order in disappeared_sells:
                try:
                    actual_balance = self.client.get_token_balance(sell_order.token_id)
                    
                    if actual_balance >= sell_order.size * 0.99:  # Tokens still there
                        logger.warning(
                            f"🔄 RECOVERY INMEDIATA: Recolocando venta {actual_balance:.0f} shares @ {sell_order.price:.2f}¢"
                        )
                        # Requeue the sell
                        pending = {
                            'token_id': sell_order.token_id,
                            'side': sell_order.side,
                            'exit_price': sell_order.price,
                            'size': actual_balance,  # Use actual balance
                            'slug': slug,
                            'entry_price': sell_order.entry_price or 0,
                            'attempts': 0
                        }
                        self._pending_sells.append(pending)
                        recovered += 1
                except Exception as e:
                    logger.error(f"❌ Recovery check failed for {sell_order.order_id[:10]}: {e}")
            
            if recovered > 0:
                logger.warning(f"🔄 {recovered} ventas recuperadas INMEDIATAMENTE")
                self.notifier.send_message(
                    f"🔄 RECOVERY INMEDIATA ({slug}):\n"
                    f"{recovered} venta(s) recolocada(s) en <1 segundo.\n"
                    f"El bot continúa operando normalmente."
                )
            else:
                total_lost_value = sum(s.size * s.price for s in disappeared_sells)
                logger.warning(
                    f"⚠️ {len(disappeared_sells)} sell orders lost (tokens not found): "
                    f"~${total_lost_value:.2f} notional. Likely liquidated."
                )
                self.notifier.send_message(
                    f"⚠️ ({slug}):\n"
                    f"{len(disappeared_sells)} órdenes canceladas (tokens no encontrados).\n"
                    f"Posiciones probablemente liquidadas al precio de resolución."
                )
        
        has_pending_stops = any(
            o.order_id in open_ids
            for o in self._stop_loss_orders.get(slug, [])
            if o.order_id not in self._known_filled
        )
        
        if not pending_sells and not has_pending_stops:
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
