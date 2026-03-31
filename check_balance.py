#!/usr/bin/env python3
"""Quick balance check for Polymarket wallet."""

import os
import sys

# Get credentials from env
FUNDER = os.getenv("POLY_FUNDER", "0x94a73570cd0df2dA112fB55DA7bB914B34efa18D")
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
RELAYER_KEY = os.getenv("RELAYER_API_KEY", "")

if not PRIVATE_KEY:
    print("ERROR: Set POLY_PRIVATE_KEY environment variable")
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.clob_types import BalanceAllowanceParams, ApiCreds
except ImportError as e:
    print(f"ERROR: {e}")
    print("Run: pip install py-clob-client")
    sys.exit(1)

HOST = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

print("="*60)
print("Polymarket Balance Check")
print("="*60)
print(f"Funder: {FUNDER}")
print(f"Relayer API: {'Yes' if RELAYER_KEY else 'No'}")
print()

try:
    client = ClobClient(
        HOST,
        chain_id=CHAIN_ID,
        key=PRIVATE_KEY,
        signature_type=0,
        funder=FUNDER,
    )
    
    # Use relayer key if available
    if RELAYER_KEY:
        creds = ApiCreds(api_key=RELAYER_KEY, api_secret="", api_passphrase="")
        print("Using relayer API key...")
    else:
        creds = client.create_or_derive_api_creds()
        print("Using auto-generated API credentials...")
    
    client.set_api_creds(creds)
    
    # Check balance
    response = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=0)
    )
    
    print("\n✅ SUCCESS!")
    print(f"Raw response: {response}")
    
    # Try to extract balance
    if hasattr(response, 'balance'):
        print(f"\nBalance: ${float(response.balance):.4f}")
    elif isinstance(response, dict):
        bal = response.get('balance') or response.get('available') or response.get('total')
        if bal:
            print(f"\nBalance: ${float(bal):.4f}")
        else:
            print("\nNo balance found in response")
    
except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
