"""Burst load test: fake exchange (N processes) -> real recorder process -> phase analysis.

Success is not "no crash": for every burst the recorder must keep up with the offered rate,
feed latency must stay bounded and must not trend upward through the phase, the sink
backlog must stay bounded, and after the burst latency must return to baseline.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import psutil

from jevbot.core.time import now_ms
from jevbot.recorder.integrity import verify_tree
from jevbot.recorder.report import _read


@dataclass
class Phase:
    name: str
    rate: float                     # bookTicker msgs/s offered across all symbols
    duration_s: float
    kind: str = "burst"             # burst | recover | baseline
    drop_at_s: float | None = None  # drop every WS connection this far into the phase


DEFAULT_PHASES = [
    Phase("baseline", 2_000, 60, "baseline"),
    Phase("burst_5k_5min", 5_000, 300),
    Phase("recover_1", 2_000, 45, "recover"),
    Phase("burst_15k_2min", 15_000, 120),
    Phase("recover_2", 2_000, 45, "recover"),
    Phase("burst_30k_30s", 30_000, 30),
    Phase("recover_3", 2_000, 60, "recover"),
    Phase("burst_15k_drop", 15_000, 60, drop_at_s=30),
    Phase("recover_4", 2_000, 45, "recover"),
]

QUICK_PHASES = [
    Phase("baseline", 2_000, 20, "baseline"),
    Phase("burst_5k", 5_000, 30),
    Phase("recover_1", 2_000, 20, "recover"),
    Phase("burst_15k_drop", 15_000, 30, drop_at_s=15),
    Phase("recover_2", 2_000, 20, "recover"),
]


@dataclass
class Criteria:
    keep_up_ratio_min: float = 0.97          # received / offered bookTicker rate
    lat_p99_max_ms: float = 1000.0           # at the end of a burst
    lat_growth_max_ms: float = 250.0         # last third vs first third of a burst
    recover_lat_p99_ms: float = 250.0        # end of a recovery phase
    backlog_rows_max: int = 200_000          # sink buffered rows
    writer_queue_max: int = 50
    reconnect_max_s: float = 5.0


@dataclass
class Harness:
    workdir: Path
    phases: list[Phase] = field(default_factory=lambda: list(DEFAULT_PHASES))
    fake_workers: int = 3
    symbols: int = 200
    ws_port: int = 28766
    http_port: int = 28765
    rotate_s: float = 30.0
    health_interval_s: float = 2.0
    criteria: Criteria = field(default_factory=Criteria)
    python: str = sys.executable

    def _ctl(self, **kv: Any) -> None:
        path = self.workdir / "control.json"
        cur = orjson.loads(path.read_bytes()) if path.exists() else {}
        cur.update(kv)
        tmp = path.with_name("control.json.tmp")
        tmp.write_bytes(orjson.dumps(cur))
        os.replace(tmp, path)

    def _fake_sent(self) -> int:
        total = 0
        for f in (self.workdir / "fake_stats").glob("fake-*.json"):
            try:
                total += orjson.loads(f.read_bytes()).get("book_msgs", 0)
            except (OSError, orjson.JSONDecodeError):
                pass
        return total

    def run(self) -> dict[str, Any]:
        wd = self.workdir
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "fake_stats").mkdir(exist_ok=True)
        self._ctl(book_rate_total=self.phases[0].rate, drop_epoch=0, silence_until=0)
        fakes = [subprocess.Popen(
            [self.python, "-m", "jevbot.cli", "fake-exchange", "--symbols", str(self.symbols),
             "--book-rate", str(self.phases[0].rate), "--ws-port", str(self.ws_port),
             "--http-port", str(self.http_port), "--reuse-port", "--control-file", str(wd / "control.json"),
             "--stats-file", str(wd / "fake_stats" / f"fake-{i}.json")],
            stdout=open(wd / f"fake-{i}.log", "wb"), stderr=subprocess.STDOUT) for i in range(self.fake_workers)]
        time.sleep(2.0)
        overlay = wd / "overlay.yaml"
        overlay.write_text(
            f"data_dir: {wd / 'data'}\nrun_dir: {wd / 'run'}\n"
            f"logging: {{json: true, file: {wd / 'recorder.log'}}}\n"
            f"binance:\n  rest_base: http://127.0.0.1:{self.http_port}\n  ws:\n    base: ws://127.0.0.1:{self.ws_port}\n"
            f"universe:\n  min_quote_vol_24h: 1000000\n  refresh_s: 3600\n"
            f"recorder:\n  health_interval_s: {self.health_interval_s}\n  health_log_interval_s: 30\n"
            f"  smoke:\n    first_event_timeout_s: 15\n"
            f"sink:\n  rotate_s: {self.rotate_s}\n  late_grace_s: 10\n")
        cfg_base = str(Path(__file__).resolve().parents[3] / "config" / "base.yaml")
        rec = subprocess.Popen([self.python, "-m", "jevbot.cli", "record", "--config", cfg_base, "--config",
                                str(overlay)], stdout=subprocess.DEVNULL, stderr=open(wd / "recorder.stderr", "wb"))
        timeline: list[dict[str, Any]] = []
        try:
            self._wait_healthy(wd / "run" / "recorder_health.json", 90)
            for ph in self.phases:
                self._ctl(book_rate_total=ph.rate)
                start, sent0 = now_ms(), self._fake_sent()
                if ph.drop_at_s is not None:
                    time.sleep(ph.drop_at_s)
                    epoch = orjson.loads((wd / "control.json").read_bytes())["drop_epoch"] + 1
                    self._ctl(drop_epoch=epoch)
                    time.sleep(ph.duration_s - ph.drop_at_s)
                else:
                    time.sleep(ph.duration_s)
                end, sent1 = now_ms(), self._fake_sent()
                timeline.append({"phase": ph.name, "kind": ph.kind, "rate": ph.rate, "start": start, "end": end,
                                 "offered_book_msgs": sent1 - sent0,
                                 "offered_book_rate": round((sent1 - sent0) / ((end - start) / 1000), 1),
                                 "drop_at": start + int(ph.drop_at_s * 1000) if ph.drop_at_s else None,
                                 "recorder_alive": rec.poll() is None})
                if rec.poll() is not None:
                    break
        finally:
            crashed = rec.poll() is not None
            if not crashed:
                rec.send_signal(signal.SIGTERM)
            try:
                code = rec.wait(120)
            except subprocess.TimeoutExpired:
                rec.kill()
                code = -9
            for f in fakes:
                f.send_signal(signal.SIGTERM)
            for f in fakes:
                try:
                    f.wait(10)
                except subprocess.TimeoutExpired:
                    f.kill()
        report = self.analyze(timeline, code, crashed)
        (wd / "loadtest_report.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2, default=str))
        (wd / "loadtest_report.md").write_text(to_markdown(report))
        return report

    @staticmethod
    def _wait_healthy(path: Path, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if orjson.loads(path.read_bytes()).get("status") == "HEALTHY":
                    return
            except (OSError, orjson.JSONDecodeError):
                pass
            time.sleep(0.5)
        raise RuntimeError("recorder did not become HEALTHY")

    def analyze(self, timeline: list[dict[str, Any]], exit_code: int, crashed_early: bool) -> dict[str, Any]:
        raw = self.workdir / "data" / "raw"
        health = sorted(_read(raw, "health", None, None).to_pylist(), key=lambda r: r["t"])
        gaps = _read(raw, "gaps", None, None).to_pylist()
        c = self.criteria
        phases_out = []
        all_ok = True
        for ph in timeline:
            rows = [h for h in health if ph["start"] + 4000 <= h["t"] <= ph["end"] and h["status"] != "STOPPED"]
            if not rows:
                phases_out.append({**ph, "ok": False, "reason": "no health rows"})
                all_ok = False
                continue
            n = len(rows)
            first, last = rows[: max(1, n // 3)], rows[-max(1, n // 3):]

            def mean(xs: list[float]) -> float:
                xs = [x for x in xs if x == x]
                return round(sum(xs) / len(xs), 2) if xs else float("nan")
            recv_public = mean([h["msgs_per_s_public"] for h in rows])
            offered = ph["offered_book_rate"]
            keep_up = round(recv_public / offered, 3) if offered else None
            lat_first, lat_last = mean([h["lat_p99_ms"] for h in first]), mean([h["lat_p99_ms"] for h in last])
            m = {
                "recv_msgs_per_s_total": mean([h["msgs_per_s"] for h in rows]),
                "recv_msgs_per_s_public": recv_public, "keep_up_ratio": keep_up,
                "cpu_pct_mean": mean([h["cpu_pct"] for h in rows]), "cpu_pct_max": max(h["cpu_pct"] for h in rows),
                "rss_mb_max": max(h["rss_mb"] for h in rows),
                "lat_p50_ms": mean([h["lat_p50_ms"] for h in rows]), "lat_p95_ms": mean([h["lat_p95_ms"] for h in rows]),
                "lat_p99_first_third": lat_first, "lat_p99_last_third": lat_last,
                "lat_p99_max": max(h["lat_p99_ms"] for h in rows), "lat_max_ms": max(h["lat_max_ms"] for h in rows),
                "loop_lag_p99_max": max(h["loop_lag_p99_ms"] for h in rows),
                "loop_lag_max": max(h["loop_lag_max_ms"] for h in rows),
                "sink_backlog_rows_max": max(h["sink_buffered_rows"] for h in rows),
                "writer_queue_max": max(h["queue_depth"] for h in rows),
                "writer_rows_per_s": mean([h["writer_rows_per_s"] for h in rows]),
                "open_files_max": max(h["open_files"] for h in rows),
                "duplicates": rows[-1]["duplicates_total"] - rows[0]["duplicates_total"],
                "invalid": rows[-1]["invalid_total"] - rows[0]["invalid_total"],
                "schema_errors": rows[-1]["schema_errors_total"] - rows[0]["schema_errors_total"],
                "statuses": sorted({h["status"] for h in rows}),
            }
            checks: dict[str, bool] = {}
            if ph["kind"] == "burst":
                checks["keeps_up"] = keep_up is not None and keep_up >= c.keep_up_ratio_min
                checks["lat_p99_bounded"] = lat_last < c.lat_p99_max_ms
                checks["lat_not_growing"] = lat_last <= lat_first + c.lat_growth_max_ms
                checks["backlog_bounded"] = m["sink_backlog_rows_max"] < c.backlog_rows_max and \
                    m["writer_queue_max"] < c.writer_queue_max
            else:
                checks["recovered"] = mean([h["lat_p99_ms"] for h in rows[-2:]]) < c.recover_lat_p99_ms
            checks["no_schema_errors"] = m["schema_errors"] == 0
            if ph.get("drop_at"):
                downs = [g for g in gaps if g["kind"] == "ws_disconnect" and abs(g["t_start"] - ph["drop_at"]) < 5000]
                rt = max(((g["t_end"] - g["t_start"]) / 1000 for g in downs), default=None)
                m["reconnect_s_max"] = rt
                m["streams_reconnected"] = len(downs)
                checks["reconnected_fast"] = rt is not None and rt < c.reconnect_max_s
            ok = all(checks.values()) and ph["recorder_alive"]
            all_ok &= ok
            phases_out.append({**ph, **m, "checks": checks, "ok": ok})
        v = verify_tree(raw)
        glob_checks = {"recorder_exit_0": exit_code == 0 and not crashed_early,
                       "integrity_ok": v.ok, "no_orphans": not v.orphans_tmp}
        return {"ok": all_ok and all(glob_checks.values()), "global": glob_checks, "exit_code": exit_code,
                "integrity": {"files_ok": v.files_ok, "rows_ok": v.rows_ok}, "phases": phases_out,
                "criteria": self.criteria.__dict__, "machine": {"cpus": psutil.cpu_count(),
                                                                "fake_workers": self.fake_workers}}


def to_markdown(rep: dict[str, Any]) -> str:
    L = [f"# Burst load test — {'PASS' if rep['ok'] else 'FAIL'}", "",
         f"global: {rep['global']}  ·  exit code {rep['exit_code']}  ·  integrity {rep['integrity']}", "",
         "| phase | offered/s | recv public/s | keep-up | CPU mean/max % | RSS max MB | lat p50 / p95 ms | "
         "p99 first→last ms | p99 max | loop lag p99 max | backlog max | writer q max | writer rows/s | "
         "dup / invalid | reconnect s | result |",
         "|" + "---|" * 16]
    for p in rep["phases"]:
        if "checks" not in p:
            L.append(f"| {p['phase']} | – | – | – | – | – | – | – | – | – | – | – | – | – | – | FAIL ({p.get('reason')}) |")
            continue
        failed = [k for k, v in p["checks"].items() if not v]
        L.append(
            f"| {p['phase']} | {p['offered_book_rate']} | {p['recv_msgs_per_s_public']} | {p['keep_up_ratio']} | "
            f"{p['cpu_pct_mean']}/{p['cpu_pct_max']} | {p['rss_mb_max']} | {p['lat_p50_ms']} / {p['lat_p95_ms']} | "
            f"{p['lat_p99_first_third']}→{p['lat_p99_last_third']} | {p['lat_p99_max']} | {p['loop_lag_p99_max']} | "
            f"{p['sink_backlog_rows_max']} | {p['writer_queue_max']} | {p['writer_rows_per_s']} | "
            f"{p['duplicates']} / {p['invalid']} | {p.get('reconnect_s_max', '–')} | "
            f"{'PASS' if p['ok'] else 'FAIL ' + ','.join(failed)} |")
    return "\n".join(L) + "\n"
