"""
Setup Token Allowances - ONE-TIME script to enable trading.
Run this ONCE before starting the bot to approve Polymarket contracts.

This approves:
1. USDC tokens for deposit and trading
2. Conditional Tokens for outcome trading

After running this successfully, the bot can operate 24/7 unattended.
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

# Maximum approval amount
MAX_UINT256 = 2**256 - 1

# ERC20 ABI (only approve function needed)
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]

# ERC1155 ABI (for Conditional Tokens - setApprovalForAll)
ERC1155_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"}
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }
]


def setup_allowances():
    """Set up all necessary allowances for Polymarket trading."""
    
    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        print("❌ PRIVATE_KEY not found in .env")
        return False
    
    # Ensure private key has 0x prefix
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    # Connect to Polygon - try multiple RPCs
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
    
    if not w3 or not w3.is_connected():
        print("❌ Failed to connect to any Polygon RPC")
        return False
    
    # Get account from private key
    account = Account.from_key(private_key)
    address = account.address
    print(f"📍 Wallet address: {address}")
    
    # Check MATIC balance for gas
    balance = w3.eth.get_balance(address)
    matic_balance = w3.from_wei(balance, 'ether')
    print(f"💰 MATIC balance: {matic_balance:.4f}")
    
    if balance < w3.to_wei(0.01, 'ether'):
        print("⚠️  Warning: Low MATIC balance. You may need more for gas.")
    
    # USDC contract
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    
    # Conditional Tokens contract
    conditional_tokens = w3.eth.contract(
        address=Web3.to_checksum_address(CONDITIONAL_TOKENS_ADDRESS), 
        abi=ERC1155_ABI
    )
    
    success_count = 0
    total_approvals = len(EXCHANGE_CONTRACTS) * 2  # USDC + CT for each
    
    for exchange in EXCHANGE_CONTRACTS:
        exchange_addr = Web3.to_checksum_address(exchange)
        print(f"\n🔧 Setting allowances for {exchange[:10]}...")
        
        # 1. Approve USDC
        try:
            current_allowance = usdc.functions.allowance(address, exchange_addr).call()
            if current_allowance >= MAX_UINT256 // 2:
                print(f"  ✅ USDC already approved")
                success_count += 1
            else:
                print(f"  📝 Approving USDC...")
                nonce = w3.eth.get_transaction_count(address)
                
                # Dynamic gas price
                gas_price = w3.eth.gas_price
                if gas_price < w3.to_wei(100, 'gwei'):
                    gas_price = w3.to_wei(150, 'gwei') # Minimum safe for Polygon
                else:
                    gas_price = int(gas_price * 1.5) # 50% buffer

                tx = usdc.functions.approve(exchange_addr, MAX_UINT256).build_transaction({
                    'from': address,
                    'nonce': nonce,
                    'gas': 100000,
                    'gasPrice': gas_price,
                    'chainId': 137
                })
                signed = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  ⏳ Sending USDC approval ({tx_hash.hex()[:10]}...)...")
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt['status'] == 1:
                    print(f"  ✅ USDC approved!")
                    success_count += 1
                else:
                    print(f"  ❌ USDC approval failed")
        except Exception as e:
            if "already known" in str(e) or "underpriced" in str(e):
                print(f"  ⚠️  Transaction pending or gas too low. Retrying with higher gas...")
            else:
                print(f"  ❌ USDC approval error: {e}")
        
        # Delay to avoid rate limiting
        time.sleep(20)
        
        # 2. Approve Conditional Tokens
        try:
            is_approved = conditional_tokens.functions.isApprovedForAll(address, exchange_addr).call()
            if is_approved:
                print(f"  ✅ Conditional Tokens already approved")
                success_count += 1
            else:
                print(f"  📝 Approving Conditional Tokens...")
                nonce = w3.eth.get_transaction_count(address)
                
                # Dynamic gas price
                gas_price = w3.eth.gas_price
                if gas_price < w3.to_wei(100, 'gwei'):
                    gas_price = w3.to_wei(150, 'gwei')
                else:
                    gas_price = int(gas_price * 1.5)

                tx = conditional_tokens.functions.setApprovalForAll(exchange_addr, True).build_transaction({
                    'from': address,
                    'nonce': nonce,
                    'gas': 100000,
                    'gasPrice': gas_price,
                    'chainId': 137
                })
                signed = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  ⏳ Sending Conditional Tokens approval ({tx_hash.hex()[:10]}...)...")
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt['status'] == 1:
                    print(f"  ✅ Conditional Tokens approved!")
                    success_count += 1
                else:
                    print(f"  ❌ Conditional Tokens approval failed")
        except Exception as e:
            print(f"  ❌ Conditional Tokens approval error: {e}")
        
        # Delay between exchanges
        if exchange != EXCHANGE_CONTRACTS[-1]:
            print("  ⏳ Waiting 20s before next exchange...")
            time.sleep(20)
    
    print(f"\n{'='*50}")
    print(f"✅ Completed: {success_count}/{total_approvals} approvals")
    
    if success_count == total_approvals:
        print("\n🎉 All allowances set! Your bot can now trade 24/7.")
        return True
    else:
        print("\n⚠️  Some approvals failed. Check errors above.")
        return False


if __name__ == "__main__":
    print("="*50)
    print("🔧 POLYMARKET ALLOWANCE SETUP")
    print("="*50)
    print("This script will approve Polymarket contracts to use your tokens.")
    print("This is a ONE-TIME setup required for automated trading.\n")
    
    confirm = input("Continue? (y/n): ").strip().lower()
    if confirm == 'y':
        setup_allowances()
    else:
        print("Cancelled.")
