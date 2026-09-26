# Remote → Local: home-PC plan (no VPS)

Karar (kullanıcı): sunucu yok, her şey bu PC'de. Hedef değişmedi: temiz 1 saatlik gate, sonra Sprint 1.
İki ortam sorununu bu PC'de çözüyoruz:

1. **Saat** (−30 → −551 ms kayma): `scripts/windows_time_sync.ps1` — PowerShell'i **Yönetici olarak** aç, bir kez çalıştır
   (NTP'yi 64 s'de bir sorgular). Kullanıcı onayı gerekir. Çıktıdaki `w32tm /stripchart` offset'lerini rapora yaz.
2. **Bant genişliği** (p95 2 MB/s, max 4,4 MB/s): yeni profil `config/home.yaml`
   - `binance.ws.compression: true` (permessage-deflate; sunucu kabul ederse trafik birkaç kat düşer)
   - `recorder.book_ticker_max_symbols: 100` (bookTicker trafiğin ~%90'ı; hacme göre ilk 100)
   - depth top-N 20 → 10
   `scripts/local_validation.ps1` artık varsayılan olarak bu profili kullanır (`-BaseOnly` ile kapatılır).

## Sıra (tek koşu)
1. `git pull` → `python -m pytest -q` (73).
2. (Yönetici) `.\scripts\windows_time_sync.ps1`.
3. `jevbot smoke --config config/base.yaml --config config/home.yaml` → `ws:*` detaylarında `extensions` alanı
   `PerMessageDeflate` içeriyor mu? (Binance sıkıştırmayı kabul ediyor mu — bunu ilk kez ölçüyoruz.)
4. `.\scripts\local_validation.ps1 -Seconds 3600`.
5. Rapor (≤ 30 satır): acceptance, `stall_diagnostics`, `feed_stalls`, `clock`, `throughput.bytes_per_s`,
   `extensions`, stale/silent sayıları, metrics quality. GO/NO-GO.

Sıkıştırma kabul edilmezse veya stall devam ederse: `stall_diagnostics` sayılarıyla birlikte raporla;
bir sonraki adım `book_ticker_max_symbols`'ı düşürmek olacak — başka konu açma.
