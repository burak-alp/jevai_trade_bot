# LOCAL_REPORT — 6 h Binance soak (2026-09-28 12:03–18:03 UTC)

## Karar
Eski `home.yaml` bookTicker profili **NO-GO** (4/9 kabul maddesi başarısız). Yeni profilsiz çıkarım yapılmaz; sonraki kayıt bookTicker kapalı/depth top30 ile yapılmalı. Gerçek emir yok.

## Kanıt
- 21,605 s; 200 sembol; 5,133,264 satır; 6 WS bağlantısı, **892 kopma**. Saatlik bookTicker kopması: 12h 118, 13h 152, 14h 132, 15h 211, 16h 139, 17h 130. Depth 12–17h toplam 3, market toplam 3; bookTicker koşuyu sürükledi.
- `recording_report.json`: smoke/integrity/schema/no_unhealthy/clock/loop ✅; kline complete, feed lag, >30s stall ❌. 360 dakika, PIT 204 sembolün 203'ü tam; WS 71,854 + REST 198 kline hücresi, 1 eksik kaldı. Clock max |offset| 72.5 ms; loop lag p99 max 91 ms.
- Feed p50 190 ms; p95 9.12 s; minute-level p99<2s payı 0.5901, en uzun stall 480 s. BookTicker 76.9M mesajda p95 2.195 s; stall sırasında CPU/bytes düştü (CPU medyan 12.5 vs 25.9%, byte/s 0.73 vs 1.04 MB), bu yerel CPU yüküne tek başına bağlanmıyor.
- REST weight max 532/2400/dk; OI ~200 poll/dk. CPU p95 37.2%, RSS max 97.6 MB; disk ~41.7 MB/h.
- `verify.json` önce 1,068 dosya / 5,133,264 satır, checksum/sidecar/manifest hatası 0. `jevbot compact --group hour --grace-min 0` exit 0: raw disk 251.2→119.1 MB; `verify_after_compact.json` 76 dosya / aynı satır / tüm bütünlük kontrolleri OK.

Dosyalar: `recording_report.md/json`, `verify.json`, `compact.json`, `verify_after_compact.json`, `record_exit.txt`. Sonraki adım ön kayıtlı POS/reg tek pipeline; aynı anda ikinci downloader yok.
