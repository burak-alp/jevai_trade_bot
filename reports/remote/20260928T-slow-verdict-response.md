# Remote → Local: slow.v1 kararı kabul + POS.v1 ön kaydı + soak erken sinyali
(yanıt: 20260928T084627Z-slow, 20260928T085819Z; soak 20260928T120050Z sürüyor)

## slow.v1 — KABUL, geçen yok
- Özetleri tek tek kontrol ettim: dev'de 6 aile×yönün hepsinde net %99 alt < 0 (en iyisi XSM long −0.069);
  h1/h2 net ortalama işareti 6/6 değişiyor. `pass_99=[]`. Kural doğru uygulandı, parametre oynanmıyor.
- Gross %95 CI'ları da her grupta 0'ı içeriyor, medyan maliyet 0.02–0.05 R. Sorun maliyet değil, gross edge yok
  → seçenek (i) maker giriş **reddedildi** (gerekçen doğru).
- 180 g ikincil portföy toplamı +0.46 R (PF 1.002); dev portföyündeki +37.8 R (max DD 36 R) ileriye taşınmıyor.
- Paper'ı durdurman kurala uygun (kilitli paper yalnızca geçen aile için). Ledger kalsın, görev devre dışı kalsın.
- `scripts/rag.py` kabul.

## Sıradaki: seçenek (ii), ama tarihsel veriyle — POS.v1 (veri görülmeden sabit)
5 dk OI / taker oranı geçmişi Binance Vision `metrics` veri setinde var (downloader destekliyor). Haftalarca
ileriye dönük toplamaya gerek yok; canlıda aynı veri public REST `openInterestHist`'ten gelir, recorder'a
bağımlı değil. Likidasyon (forceOrder) yalnızca ileriye dönük; bu teste girmez.

Stop/maliyet/evren slow.v1 ile aynı: saatlik karar, stop 3×ATR(1h), maliyet kapısı ≤ 0.10 R, PIT günlük top-50,
listing ≥ 30 g, 24 h hacim ≥ 20M. Bir `metrics` satırı yalnızca `create_time + 5 dk ≤ karar anı` ise kullanılır.
- **CROWD** (geç kalan kalabalık): 24 h getiri ≥ +4 ATR(1h) **ve** 24 h OI değişimi ≥ +15 % → short;
  aynası (≤ −4 ATR ve OI ≥ +15 %) → long. TP 2 R, 24 h, sembol başına 24 h cooldown.
- **FLUSH** (deleveraging sonrası dönüş): son 4 h'te OI ≤ −8 % **ve** 4 h getiri ≤ −3 ATR(1h) → long;
  aynası (OI ≤ −8 %, getiri ≥ +3 ATR) → short. TP 2 R, 24 h, 24 h cooldown.
- Karar kuralı slow.v1 ile birebir (4 test): dev 2025-03-01 → 2026-03-31'de net %99 alt > 0 (n ≥ 30, 7 günlük
  blok) **ve** iki yarıda net > 0. 180 g ikincil. Geçen yoksa → (iii) "bu maliyet/veri yapısında basit kural edge'i
  yok" yazılır; A1/Jev aşamasına kanıtsız geçilmez. Eşik gevşetme yok.
- Not: dev penceresi slow.v1'de de kullanıldı; yeni hipotez olduğu için geçerli, ama nihai test yine kilitli paper.

## Senden istenenler — yalnızca veri, sonuç yok
1. 6 h soak'a dokunma; 18:00 UTC'de bitince mevcut formatta raporla.
2. Soak bittikten sonra `metrics` indir (günlük dosya; sayısı çok ama küçük):
   ```powershell
   jevbot download --dataset metrics --symbols "@data/research/pool-dev.json" --granularity daily --start 2025-01-01 --end 2026-03-31 --set hist.concurrency=8
   jevbot download --dataset metrics --symbols "@data/research/pool-180d.json" --granularity daily --start 2026-03-01 --end 2026-09-27 --set hist.concurrency=8
   jevbot verify --root data/hist/um
   ```
   Rapor: sembol-gün kapsaması %, eksik gün sayısı, `create_time` 5 dk ızgarasında mı, quality warn/suspect.
   **OI/getiri istatistiğine bakma.** Ben bu arada POS ailelerini `slow.py` yanına testleriyle yazıyorum.

## Recorder: erken sinyal (12:03–12:41 UTC, `record.stderr` + `recorder_health.json`)
- 101 kopma / 38 dk ≈ 160/h; **hepsi bookTicker**: bt-1 67 nocode + 21 stale, bt-2 11 stale + 2 nocode.
  depth-3 ve market-4/5/6: 0 kopma (connects = 1).
- bt-1 tek akışta ≈ 2,200 msg/s; kopmadan önce lag EWMA ~5 s; bağlantı ömrü medyanı ~4 s. CPU %20 (tek çekirdek),
  loop lag ≤ 13 ms, TCP autotuning normal → yerel değil; en yoğun tek akış ~200 ms RTT'li yolda boğuluyor.
- Kriter değişmiyor; bookTicker bu profilde geçmeyecek. Soak raporundan sonra önerim: home profilinde
  `recorder.book_ticker: false`, spread için zaten kaydedilen `depth` (20 seviye @500ms, 5 s örnek)
  `top_n_by_volume: 10 → 30`. Bu kapsam daraltması, eşik gevşetmesi değil: hiçbir aktif hipotez tick-seviyesinde
  bookTicker istemiyor; ileriye dönük gereken forceOrder/markPrice/kline/OI akışları stabil.
