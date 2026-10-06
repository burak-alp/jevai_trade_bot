# macro.trend.v2 — Binance'te işlenebilir geniş evren, short ve kaldıraç (ön kayıt, sonuç görülmeden, 2026-10-06)

v1 GEÇTİ (252 g, kaldıraçsız). v2 üç eklentiyi test eder; seçim yalnız D (2006-05 → 2015-12), değerlendirme T
(2016-01 → 2026-09). v1'in geriye bakışı (252 g) ve sinyal kuralı aynen korunur.

## Evren (Binance perp karşılığı olanlar; Yahoo vekilleri)
- TEMEL (v1): SPY QQQ GLD SLV USO TLT BTC-USD ETH-USD.
- GENİŞ: TEMEL + IWM EWJ EWT EWY EWZ XLE SMH GDX (hisse) + BNO (Brent) CPER (bakır) PPLT (platin) PALL (paladyum)
  UNG (doğalgaz). Her biri verisi başladığında + 252 gün sonra girer.

## Getiri modeli (perp gibi): nakit + Σ w·(r − nakit) − finansman farkı·|w| − ciro maliyeti
- Finansman farkı (yıllık, |pozisyon| üzerinden): TradFi %1, kripto %5 (Binance funding ortalaması nakit üstü varsayımı).
- Ciro maliyeti taraf başı 10 bps. Short pozisyon −(r − nakit) kazanır, aynı farkı öder.

## Izgara (D'de Sharpe ile seçilir)
- Evren ∈ {TEMEL, GENİŞ} × mod ∈ {yalnız long, long/short} → 4 aday, hedef oynaklık %10, kaldıraç tavanı 3x.
- Hedef oynaklık: haftalık, ölçek = min(tavan, %10 / son 60 g portföy oynaklığı).

## Kaldıraç (seçim değil, risk tablosu)
Seçilen yapılandırma için T'de hedef oynaklık ∈ {%10, %15, %20, %30}, tavan 3x: yıllık getiri, maks düşüş, en kötü ay.

## GO (seçilen, %10 hedef, T)
1. Yıllık getiri 20 g blok bootstrap CI alt > 0.  2. Sharpe > v1 kaldıraçsız Sharpe (T'de 1.09).  3. Maks düşüş < SPY'ninki.
Geçmezse v1 temel olarak kalır. Eşik gevşetme yok.
