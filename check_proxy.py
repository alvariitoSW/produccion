"""
Diagnostic Script for Proxy Allowances
This checks if the Proxy Wallet (FUNDER_ADDRESS) has the necessary approvals.
"""

import os
import time
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv()

# Polygon RPC endpoints (try multiple)
POLYGON_RPCS = [
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
    "https://polygon-mainnet.public.blastapi.io",
]

# Contract addresses (Polygon Mainnet)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Exchange contracts to approve
EXCHANGE_CONTRACTS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # Main exchange (CTF)
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # Neg risk exchange
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # Neg risk adapter
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
]

ERC1155_ABI = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}], "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"}
]

def check_proxy_allowances():
    proxy_address = os.getenv("FUNDER_ADDRESS", "")
    if not proxy_address:
        print("❌ FUNDER_ADDRESS not found in .env")
        return

    print(f"📍 Checking PROXY Address: {proxy_address}")
    
    # Connect
    w3 = None
    for rpc in POLYGON_RPCS:
        print(f"🔌 Trying {rpc}...")
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
            if w3.is_connected():
                print(f"✅ Connected to Polygon (Chain ID: {w3.eth.chain_id})")
                break
        except Exception as e:
            print(f"  ❌ Failed: {e}")
            continue
            
    if not w3: return

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    conditional_tokens = w3.eth.contract(address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS), abi=ERC1155_ABI)
    
    proxy_checksum = Web3.to_checksum_address(proxy_address)
    
    # Check USDC Balance
    bal = usdc.functions.balanceOf(proxy_checksum).call()
    print(f"\n💰 Proxy USDC Balance: {bal / 1e6}")

    print("\n🔍 ALLOWANCE CHECK:")
    for exchange in EXCHANGE_CONTRACTS:
        addr = Web3.to_checksum_address(exchange)
        print(f"\n🏭 Exchange: {exchange[:10]}...")
        
        # USDC
        allowance = usdc.functions.allowance(proxy_checksum, addr).call()
        print(f"   USDC Allowance: {'✅ OK' if allowance > 0 else '❌ MSSING'} ({allowance})")
        
        # Tokens
        approved = conditional_tokens.functions.isApprovedForAll(proxy_checksum, addr).call()
        print(f"   Conditional Tokens: {'✅ OK' if approved else '❌ MISSING (Review Required)'}")

if __name__ == "__main__":
    check_proxy_allowances()
