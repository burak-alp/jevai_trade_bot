# Remote response — Gate 2 (LOCAL_REPORT 20260926T221600Z)

**Gate 2: GO (kapsam: bookTicker top-50).** 9/9 acceptance, 0 stall, 0 reconnect, clock ≤ 67 ms. Teşekkürler.

## Değişikliklerin incelemesi
- `home.yaml` top-50, script isolation/exit codes, `windows_time_sync.ps1` MinPollInterval/UpdateInterval, report notu,
  test düzeltmesi: **kabul**.
- Manifest thread-lock: **kabul + genişletildi** — recorder ile `jevbot compact` ayrı process'ler; Windows'ta
  process'ler arası append yarışı da satır kaybettirebilir. `append_manifest` artık thread lock + `_manifest.lock`
  dosya kilidi (msvcrt/fcntl). Test: 4 process × 200 append → 800 satır.
- Protokol notu: `src/` değişikliklerini bir dahaki sefere `local/<konu>` branch'ine koy; bu seferkiler doğru ve alındı.
- Spec: §3.1'e "trade edilebilir evren = bookTicker kapsamı (50); 200 top-of-book doğrulanmadı" notu eklendi.

## Sıradaki (yerel) — iki iş, paralel
1. **24 saat soak** (aynı profil, yeni isolated dizin):
   ```powershell
   git pull; python -m pytest -q
   .\scripts\local_validation.ps1 -Seconds 86400 -NoHist
   ```
   Soak sırasında ayrı bir PowerShell'de saatte bir: `jevbot compact --config config/base.yaml --config config/home.yaml --set data_dir=./data-<ts>`
   Rapor (≤ 25 satır): acceptance, `feed_stalls`, reconnect nedenleri, 23 h rotation gap'siz mi, RSS başı/sonu,
   disk MB/saat (compaction öncesi/sonrası), compact `skipped_unverified` = 0 mı.
2. **Sprint 1 için tarihsel veri** (soak'u etkilememesi için düşük concurrency):
   ```powershell
   jevbot download --dataset pit-listing --interval 1m --set hist.concurrency=2
   # son 180 gün, top-50 (recorder run dizinindeki universe dataset'inden hacim sırası):
   jevbot download --dataset klines --interval 1m --symbols <TOP50> --start <D-180> --end <D-2> --granularity monthly --set hist.concurrency=2
   jevbot download --dataset markPriceKlines --interval 1m --symbols <TOP50> --start <D-180> --end <D-2> --granularity monthly --set hist.concurrency=2
   jevbot download --dataset fundingRate --symbols <TOP50> --start <D-180> --end <D-2> --set hist.concurrency=2
   jevbot download --dataset metrics --symbols <TOP50> --start <D-180> --end <D-2> --set hist.concurrency=2
   jevbot verify --root data/hist/um
   ```
   Rapor: dataset başına dosya/satır, quality ok/warn/suspect sayıları, toplam MB.

Remote tarafında Sprint 1 (feature engine + scanner + labeler + replay, Arm A0) başlıyor; kod testlerle gelir,
gerçek veride çalıştırma talimatı ayrıca gönderilir.
