#!/usr/bin/env python3
"""Comprehensive balance checker for Polymarket wallet across multiple chains."""

import os
import sys
import requests

# Wallet address
ADDRESS = os.getenv("POLY_FUNDER", "0x94a73570cd0df2dA112fB55DA7bB914B34efa18D").lower()

print("="*70)
print("COMPREHENSIVE BALANCE CHECK")
print("="*70)
print(f"Wallet: {ADDRESS}")
print()

def check_polygon_rpc():
    """Check balance via Polygon RPC."""
    print("[1/5] Checking Polygon PoS via RPC...")
    
    # Token contracts on Polygon
    TOKENS = {
        'USDC (bridged)': '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
        'USDT': '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
        'USDC (native)': '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
        'WETH': '0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619',
        'WMATIC': '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270',
        'DAI': '0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063',
    }
    
    w3_url = 'https://rpc.ankr.com/polygon'
    
    # Check MATIC
    payload = {
        'jsonrpc': '2.0',
        'method': 'eth_getBalance',
        'params': [ADDRESS, 'latest'],
        'id': 1
    }
    
    try:
        resp = requests.post(w3_url, json=payload, timeout=10)
        data = resp.json()
        balance_hex = data.get('result', '0x0')
        balance = int(balance_hex, 16) / 1e18
        print(f"  MATIC: {balance:.6f}")
    except Exception as e:
        print(f"  MATIC: Error - {e}")
    
    # ERC20 balanceOf selector
    def get_balance(token_addr, decimals=6):
        data = '0x70a08231000000000000000000000000' + ADDRESS[2:]
        payload = {
            'jsonrpc': '2.0',
            'method': 'eth_call',
            'params': [{'to': token_addr, 'data': data}, 'latest'],
            'id': 1
        }
        try:
            resp = requests.post(w3_url, json=payload, timeout=10)
            result = resp.json().get('result', '0x0')
            return int(result, 16) / (10 ** decimals)
        except:
            return 0
    
    found = False
    for name, token in TOKENS.items():
        bal = get_balance(token, 6 if 'USDC' in name or 'USDT' in name else 18)
        if bal > 0:
            print(f"  {name}: ${bal:.4f}")
            found = True
    
    if not found:
        print("  No token balances found on Polygon")
    print()

def check_ethereum():
    """Check Ethereum mainnet."""
    print("[2/5] Checking Ethereum mainnet...")
    
    w3_url = 'https://rpc.ankr.com/eth'
    
    payload = {
        'jsonrpc': '2.0',
        'method': 'eth_getBalance',
        'params': [ADDRESS, 'latest'],
        'id': 1
    }
    
    try:
        resp = requests.post(w3_url, json=payload, timeout=10)
        data = resp.json()
        balance_hex = data.get('result', '0x0')
        balance = int(balance_hex, 16) / 1e18
        print(f"  ETH: {balance:.6f}")
    except Exception as e:
        print(f"  ETH: Error - {e}")
    
    # Check mainnet USDC
    USDC_ETH = '0xA0b86a33E6441E6d4c4F27e6d7C4E89A5C3e44eE'
    data = '0x70a08231000000000000000000000000' + ADDRESS[2:]
    payload = {
        'jsonrpc': '2.0',
        'method': 'eth_call',
        'params': [{'to': USDC_ETH, 'data': data}, 'latest'],
        'id': 1
    }
    try:
        resp = requests.post(w3_url, json=payload, timeout=10)
        result = resp.json().get('result', '0x0')
        bal = int(result, 16) / 1e6
        if bal > 0:
            print(f"  USDC (Ethereum): ${bal:.4f}")
    except:
        pass
    print()

def check_polygonscan():
    """Check Polygonscan API for transaction history."""
    print("[3/5] Checking Polygonscan transaction history...")
    
    try:
        # Get token transactions
        url = f'https://api.polygonscan.com/api?module=account&action=tokentx&address={ADDRESS}&sort=desc'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        
        if data.get('status') == '1' and data.get('result'):
            print("  Recent token transactions found:")
            seen = set()
            for tx in data['result'][:10]:
                token = tx.get('tokenSymbol', 'UNKNOWN')
                token_name = tx.get('tokenName', '')
                value = int(tx.get('value', 0)) / (10 ** int(tx.get('tokenDecimal', 6)))
                from_addr = tx.get('from', '').lower()
                to_addr = tx.get('to', '').lower()
                
                if token not in seen:
                    direction = "RECEIVED" if to_addr == ADDRESS else "SENT"
                    print(f"    {direction} {value:.4f} {token} ({token_name})")
                    seen.add(token)
        else:
            print("  No token transaction history found")
    except Exception as e:
        print(f"  Error: {e}")
    print()

def check_polymarket_api():
    """Check Polymarket CLOB API balance."""
    print("[4/5] Checking Polymarket CLOB API...")
    
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    if not private_key:
        print("  SKIPPED (no POLY_PRIVATE_KEY set)")
        return
    
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from py_clob_client.clob_types import BalanceAllowanceParams
        
        client = ClobClient(
            'https://clob.polymarket.com',
            chain_id=POLYGON,
            key=private_key,
            signature_type=0,
            funder=ADDRESS,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        
        response = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=0)
        )
        
        if isinstance(response, dict):
            bal = response.get('balance', '0')
            print(f"  Polymarket COLLATERAL: ${float(bal):.4f}")
            if response.get('allowances'):
                print(f"  Allowances: {len(response['allowances'])} contracts")
    except Exception as e:
        print(f"  Error: {e}")
    print()

def check_debank():
    """Check DeBank API for complete portfolio."""
    print("[5/5] Checking DeBank API (complete portfolio)...")
    
    try:
        url = f'https://api.debank.com/user/total_balance?id={ADDRESS}'
        resp = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        data = resp.json()
        
        if data.get('data') and data['data'].get('total_usd_value'):
            total = data['data']['total_usd_value']
            print(f"  Total portfolio value: ${total:.2f}")
            
            # Get chain breakdown
            url2 = f'https://api.debank.com/user/chain_balance?id={ADDRESS}'
            resp2 = requests.get(url2, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            data2 = resp2.json()
            
            if data2.get('data'):
                for chain in data2['data'][:5]:
                    name = chain.get('name', 'Unknown')
                    val = chain.get('usd_value', 0)
                    if val > 0.01:
                        print(f"    {name}: ${val:.2f}")
        else:
            print("  No portfolio data found")
    except Exception as e:
        print(f"  Error: {e}")
    print()

if __name__ == "__main__":
    check_polygon_rpc()
    check_ethereum()
    check_polygonscan()
    check_polymarket_api()
    check_debank()
    
    print("="*70)
    print("SUMMARY")
    print("="*70)
    print("If you expected to see funds but they're not showing:")
    print("1. Check you're using the correct wallet address")
    print("2. Funds might be on a different blockchain (Arbitrum, Base, etc.)")
    print("3. Funds might be in a different wallet/EOA")
    print("4. Check your wallet app for the correct address")
    print()
    print("For Polymarket trading, you need USDC on Polygon at this address:")
    print(f"  {ADDRESS}")
