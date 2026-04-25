#!/usr/bin/env python3
"""
**PALADIN v9 strategy (this file)** — second-by-second backtest harness. **No predictions, no ML.**

This module *is* the v9 strategy testbed: one run = wallet replay vs **PALADIN v9 sim** on the same
15m window. The sim’s decision kernel is ``paladin_v7_step`` (BTC volume spike + momentum entries
and cheap/forced hedges); v9 here means *this simulator + that kernel + unlimited budget defaults*.

Two arms (manifest mode):

1. **Wallet (baseline)** — chain BUY replay from ``*_wallet_raw.csv``.
2. **PALADIN v9 sim** — ``paladin_v7_step`` each second on ``*_public_prices.csv`` (PM + BTC fields).
   Causal tape access only: ``ticks[i]`` for ``i <= t`` (rolling mean uses ``[t-lookback, t)``).
   Verify: ``python PALADIN/test_paladin_v7_causal_ticks.py``.

**Live trading:** ``BOT_STRATEGY_MODE=paladin_v9`` + ``main.py`` → ``paladin_v9_live_engine.PaladinV9LiveEngine``
(same stack as v7 live: WS + Binance + CLOB). Preflight: ``python check_paladin_v9_live_ready.py``.

**Public-only batch:** ``--batch --public-snapshots-dir exports/window_price_snapshots_public`` runs v9 on the
first ``--max-windows`` ``*_prices.csv`` files (sorted by name) whose header includes ``btc_price`` /
``btc_volume``; no wallet data.

**Realistic sim defaults (not the old stress harness):** per-window ``budget_usdc=400``, ``max_shares_per_side=25``
(see ``PaladinV7Params``), optional ``--taker-slip-usd`` (default half-cent) widens the limit before the tape
check. **Fills:** ``simulate_paladin_window.try_buy`` only fills a limit buy when the **recorded mid** for that
outcome is ``<=`` the limit (then fill price ``min(limit, mid)``); otherwise the order waits for a later second
or the kernel may switch to ``v7_hedge_forced`` at timeout. Use ``--legacy-unlimited-sim`` for the old stress
preset. Post-run **sanity** checks: sim book cost vs budget, fill px in ``[0,1]``, trade notionals vs book cost (loose).

Settlement: proxy winner = higher final PM mid (same as other harnesses). Still **not** live Polymarket
(full book, fees, partial fills, latency).

Example::

  python PALADIN/paladin_v9_second_simulator.py --slug btc-updown-15m-1775442600 --per-buy
  python PALADIN/paladin_v9_second_simulator.py --batch --max-windows 10
  python PALADIN/paladin_v9_second_simulator.py --batch --public-snapshots-dir exports/window_price_snapshots_public --max-windows 100

**V9 fills only (no wallet):** each sim ``try_buy`` with second, fill px, size, post-fill avgs + inventory + U/D share ratio::

  python PALADIN/paladin_v9_second_simulator.py --public-prices exports/window_price_snapshots_public/..._prices.csv
  python PALADIN/paladin_v9_second_simulator.py --manifest .../manifest.csv --slug btc-updown-15m-1775442600 --v9-from-prices
"""

from __future__ import annotations

# User-facing label for all reports (kernel named in module docstring).
V9_STRATEGY_LINE = "PALADIN v9 strategy (second sim) | rule kernel: paladin_v7_step @ 1Hz"

import argparse
import csv
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
REPO = Path(__file__).resolve().parents[1]
_PALADIN = Path(__file__).resolve().parent
for _p in (REPO, _PALADIN):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from paladin_engine import (  # noqa: E402
    apply_buy_fill,
    pnl_if_down_usdc,
    pnl_if_up_usdc,
    roi_if_down,
    roi_if_up,
    total_cost_usdc,
)
from paladin_v7 import PaladinV7Params, PaladinV7Runner, WindowTick, load_ticks_with_btc, paladin_v7_step  # noqa: E402
from simulate_paladin_window import (  # noqa: E402
    Trade,
    forward_fill_prices,
    load_prices_by_elapsed,
    resolve_winner_from_last_prices,
    try_buy,
    window_slug_from_prices_csv,
)


def build_sim_params(
    *,
    legacy_unlimited: bool,
    budget_usdc: float,
    max_shares_per_side: float,
) -> PaladinV7Params:
    """Realistic defaults from ``PaladinV7Params`` unless ``legacy_unlimited`` (old v9 stress harness)."""
    if legacy_unlimited:
        return PaladinV7Params(
            budget_usdc=1.0e9,
            max_shares_per_side=1.0e6,
            base_order_shares=5.0,
            min_shares=5.0,
            min_notional=1.0,
        )
    return PaladinV7Params(
        budget_usdc=float(budget_usdc),
        max_shares_per_side=float(max_shares_per_side),
    )


def make_try_buy_with_slip(slip_usd: float) -> Callable[..., float] | None:
    """Bump fill price (taker / FAK walk); returns ``None`` when slip is zero (use engine default)."""
    if slip_usd <= 1e-12:
        return None

    def wrapped(
        st: Any,
        *,
        t: int,
        side: Any,
        shares: float,
        px: float,
        reason: str,
        budget: float,
        min_notional: float,
        min_shares: float = 5.0,
        pm_u: float | None = None,
        pm_d: float | None = None,
    ) -> float:
        px2 = min(0.9999, float(px) + slip_usd)
        return try_buy(
            st,
            t=t,
            side=side,
            shares=shares,
            px=px2,
            reason=reason,
            budget=budget,
            min_notional=min_notional,
            min_shares=min_shares,
            pm_u=pm_u,
            pm_d=pm_d,
        )

    return wrapped


def collect_outcome_sanity_issues(o: WindowSimOutcome, *, budget: float) -> list[str]:
    """Post-run checks: spend cap, sane prices. Does not prove exchange realism."""
    issues: list[str] = []
    if o.sc > budget + 0.05:
        issues.append(f"sim book cost {o.sc:.4f} > budget {budget:.4f}")
    spent = sum(float(tr.notional) for tr in o.trades)
    if abs(spent - o.sc) > 0.15 + 1e-6 * max(1, o.s_fills):
        issues.append(f"sum trade notion ${spent:.4f} vs book cost ${o.sc:.4f}")
    for tr in o.trades:
        if float(tr.price) - 1.0 > 1e-6:
            issues.append(f"fill px {float(tr.price):.6f} > 1.0")
        if float(tr.price) < -1e-6:
            issues.append("negative fill px")
    return issues


def _run_v9_simulation_core(
    ticks: list[WindowTick],
    prices_path: Path,
    w_snaps: list[tuple[float, float, float, float]],
    w_fills: int,
    slug: str,
    *,
    sim_params: PaladinV7Params,
    try_buy_fn: Callable[..., float] | None,
) -> WindowSimOutcome:
    runner = PaladinV7Runner()
    sim_snaps: list[tuple[float, float, float, float]] = []
    for t in range(900):
        paladin_v7_step(runner, t, ticks, params=sim_params, try_buy_fn=try_buy_fn)
        st = runner.st
        sim_snaps.append((float(st.size_up), float(st.size_down), float(st.avg_up), float(st.avg_down)))

    raw_pm = load_prices_by_elapsed(prices_path)
    series = forward_fill_prices(raw_pm, window_sec=900)
    winner, lu, ld = resolve_winner_from_last_prices(series)

    wsu, wsd, wau, wad = w_snaps[-1]
    ssu, ssd, sau, sad = sim_snaps[-1]
    wc = total_cost_usdc(wsu, wau, wsd, wad)
    sc = total_cost_usdc(ssu, sau, ssd, sad)

    if winner == "up":
        wp, sp = pnl_if_up_usdc(wsu, wau, wsd, wad), pnl_if_up_usdc(ssu, sau, ssd, sad)
    elif winner == "down":
        wp, sp = pnl_if_down_usdc(wsu, wau, wsd, wad), pnl_if_down_usdc(ssu, sau, ssd, sad)
    else:
        wp = 0.5 * (pnl_if_up_usdc(wsu, wau, wsd, wad) + pnl_if_down_usdc(wsu, wau, wsd, wad))
        sp = 0.5 * (pnl_if_up_usdc(ssu, sau, ssd, sad) + pnl_if_down_usdc(ssu, sau, ssd, sad))

    return WindowSimOutcome(
        slug=slug,
        winner=winner,
        last_u=lu,
        last_d=ld,
        w_fills=w_fills,
        wsu=wsu,
        wsd=wsd,
        wau=wau,
        wad=wad,
        wc=wc,
        wp=wp,
        s_fills=len(runner.st.trades),
        ssu=ssu,
        ssd=ssd,
        sau=sau,
        sad=sad,
        sc=sc,
        sp=sp,
        trades=list(runner.st.trades),
        ticks=ticks,
        w_snaps=w_snaps,
        sim_snaps=sim_snaps,
    )


def _safe_path(repo: Path, rel: str) -> Path:
    return (repo / rel.replace("\\", "/")).resolve()


def load_manifest_row(repo: Path, manifest: Path, slug: str) -> dict[str, str] | None:
    with manifest.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("slug", "").strip() == slug.strip():
                return row
    return None


def load_manifest_rows(manifest: Path, *, max_rows: int) -> list[dict[str, str]]:
    with manifest.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[: max(1, max_rows)]


@dataclass(slots=True)
class WindowSimOutcome:
    slug: str
    winner: str
    last_u: float
    last_d: float
    w_fills: int
    wsu: float
    wsd: float
    wau: float
    wad: float
    wc: float
    wp: float
    s_fills: int
    ssu: float
    ssd: float
    sau: float
    sad: float
    sc: float
    sp: float
    trades: list[Trade]
    ticks: list[WindowTick]
    w_snaps: list[tuple[float, float, float, float]]
    sim_snaps: list[tuple[float, float, float, float]]


def _empty_wallet_replay() -> tuple[list[tuple[float, float, float, float]], int]:
    snaps = [(0.0, 0.0, 0.0, 0.0)] * 900
    return snaps, 0


def discover_public_snapshot_paths(snapshot_dir: Path, *, limit: int) -> list[Path]:
    """Sorted ``*_prices.csv`` under ``snapshot_dir`` whose header includes BTC columns (v9 kernel needs them)."""
    out: list[Path] = []
    for p in sorted(snapshot_dir.glob("*_prices.csv")):
        if not p.is_file():
            continue
        with p.open(newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            header = next(r, None)
        if not header or "btc_volume" not in header or "btc_price" not in header:
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def run_window_public_only(
    prices_path: Path,
    *,
    sim_params: PaladinV7Params | None = None,
    try_buy_fn: Callable[..., float] | None = None,
) -> WindowSimOutcome | None:
    """PALADIN v9 sim only on a public ``*_prices.csv`` tape (no wallet). None if ticks unusable."""
    prices_path = prices_path.resolve()
    if not prices_path.is_file():
        return None
    slug_csv, ticks = load_ticks_with_btc(prices_path, window_sec=900)
    slug = (slug_csv or window_slug_from_prices_csv(prices_path)).strip()
    if len(ticks) != 900:
        return None

    w_snaps, w_fills = _empty_wallet_replay()
    p = sim_params if sim_params is not None else PaladinV7Params()
    return _run_v9_simulation_core(
        ticks, prices_path, w_snaps, w_fills, slug, sim_params=p, try_buy_fn=try_buy_fn
    )


def run_window(
    repo: Path,
    mr: dict[str, str],
    *,
    sim_params: PaladinV7Params | None = None,
    try_buy_fn: Callable[..., float] | None = None,
) -> WindowSimOutcome | None:
    """Run wallet replay + PALADIN v9 sim for one manifest row. None if data unusable."""
    prices_path = _safe_path(repo, mr["public_prices_csv"])
    wallet_path = _safe_path(repo, mr["wallet_raw_csv"])
    slug_csv, ticks = load_ticks_with_btc(prices_path, window_sec=900)
    slug = (mr.get("slug") or slug_csv or "").strip()
    if len(ticks) != 900:
        return None
    if not wallet_path.is_file():
        return None

    by_sec = bucket_wallet_buys(wallet_path)
    w_snaps, w_fills = replay_wallet(by_sec)

    p = sim_params if sim_params is not None else PaladinV7Params()
    return _run_v9_simulation_core(
        ticks, prices_path, w_snaps, w_fills, slug, sim_params=p, try_buy_fn=try_buy_fn
    )


def bucket_wallet_buys(path: Path) -> dict[int, list[tuple[str, float, float]]]:
    """elapsed_sec -> list of (side_lower, size, price) in file order."""
    out: dict[int, list[tuple[str, float, float]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("trade_side", "").upper() != "BUY":
                continue
            try:
                e = int(float(row["elapsed_sec"]))
            except (KeyError, ValueError):
                continue
            if not (0 <= e < 900):
                continue
            side = row.get("side", "up").strip().lower()
            if side not in ("up", "down"):
                continue
            sh = float(row["size"])
            px = float(row["price"])
            out[e].append((side, sh, px))
    return out


def replay_wallet(
    by_sec: dict[int, list[tuple[str, float, float]]],
) -> tuple[list[tuple[float, float, float, float]], int]:
    """Returns list of 900 (su, sd, au, ad) after each second, and total fills."""
    su = au = sd = ad = 0.0
    snaps: list[tuple[float, float, float, float]] = []
    nf = 0
    for t in range(900):
        for side, sh, px in by_sec.get(t, []):
            su, au, sd, ad = apply_buy_fill(su, au, sd, ad, side=side, add_shares=sh, fill_price=px)
            nf += 1
        snaps.append((su, sd, au, ad))
    return snaps, nf


def _fmt_roi_pct(su: float, au: float, sd: float, ad: float) -> tuple[str, str]:
    ru = roi_if_up(su, au, sd, ad) * 100.0
    rd = roi_if_down(su, au, sd, ad) * 100.0
    return f"{ru:+7.2f}%", f"{rd:+7.2f}%"


def _up_share_pct(su: float, sd: float) -> float:
    s = su + sd
    if s <= 1e-12:
        return 0.0
    return 100.0 * su / s


def _ud_ratio_str(su: float, sd: float) -> str:
    if sd <= 1e-9:
        return "inf" if su > 1e-9 else "-"
    return f"{su / sd:.4f}"


def print_v9_buy_ledger(trades: list[Trade], ticks: list[WindowTick]) -> None:
    """Each v9 sim fill: second, side, size, fill price, tape mids, post-fill sz/avg, U/D ratio, spent — no wallet."""
    print("\n" + "=" * 130)
    print(
        "PALADIN v9 - BUY LEDGER (kernel paladin_v7_step; each row = one sim fill; "
        "mid_* = PM tape at that window second; post state after fill)"
    )
    print(
        "# | sec | side | shares | fill_px | mid_u | mid_d | "
        "sz_up | sz_dn | avg_up | avg_dn | U/D | up% | spent$ | reason"
    )
    print("-" * 130)
    su = au = sd = ad = 0.0
    spent = 0.0
    for i, tr in enumerate(trades, 1):
        e = int(tr.elapsed_sec)
        e = max(0, min(e, len(ticks) - 1))
        tk = ticks[e]
        pu, pd = float(tk.pm_u), float(tk.pm_d)
        su, au, sd, ad = apply_buy_fill(
            su, au, sd, ad, side=tr.side, add_shares=float(tr.shares), fill_price=float(tr.price)
        )
        spent += float(tr.notional)
        uds = _ud_ratio_str(su, sd)
        upp = _up_share_pct(su, sd)
        rs = (tr.reason or "").replace("\t", " ")[:44]
        print(
            f"{i:3d} | {e:3d} | {tr.side:4s} | {tr.shares:7.2f} | {tr.price:7.4f} | "
            f"{pu:5.3f} | {pd:5.3f} | {su:8.2f} | {sd:8.2f} | {au:8.4f} | {ad:8.4f} | "
            f"{uds:>7s} | {upp:5.1f}% | {spent:8.2f} | {rs}"
        )
    print("=" * 130)
    print(f"fills={len(trades)}  settled v9 PnL uses final tape + positions above (see window summary).")


def _print_v9_only_window_report(o: WindowSimOutcome, *, slug: str, print_ledger: bool) -> None:
    """v9 sim summary + optional buy ledger — no wallet baseline."""
    winner, lu, ld = o.winner, o.last_u, o.last_d
    ssu, ssd, sau, sad = o.ssu, o.ssd, o.sau, o.sad
    sc, sp = o.sc, o.sp
    print("=" * 88)
    print(V9_STRATEGY_LINE)
    print(f"window={slug} | proxy settlement winner={winner} (final PM mid up={lu:.4f} down={ld:.4f})")
    print("--- PALADIN v9 sim (prices tape only; no wallet) ---")
    print(f"  v9 fills: {o.s_fills}")
    print(f"  Final sz_up={ssu:.4f} sz_dn={ssd:.4f} avg_up={sau:.4f} avg_dn={sad:.4f} cost=${sc:.2f}")
    print(f"  roi_if_up={roi_if_up(ssu, sau, ssd, sad)*100:+.2f}%  roi_if_down={roi_if_down(ssu, sau, ssd, sad)*100:+.2f}%")
    print(f"  Settled v9 PnL: ${sp:+.2f}" + (f"  (ROI on cost {100.0 * sp / sc:+.2f}%)" if sc > 1e-9 else ""))
    print("=" * 88)
    if print_ledger:
        print_v9_buy_ledger(o.trades, o.ticks)


def _print_single_window_report(o: WindowSimOutcome, *, slug: str, heartbeat: int, per_buy: bool) -> None:
    w_snaps, sim_snaps = o.w_snaps, o.sim_snaps
    winner, lu, ld = o.winner, o.last_u, o.last_d
    print("=" * 88)
    print(f"STRATEGY TEST: {V9_STRATEGY_LINE}")
    print(f"window={slug} | proxy settlement winner={winner} (final PM mid up={lu:.4f} down={ld:.4f})")
    print("baseline = wallet chain BUY replay | under test = PALADIN v9 sim (see module doc)")
    print("=" * 88)

    if heartbeat > 0:
        print(
            f"\nHeartbeat every {heartbeat}s "
            f"(sec | wallet roi_u roi_d su sd | v9 roi_u roi_d su sd)"
        )
        print("-" * 88)
        for t in range(0, 900, heartbeat):
            wsu, wsd, wau, wad = w_snaps[t]
            ssu, ssd, sau, sad = sim_snaps[t]
            wru, wrd = _fmt_roi_pct(wsu, wau, wsd, wad)
            sru, srd = _fmt_roi_pct(ssu, sau, ssd, sad)
            print(
                f"{t:4d} | wallet {wru} {wrd} su={wsu:8.2f} sd={wsd:8.2f} | "
                f"v9 {sru} {srd} su={ssu:8.2f} sd={ssd:8.2f}"
            )

    wsu, wsd, wau, wad = o.wsu, o.wsd, o.wau, o.wad
    ssu, ssd, sau, sad = o.ssu, o.ssd, o.sau, o.sad
    wc, sc = o.wc, o.sc

    print("\n--- SUMMARY: WALLET REPLAY (on-chain BUYS) ---")
    print(f"  BUY fills applied: {o.w_fills}")
    print(f"  Final sz_up={wsu:.4f} sz_dn={wsd:.4f} avg_up={wau:.4f} avg_dn={wad:.4f} cost=${wc:.2f}")
    print(f"  roi_if_up={roi_if_up(wsu, wau, wsd, wad)*100:+.2f}%  roi_if_down={roi_if_down(wsu, wau, wsd, wad)*100:+.2f}%")

    print("\n--- SUMMARY: PALADIN v9 SIM (unlimited budget; kernel = paladin_v7_step) ---")
    print(f"  v9 fills (try_buy successes): {o.s_fills}")
    print(f"  Final sz_up={ssu:.4f} sz_dn={ssd:.4f} avg_up={sau:.4f} avg_dn={sad:.4f} cost=${sc:.2f}")
    print(f"  roi_if_up={roi_if_up(ssu, sau, ssd, sad)*100:+.2f}%  roi_if_down={roi_if_down(ssu, sau, ssd, sad)*100:+.2f}%")

    print("\n--- SETTLEMENT (proxy winner above) ---")
    wp, sp = o.wp, o.sp
    print(f"  Wallet PnL: ${wp:+.2f}  (ROI on cost {100.0 * wp / wc:+.2f}%)" if wc > 1e-9 else f"  Wallet PnL: ${wp:+.2f}")
    print(f"  v9 PnL:     ${sp:+.2f}  (ROI on cost {100.0 * sp / sc:+.2f}%)" if sc > 1e-9 else f"  v9 PnL:     ${sp:+.2f}")

    if per_buy:
        print_v9_buy_ledger(o.trades, o.ticks)


def _win_rate_pct(pnls: list[float], *, eps: float = 1e-9) -> tuple[float, int]:
    """Fraction of windows with settled PnL > eps. Returns (percent, win_count)."""
    if not pnls:
        return 0.0, 0
    wins = sum(1 for x in pnls if x > eps)
    return 100.0 * wins / len(pnls), wins


def _print_public_snapshots_batch(
    results: list[tuple[str, WindowSimOutcome]],
    *,
    snap_dir: Path,
    requested_cap: int,
    sim_note: str = "",
) -> None:
    """v9-only table (no wallet); same TOTAL / AVG semantics as wallet batch for v9 columns."""
    hdr = "slug | win | v9_buys | v9_cost$ | v9_pnl$ | v9_ROI%"
    print("=" * 100)
    print(V9_STRATEGY_LINE)
    print(
        f"BATCH public snapshots | windows={len(results)} (cap {requested_cap}) | dir={snap_dir} | "
        "v9 only (BTC tape) | settlement=proxy PM winner"
    )
    if sim_note:
        print(f"SIM CONSTRAINTS: {sim_note}")
    print(hdr)
    print("-" * len(hdr))
    sum_sp = sum_sc = 0.0
    s_rois: list[float] = []
    for slug, o in results:
        s_roi = 100.0 * o.sp / o.sc if o.sc > 1e-9 else 0.0
        s_rois.append(s_roi)
        sum_sp += o.sp
        sum_sc += o.sc
        print(f"{slug} | {o.winner:4s} | {o.s_fills:6d} | {o.sc:9.2f} | {o.sp:+8.2f} | {s_roi:+7.2f}%")
    if results:
        n = len(results)
        tot_s_roi = 100.0 * sum_sp / sum_sc if sum_sc > 1e-9 else 0.0
        mean_s_roi = sum(s_rois) / n
        print("-" * len(hdr))
        print(
            f"{'TOTAL (sum $; ROI = sum_pnl/sum_cost)':32s} |      | "
            f"        | {sum_sc:9.2f} | {sum_sp:+8.2f} | {tot_s_roi:+7.2f}%"
        )
        print(
            f"{'AVG per window ($ cost / $ pnl)':32s} |      | "
            f"        | {sum_sc/n:9.2f} | {sum_sp/n:+8.2f} | {mean_s_roi:+7.2f}%"
        )
        s_wr, s_wn = _win_rate_pct([o.sp for _, o in results])
        print(f"Win rate (v9 settled PnL > 0): {s_wr:.1f}%  ({s_wn}/{n} windows)")
    print("=" * 100)


def _sim_setup_from_args(args: argparse.Namespace) -> tuple[PaladinV7Params, Callable[..., float] | None, str]:
    sim_params = build_sim_params(
        legacy_unlimited=args.legacy_unlimited_sim,
        budget_usdc=args.budget_usdc,
        max_shares_per_side=args.max_shares_per_side,
    )
    try_buy_fn = make_try_buy_with_slip(args.taker_slip_usd)
    mode = "legacy-unlimited" if args.legacy_unlimited_sim else "realistic"
    note = (
        f"{mode} | budget_usdc={sim_params.budget_usdc:g} max_sh/side={sim_params.max_shares_per_side:g} "
        f"taker_slip_usd={args.taker_slip_usd:g}"
    )
    return sim_params, try_buy_fn, note


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PALADIN v9 strategy test: per-second wallet replay vs v9 sim (paladin_v7_step kernel)"
    )
    ap.add_argument("--slug", type=str, default="btc-updown-15m-1775442600")
    ap.add_argument(
        "--manifest",
        type=Path,
        default=REPO / "exports/target_wallet_e1_dataset/analysis_10windows_btc/manifest.csv",
    )
    ap.add_argument(
        "--public-snapshots-dir",
        type=Path,
        default=None,
        help="With --batch: run v9 only on first --max-windows *_prices.csv (sorted by name) that include "
        "btc_price/btc_volume; no wallet manifest required",
    )
    ap.add_argument(
        "--public-prices",
        type=Path,
        default=None,
        help="One *_prices.csv (must include btc_price + btc_volume): v9 sim only — prints summary + v9 buy ledger (no wallet).",
    )
    ap.add_argument(
        "--v9-from-prices",
        action="store_true",
        help="With --slug + --manifest: read public_prices_csv from that row only — v9 sim + buy ledger (no wallet CSV).",
    )
    ap.add_argument(
        "--v9-ledger",
        action="store_true",
        help="With --batch --public-snapshots-dir: after the summary table, print v9 buy ledger for every window (verbose).",
    )
    ap.add_argument("--heartbeat", type=int, default=60, help="Print one line every N seconds (0=disable)")
    ap.add_argument(
        "--per-buy",
        action="store_true",
        help="After v9 sim (manifest+wallet run): print v9 buy ledger — sec, fill px, shares, post sz/avg, U/D, up%%.",
    )
    ap.add_argument(
        "--batch",
        action="store_true",
        help="Run batch: either --manifest rows or --public-snapshots-dir CSVs (see --max-windows)",
    )
    ap.add_argument("--max-windows", type=int, default=10, help="With --batch, max rows or snapshot files to run")
    ap.add_argument(
        "--legacy-unlimited-sim",
        action="store_true",
        help="Stress preset: huge budget and per-side cap (old harness; not realistic)",
    )
    ap.add_argument(
        "--budget-usdc",
        type=float,
        default=400.0,
        help="Sim spend cap per window (default 400; ignored with --legacy-unlimited-sim)",
    )
    ap.add_argument(
        "--max-shares-per-side",
        type=float,
        default=25.0,
        help="Max shares per leg (default 25; ignored with --legacy-unlimited-sim)",
    )
    ap.add_argument(
        "--taker-slip-usd",
        type=float,
        default=0.005,
        help="Add to each simulated fill price (FAK walk); 0 disables (default 0.5c)",
    )
    args = ap.parse_args()
    sim_params, try_buy_fn, sim_note = _sim_setup_from_args(args)

    if args.public_prices is not None:
        p = args.public_prices.resolve() if args.public_prices.is_absolute() else (REPO / args.public_prices).resolve()
        if not p.is_file():
            print(f"Public prices CSV not found: {p}", file=sys.stderr)
            return 2
        o = run_window_public_only(p, sim_params=sim_params, try_buy_fn=try_buy_fn)
        if o is None:
            print("v9-from-prices failed: need 900s tape with btc_price + btc_volume columns.", file=sys.stderr)
            return 2
        print(f"SIM CONSTRAINTS: {sim_note}", flush=True)
        for msg in collect_outcome_sanity_issues(o, budget=sim_params.budget_usdc):
            print(f"Sanity: {msg}", flush=True)
        _print_v9_only_window_report(o, slug=o.slug, print_ledger=True)
        print("\nDone. PALADIN v9 second sim (public tape only).")
        return 0

    if args.batch and args.public_snapshots_dir is not None:
        snap_dir = args.public_snapshots_dir if args.public_snapshots_dir.is_absolute() else (REPO / args.public_snapshots_dir).resolve()
        if not snap_dir.is_dir():
            print(f"Public snapshots directory not found: {snap_dir}", file=sys.stderr)
            return 2
        paths = discover_public_snapshot_paths(snap_dir, limit=max(1, args.max_windows))
        results: list[tuple[str, WindowSimOutcome]] = []
        sanity_pairs: list[tuple[str, str]] = []
        for p in paths:
            o = run_window_public_only(p, sim_params=sim_params, try_buy_fn=try_buy_fn)
            if o is None:
                print(f"skip {p.name}: missing/invalid 900s BTC ticks", file=sys.stderr)
                continue
            results.append((o.slug, o))
            for msg in collect_outcome_sanity_issues(o, budget=sim_params.budget_usdc):
                sanity_pairs.append((o.slug, msg))
        _print_public_snapshots_batch(
            results, snap_dir=snap_dir, requested_cap=args.max_windows, sim_note=sim_note
        )
        if sanity_pairs:
            print("Sanity warnings (first 10):", flush=True)
            for slug, msg in sanity_pairs[:10]:
                print(f"  {slug}: {msg}", flush=True)
            if len(sanity_pairs) > 10:
                print(f"  ... and {len(sanity_pairs) - 10} more", flush=True)
        else:
            print("Sanity: no spend-cap / fill-px issues across batch.", flush=True)
        if args.v9_ledger and results:
            for slug, o in results:
                print(f"\n{'#' * 20} {slug} {'#' * 20}")
                print_v9_buy_ledger(o.trades, o.ticks)
        print("Done (batch). PALADIN v9 second sim - no ML; kernel paladin_v7_step on recorded ticks.")
        return 0 if results else 1

    manifest = args.manifest if args.manifest.is_absolute() else (REPO / args.manifest).resolve()
    if not manifest.is_file():
        print(f"Manifest not found: {manifest}", file=sys.stderr)
        return 2

    if args.v9_from_prices:
        mr = load_manifest_row(REPO, manifest, args.slug)
        if mr is None:
            print(f"Slug not in manifest: {args.slug}", file=sys.stderr)
            return 2
        pp = _safe_path(REPO, mr["public_prices_csv"])
        if not pp.is_file():
            print(f"public_prices_csv not found: {pp}", file=sys.stderr)
            return 2
        o = run_window_public_only(pp, sim_params=sim_params, try_buy_fn=try_buy_fn)
        if o is None:
            print("v9-from-prices failed: invalid 900s tape or missing BTC columns.", file=sys.stderr)
            return 2
        slug = (mr.get("slug") or args.slug or o.slug).strip()
        print(f"SIM CONSTRAINTS: {sim_note}", flush=True)
        for msg in collect_outcome_sanity_issues(o, budget=sim_params.budget_usdc):
            print(f"Sanity: {msg}", flush=True)
        _print_v9_only_window_report(o, slug=slug, print_ledger=True)
        print("\nDone. PALADIN v9 second sim (manifest public_prices_csv only).")
        return 0

    if args.batch:
        rows = load_manifest_rows(manifest, max_rows=args.max_windows)
        results: list[tuple[str, WindowSimOutcome]] = []
        sanity_pairs: list[tuple[str, str]] = []
        for mr in rows:
            slug = mr.get("slug", "").strip()
            o = run_window(REPO, mr, sim_params=sim_params, try_buy_fn=try_buy_fn)
            if o is None:
                print(f"skip {slug}: missing/invalid 900s ticks or wallet CSV", file=sys.stderr)
                continue
            results.append((slug, o))
            for msg in collect_outcome_sanity_issues(o, budget=sim_params.budget_usdc):
                sanity_pairs.append((slug, msg))

        print("=" * 140)
        print(V9_STRATEGY_LINE)
        print(f"SIM CONSTRAINTS: {sim_note}")
        print(
            f"BATCH | windows={len(results)} (manifest cap {args.max_windows}) | "
            "wallet=chain replay | v9=second sim | settlement=proxy PM winner"
        )
        hdr = (
            "slug | win | w_buys | w_cost$ | w_pnl$ | w_ROI% | "
            "v9_buys | v9_cost$ | v9_pnl$ | v9_ROI% | v9_pnl - w_pnl$"
        )
        print(hdr)
        print("-" * len(hdr))
        sum_wp = sum_sp = sum_wc = sum_sc = 0.0
        w_rois: list[float] = []
        s_rois: list[float] = []
        for slug, o in results:
            w_roi = 100.0 * o.wp / o.wc if o.wc > 1e-9 else 0.0
            s_roi = 100.0 * o.sp / o.sc if o.sc > 1e-9 else 0.0
            w_rois.append(w_roi)
            s_rois.append(s_roi)
            dpnl = o.sp - o.wp
            sum_wp += o.wp
            sum_sp += o.sp
            sum_wc += o.wc
            sum_sc += o.sc
            print(
                f"{slug} | {o.winner:4s} | {o.w_fills:6d} | {o.wc:9.2f} | {o.wp:+8.2f} | {w_roi:+7.2f}% | "
                f"{o.s_fills:6d} | {o.sc:9.2f} | {o.sp:+8.2f} | {s_roi:+7.2f}% | {dpnl:+11.2f}"
            )
        if results:
            n = len(results)
            tot_w_roi = 100.0 * sum_wp / sum_wc if sum_wc > 1e-9 else 0.0
            tot_s_roi = 100.0 * sum_sp / sum_sc if sum_sc > 1e-9 else 0.0
            mean_w_roi = sum(w_rois) / n
            mean_s_roi = sum(s_rois) / n
            print("-" * len(hdr))
            print(
                f"{'TOTAL (sum $; ROI = sum_pnl/sum_cost)':32s} |      | "
                f"        | {sum_wc:9.2f} | {sum_wp:+8.2f} | {tot_w_roi:+7.2f}% | "
                f"        | {sum_sc:9.2f} | {sum_sp:+8.2f} | {tot_s_roi:+7.2f}% | "
                f"{sum_sp - sum_wp:+11.2f}"
            )
            print(
                f"{'AVG per window ($ cost / $ pnl)':32s} |      | "
                f"        | {sum_wc/n:9.2f} | {sum_wp/n:+8.2f} | {mean_w_roi:+7.2f}% | "
                f"        | {sum_sc/n:9.2f} | {sum_sp/n:+8.2f} | {mean_s_roi:+7.2f}% | "
                f"{(sum_sp - sum_wp)/n:+11.2f}"
            )
            w_wr, w_wn = _win_rate_pct([o.wp for _, o in results])
            s_wr, s_wn = _win_rate_pct([o.sp for _, o in results])
            print(
                f"Win rate (settled PnL > 0): wallet {w_wr:.1f}% ({w_wn}/{n})  |  "
                f"v9 {s_wr:.1f}% ({s_wn}/{n})"
            )
        if sanity_pairs:
            print("Sanity warnings (first 10):", flush=True)
            for slug, msg in sanity_pairs[:10]:
                print(f"  {slug}: {msg}", flush=True)
            if len(sanity_pairs) > 10:
                print(f"  ... and {len(sanity_pairs) - 10} more", flush=True)
        else:
            print("Sanity: no spend-cap / fill-px issues across batch.", flush=True)
        print("=" * 140)
        print("Done (batch). PALADIN v9 second sim - no ML; kernel paladin_v7_step on recorded ticks.")
        return 0 if results else 1

    mr = load_manifest_row(REPO, manifest, args.slug)
    if mr is None:
        print(f"Slug not in manifest: {args.slug}", file=sys.stderr)
        return 2

    o = run_window(REPO, mr, sim_params=sim_params, try_buy_fn=try_buy_fn)
    if o is None:
        print("Window failed (ticks or wallet).", file=sys.stderr)
        return 2

    print(f"SIM CONSTRAINTS: {sim_note}", flush=True)
    for msg in collect_outcome_sanity_issues(o, budget=sim_params.budget_usdc):
        print(f"Sanity: {msg}", flush=True)

    _print_single_window_report(o, slug=args.slug, heartbeat=args.heartbeat, per_buy=args.per_buy)
    print("\nDone. PALADIN v9 second sim - no ML; kernel paladin_v7_step on recorded ticks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
