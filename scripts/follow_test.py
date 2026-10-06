"""follow.v1 (pre-registered in reports/remote/20261006T-follow-prereg.md): alpha of 13F / congress copy ETFs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jevbot.research.macro import CASH, OUT, block_ci, fetch  # noqa: E402

TESTS = {"GURU": ["SPY", "QQQ", "IWM"], "NANC": ["SPY", "QQQ"]}


def series(data, t):
    return data[t]


def main() -> int:
    f = OUT / "prices_follow.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {
        t: fetch(t) for t in ["GURU", "NANC", "SPY", "QQQ", "IWM", CASH]}
    f.write_text(json.dumps(data), encoding="utf-8")
    res = {}
    for y, xs in TESTS.items():
        days = sorted(set(data[y]) & set(data["SPY"]) & set.intersection(*[set(data[x]) for x in xs]))
        irx = sorted(data[CASH].items())
        cash, j, last = [], 0, 0.0
        for d in days:
            while j < len(irx) and irx[j][0] <= d:
                last = irx[j][1]
                j += 1
            cash.append(last / 100 / 252)
        cash = np.array(cash[1:])
        r = {t: np.diff(np.log([data[t][d] for d in days])) for t in [y] + xs}
        Y = np.expm1(r[y]) - cash
        X = np.column_stack([np.ones(len(Y))] + [np.expm1(r[x]) - cash for x in xs])

        def alpha(idx):
            b, *_ = np.linalg.lstsq(X[idx], Y[idx], rcond=None)
            return b[0] * 252

        b, *_ = np.linalg.lstsq(X, Y, rcond=None)
        rng = np.random.default_rng(0)
        n, blk = len(Y), 20
        st = np.arange(n - blk + 1)
        boots = [alpha((rng.choice(st, int(np.ceil(n / blk)))[:, None] + np.arange(blk)).ravel()[:n]) for _ in range(2000)]
        ann = lambda x: float(np.prod(1 + x) ** (252 / len(x)) - 1)  # noqa: E731
        res[y] = {"from": days[0], "to": days[-1], "days": n, "alpha_ann": float(b[0] * 252),
                  "alpha_ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
                  "betas": {x: round(float(v), 3) for x, v in zip(xs, b[1:])},
                  "ann_return": ann(np.expm1(r[y])), "spy_ann_return": ann(np.expm1(r["SPY"]))}
        res[y]["pass"] = res[y]["alpha_ci95"][0] > 0
    (OUT / "result_follow.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
