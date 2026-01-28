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
        self.notifier.send_ladder_placed(slug, orders_placed)
        
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
            
            # If order is not in open orders, it was filled (or cancelled)
            if order.order_id not in open_order_ids:
                self._process_buy_fill(order, event)
                self._known_filled.add(order.order_id)
        
        # Check sell orders (take-profit)
        for order in self._sell_orders.get(slug, []):
            if order.order_id in self._known_filled:
                continue
            
            if order.order_id not in open_order_ids:
                self._process_sell_fill(order, event, is_stop_loss=False)
                self._known_filled.add(order.order_id)
        
        # Check stop-loss orders
        for order in self._stop_loss_orders.get(slug, []):
            if order.order_id in self._known_filled:
                continue
            
            if order.order_id not in open_order_ids:
                self._process_sell_fill(order, event, is_stop_loss=True)
                self._known_filled.add(order.order_id)
    
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
        
        # Place sell order at DYNAMIC exit price
        sell_order = self.client.place_limit_order(
            token_id=order.token_id,
            side=order.side,
            order_type=OrderType.SELL,
            price=exit_price,
            size=order.size,
            event_slug=slug
        )
        
        if sell_order:
            sell_order.entry_price = entry_price  # Track original entry
            self._sell_orders[slug].append(sell_order)
        
        # STOP-LOSS: Only for 48¢ entries
        if self._needs_stop_loss(entry_price):
            stop_order = self.client.place_limit_order(
                token_id=order.token_id,
                side=order.side,
                order_type=OrderType.SELL,
                price=STOP_LOSS_PRICE,
                size=order.size,
                event_slug=slug
            )
            
            if stop_order:
                stop_order.entry_price = entry_price
                self._stop_loss_orders[slug].append(stop_order)
                logger.info(
                    f"🛡️ STOP-LOSS placed: {order.side.display_name} "
                    f"@ {int(STOP_LOSS_PRICE*100)}¢ (protects {int(entry_price*100)}¢ entry)"
                )
        
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
        Cancel all buy orders, keep sells active.
        
        Returns:
            Number of orders cancelled
        """
        slug = event.slug
        
        if self._states.get(slug) != StrategyState.ACCUMULATING:
            return 0
        
        cancelled = 0
        
        # Cancel all pending buy orders
        for order in self._buy_orders.get(slug, []):
            if order.order_id not in self._known_filled:
                if self.client.cancel_order(order.order_id):
                    cancelled += 1
        
        self._states[slug] = StrategyState.EXITING
        
        logger.info(f"🔴 LIVE MODE: {slug} | Cancelled {cancelled} buys")
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
