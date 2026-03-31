# Polymarket BTC 5-Minute Scalper - Local Setup

## Quick Start

1. **Install Python 3.10+**

2. **Create virtual environment:**
```bash
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
.venv\Scripts\activate     # Windows
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

4. **Set environment variables:**
```bash
export POLY_PRIVATE_KEY="0xd74ef8655b4f8e66ed7e34e4900e7aa3b8b304fcf1bc5e2599e27e893ac8c871"
export POLY_FUNDER="0x94a73570cd0df2da112fb55da7bb914b34efa18d"
export POLY_DRY_RUN=false  # Set to true for testing without real trades
```

**Important for Polymarket web wallet users:**
If your wallet was created on Polymarket.com (Magic Link / email login), set:
```bash
export POLY_SIGNATURE_TYPE=1  # Default is 1 for Safe/Proxy wallets
```
For regular MetaMask/external wallets, use:
```bash
export POLY_SIGNATURE_TYPE=0  # For EOA wallets
```

5. **Run the bot:**
```bash
python polymarket_btc_scalper.py
```

## Using Relayer API Key (For Pre-registered Wallets)

If you have a relayer API key from Polymarket, set these additional variables:

```bash
export RELAYER_API_KEY="019d0b3d-4dc9-7988-b94a-1117c08e0631"
export RELAYER_SECRET=""  # Optional
export RELAYER_PASSPHRASE=""  # Optional
```

## Check Balance First

Test your credentials before running the bot:

```bash
python check_balance.py
```

This will show your wallet balance and verify API credentials are working.

### Can't Find Your Funds?

If `check_balance.py` shows $0 but you expected funds, use the multi-chain finder:

```bash
python find_funds.py
```

This checks:
- Ethereum, Polygon, Arbitrum, Base, Optimism, BSC, Avalanche
- Native tokens (ETH, MATIC, BNB, AVAX)
- DeBank API (all chains + tokens)

**Common issues:**
- Wrong wallet address in `POLY_FUNDER`
- Funds on a different blockchain
- Funds in a different wallet app

**To fix:** Copy the exact address from your wallet app's "Receive" screen and update `POLY_FUNDER`.

## Features

- **Dual logging:** Terminal + file logs
- **Per-window log files:** New log file created for each 5-minute window
- **Logs directory:** All logs saved in `logs/` folder
- **File naming:** `YYYYMMDD_HHMMSS_{window_slug}.log`

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `POLY_PRIVATE_KEY` | required | Your wallet private key |
| `POLY_FUNDER` | required | Funder wallet address |
| `RELAYER_API_KEY` | "" | Relayer API key (if pre-registered) |
| `RELAYER_SECRET` | "" | Relayer API secret |
| `RELAYER_PASSPHRASE` | "" | Relayer API passphrase |
| `POLY_DRY_RUN` | true | If true, simulates trades without real orders |
| `BOT_PROFIT_TARGET_PCT` | 10 | Exit when profit reaches this % |
| `BOT_MAX_HOLD_SECONDS` | 150 | Force close after this many seconds |
| `BOT_MIN_ENTRY_PRICE` | 0.35 | Minimum contract price to enter |
| `BOT_MAX_ENTRY_PRICE` | 0.65 | Maximum contract price to enter |
| `BOT_TRADE_BALANCE_FRACTION` | 0.10 | Position size as fraction of balance |
| `BOT_MIN_TRADE_USD` | 1 | Minimum trade size |
| `BOT_LOG_LEVEL` | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Log Output Example

Terminal:
```
2026-03-22 18:47:16 INFO polymarket_btc_scalper - New window detected: btc-updown-5m-1774176600
2026-03-22 18:47:16 INFO polymarket_btc_scalper - Logging to file: /path/to/logs/20260322_184716_btc-updown-5m-1774176600.log
2026-03-22 18:47:16 INFO polymarket_btc_scalper - 🚨 SIGNAL TRIGGERED #1: UP | Time: 18:47:16 | Window ends in: 464s | Contract price: $0.5200 | Rapid move in 3s | Shares: 2 | Notional: $1.04
```

Log file (logs/20260322_184716_btc-updown-5m-1774176600.log):
```
2026-03-22 18:47:16,123 INFO polymarket_btc_scalper - New window detected: btc-updown-5m-1774176600
2026-03-22 18:47:16,124 INFO polymarket_btc_scalper - Logging to file: /path/to/logs/20260322_184716_btc-updown-5m-1774176600.log
...
```

## Strategy

- **Signal detection:** Checks last 3 one-second candles
- **Trigger:** Any 1s candle with $2+ move → UP/DOWN signal
- **Entry:** Market buy when signal triggered and price in range [0.35, 0.65]
- **Exit:** 10% profit target OR 150s hold time OR window ending
- **Position sizing:** 10% of wallet balance per trade

## Requirements

- Python 3.10+
- USDC on Polygon network in your wallet
- Access to Polymarket (not geoblocked in your region)

## Stopping the Bot

Press `Ctrl+C` to gracefully shutdown.
