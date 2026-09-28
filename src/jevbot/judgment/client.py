"""TypeSafe ``POST /v1/systemone`` client (spec §7.3-7.4), shadow edition.

* The API key is read from the ``JEV_API_KEY`` environment variable (on Windows also from the user
  environment in the registry, so a ``setx`` works without restarting the parent process). It is
  never logged, written or returned.
* ``ask`` never raises: every outcome is a result dict with a ``status``.
* Answers are validated: every asked question present, probabilities in [0, 1], choice
  probabilities summing to 1 within 0.02 (then normalised); otherwise ``invalid``.
* A simple breaker (5 consecutive failures -> 60 s pause) and a daily call budget.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from typing import Any, Callable

import httpx

from jevbot.core.logging import get_logger

log = get_logger(__name__)
DAY_MS = 86_400_000


def load_api_key(var: str = "JEV_API_KEY") -> str | None:
    key = os.environ.get(var)
    if key or sys.platform != "win32":
        return key or None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, var)[0]) or None
    except OSError:
        return None


def pick_model(models: list[dict[str, Any]], family: str = "jev") -> tuple[str | None, str]:
    """Only ``<family>-*`` models (the account also lists other products). Prefer the highest pinned
    version ``jev-X.Y.Z``; else the ``jev-latest`` alias (never ``preview``). Returns (name, pinning)."""
    names = [str(m["name"]) for m in models if str(m.get("name", "")).startswith(f"{family}-")]
    pinned = [n for n in names if re.fullmatch(rf"{family}-\d+(\.\d+)*", n)]
    if pinned:
        return max(pinned, key=lambda n: tuple(int(x) for x in n.split("-", 1)[1].split("."))), "versioned"
    if f"{family}-latest" in names:
        return f"{family}-latest", "alias_only"
    return None, "none"


def parse_answers(questions: dict[str, dict[str, Any]], body: dict[str, Any]) -> tuple[dict[str, float], str | None]:
    """Response answers -> flat probabilities (``q`` for noul, ``q.option`` for choice) or an error."""
    ans = body.get("answers")
    if not isinstance(ans, dict):
        return {}, "no answers"
    probs: dict[str, float] = {}
    for name, q in questions.items():
        a = ans.get(name)
        if not isinstance(a, dict):
            return {}, f"missing answer {name}"
        if q["type"] == "noul":
            p = a.get("noul")
            if not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
                return {}, f"bad noul {name}"
            probs[name] = float(p)
        elif q["type"] == "choice":
            pr = a.get("probabilities")
            opts = list(q["criteria"])
            if not isinstance(pr, dict) or any(not isinstance(pr.get(o), (int, float)) for o in opts):
                return {}, f"bad choice {name}"
            vals = [float(pr[o]) for o in opts]
            tot = sum(vals)
            if any(v < 0 or v > 1 for v in vals) or abs(tot - 1.0) >= 0.02:
                return {}, f"choice sum {tot:.3f} {name}"
            for o, v in zip(opts, vals):
                probs[f"{name}.{o}"] = v / tot
    return probs, None


class JevClient:
    def __init__(self, base_url: str = "https://api.typesafe.ai", api_key: str | None = None,
                 timeout_s: float = 30.0, max_concurrency: int = 8, max_calls_per_day: int = 2000,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        key = api_key if api_key is not None else load_api_key()
        if not key:
            raise RuntimeError("JEV_API_KEY is not set (setx JEV_API_KEY <key> in your own terminal)")
        self.http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport,
                                      headers={"Authorization": f"Bearer {key}"})
        self.sem = asyncio.Semaphore(max_concurrency)
        self.max_calls_per_day = max_calls_per_day
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._day, self._calls = -1, 0
        self._fails, self._open_until = 0, 0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def models(self) -> list[dict[str, Any]]:
        r = await self.http.get("/v1/models")
        r.raise_for_status()
        return list(r.json().get("models", []))

    async def ask(self, model: str, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        now = self.clock()
        if now // DAY_MS != self._day:
            self._day, self._calls = now // DAY_MS, 0
        if now < self._open_until:
            return {"status": "breaker", "t_sent": now}
        if self._calls >= self.max_calls_per_day:
            return {"status": "budget", "t_sent": now}
        self._calls += 1
        async with self.sem:
            t0 = self.clock()
            try:
                r = await self.http.post("/v1/systemone", json={"model": model, "state": state, "questions": questions})
                t1 = self.clock()
                if r.status_code != 200:
                    return self._fail({"status": "error", "http_status": r.status_code, "error": r.text[:300],
                                       "t_sent": t0, "t_received": t1, "latency_ms": t1 - t0})
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                t1 = self.clock()
                return self._fail({"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}",
                                   "t_sent": t0, "t_received": t1, "latency_ms": t1 - t0})
        probs, err = parse_answers(questions, body)
        self._fails = 0
        out = {"status": "invalid" if err else "ok", "model_returned": body.get("model"), "probs": probs,
               "usage": body.get("usage"), "t_sent": t0, "t_received": t1, "latency_ms": t1 - t0}
        if err:
            out["error"] = err
            out["raw"] = body
        return out

    def _fail(self, out: dict[str, Any]) -> dict[str, Any]:
        self._fails += 1
        if self._fails >= 5:
            self._open_until = self.clock() + 60_000
            log.warning("jev_breaker_open", fails=self._fails)
        return out
