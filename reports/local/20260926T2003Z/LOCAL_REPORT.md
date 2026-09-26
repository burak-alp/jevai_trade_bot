# LOCAL_REPORT 2026-09-26T20:03Z — Windows retest

## Sonuç: NO-GO (1 saatlik gate)

`f6ad480` yerelde alındı. `websockets==17.1`, Python 3.11.9 ile integration 9/9 ve unit 60/60 geçti. Gerçek Binance smoke art arda 3/3 geçti (`rest`, `ws:market`, `ws:public`); her üçünde `stage=done`.

Kullanıcı onayıyla Windows saati eşzamanlandı. `time.windows.com` stripchart farkı yaklaşık +10 ms, Binance smoke REST farkı +63.5 ms; sonraki 120 saniyelik kayıtta medyan offset 55.5 ms.

## 120 saniyelik gerçek kayıt

- Komut: `jevbot record --duration 120 --set data_dir=./data-local-synced --set run_dir=./run-local-synced --set logging.file=./logs/record-local-synced.jsonl`. Public market data; key, emir, Jev çağrısı yok.
- Exit 0; 200 sembol, 45.770 satır. `verify`: 18/18 dosya, 45.770/45.770 satır, checksum/manifest/orphan hatası yok. Kline tam: 200/200 sembol, 0 eksik dakika; schema/duplicate/invalid/late 0.
- Mesaj/s p50 4061, p95 4508; CPU p50 %22.8, p95 %27.0; RSS p95 79 MB; disk hızı yaklaşık 74 MB/saat (kısa pencere tahmini); REST weight p95 239/2400 per minute, OI yaklaşık 184 poll/dk.
- Health: HEALTHY 10, DEGRADED 2, STOPPED 1. Acceptance 7/8: `feed_lag_p99_under_2s=false`. `bookTicker/public` p99 max 7699 ms, health aggregate p99 max 8082 ms. Diğer akışların p99 max değerleri: depth 141 ms, forceOrder 442 ms, kline 368 ms, markPrice 695 ms.
- `public-5` (100 stream) 20:02:10 UTC'de `closed:nocode` ile koptu, 1152 ms sonra yeniden bağlandı. 100 `ws_disconnect` gap satırı kaydedildi (tek bağlantı olayı). 8 saniyelik gecikme sıçramasının bu olayla ilişkili olması olası; kaynak ve kuyruk etkisi henüz doğrulanmadı.

## Düzeltme istekleri

1. [P1] `src/jevbot/cli.py:103` — native Windows `recording-report` varsayılan cp1252 ile `UnicodeEncodeError` (`→` karakteri); `Path.write_text(md, encoding="utf-8")` kullan. `PYTHONUTF8=1` ile aynı JSON ve Markdown raporu oluştu. `scripts/local_validation.ps1` mevcut haliyle bu adımda hata alır; rapor komutunun exit kodunu da kontrol et.
2. [P1] `public-5` kopması sonrası `bookTicker` p99 yaklaşık 7.7 s ve acceptance FAIL. `logs/record-local-synced.jsonl` içindeki `ws_disconnected` ve `ws_gap_recorded` olayları ile `run-local-synced/recording_report.json` karşılaştırılabilir. Kopuş nedeni (`closed:nocode`) ve backlog davranışını araştır; acceptance eşiğini yalnızca sonucu geçirmek için gevşetme.
3. [P2] Önceki 90 saniyelik saat kayık koşuda iki kline taşıyan son market bağlantısı 15 s sessizlikten yeniden bağlandı. Sparse akışlar için `silence_timeout_s=15` yanlış pozitif olabilir; veri sürekliliği ve reconnect sayısıyla sınanmalı.

Bir saatlik koşuyu bu ölçüm zaten `feed_lag_p99_under_2s` koşulunda kırdığı için başlatmadım. Yukarıdaki sorunlar ele alınınca yeniden deneyeceğim; historical downloader henüz çalıştırılmadı.
