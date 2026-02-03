"""
Data Models for Production Bot.
KISS: Only the essential models for real trading.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
import time


class OrderSide(Enum):
    """Which token we're trading."""
    YES = "YES"
    NO = "NO"
    
    @property
    def display_name(self) -> str:
        return "🔼 YES" if self == OrderSide.YES else "🔽 NO"


class OrderType(Enum):
    """Buy or Sell."""
    BUY = "BUY"
    SELL = "SELL"


class MarketPhase(Enum):
    """Event lifecycle phase."""
    PRE_MARKET = "pre_market"
    LIVE = "live"
    ENDED = "ended"


class StrategyState(Enum):
    """Strategy state for an event."""
    ACCUMULATING = "accumulating"  # Pre-market, placing buys
    EXITING = "exiting"            # Live, only sells active
    COMPLETED = "completed"        # All positions closed


@dataclass
class EventContext:
    """
    Represents a discovered Polymarket event.
    """
    slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    start_timestamp: float  # Unix timestamp when event goes LIVE
    phase: MarketPhase = MarketPhase.PRE_MARKET
    
    # Real-time price data (best bids for selling)
    # Default to None to indicate "not yet fetched"
    yes_bid: Optional[float] = None
    no_bid: Optional[float] = None
    
    def time_until_start(self) -> float:
        """Seconds until event starts (negative if started)."""
        return self.start_timestamp - time.time()
    
    def has_started(self) -> bool:
        """Check if event has started."""
        return time.time() >= self.start_timestamp
    
    def update_phase(self) -> None:
        """Update phase based on current time."""
        if not self.has_started():
            self.phase = MarketPhase.PRE_MARKET
        else:
            self.phase = MarketPhase.LIVE


@dataclass
class TrackedOrder:
    """
    An order we placed and are tracking.
    """
    order_id: str
    token_id: str
    side: OrderSide
    order_type: OrderType
    price: float
    size: float
    event_slug: str
    placed_at: float = field(default_factory=time.time)
    
    # For matching entry to exit
    entry_price: Optional[float] = None  # Set on sell orders to track original buy
    
    # Track how much of this order we have already processed (accumulated/sold)
    processed_size: float = 0.0


@dataclass
class Position:
    """
    An open position (filled buy, pending exit).
    """
    side: OrderSide
    entry_price: float
    size: float
    token_id: str
    event_slug: str
    entry_time: float = field(default_factory=time.time)


@dataclass
class CycleResult:
    """
    Results from a complete trading cycle for one event.
    """
    event_slug: str
    fills_yes: List[float] = field(default_factory=list)
    fills_no: List[float] = field(default_factory=list)
    total_pnl: float = 0.0
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    
    @property
    def total_fills(self) -> int:
        return len(self.fills_yes) + len(self.fills_no)
