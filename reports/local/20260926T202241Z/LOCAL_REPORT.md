# LOCAL_REPORT 2026-09-26T20:22Z — 1 saat gerçek Binance

## Sonuç: NO-GO (Sprint 1)

`ba6cc56` yerelde alındı. Windows/Python 3.11.9 ile 71/71 test ve gerçek Binance smoke geçti. Public data only; key, emir, Jev çağrısı yok. Kayıt exit 0, süre 3605.6 s; verify ve report exit 0.

## Acceptance

- PASS: smoke, integrity (183/183 dosya, 1.317.458/1.317.458 satır), schema errors 0, UNHEALTHY 0, kline complete (200/200, eksik dakika 0), loop lag p99 < 500 ms.
- FAIL: clock offset max abs 550.8 ms (>500), feed p99 <2 s aralık payı **%85,52** (<%99), en uzun feed stall **80 s** (>30 s). Feed stall 28 episode; tek episode max p99 11.276 ms.
- Gate ham `t_recv−t_event` gecikmesini kullanıyor. Her aralığın saat farkını ekleyerek yaptığım bağımsız kontrolle iyi aralık payı **%88,58** (359 aralık); saat düzeltmesi de %99 eşiğini karşılamıyor.
- Windows Time kaynak bağlantısı vardı, fakat fark koşu boyunca yaklaşık −30 ms'den −551 ms'ye kaydı. Koşu sırasında yeniden eşzamanlama için açılan yönetici onayı iptal edildi; saat daha fazla değiştirilmedi.

## Ana ölçümler

- Universe 200; mesaj/s p50 3993, p95 9020, max 20.617. CPU p50 %25,2 / p95 %40,6 / max %67; RSS p95 84,1 MB; disk yaklaşık 75 MB/saat.
- 14 WS reconnect: **12 `stale_feed`, 2 `closed:nocode`**; gap tablosunda 1400 stream satırı, kline kaybı 0. Logda 15 `ws_silent_but_alive` (sparse bağlantı ping ile korundu).
- Feed stall her iki route'ta görüldü: bookTicker/public p99 max 7860 ms, kline/market p99 max 13.470 ms. REST weight p95 356, max 840 / limit 2400; OI yaklaşık 199 poll/dk.
- Kanıt: `recording_report.json`, `verify.json`; ham kayıt ve loglar yerelde `D:\JevAI\data`, `D:\JevAI\logs` altında.

## Historical downloader

- Klines 6/6, bookDepth 1/1, aggTrades 1/1, fundingRate 2/2 kalite OK. `verify --root data/hist/um`: 13 dosya, 1.595.499 satır, checksum/manifest sorunu yok.
- Metrics **3/3 suspect ve karantinada**. Her gün 288 benzersiz 5 dakikalık zaman damgası ve %100 coverage var; kaynak satır sırası monoton değil (gün başına 30–35 ters sıra). Geçerli epoch değerlerini Parquet'te gördüm. Zaman sırasına göre sıralayıp yeniden kalite kontrolü öneriyorum; sıralama öncesi otomatik kabul etmeyin.

## Remote'a tek istek

Sprint 1'e geçmeyelim. Feed stall (>30 s, %85,52 iyi aralık) nedenini ve metrics sıralama kontrolünü ele alalım; yerel saati kullanıcı müdahalesi gerektiren ortam koşulu olarak ayrı tutalım. Kabul eşiklerini geçmek için gevşetmeyelim. Sonra tek tekrar koşusu yapalım.
