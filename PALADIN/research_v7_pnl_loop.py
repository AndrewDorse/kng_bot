#!/usr/bin/env python3
"""
Long-run PALADIN v7 research: parameter mutations + STRATEGY_CORE-aligned try_buy hooks.

Periodic reports: baseline vs best champion on eval + optional validate set.

See PALADIN/STRATEGY_CORE.md — profit lock and min 5-share clips are respected.

Usage (2 hours, report every 10 minutes):
  python PALADIN/research_v7_pnl_loop.py --duration-seconds 7200 --report-interval-seconds 600

Quick test:
  python PALADIN/research_v7_pnl_loop.py --duration-seconds 120 --report-interval-seconds 60 --eval-windows 200
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
PAL = Path(__file__).resolve().parent
for p in (REPO, PAL):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from batch_paladin_v7_budget import (  # noqa: E402
    DEFAULT_EXPORTS,
    _minimal_bot_config_for_v7_defaults,
    paladin_v7_params_from_bot_config,
    pm_series_from_ticks,
    prepare_budget_batch_paths,
)
from paladin_v7 import PaladinV7Params, load_ticks_with_btc, run_window_v7  # noqa: E402
from simulate_paladin_window import (  # noqa: E402
    resolve_winner_from_last_prices,
    settled_pnl_usdc,
    try_buy,
)

TryBuyFn = Callable[..., float]


def baseline_params() -> PaladinV7Params:
    return paladin_v7_params_from_bot_config(_minimal_bot_config_for_v7_defaults())


def load_paths(exports_dir: Path, *, max_collect: int, min_max_elapsed: int) -> list[Path]:
    prep = prepare_budget_batch_paths(
        exports_dir=exports_dir,
        all_windows=False,
        max_windows=max_collect,
        min_max_elapsed=min_max_elapsed,
        pools=(max_collect,),
    )
    return prep.paths


def simulate_on_paths(
    paths: list[Path],
    params: PaladinV7Params,
    *,
    try_buy_fn: TryBuyFn | None = None,
) -> dict[str, Any]:
    pnls: list[float] = []
    orders: list[int] = []
    imbalanced = 0
    skipped = 0
    spent_list: list[float] = []
    for path in paths:
        slug, ticks = load_ticks_with_btc(path)
        if len(ticks) < 900 or not slug:
            skipped += 1
            continue
        st = run_window_v7(ticks, params=params, try_buy_fn=try_buy_fn)
        pm = pm_series_from_ticks(ticks)
        w, _, _ = resolve_winner_from_last_prices(pm)
        pnls.append(float(settled_pnl_usdc(st.snapshot_metrics(), w)))
        orders.append(len(st.trades))
        spent_list.append(float(st.spent_usdc))
        if abs(float(st.size_up) - float(st.size_down)) > 0.05:
            imbalanced += 1
    n = len(pnls)
    if n == 0:
        return {
            "n": 0,
            "total_pnl": 0.0,
            "mean_pnl": 0.0,
            "wr_pct": 0.0,
            "imb_pct": 0.0,
            "avg_orders": 0.0,
            "roi_proxy": 0.0,
        }
    total = float(sum(pnls))
    wins = sum(1 for x in pnls if x > 1e-6)
    spent = float(sum(spent_list)) + 1e-9
    return {
        "n": n,
        "total_pnl": total,
        "mean_pnl": total / n,
        "wr_pct": 100.0 * wins / n,
        "imb_pct": 100.0 * imbalanced / n,
        "avg_orders": float(sum(orders)) / n,
        "roi_proxy": total / spent,
    }


def try_buy_core_profit_lock(
    st: Any,
    *,
    t: int,
    side: str,
    shares: float,
    px: float,
    reason: str,
    budget: float,
    min_notional: float,
    min_shares: float,
    pm_u: float | None = None,
    pm_d: float | None = None,
) -> float:
    """STRATEGY_CORE: stop first/layer-2 adds when fully profitable; hedges always allowed."""
    snap = st.snapshot_metrics()
    su = float(snap["size_up"])
    sd = float(snap["size_down"])
    ru = float(snap["roi_up"])
    rd = float(snap["roi_dn"])
    pu = float(snap["pnl_if_up_usdc"])
    pd = float(snap["pnl_if_down_usdc"])
    roi_lock = su >= 19.5 and sd >= 19.5 and ru >= 0.10 and rd >= 0.10
    dollar_lock = su >= 19.5 and sd >= 19.5 and pu >= 5.0 and pd >= 5.0
    if (roi_lock or dollar_lock) and reason in (
        "v7_first_binance_spike",
        "v7_layer2_dip_lead",
        "v7_layer2_lowvwap_dip",
        "v7_imbalance_repair",
    ):
        return 0.0
    return float(
        try_buy(
            st,
            t=t,
            side=side,
            shares=shares,
            px=px,
            reason=reason,
            budget=budget,
            min_notional=min_notional,
            min_shares=min_shares,
            pm_u=pm_u,
            pm_d=pm_d,
        )
    )


def try_buy_skip_refill(
    st: Any,
    *,
    t: int,
    side: str,
    shares: float,
    px: float,
    reason: str,
    budget: float,
    min_notional: float,
    min_shares: float,
    pm_u: float | None = None,
    pm_d: float | None = None,
) -> float:
    if reason in ("v7_layer2_dip_lead", "v7_layer2_lowvwap_dip", "v7_imbalance_repair"):
        return 0.0
    return float(
        try_buy(
            st,
            t=t,
            side=side,
            shares=shares,
            px=px,
            reason=reason,
            budget=budget,
            min_notional=min_notional,
            min_shares=min_shares,
            pm_u=pm_u,
            pm_d=pm_d,
        )
    )


def try_buy_profit_lock_no_refill(
    st: Any,
    *,
    t: int,
    side: str,
    shares: float,
    px: float,
    reason: str,
    budget: float,
    min_notional: float,
    min_shares: float,
    pm_u: float | None = None,
    pm_d: float | None = None,
) -> float:
    if reason in ("v7_layer2_dip_lead", "v7_layer2_lowvwap_dip", "v7_imbalance_repair"):
        return 0.0
    return try_buy_core_profit_lock(
        st,
        t=t,
        side=side,
        shares=shares,
        px=px,
        reason=reason,
        budget=budget,
        min_notional=min_notional,
        min_shares=min_shares,
        pm_u=pm_u,
        pm_d=pm_d,
    )


def try_buy_slip_pct(pct: float) -> TryBuyFn:
    def wrapped(
        st: Any,
        *,
        t: int,
        side: str,
        shares: float,
        px: float,
        reason: str,
        budget: float,
        min_notional: float,
        min_shares: float,
        pm_u: float | None = None,
        pm_d: float | None = None,
    ) -> float:
        sh2 = max(0.0, float(shares) * (1.0 - pct))
        if sh2 + 1e-9 < min_shares:
            return 0.0
        return float(
            try_buy(
                st,
                t=t,
                side=side,
                shares=sh2,
                px=px,
                reason=reason,
                budget=budget,
                min_notional=min_notional,
                min_shares=min_shares,
                pm_u=pm_u,
                pm_d=pm_d,
            )
        )

    return wrapped


TRY_BUY_REGISTRY: dict[str, TryBuyFn | None] = {
    "": None,
    "profit_lock": try_buy_core_profit_lock,
    "skip_refill": try_buy_skip_refill,
    "profit_lock_no_refill": try_buy_profit_lock_no_refill,
    "slip1": try_buy_slip_pct(0.01),
    "slip2": try_buy_slip_pct(0.02),
}


def resolve_try_buy(key: str) -> TryBuyFn | None:
    if key not in TRY_BUY_REGISTRY:
        return None
    return TRY_BUY_REGISTRY[key]


def score(m: dict[str, Any]) -> float:
    if m["n"] <= 0:
        return -1e18
    return (
        float(m["mean_pnl"])
        + 0.004 * float(m["wr_pct"])
        + 0.25 * float(m["roi_proxy"])
        - 0.015 * float(m["imb_pct"])
    )


def experiment_definitions() -> list[tuple[str, PaladinV7Params, str]]:
    """(label, full params, try_buy_key)."""
    b = baseline_params()
    rows: list[tuple[str, PaladinV7Params, str]] = []

    rows.append(("BASELINE", b, ""))
    rows.append(("core_profit_lock_STRATEGY_CORE", b, "profit_lock"))
    rows.append(("core_no_refill", b, "skip_refill"))
    rows.append(("core_profit_lock_no_refill", b, "profit_lock_no_refill"))
    rows.append(("exec_slip1pct", b, "slip1"))
    rows.append(("exec_slip2pct", b, "slip2"))

    for m in (0.03, 0.04, 0.05):
        rows.append((f"cheap_other_margin_{m}", replace(b, cheap_other_margin=float(m)), ""))
    for c in (0.98, 0.985, 0.995):
        rows.append((f"cheap_pair_sum_max_{c}", replace(b, cheap_pair_sum_max=float(c)), ""))
    for f in (1.1, 1.2, 1.28):
        rows.append((f"forced_hedge_max_{f}", replace(b, forced_hedge_max_book_sum=float(f)), ""))
    for r in (2.2, 2.5, 3.0):
        rows.append((f"vol_spike_ratio_{r}", replace(b, volume_spike_ratio=float(r)), ""))
    for mv in (1.5, 2.5, 3.5):
        rows.append((f"btc_move_min_{mv}", replace(b, btc_abs_move_min_usd=float(mv)), ""))
    for fl in (0.58, 0.62, 0.64):
        rows.append((f"first_leg_max_pm_{fl}", replace(b, first_leg_max_pm=float(fl)), ""))
    for ht in (60.0, 90.0, 120.0):
        rows.append((f"hedge_timeout_{ht}", replace(b, hedge_timeout_seconds=float(ht)), ""))
    for dip in (0.03, 0.05, 0.07):
        rows.append((f"layer2_dip_{dip}", replace(b, layer2_dip_below_avg=float(dip)), ""))
    for cd in (0.0, 20.0, 35.0):
        rows.append((f"pair_cooldown_{cd}", replace(b, pair_cooldown_sec=float(cd)), ""))
    for bo in (5.0, 7.0, 8.0):
        rows.append((f"base_order_{bo}", replace(b, base_order_shares=float(bo)), ""))
    for mx in (10.0, 15.0, 20.0):
        rows.append((f"max_side_{mx}", replace(b, max_shares_per_side=float(mx)), ""))

    rows.append(("pair_tight_cheap_loose_force", replace(b, cheap_other_margin=0.035, forced_hedge_max_book_sum=1.22), ""))
    rows.append(("pair_high_spike_tight_first", replace(b, volume_spike_ratio=2.8, first_leg_max_pm=0.58), ""))
    rows.append(("pair_base6_profit_lock", replace(b, base_order_shares=6.0), "profit_lock"))
    rows.append(("pair_vol28_first058_lock", replace(b, volume_spike_ratio=2.8, first_leg_max_pm=0.58), "profit_lock"))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-seconds", type=int, default=7200)
    ap.add_argument("--report-interval-seconds", type=int, default=600)
    ap.add_argument(
        "--eval-windows",
        type=int,
        default=180,
        help="Smaller = faster waves (each experiment runs a full replay per window).",
    )
    ap.add_argument("--validate-windows", type=int, default=800)
    ap.add_argument("--max-collect", type=int, default=900)
    ap.add_argument("--min-max-elapsed", type=int, default=800)
    ap.add_argument("--exports-dir", type=Path, default=DEFAULT_EXPORTS)
    ap.add_argument("--session-dir", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(int(args.seed))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    session_dir = args.session_dir or (REPO / "exports" / f"v7_research_session_{ts}")
    session_dir.mkdir(parents=True, exist_ok=True)
    leaderboard_path = session_dir / "leaderboard.jsonl"

    all_paths = load_paths(args.exports_dir, max_collect=int(args.max_collect), min_max_elapsed=int(args.min_max_elapsed))
    if len(all_paths) < int(args.eval_windows):
        print(f"ERROR: only {len(all_paths)} paths; need >= eval-windows", file=sys.stderr)
        return 2

    rng.shuffle(all_paths)
    eval_paths = all_paths[: int(args.eval_windows)]
    val_n = int(args.validate_windows)
    validate_paths = all_paths[:val_n] if val_n > 0 else []

    experiments = experiment_definitions()
    bparams = baseline_params()

    by_label: dict[str, dict[str, Any]] = {}
    start = time.monotonic()
    end = start + float(args.duration_seconds)
    next_report = start + float(args.report_interval_seconds)
    wave = 0
    report_idx = 0

    def upsert_row(label: str, p: PaladinV7Params, tb_key: str, m: dict[str, Any]) -> None:
        sc = score(m)
        row = {
            "label": label,
            "try_buy_key": tb_key,
            "score": sc,
            "params": asdict(p),
            **m,
        }
        old = by_label.get(label)
        if old is None or sc > float(old["score"]):
            by_label[label] = row
        with leaderboard_path.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps({"wave": wave, "row": row}) + "\n")

    def leaderboard_sorted() -> list[dict[str, Any]]:
        return sorted(by_label.values(), key=lambda r: float(r["score"]), reverse=True)

    print(f"session_dir={session_dir}", flush=True)
    print(f"eval={len(eval_paths)} validate={len(validate_paths)} static_experiments={len(experiments)}", flush=True)

    while time.monotonic() < end:
        wave += 1
        shift = (wave * 19) % max(1, len(all_paths) - len(eval_paths))
        eval_paths_w = all_paths[shift : shift + len(eval_paths)]
        if len(eval_paths_w) < len(eval_paths):
            eval_paths_w = all_paths[: len(eval_paths)]

        # Wave 1: full static grid (expensive once). Later waves: random mutations only (faster).
        exp_batch = experiments if wave == 1 else []
        for label, p, tb_key in exp_batch:
            if time.monotonic() >= end:
                break
            fn = resolve_try_buy(tb_key)
            m = simulate_on_paths(eval_paths_w, p, try_buy_fn=fn)
            upsert_row(label, p, tb_key, m)

        n_rand = 36 if wave > 1 else 18
        for _ in range(n_rand):
            if time.monotonic() >= end:
                break
            p = bparams
            if rng.random() < 0.45:
                p = replace(p, cheap_other_margin=rng.choice([0.03, 0.035, 0.04, 0.045]))
            if rng.random() < 0.45:
                p = replace(p, volume_spike_ratio=rng.choice([2.0, 2.3, 2.5, 2.8, 3.1]))
            if rng.random() < 0.35:
                p = replace(p, first_leg_max_pm=rng.choice([0.55, 0.58, 0.60, 0.62, 0.64]))
            if rng.random() < 0.35:
                p = replace(p, forced_hedge_max_book_sum=rng.choice([1.1, 1.15, 1.2, 1.25, 1.3]))
            if rng.random() < 0.3:
                p = replace(p, hedge_timeout_seconds=float(rng.choice([60, 75, 90, 105, 120])))
            if rng.random() < 0.25:
                p = replace(p, base_order_shares=float(rng.choice([5, 6, 7, 8])))
            tb_key = ""
            rdraw = rng.random()
            if rdraw < 0.14:
                tb_key = "profit_lock"
            elif rdraw < 0.22:
                tb_key = "skip_refill"
            elif rdraw < 0.28:
                tb_key = "profit_lock_no_refill"
            lab = f"rand_w{wave}_{rng.randint(10000, 99999)}"
            fn = resolve_try_buy(tb_key)
            m = simulate_on_paths(eval_paths_w, p, try_buy_fn=fn)
            upsert_row(lab, p, tb_key, m)

        # cap random junk: keep BASELINE + all non-rand + top 80 rand by score
        lb = leaderboard_sorted()
        rand_rows = [r for r in lb if str(r["label"]).startswith("rand_")]
        rest = [r for r in lb if not str(r["label"]).startswith("rand_")]
        rand_rows.sort(key=lambda r: float(r["score"]), reverse=True)
        keep_rand = rand_rows[:80]
        by_label.clear()
        for r in rest + keep_rand:
            by_label[str(r["label"])] = r

        now = time.monotonic()
        if now >= next_report:
            lb = leaderboard_sorted()
            best = lb[0] if lb else None
            mb = simulate_on_paths(eval_paths_w, bparams, try_buy_fn=None)

            lines: list[str] = []
            lines.append(f"# PALADIN v7 research report #{report_idx}")
            lines.append(f"utc={datetime.now(timezone.utc).isoformat()}")
            lines.append(f"session_dir={session_dir}")
            lines.append(f"eval_windows={len(eval_paths_w)}  wave={wave}")
            lines.append("")
            lines.append("## Baseline (eval set)")
            lines.append(json.dumps({"label": "BASELINE", **mb, "params": asdict(bparams)}, indent=2))
            lines.append("")

            if best and best["label"] != "BASELINE":
                p_best = PaladinV7Params(**{k: v for k, v in best["params"].items()})
                fn_best = resolve_try_buy(str(best.get("try_buy_key", "")))
                ch = simulate_on_paths(eval_paths_w, p_best, try_buy_fn=fn_best)
                lines.append(f"## Champion: {best['label']} (eval re-sim)")
                lines.append(json.dumps({**ch, "try_buy_key": best.get("try_buy_key"), "score": best["score"]}, indent=2))
                lines.append("")
                if validate_paths:
                    mb_v = simulate_on_paths(validate_paths, bparams, try_buy_fn=None)
                    ch_v = simulate_on_paths(validate_paths, p_best, try_buy_fn=fn_best)
                    lines.append("## Validate (first N paths, same ordering as batch harness)")
                    lines.append(json.dumps({"baseline": mb_v, "champion": ch_v}, indent=2))
                    lines.append("")

            lines.append("## Top 10 (composite score)")
            for i, r in enumerate(lb[:10], 1):
                lines.append(
                    f"{i}. {r['label']}\tscore={r['score']:.4f}\tmean_pnl={r['mean_pnl']:.4f}\twr={r['wr_pct']:.1f}%"
                    f"\troi_proxy={r['roi_proxy']:.4f}\timb={r['imb_pct']:.1f}%\ttb={r.get('try_buy_key','')}\tn={r['n']}"
                )

            rep_path = session_dir / f"report_{report_idx:03d}.md"
            rep_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"Wrote {rep_path}", flush=True)
            report_idx += 1
            next_report += float(args.report_interval_seconds)

        time.sleep(0.02)

    lb = leaderboard_sorted()
    top_path = session_dir / "FINAL_TOP10.md"
    tlines = ["# FINAL top 10", ""]
    for i, r in enumerate(lb[:10], 1):
        tlines.append(
            f"{i}. **{r['label']}** | score={r['score']:.4f} | mean_pnl=${r['mean_pnl']:.4f} | "
            f"wr={r['wr_pct']:.1f}% | roi_proxy={r['roi_proxy']:.4f} | imb={r['imb_pct']:.1f}% | try_buy={r.get('try_buy_key','')}"
        )
    top_path.write_text("\n".join(tlines) + "\n", encoding="utf-8")
    print(f"Done {top_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
