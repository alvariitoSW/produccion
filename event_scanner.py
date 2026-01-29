"""
Event Scanner - Discovers Bitcoin Up/Down events from Gamma API.
Generates dynamic slugs based on current time (same pattern as working simulator).
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
import json

import requests
import pytz

from config import GAMMA_API
from models import EventContext, MarketPhase

logger = logging.getLogger(__name__)

# Eastern timezone for Polymarket events
ET = pytz.timezone('America/New_York')

# Event duration (1 hour = 3600 seconds)
EVENT_DURATION = 3600

# Month names for slug generation
MONTH_NAMES = {
    1: "january", 2: "february", 3: "march", 4: "april",
    5: "may", 6: "june", 7: "july", 8: "august",
    9: "september", 10: "october", 11: "november", 12: "december"
}


def generate_slug(timestamp: int) -> str:
    """
    Generate event slug from timestamp.
    
    Format: bitcoin-up-or-down-{month}-{day}-{hour}(am/pm)-et
    Example: bitcoin-up-or-down-january-23-6pm-et
    """
    dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    dt_et = dt_utc.astimezone(ET)
    
    month = MONTH_NAMES[dt_et.month]
    day = dt_et.day
    hour = dt_et.hour
    
    # Convert to 12-hour format
    if hour == 0:
        hour_12 = 12
        am_pm = "am"
    elif hour < 12:
        hour_12 = hour
        am_pm = "am"
    elif hour == 12:
        hour_12 = 12
        am_pm = "pm"
    else:
        hour_12 = hour - 12
        am_pm = "pm"
    
    return f"bitcoin-up-or-down-{month}-{day}-{hour_12}{am_pm}-et"


def get_current_hour_timestamp() -> int:
    """Get timestamp of the current hour in ET."""
    now = datetime.now(ET)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    return int(hour_start.timestamp())


class EventScanner:
    """
    Scans for Bitcoin Up/Down hourly events using dynamic slug generation.
    KISS: One simple job - find events we can trade.
    """
    
    def __init__(self, max_events: int = 24):
        self.max_events = max_events
        self._active_events: Dict[str, EventContext] = {}
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "ProductionBot/1.0"
        })
    
    def scan_for_events(self) -> List[EventContext]:
        """
        Scan for active Bitcoin Up/Down events by generating slugs dynamically.
        
        Returns:
            List of discovered events
        """
        new_events = []
        current_ts = get_current_hour_timestamp()
        
        # Scan current hour + next 3 hours
        hours_to_scan = 24
        
        for i in range(hours_to_scan):
            # Skip if we have enough events
            if len(self._active_events) >= self.max_events:
                break
            
            ts = current_ts + (i * EVENT_DURATION)
            slug = generate_slug(ts)
            
            # Skip if already tracking
            if slug in self._active_events:
                continue
            
            # Try to fetch this event
            event = self._fetch_event_by_slug(slug, ts)
            if event:
                self._active_events[slug] = event
                new_events.append(event)
                logger.info(f"🔍 New event discovered: {slug}")
        
        return new_events
    
    def _fetch_event_by_slug(self, slug: str, timestamp: int) -> Optional[EventContext]:
        """Fetch event data from Gamma API using specific slug."""
        try:
            url = f"{GAMMA_API}/events"
            params = {"slug": slug}
            
            response = self._session.get(url, params=params, timeout=15)
            response.raise_for_status()
            events = response.json()
            
            if not events:
                logger.debug(f"⚠️  No event found: {slug}")
                return None
            
            event_data = events[0]
            return self._parse_event(event_data, slug, timestamp)
            
        except Exception as e:
            logger.error(f"❌ Fetch error for {slug}: {e}")
            return None
    
    def _parse_event(self, data: Dict, slug: str, timestamp: int) -> Optional[EventContext]:
        """Parse event data from Gamma API."""
        try:
            # Get markets (YES/NO tokens)
            markets = data.get("markets", [])
            if not markets:
                return None
            
            market = markets[0]
            condition_id = market.get("conditionId", "")
            
            # Extract token IDs
            clob_tokens = market.get("clobTokenIds", "")
            yes_token_id = None
            no_token_id = None
            
            if isinstance(clob_tokens, str):
                try:
                    tokens = json.loads(clob_tokens)
                    if len(tokens) >= 2:
                        yes_token_id = tokens[0]
                        no_token_id = tokens[1]
                except json.JSONDecodeError:
                    pass
            elif isinstance(clob_tokens, list) and len(clob_tokens) >= 2:
                yes_token_id = clob_tokens[0]
                no_token_id = clob_tokens[1]
            
            if not condition_id or not yes_token_id or not no_token_id:
                logger.warning(f"⚠️  Incomplete data for {slug}")
                return None
            
            # Determine phase
            if time.time() >= timestamp:
                phase = MarketPhase.LIVE
            else:
                phase = MarketPhase.PRE_MARKET
            
            logger.info(
                f"✅ Parsed {slug}: condition={condition_id[:20]}... "
                f"phase={phase.name}"
            )
            
            return EventContext(
                slug=slug,
                condition_id=condition_id,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                start_timestamp=timestamp,
                phase=phase
            )
            
        except Exception as e:
            logger.error(f"❌ Parse error: {e}")
            return None
    
    def get_active_events(self) -> List[EventContext]:
        """Get all tracked events."""
        return list(self._active_events.values())
    
    def remove_event(self, slug: str) -> None:
        """Remove an event from tracking."""
        if slug in self._active_events:
            del self._active_events[slug]
            logger.info(f"🗑️ Event removed: {slug}")
    
    def update_phases(self) -> List[EventContext]:
        """
        Update phases for all events.
        Returns events that transitioned to LIVE.
        """
        transitioned = []
        
        for event in self._active_events.values():
            old_phase = event.phase
            event.update_phase()
            
            if old_phase == MarketPhase.PRE_MARKET and event.phase == MarketPhase.LIVE:
                transitioned.append(event)
                logger.info(f"🔴 Event went LIVE: {event.slug}")
        
        return transitioned
