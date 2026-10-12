"""Telegram command listener: answers "durum" with the desk status. Long-polls getUpdates; replies ONLY to the
configured TELEGRAM_CHAT_ID (other chats are ignored). Token and chat id come from env / the Windows user registry and
are never printed (errors are reported by type only, because HTTP errors can contain the URL with the token).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import httpx

from jevbot.account import _secret

HELP = ("Komutlar:\n• durum — hesaplar, varlık sınıfı kâr/zarar, komite notu, karne\n• yardım — bu liste\n"
        "Sanal hesap; gerçek para yok.")


def reply_for(text: str, status: Callable[[], str]) -> str | None:
    t = (text or "").strip().lower().lstrip("/")
    if t in ("durum", "status", "ne durumdayız", "ne durumdayiz"):
        return status()
    if t in ("start", "yardım", "yardim", "help"):
        return HELP
    return None


def handle_updates(updates: list[dict], chat_id: str, status: Callable[[], str]) -> tuple[int | None, list[str]]:
    """(next offset, replies) for one getUpdates batch; only messages from ``chat_id`` are answered."""
    nxt, out = None, []
    for u in updates:
        nxt = max(nxt or 0, int(u["update_id"]) + 1)
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(chat_id):
            continue
        r = reply_for(m.get("text", ""), status)
        if r:
            out.append(r)
    return nxt, out


def pending_alerts(alert_dir: Path | None) -> list[tuple[Path, str]]:
    """Unsent event-watch files (alert-*.json) as (path, message); the sent list lives in alerts_sent.txt."""
    if alert_dir is None or not alert_dir.exists():
        return []
    sent_f = alert_dir / "alerts_sent.txt"
    sent = set(sent_f.read_text(encoding="utf-8").split()) if sent_f.exists() else set()
    out = []
    for f in sorted(alert_dir.glob("alert-*.json")):
        if f.name in sent:
            continue
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
            arrows = ", ".join(f"{k} {v}" for k, v in (a.get("assets") or {}).items())
            msg = (f"🚨 Olay nöbeti ({a.get('severity', '?')}): {a.get('title', '')}\n{a.get('summary', '')}"
                   + (f"\nEtkilenen: {arrows}" if arrows else "")
                   + "\n(Bilgi: işlem kararını fiyat freni ve haftalık komite verir.)")
        except (OSError, ValueError, AttributeError):
            msg = f"🚨 Olay nöbeti: okunamayan uyarı dosyası {f.name}"
        out.append((f, msg))
    return out


def mark_sent(alert_dir: Path, name: str) -> None:
    with open(alert_dir / "alerts_sent.txt", "a", encoding="utf-8") as fh:
        fh.write(name + "\n")


def listen(status: Callable[[], str], offset_file: Path, *, transport: httpx.BaseTransport | None = None,
           once: bool = False, poll_s: int = 50, alert_dir: Path | None = None) -> None:
    token, chat = _secret("TELEGRAM_BOT_TOKEN"), _secret("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil")
    base = f"https://api.telegram.org/bot{token}"
    offset = int(offset_file.read_text()) if offset_file.exists() else None
    with httpx.Client(timeout=poll_s + 20, transport=transport) as c:
        while True:
            try:
                params = {"timeout": poll_s, **({"offset": offset} if offset is not None else {})}
                r = c.get(f"{base}/getUpdates", params=params)
                ups = r.json().get("result", []) if r.status_code == 200 else []
                nxt, replies = handle_updates(ups, chat, status)
                for text in replies:
                    for i in range(0, len(text), 4000):
                        c.post(f"{base}/sendMessage", json={"chat_id": chat, "text": text[i:i + 4000]})
                for f, msg in pending_alerts(alert_dir):
                    if c.post(f"{base}/sendMessage", json={"chat_id": chat, "text": msg[:4000]}).status_code == 200:
                        mark_sent(alert_dir, f.name)                   # type: ignore[arg-type]
                if nxt is not None:
                    offset = nxt
                    offset_file.parent.mkdir(parents=True, exist_ok=True)
                    offset_file.write_text(str(offset))
            except (httpx.HTTPError, ValueError, OSError) as e:
                print(f"telegram-listen: {type(e).__name__}; 10 s sonra tekrar", flush=True)
                time.sleep(10)
            except Exception as e:                       # noqa: BLE001 - a status bug must not kill the listener
                print(f"telegram-listen: durum hatası {type(e).__name__}", flush=True)
                time.sleep(10)
            if once:
                return


def status_from(state_dir: Path) -> Callable[[], str]:
    def f() -> str:
        from jevbot.macro_desk import status_text
        from jevbot.macro_paper import Binance
        return status_text(state_dir, Binance().prices())
    return f

