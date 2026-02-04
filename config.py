"""
Configuration for Production Bot.
KISS principle: All constants in one place.
"""

import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ===========================================
# API CONFIGURATION
# ===========================================
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet

# ===========================================
# CREDENTIALS (from .env)
# ===========================================
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")  # Wallet address for MetaMask/EOA
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET = os.getenv("CLOB_API_SECRET", "")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE", "")

# ===========================================
# TELEGRAM
# ===========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ===========================================
# STRATEGY PARAMETERS
# ===========================================
# Ladder levels (buy prices in dollars, e.g., 0.40 = 40 cents)
LADDER_LEVELS = [0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48]

# Dynamic exit prices based on entry level (risk management)
# Entry 48¢ → Exit 49¢
# Entry 46-47¢ → Exit 48¢
# Entry 40-45¢ → Exit 47¢
EXIT_PRICES = {
    0.48: 0.49,  # 48 → 49 (1¢ profit)
    0.47: 0.48,  # 47 → 48 (1¢ profit)
    0.46: 0.48,  # 46 → 48 (2¢ profit)
    0.45: 0.47,  # 45 → 47 (2¢ profit)
    0.44: 0.47,  # 44 → 47 (3¢ profit)
    0.43: 0.47,  # 43 → 47 (4¢ profit)
    0.42: 0.47,  # 42 → 47 (5¢ profit)
    0.41: 0.47,  # 41 → 47 (6¢ profit)
    0.39: 0.45,  # 39 → 45 (6¢ profit)
    0.38: 0.45,  # 38 → 45 (7¢ profit)
    0.37: 0.45,  # 37 → 45 (8¢ profit)
}

# Stop-loss configuration (only for high-risk entries)
# Only 48¢ entries have a stop-loss due to tight margin
STOP_LOSS_PRICE = 0.18  # 18¢ stop-loss
STOP_LOSS_ENTRIES = [0.48]  # Only apply to these entry levels

# Size per order (in shares)
ORDER_SIZE = 30.0

# ===========================================
# POLYMARKET ORDER LIMITS
# ===========================================
# CRITICAL: CLOB API enforces minimum notional value per order
# Formula: Precio × Cantidad ≥ 1 USDC
# Error if violated: INVALID_ORDER_MIN_SIZE
#
# Examples:
#   - At 0.40¢: Need 2.5 shares minimum (0.40 × 2.5 = 1.0 USDC)
#   - At 0.47¢: Need 2.13 shares minimum (0.47 × 2.13 ≈ 1.0 USDC)
#   - At 0.20¢: Need 5.0 shares minimum (0.20 × 5.0 = 1.0 USDC)
#
# DUST PROBLEM: Partial fills <minimum = LOCKED until expiration
# Strategy dynamically calculates minimum per price level

MIN_NOTIONAL_VALUE_USDC = 1.0  # Hard limit from Polymarket CLOB API

# ===========================================
# TIMING (Optimized for Polymarket CLOB API limits)
# ===========================================
# API Limits Reference:
#   - GET endpoints: ~900 req/10s (90/s)
#   - POST /order: 3,500 req/10s (350/s burst)
#   - Practical recommendation: 5-10 orders/s
#
# Our usage per cycle (~2 events, 18 orders each):
#   - ~1 get_open_orders + ~4 get_order_book + ~36 get_order = ~41 req/cycle
#   - At 0.5s poll: 82 req/s (safe, under 90/s limit)

POLL_INTERVAL_SECONDS = 0.5   # Aggressive polling (82 req/s < 90/s limit)
SCANNER_INTERVAL_SECONDS = 60  # How often to scan for new events
HEARTBEAT_INTERVAL = 30  # Heartbeat log interval
PRE_MARKET_HOURS = 48  # How many hours ahead to scan for events

# ===========================================
# SELL ORDER RELIABILITY (Speed-optimized)
# ===========================================
# POST /order limit: 350/s burst, but practical limit is 5-10/s
# We retry fast to catch settlement delays
SELL_RETRY_ATTEMPTS = 3      # Retries for SELL orders (critical)
SELL_RETRY_DELAY = 0.1       # Fast retry (100ms) - API can handle it

# ===========================================
# RISK LIMITS
# ===========================================
MAX_CONCURRENT_EVENTS = 2
MAX_ALLOCATION_PER_EVENT = 1000.0  # Max USD exposure per event

# ===========================================
# LOGGING
# ===========================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
