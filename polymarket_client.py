"""
Polymarket Client - Authenticated connection to CLOB API.
Handles all order placement and status checking.
"""

import logging
from typing import Optional, List, Dict, Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL

import time as time_module

from config import (
    CLOB_HOST, CHAIN_ID,
    PRIVATE_KEY, FUNDER_ADDRESS,
    SELL_RETRY_ATTEMPTS, SELL_RETRY_DELAY
)
from models import OrderSide, OrderType, TrackedOrder

logger = logging.getLogger(__name__)


class PolymarketClient:
    """
    Wrapper around py-clob-client for clean order management.
    KISS: One method per action.
    """
    
    def __init__(self):
        self._client: Optional[ClobClient] = None
        self._connected = False
        self._signature_type = 2  # 2 for Polymarket proxy wallets (browser login)
    
    def connect(self) -> bool:
        """
        Initialize authenticated connection to Polymarket.
        
        Following official documentation pattern:
        1. Create client with signature_type and funder
        2. Call set_api_creds(client.create_or_derive_api_creds())
        
        Returns:
            True if connected successfully
        """
        if not PRIVATE_KEY:
            logger.error("❌ Missing PRIVATE_KEY. Check .env file.")
            return False
        
        if not FUNDER_ADDRESS:
            logger.error("❌ Missing FUNDER_ADDRESS. Check .env file.")
            return False
        
        try:
            # Create client with signature_type and funder
            logger.info("🔐 Creating authenticated client...")
            self._client = ClobClient(
                host=CLOB_HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=self._signature_type,  # 2 for proxy wallets
                funder=FUNDER_ADDRESS
            )
            
            # Set API credentials using derive
            logger.info("🔑 Setting API credentials...")
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            logger.info("✅ API credentials configured")
            
            # Test connection by fetching balance
            logger.info("💰 Testing connection (fetching balance)...")
            balance_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self._client.get_balance_allowance(params=balance_params)
            
            # Balance is in micro-units (1e6)
            balance_raw = int(result.get("balance", 0))
            balance_usdc = balance_raw / 1_000_000
            logger.info(f"💰 Balance: ${balance_usdc:.2f} USDC")
            
            self._connected = True
            logger.info("✅ Connected to Polymarket CLOB")
            return True
            
        except Exception as e:
            logger.error(f"❌ Connection failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None
    
    def place_limit_order(
        self,
        token_id: str,
        side: OrderSide,
        order_type: OrderType,
        price: float,
        size: float,
        event_slug: str
    ) -> Optional[TrackedOrder]:
        """
        Place a limit order on Polymarket.
        SELL orders have automatic retry (critical for arbitrage).
        
        Args:
            token_id: YES or NO token ID
            side: YES or NO
            order_type: BUY or SELL
            price: Price in dollars (e.g., 0.48)
            size: Number of shares
            event_slug: For tracking
            
        Returns:
            TrackedOrder if successful, None otherwise
        """
        if not self.is_connected:
            logger.error("❌ Not connected")
            return None
        
        # SELL orders are critical - retry on failure
        max_attempts = SELL_RETRY_ATTEMPTS if order_type == OrderType.SELL else 1
        
        for attempt in range(max_attempts):
            try:
                # Map to py-clob-client types
                clob_side = BUY if order_type == OrderType.BUY else SELL
                
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side=clob_side,
                    token_id=token_id
                )
                
                # Create and post the order
                signed_order = self._client.create_order(order_args)
                response = self._client.post_order(signed_order, ClobOrderType.GTC)
                
                order_id = response.get("orderID", "")
                
                if not order_id:
                    error_msg = response.get("error", response.get("message", str(response)))
                    logger.error(f"❌ Order failed (attempt {attempt+1}/{max_attempts}): {error_msg}")
                    
                    # Retry SELL orders after brief delay
                    if order_type == OrderType.SELL and attempt < max_attempts - 1:
                        time_module.sleep(SELL_RETRY_DELAY * (attempt + 1))
                        continue
                    return None
                
                tracked = TrackedOrder(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    order_type=order_type,
                    price=price,
                    size=size,
                    event_slug=event_slug
                )
                
                logger.info(
                    f"📝 Order placed: {order_type.value} {side.display_name} "
                    f"@ {int(price*100)}¢ x{size} | ID: {order_id[:8]}..."
                )
                
                return tracked
                
            except Exception as e:
                logger.error(f"❌ Order error (attempt {attempt+1}/{max_attempts}): {e}")
                
                # Retry SELL orders after brief delay
                if order_type == OrderType.SELL and attempt < max_attempts - 1:
                    time_module.sleep(SELL_RETRY_DELAY * (attempt + 1))
                    continue
                return None
        
        return None
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if cancelled
        """
        if not self.is_connected:
            return False
        
        try:
            self._client.cancel(order_id)
            logger.info(f"❌ Order cancelled: {order_id[:8]}...")
            return True
        except Exception as e:
            logger.error(f"❌ Cancel failed: {e}")
            return False
    
    def cancel_all_orders(self) -> int:
        """
        Cancel all open orders.
        
        Returns:
            Number of orders cancelled
        """
        if not self.is_connected:
            return 0
        
        try:
            response = self._client.cancel_all()
            cancelled = response.get("canceled", [])
            logger.info(f"❌ Cancelled {len(cancelled)} orders")
            return len(cancelled)
        except Exception as e:
            logger.error(f"❌ Cancel all failed: {e}")
            return 0
    
    def cancel_orders_batch(self, order_ids: List[str]) -> int:
        """
        Cancel multiple orders at once (faster than individual cancels).
        
        Args:
            order_ids: List of order IDs to cancel
            
        Returns:
            Number of orders cancelled
        """
        if not self.is_connected or not order_ids:
            return 0
        
        try:
            # Use cancel_orders batch endpoint
            response = self._client.cancel_orders(order_ids)
            cancelled = response.get("canceled", [])
            logger.info(f"❌ Batch cancelled {len(cancelled)}/{len(order_ids)} orders")
            return len(cancelled)
        except Exception as e:
            logger.error(f"❌ Batch cancel failed: {e}")
            # Fallback to individual cancels
            cancelled = 0
            for order_id in order_ids:
                if self.cancel_order(order_id):
                    cancelled += 1
            return cancelled
    
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all open orders.
        
        Returns:
            List of order dictionaries
        """
        if not self.is_connected:
            return []
        
        try:
            orders = self._client.get_orders()
            return orders if orders else []
        except Exception as e:
            logger.error(f"❌ Get orders failed: {e}")
            return []

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """
        Get a single order by ID.
        """
        if not self.is_connected:
            return {}
        
        try:
            return self._client.get_order(order_id)
        except Exception as e:
            # Log as debug - this is a transient API error that gets retried automatically
            logger.debug(f"⏳ Get order {order_id[:8]}... failed (will retry): {e}")
            return {}
    
    def get_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get recent trades (fills).
        
        Returns:
            List of trade dictionaries
        """
        if not self.is_connected:
            return []
        
        try:
            trades = self._client.get_trades()
            return trades[:limit] if trades else []
        except Exception as e:
            logger.error(f"❌ Get trades failed: {e}")
            return []

    def get_balance(self) -> float:
        """
        Get current USDC collateral balance.
        """
        if not self.is_connected:
            return 0.0
            
        try:
            balance_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self._client.get_balance_allowance(params=balance_params)
            balance_raw = int(result.get("balance", 0))
            return balance_raw / 1_000_000
        except Exception as e:
            logger.error(f"❌ Get balance failed: {e}")
            return 0.0

    def get_token_balance(self, token_id: str) -> float:
        """
        Get balance for a specific outcome token (YES/NO shares).
        Returns raw float (e.g. 5.0).
        """
        if not self.is_connected:
            return 0.0
            
        try:
            # AssetType.CONDITIONAL is for specific outcome tokens
            balance_params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id
            )
            result = self._client.get_balance_allowance(params=balance_params)
            
            # Balance is in micro-units (1e6)
            balance_raw = int(result.get("balance", 0))
            return balance_raw / 1_000_000
        except Exception as e:
            logger.error(f"❌ Get token balance failed: {e}")
            return 0.0
    
    def get_order_book(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order book for a token.
        
        Returns:
            Order book dictionary with 'bids' and 'asks'
        """
        if not self.is_connected:
            return None
        
        try:
            return self._client.get_order_book(token_id)
        except Exception as e:
            logger.error(f"❌ Get order book failed: {e}")
            return None


# Singleton instance
_client: Optional[PolymarketClient] = None


def get_client() -> PolymarketClient:
    """Get the singleton PolymarketClient instance."""
    global _client
    if _client is None:
        _client = PolymarketClient()
    return _client
