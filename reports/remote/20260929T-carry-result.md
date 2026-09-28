# Remote → Local: carry.v1 sonucu (ön kayıt 20260929T-carry-prereg.md; kod f4aeb35, sonuçtan önce commit)

| kol | yıllık [95 %] | %99 alt | toplam | maks DD | h1 | h2 | işlem | ort. tutma | ort. poz |
|---|---|---|---|---|---|---|---|---|---|
| **carry.v1** (seçici, altlar) | +1.0 % [−2.3, +3.7] | −3.7 % | +2.8 % | 4.9 % | +5.6 % | −3.5 % | 237 | 10.8 g | 2.6/10 |
| carry.base BTC+ETH (referans) | +4.9 % [+4.0, +5.9] | +3.8 % | +14.4 % | 0.4 % | +7.2 % | +2.7 % | 2 | tüm dönem | 2/2 |

Ayrıştırma (sermaye payı toplamları): v1 funding +14.8 %, baz −3.1 %, maliyet −8.9 %; base funding +14.4 %, baz +0.1 %,
maliyet −1.1 %. 447 perp spot çiftine eşlendi.

## Karar
- **carry.v1 GEÇMEDİ** (ön kayıtlı kol): %99 alt < 0, h2 negatif. Yüksek funding'li altlar hızla ortalamaya dönüyor;
  ~11 günlük tutmada giriş/çıkış maliyeti funding'in %60'ını yiyor, baz da aleyhte.
- **carry.base** aynı kriterleri karşılıyor (önceden bildirilmiş referans kol, ayar yok) ama ekonomik olarak **küçük**:
  yıllık ~%5, son yarıda ~%2.7 — funding 2025-26'da sıkıştı. Bu, stabil coin "earn" faizleriyle aynı mertebe;
  risk (borsa, perp teminatı) eklenince üstünlüğü tartışmalı. "Kârlı bot" hedefini karşılamıyor.
- Sonuç: VIP0 taker maliyetiyle seçici alt carry yok; BTC/ETH carry gerçek ama nakit faizine yakın. Parametre araması yapılmadı.
