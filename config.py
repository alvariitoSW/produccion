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
    0.40: 0.47,  # 40 → 47 (7¢ profit)
}

# Stop-loss configuration (only for high-risk entries)
# Only 48¢ entries have a stop-loss due to tight margin
STOP_LOSS_PRICE = 0.18  # 18¢ stop-loss
STOP_LOSS_ENTRIES = [0.48]  # Only apply to these entry levels

# Size per order (in shares)
ORDER_SIZE = 25.0

# ===========================================
# TIMING
# ===========================================
POLL_INTERVAL_SECONDS = 2  # How often to check order status (fast for quick sells)
SCANNER_INTERVAL_SECONDS = 60  # How often to scan for new events
HEARTBEAT_INTERVAL = 30  # Heartbeat log interval
PRE_MARKET_HOURS = 48  # How many hours ahead to scan for events

# ===========================================
# RISK LIMITS
# ===========================================
MAX_CONCURRENT_EVENTS = 2
MAX_ALLOCATION_PER_EVENT = 1000.0  # Max USD exposure per event

# ===========================================
# LOGGING
# ===========================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
