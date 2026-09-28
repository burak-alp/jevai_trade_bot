"""Small, local retrieval layer for JevAI docs, reports and source code.

No model, API key or network call. The bounded output is intended as evidence
for an assistant prompt; retrieved text is data, never an instruction.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "run" / "rag" / "index.sqlite"
MAX_CHUNK = 1000


def source_files(root: Path):
    for name in ("README.md", "LOCAL_AGENT.md"):
        p = root / name
        if p.is_file():
            yield p
    for folder, suffixes in (("docs", {".md"}), ("reports", {".md"}),
                             ("src", {".py"}), ("config", {".yaml", ".yml"}),
                             ("scripts", {".py", ".ps1"})):
        base = root / folder
        if base.exists():
            yield from (p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix in suffixes)


def chunks(content: str):
    """Bounded line chunks; keep a heading with following Markdown text."""
    lines = content.splitlines()
    start = 1
    part: list[str] = []
    size = 0
    for number, line in enumerate(lines, 1):
        heading = line.startswith("#") and part and size >= 200
        if part and (heading or size + len(line) + 1 > MAX_CHUNK):
            yield start, number - 1, "\n".join(part)
            part, size, start = [], 0, number
        if not part:
            start = number
        part.append(line)
        size += len(line) + 1
    if part:
        yield start, len(lines), "\n".join(part)


def connect(db: Path):
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
                 "path UNINDEXED, start_line UNINDEXED, end_line UNINDEXED, body, tokenize='unicode61')")
    return conn


def update(conn: sqlite3.Connection, root: Path) -> tuple[int, int]:
    known = dict(conn.execute("SELECT path, sha256 FROM files"))
    seen = set()
    changed = 0
    for path in source_files(root):
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if known.get(rel) == digest:
            continue
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        body = raw.decode("utf-8", errors="replace")
        conn.executemany("INSERT INTO chunks(path, start_line, end_line, body) VALUES (?, ?, ?, ?)",
                         ((rel, a, b, text) for a, b, text in chunks(body)))
        conn.execute("INSERT INTO files(path, sha256) VALUES (?, ?) "
                     "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256", (rel, digest))
        changed += 1
    for rel in known.keys() - seen:
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel,))
        changed += 1
    conn.commit()
    return changed, conn.execute("SELECT count(*) FROM chunks").fetchone()[0]


def retrieve(conn: sqlite3.Connection, question: str, scope: str, limit: int, max_chars: int) -> str:
    terms = list(dict.fromkeys(re.findall(r"\w+", question, flags=re.UNICODE)))
    if not terms:
        return "No searchable terms."
    match = " OR ".join('"' + term.replace('"', '') + '"' for term in terms[:12])
    conditions = {"all": "1=1", "reports": "path LIKE 'reports/%'",
                  "docs": "(path LIKE 'docs/%' OR path IN ('README.md', 'LOCAL_AGENT.md'))",
                  "code": "(path LIKE 'src/%' OR path LIKE 'scripts/%' OR path LIKE 'config/%')"}
    rows = conn.execute(f"SELECT path, start_line, end_line, body FROM chunks "
                        f"WHERE chunks MATCH ? AND {conditions[scope]} ORDER BY bm25(chunks) LIMIT ?",
                        (match, max(limit * 4, 20))).fetchall()
    out = ""
    used = set()
    for path, start, end, body in rows:
        if path in used and len(rows) > limit:
            continue
        ref = f"[{path}:{start}-{end}]\n"
        available = max_chars - len(out) - len(ref) - 3
        if available < 100:
            break
        excerpt = body[:min(650, available)].strip()
        if len(excerpt) < len(body):
            excerpt += "…"
        out += ref + excerpt + "\n\n"
        used.add(path)
        if len(used) >= limit:
            break
    return out.rstrip() or "No matches."


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Local, bounded JevAI retrieval; no LLM/API calls")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--db", type=Path, default=None)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("index", help="incrementally refresh the local index")
    q = sub.add_parser("query", help="refresh and return cited, token-bounded excerpts")
    q.add_argument("question", nargs="+", help="search terms")
    q.add_argument("--scope", choices=("all", "docs", "reports", "code"), default="all")
    q.add_argument("--limit", type=int, default=4)
    q.add_argument("--max-chars", type=int, default=2400)
    args = ap.parse_args()
    root = args.root.resolve()
    db = args.db or root / "run" / "rag" / "index.sqlite"
    with connect(db) as conn:
        changed, total = update(conn, root)
        if args.command == "index":
            print(f"Indexed {total} chunks; {changed} files changed; {db}")
        else:
            print(retrieve(conn, " ".join(args.question), args.scope, max(1, args.limit),
                           max(300, args.max_chars)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
