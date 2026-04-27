#!/usr/bin/env python3
"""
Simulate SHAMAN v1 on recent Binance klines: fixed entry price 0.5 (binary payoff).

Assumption: each clip risks ``stake_usdc`` (same sizing as live: $1 per winning-side signal, capped).
If next Binance candle matches the rule majority (G vs R): profit = +stake (double gross: $1/sh on $0.5/sh).
If wrong: loss = -stake (full stake lost).

Fetches 600 x 5m and 200 x 15m candles from Binance public API unless --csv paths are passed.

With ``--last-week``, only the trailing **7d** of each timeframe is simulated (2016 x 5m bars,
672 x 15m bars), using the **same** aggregate-signal + next-candle payoff model. ROI is
``total_pnl / sum(stakes)`` for that window (stakes scale with concurrent winning-side rules, $1 each by default).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parents[1]
RULES_PATH = REPO / "PALADIN" / "shaman_v1_rules.json"
OUT_DIR = REPO / "exports" / "btc_binance_klines"
DEFAULT_CSV_5M = OUT_DIR / "btcusdt_5m_last10000_binance.csv"
DEFAULT_CSV_15M = OUT_DIR / "btcusdt_15m_last6000_binance.csv"
KLINES_URL = "https://api.binance.com/api/v3/klines"
ENTRY_PX = 0.5
WEEK_5M_BARS = 7 * 24 * 12  # 2016
WEEK_15M_BARS = 7 * 24 * 4  # 672


def _load_eval():
    path = REPO / "PALADIN" / "shaman_v1_eval.py"
    spec = importlib.util.spec_from_file_location("shaman_v1_eval", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_eval = _load_eval()
_aggregate_signals = _eval.aggregate_signals


def _rg(i: int, o: list[float], c: list[float]) -> str | None:
    if c[i] > o[i]:
        return "G"
    if c[i] < o[i]:
        return "R"
    return None


def _fetch_klines(symbol: str, interval: str, limit: int) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """Binance max 1000 per request; paginate when ``limit`` > 1000."""
    all_rows: list[Any] = []
    end_time: int | None = None
    while len(all_rows) < limit:
        need = min(1000, limit - len(all_rows))
        params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": need}
        if end_time is not None:
            params["endTime"] = end_time
        r = requests.get(KLINES_URL, params=params, timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_rows = batch + all_rows
        end_time = int(batch[0][0]) - 1
        if len(batch) < need:
            break
    all_rows = all_rows[-limit:]
    o, hi, lo, c, v = [], [], [], [], []
    for row in all_rows:
        o.append(float(row[1]))
        hi.append(float(row[2]))
        lo.append(float(row[3]))
        c.append(float(row[4]))
        v.append(float(row[5]))
    return o, hi, lo, c, v


def _load_rules(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return [x for x in data if isinstance(x, dict)]


USDC_PER_SIGNAL = 1.0
NOTIONAL_MAX_USDC = 500.0


def _notional(winning: int) -> float:
    n = max(1, winning)
    return min(NOTIONAL_MAX_USDC, float(n) * USDC_PER_SIGNAL)


def simulate(
    *,
    label: str,
    rules: list[dict[str, Any]],
    o: list[float],
    hi: list[float],
    lo: list[float],
    c: list[float],
    v: list[float],
    min_bars: int,
    week_bars: int | None = None,
) -> dict[str, Any]:
    n = len(o)
    t0 = min_bars
    if week_bars is not None and n > week_bars + min_bars:
        t0 = max(min_bars, n - week_bars)
    pnl = 0.0
    total_staked = 0.0
    trades = wins = losses = skips_doji = 0
    for t in range(t0, n - 2):
        ng, nr = _aggregate_signals(rules, o, c, v, hi, lo, t)
        if ng == nr:
            continue
        pred = "G" if ng > nr else "R"
        stake = _notional(max(ng, nr))
        act = _rg(t + 1, o, c)
        if act is None:
            skips_doji += 1
            continue
        trades += 1
        total_staked += stake
        if pred == act:
            wins += 1
            pnl += stake
        else:
            losses += 1
            pnl -= stake

    wr = wins / trades if trades else 0.0
    roi = (pnl / total_staked * 100.0) if total_staked > 1e-12 else 0.0
    return {
        "label": label,
        "bars": n,
        "t0": t0,
        "week_bars": week_bars,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "skips_doji": skips_doji,
        "win_rate": wr,
        "total_pnl_usdc": pnl,
        "total_staked_usdc": total_staked,
        "avg_stake_usdc": (total_staked / trades) if trades else 0.0,
        "avg_pnl_per_trade_usdc": (pnl / trades) if trades else 0.0,
        "roi_on_staked_pct": roi,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--rules", type=Path, default=RULES_PATH)
    ap.add_argument("--five-m-limit", type=int, default=600, dest="m5")
    ap.add_argument("--fifteen-m-limit", type=int, default=200, dest="m15")
    ap.add_argument("--csv-5m", type=Path, default=None, help="Optional OHLCV CSV instead of fetch")
    ap.add_argument("--csv-15m", type=Path, default=None)
    ap.add_argument(
        "--last-week",
        action="store_true",
        help="Simulate only trailing 7d per TF; use default CSVs if present else fetch 10k/3k.",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write markdown report (default: exports/.../SHAMAN_SIM_1WEEK_REPORT.md when --last-week).",
    )
    args = ap.parse_args()

    all_rules = _load_rules(Path(args.rules))
    rules_5m = [r for r in all_rules if str(r.get("timeframe", "")).strip() == "5m"]
    rules_15m = [r for r in all_rules if str(r.get("timeframe", "")).strip() == "15m"]

    m5, m15 = args.m5, args.m15
    csv5, csv15 = args.csv_5m, args.csv_15m
    week_5: int | None = None
    week_15: int | None = None
    if args.last_week:
        week_5, week_15 = WEEK_5M_BARS, WEEK_15M_BARS
        m5, m15 = 10000, 3000
        if csv5 is None and DEFAULT_CSV_5M.is_file():
            csv5 = DEFAULT_CSV_5M
        if csv15 is None and DEFAULT_CSV_15M.is_file():
            csv15 = DEFAULT_CSV_15M

    def load_series(interval: str, limit: int, csv_path: Path | None):
        if csv_path is not None:
            import csv

            rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
            o = [float(r["open"]) for r in rows]
            c = [float(r["close"]) for r in rows]
            v = [float(r["volume"]) for r in rows]
            hi = [float(r["high"]) for r in rows]
            lo = [float(r["low"]) for r in rows]
            return o, hi, lo, c, v
        return _fetch_klines(args.symbol, interval, limit)

    o5, hi5, lo5, c5, v5 = load_series("5m", m5, csv5)
    o15, hi15, lo15, c15, v15 = load_series("15m", m15, csv15)

    r5 = simulate(
        label="5m",
        rules=rules_5m,
        o=o5,
        hi=hi5,
        lo=lo5,
        c=c5,
        v=v5,
        min_bars=50,
        week_bars=week_5,
    )
    r15 = simulate(
        label="15m",
        rules=rules_15m,
        o=o15,
        hi=hi15,
        lo=lo15,
        c=c15,
        v=v15,
        min_bars=50,
        week_bars=week_15,
    )

    comb_pnl = r5["total_pnl_usdc"] + r15["total_pnl_usdc"]
    comb_staked = r5["total_staked_usdc"] + r15["total_staked_usdc"]
    comb_trades = r5["trades"] + r15["trades"]
    comb_wins = r5["wins"] + r15["wins"]
    comb_roi = (comb_pnl / comb_staked * 100.0) if comb_staked > 1e-12 else 0.0
    comb_wr = (comb_wins / comb_trades) if comb_trades else 0.0

    print(
        "SHAMAN v1 simulation - entry always {:.2f} (win +stake, loss -stake); stake $3..$6 by signal count".format(
            ENTRY_PX
        )
    )
    print(f"Symbol={args.symbol} rules_file={args.rules}")
    if args.last_week:
        print("Mode: LAST 7 CALENDAR DAYS per timeframe (PnL/ROI for that window only).")
    print(f"rule_rows: 5m={len(rules_5m)} 15m={len(rules_15m)} (15m sim is 0 if no 15m rules in JSON)")
    print()
    for r in (r5, r15):
        extra = ""
        if r.get("week_bars"):
            extra = f" t0={r['t0']} week_bars~{r['week_bars']}"
        print(
            f"{r['label']}: bars={r['bars']}{extra} trades={r['trades']} wins={r['wins']} losses={r['losses']} "
            f"doji_skips={r['skips_doji']} win_rate={r['win_rate']:.2%} "
            f"staked=${r['total_staked_usdc']:.2f} roi_on_staked={r['roi_on_staked_pct']:+.2f}% "
            f"avg_pnl/trade={r['avg_pnl_per_trade_usdc']:+.3f} total_pnl_usdc={r['total_pnl_usdc']:+.2f}"
        )
    print()
    print(
        f"COMBINED: trades={comb_trades} win_rate={comb_wr:.2%} staked=${comb_staked:.2f} "
        f"roi_on_staked={comb_roi:+.2f}% avg_pnl/trade={(comb_pnl / comb_trades) if comb_trades else 0:+.3f} "
        f"total_pnl_usdc={comb_pnl:+.2f}"
    )

    report_path = args.report
    if args.last_week and report_path is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = OUT_DIR / "SHAMAN_SIM_1WEEK_REPORT.md"
    if report_path is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "# SHAMAN v1 — 1 week simulation (aggregate signals, fixed 0.5 payoff model)",
            "",
            f"- Generated (UTC): `{stamp}`",
            f"- Rules: `{Path(args.rules).as_posix()}`",
            f"- 5m series: **{r5['bars']}** bars, sim from index **{r5['t0']}** (trailing ~7d when week mode).",
            f"- 15m series: **{r15['bars']}** bars, sim from index **{r15['t0']}**.",
            "",
            "## Assumptions",
            "",
            "- At each closed bar `t`, count matching rules → majority prediction vs **next** bar R/G.",
            "- Stake per clip: **$3** + **$1** per extra concurrent rule on the winning side, cap **$6**.",
            "- Win: **+stake** USDC; loss: **-stake**; doji next bar: skip (not counted as win/loss).",
            "",
            "## 5m",
            "",
            f"| Metric | Value |",
            f"|--------|------:|",
            f"| Trades | {r5['trades']} |",
            f"| Wins / losses | {r5['wins']} / {r5['losses']} |",
            f"| Doji skips | {r5['skips_doji']} |",
            f"| Win rate | {r5['win_rate']:.4f} |",
            f"| Total staked (sum of stakes) | {r5['total_staked_usdc']:.2f} |",
            f"| Avg stake | {r5['avg_stake_usdc']:.4f} |",
            f"| **Total PnL (USDC)** | **{r5['total_pnl_usdc']:+.2f}** |",
            f"| Avg PnL / trade | {r5['avg_pnl_per_trade_usdc']:+.4f} |",
            f"| **ROI on staked** | **{r5['roi_on_staked_pct']:+.2f}%** |",
            "",
            "## 15m",
            "",
            f"| Metric | Value |",
            f"|--------|------:|",
            f"| Trades | {r15['trades']} |",
            f"| Wins / losses | {r15['wins']} / {r15['losses']} |",
            f"| Doji skips | {r15['skips_doji']} |",
            f"| Win rate | {r15['win_rate']:.4f} |",
            f"| Total staked | {r15['total_staked_usdc']:.2f} |",
            f"| Avg stake | {r15['avg_stake_usdc']:.4f} |",
            f"| **Total PnL (USDC)** | **{r15['total_pnl_usdc']:+.2f}** |",
            f"| Avg PnL / trade | {r15['avg_pnl_per_trade_usdc']:+.4f} |",
            f"| **ROI on staked** | **{r15['roi_on_staked_pct']:+.2f}%** |",
            "",
            "## Combined (5m + 15m, separate clocks)",
            "",
            f"| Metric | Value |",
            f"|--------|------:|",
            f"| Trades | {comb_trades} |",
            f"| Win rate | {comb_wr:.4f} |",
            f"| Total staked | {comb_staked:.2f} |",
            f"| **Total PnL (USDC)** | **{comb_pnl:+.2f}** |",
            f"| Avg PnL / trade | {(comb_pnl / comb_trades) if comb_trades else 0:+.4f} |",
            f"| **ROI on staked** | **{comb_roi:+.2f}%** |",
            "",
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print()
        print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
