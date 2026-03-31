#!/usr/bin/env python3
"""Multi-chain balance finder for Polymarket wallet."""

import os
import sys
import requests

# Addresses to check
FUNDER = os.getenv("POLY_FUNDER", "0x94a73570cd0df2dA112fB55DA7bB914B34efa18D").lower()
RELAYER = "0x272875292573b54e6e81913be664f22f00ab4741".lower()

ADDRESSES = {
    "Funder (your wallet)": FUNDER,
    "Relayer (API key owner)": RELAYER,
}

print("="*70)
print("MULTI-CHAIN BALANCE FINDER")
print("="*70)
print()

def check_chain(address, rpc_url, name, native_symbol="ETH"):
    """Check native balance on a chain."""
    payload = {
        'jsonrpc': '2.0',
        'method': 'eth_getBalance',
        'params': [address, 'latest'],
        'id': 1
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        data = resp.json()
        balance_hex = data.get('result', '0x0')
        balance = int(balance_hex, 16) / 1e18
        if balance > 0.001:
            return f"  {native_symbol}: {balance:.6f}"
    except:
        pass
    return None

# Chain RPCs
CHAINS = [
    ("Ethereum", "https://rpc.ankr.com/eth", "ETH"),
    ("Polygon", "https://rpc.ankr.com/polygon", "MATIC"),
    ("Arbitrum", "https://rpc.ankr.com/arbitrum", "ETH"),
    ("Base", "https://rpc.ankr.com/base", "ETH"),
    ("Optimism", "https://rpc.ankr.com/optimism", "ETH"),
    ("BSC", "https://rpc.ankr.com/bsc", "BNB"),
    ("Avalanche", "https://rpc.ankr.com/avalanche", "AVAX"),
]

print("Checking native balances across chains...")
print()

found_any = False
for addr_name, address in ADDRESSES.items():
    print(f"{addr_name}:")
    print(f"  {address}")
    
    found_on_chain = False
    for chain_name, rpc, symbol in CHAINS:
        result = check_chain(address, rpc, chain_name, symbol)
        if result:
            print(result)
            found_on_chain = True
            found_any = True
    
    if not found_on_chain:
        print("  No native balances found")
    print()

# Check DeBank for complete picture
print("Checking DeBank API (all chains + tokens)...")
for addr_name, address in ADDRESSES.items():
    print(f"\n{addr_name}:")
    try:
        url = f'https://api.debank.com/user/total_balance?id={address}'
        resp = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        data = resp.json()
        
        if data.get('data') and data['data'].get('total_usd_value'):
            total = data['data']['total_usd_value']
            print(f"  Total: ${total:.2f}")
            found_any = True
            
            # Chain breakdown
            url2 = f'https://api.debank.com/user/chain_balance?id={address}'
            resp2 = requests.get(url2, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            data2 = resp2.json()
            
            if data2.get('data'):
                for chain in data2['data'][:5]:
                    name = chain.get('name', 'Unknown')
                    val = chain.get('usd_value', 0)
                    if val > 0.01:
                        print(f"    {name}: ${val:.2f}")
        else:
            print("  No portfolio data")
    except Exception as e:
        print(f"  Error: {e}")

print()
print("="*70)
if not found_any:
    print("NO FUNDS FOUND")
    print("="*70)
    print()
    print("Your wallet appears to be empty across all major chains.")
    print()
    print("If you believe you have funds:")
    print("1. Check your wallet app's 'Receive' or 'Account' section")
    print("2. Copy the EXACT address shown there")
    print("3. Compare with the addresses checked above")
    print()
    print("Common issues:")
    print("- Using a different wallet (MetaMask vs Rainbow vs Coinbase)")
    print("- Funds on a less common L2 (zkSync, Scroll, etc.)")
    print("- Funds in a smart contract wallet (Safe/Argent)")
    print("- Connected to a different account in your wallet")
else:
    print("FUNDS FOUND!")
    print("="*70)
    print()
    print("Polymarket requires USDC on Polygon.")
    print("If your funds are on another chain, bridge them to Polygon first.")
