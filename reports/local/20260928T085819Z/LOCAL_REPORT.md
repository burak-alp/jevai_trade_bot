# Local → Remote: paper başladı, geliştirme hattı ve yerel RAG

- `42bbb3e` alındı; paper zamanlama testleri 4/4 geçti. Sürekli `jevbot paper --state-dir run/paper` PID 8392 ile 08:55 UTC'de başladı; gecikmiş 08:00 tick'i doğru biçimde `skipped: late` kaydedildi. Gerçek emir/anahtar yok.
- Slow geliştirme hattı PID 4280; kendi pytest adımı exit 0 / 101 s, 1d PIT indirme sürüyor. 6h recorder başlatıcısı PID 13440, 12:00 UTC için bekliyor; iş çakışması bilinçli hızlandırma deneyi.
- Kullanıcının token maliyeti isteği için `scripts/rag.py` eklendi: SQLite FTS5, yerel artımlı indeks, docs/reports/code scope, kaynak satırı ve `--max-chars`. Model/API/ağ maliyeti yok. `python scripts/rag.py query "PIT evreni" --scope reports --max-chars 1800`.
- İlk indeks: 95 dosya / 721 parça. Stale silme, kaynak referansı ve çıktı sınırı testi geçti. README ve LOCAL_AGENT kullanımını anlatıyor.
- Bot araştırma/paper aşamasında; canlı emir yetkisi ve ekonomik edge iddiası yok. Geliştirme + ileriye dönük kapılar değişmedi.
