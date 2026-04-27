#!/usr/bin/env python3
"""
Evaluate each **live** ``timeframe == "5m"`` rule in ``shaman_v1_rules.json`` on the trailing
**N×7 days** of Binance BTCUSDT 5m OHLCV (next-bar R/G vs rule ``pred``).

PnL model (per rule, isolated): **$1** per fill by default; win **+1**, loss **-1**; doji next bar excluded.

Writes markdown + CSV under ``exports/btc_binance_klines/`` (for ``--weeks 4`` also
``REPORT_5m_live_rules_last_month.md``).

Run from repo root:
  python PALADIN/report_5m_live_rules_two_weeks.py --weeks 2
  python PALADIN/report_5m_live_rules_two_weeks.py --weeks 4
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
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
BARS_PER_WEEK_5M = 7 * 24 * 12  # 2016 five-minute bars per 7d
WARMUP = 400
DEFAULT_STAKE_USDC = 1.0

sys.path.insert(0, str(PAL))
import shaman_v1_eval as sve  # noqa: E402


def _fetch_5m(*, symbol: str, total: int, out_path: Path) -> int:
    all_rows: list[list[Any]] = []
    end_time: int | None = None
    while len(all_rows) < total:
        need = min(1000, total - len(all_rows))
        q = f"symbol={symbol}&interval=5m&limit={need}"
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
    all_rows = all_rows[-total:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "open_time_ms",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time_ms",
                "quote_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ]
        )
        for k in all_rows:
            w.writerow(k[:12])
    return len(all_rows)


def _load_ohlcv(path: Path) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    o, c, hi, lo, v = [], [], [], [], []
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            o.append(float(row["open"]))
            c.append(float(row["close"]))
            hi.append(float(row["high"]))
            lo.append(float(row["low"]))
            v.append(float(row["volume"]))
    return o, c, hi, lo, v


def _eval_rule(
    *,
    family: str,
    pattern_key: str,
    pred: str,
    o: list[float],
    c: list[float],
    hi: list[float],
    lo: list[float],
    vol: list[float],
    aux: dict[str, Any],
    t_lo: int,
    t_hi: int,
    stake_usdc: float,
) -> dict[str, Any]:
    wins = losses = 0
    hits = 0
    for t in range(t_lo, t_hi + 1):
        if not sve.match_rule(family, pattern_key, o, c, vol, hi, lo, t, aux=aux):
            continue
        act = sve._rg(t + 1, o, c)  # noqa: SLF001
        if act is None:
            continue
        hits += 1
        if act == pred:
            wins += 1
        else:
            losses += 1
    wr = (wins / hits) if hits else None
    pnl = float(stake_usdc) * (wins - losses)
    notional = float(stake_usdc) * float(hits)
    return {
        "hits": hits,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "pnl_usdc": pnl,
        "notional_staked_usdc": notional,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--weeks", type=int, default=2, help="Trailing calendar weeks of 5m bars (7d = 2016 bars each).")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--stake-usdc", type=float, default=DEFAULT_STAKE_USDC)
    args = ap.parse_args()

    weeks = max(1, min(52, int(args.weeks)))
    eval_bars = weeks * BARS_PER_WEEK_5M
    stake = float(args.stake_usdc)
    fetch_n = eval_bars + WARMUP
    kpath = OUT_DIR / f"btcusdt_5m_last{fetch_n}_binance.csv"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_fetch:
        got = _fetch_5m(symbol=args.symbol, total=fetch_n, out_path=kpath)
        print(f"Fetched {got} rows -> {kpath}", file=sys.stderr)
    elif not kpath.is_file():
        print(f"--skip-fetch but missing {kpath}", file=sys.stderr)
        return 1

    o, c, hi, lo, vol = _load_ohlcv(kpath)
    n = len(o)
    aux = sve._build_aux(o, c, vol, hi, lo)  # noqa: SLF001
    t_lo = max(50, n - eval_bars)
    t_hi = n - 2
    if t_lo > t_hi:
        print("Not enough bars", file=sys.stderr)
        return 1

    with LIVE_JSON.open(encoding="utf-8") as f:
        rules: list[dict[str, Any]] = json.load(f)
    five = [r for r in rules if str(r.get("timeframe", "")).strip() == "5m"]

    rows: list[dict[str, Any]] = []
    for r in five:
        fam, pk, pr = r["family"], str(r["pattern_key"]).strip(), str(r["pred"]).strip()
        ev = _eval_rule(
            family=fam,
            pattern_key=pk,
            pred=pr,
            o=o,
            c=c,
            hi=hi,
            lo=lo,
            vol=vol,
            aux=aux,
            t_lo=t_lo,
            t_hi=t_hi,
            stake_usdc=stake,
        )
        rows.append(
            {
                "family": fam,
                "pattern_key": pk,
                "pred": pr,
                "wr_json": float(r.get("wr", 0)),
                "n_json": int(r.get("n", 0)),
                **ev,
            }
        )

    with_hits = [x for x in rows if x["hits"] > 0]
    sum_hits = sum(x["hits"] for x in rows)
    sum_wins = sum(x["wins"] for x in rows)
    sum_losses = sum(x["losses"] for x in rows)
    sum_pnl = sum(x["pnl_usdc"] for x in rows)
    sum_notional = sum(x["notional_staked_usdc"] for x in rows)
    pooled_wr = (sum_wins / sum_hits) if sum_hits else None
    wrs = [x["wr"] for x in with_hits if x["wr"] is not None]
    mean_rule_wr = sum(wrs) / len(wrs) if wrs else None

    below_60 = [x for x in with_hits if x["wr"] is not None and x["wr"] < 0.60]
    below_60.sort(key=lambda x: (x["wr"] or 0, x["hits"]))

    by_wr = sorted(with_hits, key=lambda x: (-(x["wr"] or 0), -x["hits"]))
    by_pnl = sorted(rows, key=lambda x: (-x["pnl_usdc"], -x["hits"]))
    by_fills = sorted(rows, key=lambda x: (-x["hits"], -(x["wr"] or 0)))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _rule_line(x: dict[str, Any]) -> str:
        return f"{x['family']} | {x['pattern_key']} | pred={x['pred']}"

    if weeks == 2:
        report_stem = "REPORT_5m_live_rules_two_weeks"
    else:
        report_stem = f"REPORT_5m_live_rules_{weeks}weeks"
    report = OUT_DIR / f"{report_stem}.md"
    csv_path = OUT_DIR / f"{report_stem}.csv"

    with csv_path.open("w", newline="", encoding="utf-8") as cf:
        w = csv.writer(cf)
        w.writerow(
            [
                "hits",
                "wins",
                "losses",
                "wr_eval",
                "pnl_usdc",
                "notional_staked_usdc",
                "wr_json",
                "n_json",
                "family",
                "pattern_key",
                "pred",
            ]
        )
        for x in sorted(rows, key=lambda z: (-(z["wr"] or 0), -z["hits"])):
            w.writerow(
                [
                    x["hits"],
                    x["wins"],
                    x["losses"],
                    f"{x['wr']:.6f}" if x["wr"] is not None else "",
                    f"{x['pnl_usdc']:.4f}",
                    f"{x['notional_staked_usdc']:.4f}",
                    x["wr_json"],
                    x["n_json"],
                    x["family"],
                    x["pattern_key"],
                    x["pred"],
                ]
            )

    title_suffix = f"{weeks} week(s)" if weeks != 4 else "~1 month (4 weeks)"
    lines = [
        f"# Live 5m SHAMAN rules — trailing {title_suffix} (Binance BTCUSDT 5m)",
        "",
        f"- Generated (UTC): `{stamp}`",
        f"- Window: **{weeks}** × 7d = **{eval_bars}** 5m bars on the close of the series.",
        f"- Klines: `{kpath.name}` (**{n}** rows); eval `t` in `[{t_lo}, {t_hi}]` (**{t_hi - t_lo + 1}** steps).",
        f"- Live 5m rules: **{len(five)}**; with ≥1 fill: **{len(with_hits)}**.",
        f"- Stake per fill (isolated rule sim): **${stake:.2f}** USDC (win +stake, loss -stake).",
        "",
        "## Totals (sum over rules — overlaps allowed)",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Sum of fills (hits) | {sum_hits} |",
        f"| Sum of wins | {sum_wins} |",
        f"| Sum of losses | {sum_losses} |",
        f"| **Pooled WR** (sum wins / sum fills) | **{pooled_wr:.4f}** |" if pooled_wr is not None else "| **Pooled WR** | n/a |",
        f"| **Sum PnL (USDC)** | **{sum_pnl:+.2f}** |",
        f"| Sum notional staked (fills x ${stake:.0f}) | {sum_notional:.2f} |",
        f"| ROI on sum staked | **{(sum_pnl / sum_notional * 100) if sum_notional > 1e-12 else 0:+.2f}%** |",
        f"| Mean WR (rules with fills only) | {mean_rule_wr:.4f} |" if mean_rule_wr is not None else "| Mean WR (rules with fills) | n/a |",
        "",
        "## Rules with eval WR < 60% (decision list)",
        "",
        f"**Count:** {len(below_60)}",
        "",
        "| wr_eval | hits | wins | pnl | wr (JSON) | rule |",
        "|--------:|-----:|-----:|----:|----------:|------|",
    ]
    for x in below_60:
        wre = f"{x['wr']:.4f}" if x["wr"] is not None else ""
        rl = _rule_line(x).replace("|", "\\|")
        lines.append(
            f"| {wre} | {x['hits']} | {x['wins']} | {x['pnl_usdc']:+.2f} | {x['wr_json']:.4f} | {rl} |"
        )
    if not below_60:
        lines.append("*None in this window.*")

    lines.extend(
        [
            "",
            "## Top 5 by WR (among rules with fills; tie-break: more fills)",
            "",
        ]
    )
    for i, x in enumerate(by_wr[:5], 1):
        wrs_s = f"{x['wr']:.4f}" if x["wr"] is not None else "n/a"
        lines.append(
            f"{i}. **WR={wrs_s}** fills={x['hits']} pnl=**{x['pnl_usdc']:+.2f}** — `{_rule_line(x)}`"
        )
    lines.extend(["", "## Top 5 by PnL (USDC)", ""])
    for i, x in enumerate(by_pnl[:5], 1):
        wrs_s = f"{x['wr']:.4f}" if x["wr"] is not None else "n/a"
        lines.append(
            f"{i}. **pnl={x['pnl_usdc']:+.2f}** WR={wrs_s} fills={x['hits']} — `{_rule_line(x)}`"
        )
    lines.extend(["", "## Top 5 by fills (hit count)", ""])
    for i, x in enumerate(by_fills[:5], 1):
        wrs_s = f"{x['wr']:.4f}" if x["wr"] is not None else "n/a"
        lines.append(
            f"{i}. **fills={x['hits']}** notional_staked={x['notional_staked_usdc']:.2f} WR={wrs_s} pnl={x['pnl_usdc']:+.2f} — `{_rule_line(x)}`"
        )
    lines.extend(
        [
            "",
            f"Full table: `{csv_path.relative_to(REPO)}`",
            "",
        ]
    )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {report}", file=sys.stderr)
    print(f"Wrote {csv_path}", file=sys.stderr)

    if weeks == 4:
        month_path = OUT_DIR / "REPORT_5m_live_rules_last_month.md"
        shutil.copyfile(report, month_path)
        print(f"Wrote {month_path} (copy of 4-week report)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
