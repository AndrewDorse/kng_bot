#!/usr/bin/env python3
"""
Replay SHAMAN v1 live rule aggregation (same vote logic as ``ShamanV1Engine``) on recent
Binance BTCUSDT candles: for each closed bar in the trailing window, compute nG / nR at ``t``.

No Polymarket; Binance REST only.

Run from repo root:
  python PALADIN/replay_shaman_live_last_hours.py --hours 12
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PAL = REPO / "PALADIN"
OUT_DIR = REPO / "exports" / "btc_binance_klines"
LIVE_JSON = PAL / "shaman_v1_rules.json"
BINANCE = "https://api.binance.com/api/v3/klines"
MIN_T = 50


def _fetch(*, symbol: str, interval: str, total: int) -> list[list[Any]]:
    all_rows: list[list[Any]] = []
    end_time: int | None = None
    while len(all_rows) < total:
        need = min(1000, total - len(all_rows))
        q = f"symbol={symbol}&interval={interval}&limit={need}"
        if end_time is not None:
            q += f"&endTime={end_time}"
        url = f"{BINANCE}?{q}"
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    batch = json.loads(r.read().decode())
                break
            except Exception as exc:
                if attempt == 3:
                    raise RuntimeError(f"Binance fetch failed: {url} ({exc})") from exc
                time.sleep(1.5 * attempt)
        if not batch:
            break
        all_rows = batch + all_rows
        end_time = int(batch[0][0]) - 1
        if len(batch) < need:
            break
    return all_rows[-total:]


def _series(rows: list[list[Any]]) -> tuple[list[int], list[float], list[float], list[float], list[float], list[float]]:
    opens_ms = [int(k[0]) for k in rows]
    o = [float(k[1]) for k in rows]
    hi = [float(k[2]) for k in rows]
    lo = [float(k[3]) for k in rows]
    c = [float(k[4]) for k in rows]
    v = [float(k[5]) for k in rows]
    return opens_ms, o, hi, lo, c, v


def _pred(ng: int, nr: int) -> str | None:
    if ng > nr:
        return "G"
    if nr > ng:
        return "R"
    return None


def _next_bar_wr_pnl(
    ev: list[dict[str, Any]],
    o: list[float],
    c: list[float],
    rg_fn: Any,
    *,
    stake_usdc: float = 1.0,
) -> tuple[int, int, int, float, float | None]:
    """
    When aggregate pred is G or R, compare to next bar R/G (doji excluded).
    Returns (wins, losses, skipped_tie_bars, pnl_usdc, wr_or_None).
    """
    wins = losses = skipped_tie = 0
    for row in ev:
        pr = row["pred"]
        if pr == "TIE":
            skipped_tie += 1
            continue
        t = int(row["t"])
        if t + 1 >= len(o):
            continue
        act = rg_fn(t + 1, o, c)
        if act is None:
            continue
        if act == pr:
            wins += 1
        else:
            losses += 1
    hits = wins + losses
    pnl = float(stake_usdc) * (wins - losses)
    wr = (wins / hits) if hits else None
    return wins, losses, skipped_tie, pnl, wr


def _aggregate_at_t(
    rules: list[dict[str, Any]],
    o: list[float],
    hi: list[float],
    lo: list[float],
    c: list[float],
    v: list[float],
    t: int,
    aux: dict[str, Any],
    match_rule: Any,
) -> tuple[int, int]:
    ng = nr = 0
    for rule in rules:
        if not match_rule(
            str(rule["family"]),
            str(rule["pattern_key"]),
            o,
            c,
            v,
            hi,
            lo,
            t,
            aux=aux,
        ):
            continue
        if rule.get("pred") == "G":
            ng += 1
        elif rule.get("pred") == "R":
            nr += 1
    return ng, nr


def _replay(
    *,
    label: str,
    rules: list[dict[str, Any]],
    opens_ms: list[int],
    o: list[float],
    hi: list[float],
    lo: list[float],
    c: list[float],
    v: list[float],
    bars_in_window: int,
    match_rule: Any,
    build_aux: Any,
) -> list[dict[str, Any]]:
    n = len(o)
    aux = build_aux(o, c, v, hi, lo)
    t_end = n - 2
    t_start = max(MIN_T, t_end - bars_in_window + 1)
    out: list[dict[str, Any]] = []
    for t in range(t_start, t_end + 1):
        ng, nr = _aggregate_at_t(rules, o, hi, lo, c, v, t, aux, match_rule)
        pr = _pred(ng, nr)
        out.append(
            {
                "label": label,
                "open_ms": opens_ms[t],
                "t": t,
                "n_g": ng,
                "n_r": nr,
                "pred": pr or "TIE",
                "votes": ng + nr,
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0, help="Trailing window length in hours.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--out-csv", type=Path, default=None, help="Optional CSV path (default under exports/).")
    ap.add_argument("--stake-usdc", type=float, default=1.0, help="Per next-bar trial for PnL (isolated vote).")
    args = ap.parse_args()

    hours = max(0.25, float(args.hours))
    bars_5m = max(1, int(round(hours * 12)))  # 12 x 5m per hour
    bars_15m = max(1, int(round(hours * 4)))  # 4 x 15m per hour
    warm_5m = max(MIN_T + 10, 400)
    warm_15m = max(MIN_T + 10, 200)
    n5 = bars_5m + warm_5m
    n15 = bars_15m + warm_15m

    sys.path.insert(0, str(PAL))
    import shaman_v1_eval as sve  # noqa: PLC0415

    rows5 = _fetch(symbol=args.symbol, interval="5m", total=n5)
    rows15 = _fetch(symbol=args.symbol, interval="15m", total=n15)
    ms5, o5, hi5, lo5, c5, v5 = _series(rows5)
    ms15, o15, hi15, lo15, c15, v15 = _series(rows15)

    rules_all: list[dict] = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    r5 = [r for r in rules_all if str(r.get("timeframe", "")).strip() == "5m"]
    r15 = [r for r in rules_all if str(r.get("timeframe", "")).strip() == "15m"]

    ev5 = _replay(
        label="5m",
        rules=r5,
        opens_ms=ms5,
        o=o5,
        hi=hi5,
        lo=lo5,
        c=c5,
        v=v5,
        bars_in_window=bars_5m,
        match_rule=sve.match_rule,
        build_aux=sve._build_aux,  # noqa: SLF001
    )
    ev15 = _replay(
        label="15m",
        rules=r15,
        opens_ms=ms15,
        o=o15,
        hi=hi15,
        lo=lo15,
        c=c15,
        v=v15,
        bars_in_window=bars_15m,
        match_rule=sve.match_rule,
        build_aux=sve._build_aux,  # noqa: SLF001
    )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_open_5 = datetime.fromtimestamp(ms5[-1] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    first_open_5 = datetime.fromtimestamp(ev5[0]["open_ms"] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    last_open_15 = datetime.fromtimestamp(ms15[-1] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    first_open_15 = datetime.fromtimestamp(ev15[0]["open_ms"] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _summ(rows: list[dict]) -> tuple[int, int, int, int]:
        with_vote = sum(1 for x in rows if x["votes"] > 0)
        g = sum(1 for x in rows if x["pred"] == "G")
        r = sum(1 for x in rows if x["pred"] == "R")
        tie = sum(1 for x in rows if x["pred"] == "TIE")
        return with_vote, g, r, tie

    w5, g5, r5c, t5 = _summ(ev5)
    w15, g15, r15c, t15 = _summ(ev15)

    stake = float(args.stake_usdc)
    w5b, l5b, sk5, pnl5, wr5 = _next_bar_wr_pnl(ev5, o5, c5, sve._rg, stake_usdc=stake)  # noqa: SLF001
    w15b, l15b, sk15, pnl15, wr15 = _next_bar_wr_pnl(ev15, o15, c15, sve._rg, stake_usdc=stake)  # noqa: SLF001

    out_csv = args.out_csv
    if out_csv is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        htag = str(int(hours)) if float(hours) == int(hours) else str(hours).replace(".", "p")
        out_csv = OUT_DIR / f"REPLAY_shaman_live_last_{htag}h_{stamp[:10]}.csv"

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["label", "open_ms", "open_utc", "t", "n_g", "n_r", "pred", "votes"],
        )
        w.writeheader()
        for block in (ev5, ev15):
            for row in block:
                utc = datetime.fromtimestamp(row["open_ms"] / 1000.0, tz=timezone.utc).isoformat()
                w.writerow(
                    {
                        "label": row["label"],
                        "open_ms": row["open_ms"],
                        "open_utc": utc,
                        "t": row["t"],
                        "n_g": row["n_g"],
                        "n_r": row["n_r"],
                        "pred": row["pred"],
                        "votes": row["votes"],
                    }
                )

    print(f"Generated (UTC): {stamp}")
    print(f"Symbol: {args.symbol}  hours: {hours}")
    print(f"Live rules loaded: 5m={len(r5)}  15m={len(r15)}")
    print()
    print("=== 5m replay (engine vote at each closed bar) ===")
    print(f"Bars in window: {len(ev5)}  (series len={len(o5)}, last kline open: {last_open_5})")
    print(f"Window first closed-bar open: {first_open_5}")
    print(f"Bars with >=1 rule firing: {w5} / {len(ev5)}")
    print(f"Pred counts: G={g5}  R={r5c}  TIE={t5}")
    h5 = w5b + l5b
    print(
        f"Next-bar (pred G/R vs t+1 candle): hits={h5} wins={w5b} losses={l5b} "
        f"WR={wr5:.4f}" if wr5 is not None else f"Next-bar: no scored trials (doji-only or edge)"
    )
    print(f"PnL @ ${stake:.2f}/trial (wins-losses)*stake: {pnl5:+.2f} USDC")
    print()
    print("=== 15m replay ===")
    print(f"Bars in window: {len(ev15)}  (series len={len(o15)}, last kline open: {last_open_15})")
    print(f"Window first closed-bar open: {first_open_15}")
    print(f"Bars with >=1 rule firing: {w15} / {len(ev15)}")
    print(f"Pred counts: G={g15}  R={r15c}  TIE={t15}")
    h15 = w15b + l15b
    print(
        f"Next-bar (pred G/R vs t+1 candle): hits={h15} wins={w15b} losses={l15b} "
        f"WR={wr15:.4f}" if wr15 is not None else "Next-bar: no scored trials"
    )
    print(f"PnL @ ${stake:.2f}/trial: {pnl15:+.2f} USDC")
    print()
    comb_h = h5 + h15
    comb_w = w5b + w15b
    comb_l = l5b + l15b
    comb_pnl = pnl5 + pnl15
    comb_wr = (comb_w / comb_h) if comb_h else None
    print("=== Combined 5m + 15m (separate trials; not deduped bars) ===")
    if comb_wr is not None:
        print(f"hits={comb_h} wins={comb_w} losses={comb_l} WR={comb_wr:.4f}  PnL={comb_pnl:+.2f} USDC")
    print()
    print(f"Wrote {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
