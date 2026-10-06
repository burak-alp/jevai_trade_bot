"""regime.map.v1 (pre-registered in reports/remote/20261006T-regime-map-prereg.md): which strategy in which regime?

  python scripts/regime_map.py pool  [--hist data/hist/um]      -> run/regime_map/pool.json (daily top-20, PIT)
  python scripts/regime_map.py run   [--hist data/hist/um]      -> run/regime_map/result.json + printed tables

Universe: USDT-quoted, non-stable bases, top 20 by prior-day 1d quote volume (delisted included). Equal weight
1/20 per member, gross <= 1x, 6 bps per side on turnover, funding paid/received. Signals at bar close, earn the next
bar. Strategies: trend (sign of L-return) and Donchian breakout, daily and 4h, long/short and long-only (20).
Regimes from BTC 1d: causal r90 (trailing 90 d) and oracle (t-45 .. t+45), bull > +0.15, bear < -0.15.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jevbot.hist.catalog import list_files  # noqa: E402
from jevbot.research.data import load_funding  # noqa: E402

DAY = 86_400_000
H4 = 4 * 3_600_000
START, END = date(2021, 8, 1), date(2026, 9, 27)          # data grid (warm-up from August 2021)
EVAL0, SPLIT, EVAL1 = date(2021, 10, 1), date(2024, 4, 1), date(2026, 9, 27)
TOP, COST, BAND = 20, 0.0006, 0.15
STABLE = ("USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "UST", "EUR", "GBP", "AEUR")
OUT = Path("run/regime_map")


def ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


_NON_COIN: set[str] | None = None


def ok_symbol(s: str) -> bool:
    """USDT-quoted crypto: no stable bases, no TradFi/index contracts (exchangeInfo underlyingType != COIN, saved by
    ``non_coin.json``; delisted symbols predate TradFi listings and are crypto)."""
    global _NON_COIN
    if _NON_COIN is None:
        f = OUT / "non_coin.json"
        _NON_COIN = set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()
    return s.endswith("USDT") and not s[:-4].startswith(STABLE) and "_" not in s and s not in _NON_COIN


def load_bars(root: Path, symbols: set[str] | None, step: int, cols=("close", "high", "low", "quote_volume")):
    """{symbol: {col: array over the START..END grid at ``step``}} (NaN where missing)."""
    t0, n = ms(START), (ms(END) - ms(START)) // step
    out: dict[str, dict[str, np.ndarray]] = {}
    for f in list_files(root, symbols=symbols):
        sym = f.parent.name.split("=", 1)[-1]
        if not ok_symbol(sym):
            continue
        t = pq.read_table(f, columns=["open_time", *cols])
        idx = (t.column("open_time").to_numpy() - t0) // step
        good = (idx >= 0) & (idx < n)
        d = out.setdefault(sym, {c: np.full(n, np.nan) for c in cols})
        for c in cols:
            d[c][idx[good]] = t.column(c).to_numpy(zero_copy_only=False).astype(float)[good]
    return out


def make_pool(hist: Path) -> dict:
    daily = load_bars(hist / "klines" / "1d", None, DAY, ("close", "quote_volume"))
    syms = sorted(daily)
    qv = np.stack([np.nan_to_num(daily[s]["quote_volume"], nan=-1) for s in syms], axis=1)
    per_day, pool = {}, set()
    for i in range(1, qv.shape[0]):
        top = [syms[k] for k in np.argsort(-qv[i - 1], kind="stable")[:TOP] if qv[i - 1, k] > 0]
        per_day[date.fromordinal(START.toordinal() + i).isoformat()] = top
        pool.update(top)
    return {"top": TOP, "pool": sorted(pool), "per_day": per_day}


# ---------------------------------------------------------------- strategies on a bar grid
def positions(kind: str, C: np.ndarray, H: np.ndarray, L: np.ndarray, p: int, long_only: bool) -> np.ndarray:
    T, S = C.shape
    pos = np.zeros((T, S))
    if kind == "trend":
        with np.errstate(invalid="ignore", divide="ignore"):
            pos[p:] = np.sign(np.log(C[p:] / C[:-p]))
    else:                                                   # Donchian: enter on N-high/low, exit on N/2 opposite
        q = max(p // 2, 1)
        cur = np.zeros(S)
        for t in range(p, T):
            hi, lo = np.nanmax(H[t - p:t], axis=0), np.nanmin(L[t - p:t], axis=0)
            ex_lo, ex_hi = np.nanmin(L[t - q:t], axis=0), np.nanmax(H[t - q:t], axis=0)
            c = C[t]
            cur = np.where((cur > 0) & (c < ex_lo), 0, cur)
            cur = np.where((cur < 0) & (c > ex_hi), 0, cur)
            cur = np.where(c > hi, 1, np.where(c < lo, -1, cur))
            cur = np.where(np.isnan(c), 0, cur)
            pos[t] = cur
    pos = np.nan_to_num(pos)
    return np.clip(pos, 0, 1) if long_only else pos


def bar_pnl(pos: np.ndarray, C: np.ndarray, F: np.ndarray, member: np.ndarray) -> np.ndarray:
    """Portfolio simple return per bar t+1 (index t+1); weights 1/TOP on members with a price."""
    w = np.where(member & ~np.isnan(C), pos, 0.0) / TOP
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.nan_to_num(C[1:] / C[:-1] - 1)
    turn = np.abs(np.diff(np.vstack([np.zeros(w.shape[1]), w]), axis=0))
    pnl = np.zeros(len(C))
    pnl[1:] = (w[:-1] * r).sum(1) - (w[:-1] * F[1:]).sum(1) - COST * turn[:-1].sum(1)
    return pnl


def funding_grid(hist: Path, syms: list[str], step: int, T: int) -> np.ndarray:
    """Funding rate summed into the bar (t-1, t] it is paid in (paid at the end of bar index t)."""
    F = np.zeros((T, len(syms)))
    t0 = ms(START)
    for k, s in enumerate(syms):
        ft, fr = load_funding(hist, s)
        idx = np.ceil((ft - t0) / step).astype(int)
        good = (idx > 0) & (idx < T)
        np.add.at(F[:, k], idx[good], fr[good])
    return F


def run_grid(hist: Path, pool: dict, step: int, label: str, params: dict[str, tuple[int, ...]]):
    root = hist / "klines" / ("1d" if step == DAY else "4h")
    bars = load_bars(root, set(pool["pool"]), step, ("close", "high", "low"))
    syms = sorted(bars)
    C = np.stack([bars[s]["close"] for s in syms], 1)
    H = np.stack([bars[s]["high"] for s in syms], 1)
    Lo = np.stack([bars[s]["low"] for s in syms], 1)
    T = len(C)
    times = ms(START) + np.arange(T) * step
    member = np.zeros((T, len(syms)), bool)
    col = {s: k for k, s in enumerate(syms)}
    for i, t in enumerate(times):                           # member on the bar's UTC day (decided at the bar close)
        for s in pool["per_day"].get(datetime.fromtimestamp(t / 1000, timezone.utc).date().isoformat(), []):
            if s in col:
                member[i, col[s]] = True
    F = funding_grid(hist, syms, step, T)
    day_of_bar = (times - ms(START)) // DAY
    out = {}
    per = 1 if step == DAY else 6
    for kind, ps in params.items():
        for p in ps:
            for lo in (False, True):
                pos = positions(kind, C, H, Lo, p, lo)
                pnl = bar_pnl(pos, C, F, member)
                daily = np.zeros(int(day_of_bar.max()) + 1)
                np.add.at(daily, day_of_bar, np.log1p(pnl))
                name = f"{label}_{kind}_{p // per if kind == 'trend' else p}{'_L' if lo else '_LS'}"
                out[name] = np.expm1(daily)
    ref = {}
    if step == DAY:
        w = np.where(member & ~np.isnan(C), 1.0, 0.0)
        ref["ref_hold_universe"] = bar_pnl(w, C, F, member)
        b = col["BTCUSDT"]
        with np.errstate(invalid="ignore"):
            btc = np.r_[0, np.nan_to_num(C[1:, b] / C[:-1, b] - 1)]
        ref["ref_hold_btc"] = btc
        ref["_btc_close"] = C[:, b]
    return out, ref


# ---------------------------------------------------------------- evaluation
def ann(x: np.ndarray) -> float:
    return float(np.expm1(np.mean(np.log1p(x)) * 365)) if len(x) else float("nan")


def sharpe(x: np.ndarray) -> float:
    return float(x.mean() / x.std() * np.sqrt(365)) if len(x) > 2 and x.std() > 0 else float("nan")


def block_ci(x: np.ndarray, f, n: int = 2000, block: int = 7, seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    starts = np.arange(0, len(x) - block + 1)
    vals = []
    for _ in range(n):
        idx = (rng.choice(starts, nb)[:, None] + np.arange(block)).ravel()[: len(x)]
        vals.append(f(x[idx]))
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


def regimes(btc: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Label of return day d: causal = r90 known at the close of day d-1; oracle = centred 90 d return."""
    T = len(btc)
    lc = np.log(btc)
    causal = np.full(T, "", dtype=object)
    oracle = np.full(T, "", dtype=object)
    lab = lambda r: "bull" if r > BAND else "bear" if r < -BAND else "side"  # noqa: E731
    for d in range(T):
        if d - 1 - 90 >= 0 and np.isfinite(lc[d - 1]) and np.isfinite(lc[d - 91]):
            causal[d] = lab(lc[d - 1] - lc[d - 91])
        if d - 45 >= 0 and d + 45 < T and np.isfinite(lc[d + 45]) and np.isfinite(lc[d - 45]):
            oracle[d] = lab(lc[d + 45] - lc[d - 45])
    return causal, oracle


def switching(strats: dict[str, np.ndarray], lab: np.ndarray, fit: np.ndarray, test: np.ndarray):
    """Best Sharpe strategy per regime on ``fit`` days, applied on ``test`` days; switch cost 2 x COST x 1."""
    choice = {}
    for g in ("bull", "side", "bear"):
        m = fit & (lab == g)
        if m.sum() >= 30:
            choice[g] = max(strats, key=lambda k: sharpe(strats[k][m]))
    r = np.zeros(len(lab))
    prev = None
    for d in np.where(test)[0]:
        k = choice.get(lab[d])
        r[d] = strats[k][d] if k else 0.0
        if prev is not None and k != prev:
            r[d] -= 2 * COST
        prev = k
    return choice, r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pool", "run"])
    ap.add_argument("--hist", default="data/hist/um")
    a = ap.parse_args()
    hist = Path(a.hist)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "pool":
        pool = make_pool(hist)
        (OUT / "pool.json").write_text(json.dumps(pool), encoding="utf-8")
        print(f"pool {len(pool['pool'])} symbols over {len(pool['per_day'])} days")
        return 0
    pool = json.loads((OUT / "pool.json").read_text(encoding="utf-8"))
    d_s, d_ref = run_grid(hist, pool, DAY, "1d", {"trend": (10, 30, 90), "donchian": (20, 55)})
    h_s, _ = run_grid(hist, pool, H4, "4h", {"trend": (12, 30, 60), "donchian": (20, 55)})
    btc = d_ref.pop("_btc_close")
    T = len(btc)
    strats = {**d_s, **{k: v[:T] for k, v in h_s.items()}}
    days = np.array([date.fromordinal(START.toordinal() + i) for i in range(T)])
    causal, oracle = regimes(btc)
    ev = (days >= EVAL0) & (days < EVAL1)
    p1, p2 = ev & (days < SPLIT), ev & (days >= SPLIT)
    res: dict = {"window": [str(EVAL0), str(EVAL1)], "split": str(SPLIT), "regime_days": {}, "map": {}}
    for nm, lab in (("causal", causal), ("oracle", oracle)):
        res["regime_days"][nm] = {g: int((ev & (lab == g)).sum()) for g in ("bull", "side", "bear")}
    allk = {**strats, **d_ref}
    for k, x in allk.items():
        row = {"all": {"ann": ann(x[ev]), "sharpe": sharpe(x[ev])}}
        for nm, lab in (("causal", causal), ("oracle", oracle)):
            for g in ("bull", "side", "bear"):
                m = ev & (lab == g)
                row[f"{nm}_{g}"] = {"ann": ann(x[m]), "sharpe": sharpe(x[m])}
        res["map"][k] = row
    # out-of-sample test
    best1 = max(strats, key=lambda k: sharpe(strats[k][p1]))
    ch_c, sw_c = switching(strats, causal, p1, p2)
    ch_o, sw_o = switching(strats, oracle, p1, p2)
    diff = (sw_c - strats[best1])[p2]
    res["oos"] = {
        "single_best_p1": best1, "choice_causal": ch_c, "choice_oracle": ch_o,
        "p2_switch_causal": {"ann": ann(sw_c[p2]), "sharpe": sharpe(sw_c[p2]),
                             "ann_ci95": block_ci(sw_c[p2], ann)},
        "p2_single_best": {"ann": ann(strats[best1][p2]), "sharpe": sharpe(strats[best1][p2])},
        "p2_switch_oracle": {"ann": ann(sw_o[p2]), "sharpe": sharpe(sw_o[p2])},
        "p2_hold_universe": {"ann": ann(d_ref["ref_hold_universe"][p2]), "sharpe": sharpe(d_ref["ref_hold_universe"][p2])},
        "p2_hold_btc": {"ann": ann(d_ref["ref_hold_btc"][p2]), "sharpe": sharpe(d_ref["ref_hold_btc"][p2])},
        "diff_daily_mean_bps": float(diff.mean() * 1e4),
        "diff_ci95_bps": [v * 1e4 for v in block_ci(diff, np.mean)],
    }
    o = res["oos"]
    o["pass"] = bool(o["diff_ci95_bps"][0] > 0 and o["p2_switch_causal"]["ann_ci95"][0] > 0)
    (OUT / "result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print("regime days (eval):", res["regime_days"])
    hdr = ["all", "oracle_bull", "oracle_side", "oracle_bear", "causal_bull", "causal_side", "causal_bear"]
    print(f"{'strategy':22s}" + "".join(f"{h:>14s}" for h in hdr) + "   (annual return / Sharpe)")
    for k, row in res["map"].items():
        print(f"{k:22s}" + "".join(f"{row[h]['ann'] * 100:7.0f}%/{row[h]['sharpe']:5.2f}" for h in hdr))
    print(json.dumps(o, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
