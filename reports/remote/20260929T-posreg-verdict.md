# Remote → Local: pos.v1 + reg.v1 kararı (hat 20260928T181415Z-posreg, hepsi exit 0)

Önceden kayıtlı kurallar aynen uygulandı; eşik/parametre değişmedi.

## pos.v1 (CROWD/FLUSH, dev 2025-03-01 → 2026-03-31) — GEÇMEDİ
`pass_99 = []`. Hiçbir aile×yönün net %99 alt sınırı > 0 değil.

| aile×yön | dev n | dev net R | %99 alt | h1 | h2 | 180d |
|---|---:|---:|---:|---:|---:|---:|
| CROWD long | 210 | −0.033 | −0.208 | +0.136 | −0.162 | −0.071 |
| CROWD short | 730 | −0.042 | −0.124 | −0.053 | −0.001 | −0.011 |
| FLUSH long | 49 | −0.221 | −0.481 | −0.142 | −0.310 | +0.044 |
| FLUSH short | 48 | +0.115 | −0.244 | +0.163 | +0.027 | −0.191 |

## reg.v1 (BTC 1h trend kapısı, holdout 2024-01-01 → 2025-03-01, hiç bakılmamıştı) — GEÇMEDİ
`reg_pass_99 = []`. Kapı eklemek sonucu iyileştirmedi: TSM long @reg +0.001 (kapısız −0.028), TSM short @reg +0.003
(kapısız +0.093), XSM @reg her iki yönde kapısızdan kötü. Kural gereği bu holdout yakıldı; başka rejim tanımı denenmez.

## slow.v1 ilk örneklem dışı tekrarı (aynı holdout, yalnız bilgi) — geçen yok
Tümü net +0.003 R [−0.074, +0.091]. Tek dikkat çeken FUND: net +0.153 [95 %: +0.021, +0.281], 99 % alt −0.018;
FUND short iki yarıda da pozitif (+0.177 / +0.106) ama 99 % alt −0.053 → kuralı geçmiyor. Aynı aile dev'de
(2025-26) −0.015 idi; dönemler arası tutarsız. **Kiraz toplama yok:** FUND için yeni test ancak yeni bir ön kayıt ve
yeni (ileriye dönük) veriyle yapılabilir.

## Sonuç ve sıradaki
Ön kayıttaki seçenek (iii): bu maliyet yapısında 5 dk BRK/PB, slow.v1, pos.v1 ve BTC-trend kapısıyla basit kural
edge'i **yok**. Deterministik tabanın kendisi sıfır ortalamalı. Mimarideki açık soru artık yalnız Jev:
- **Jev shadow sürüyor** (B: trade_success filtresi, C: direction paneli). Kapılar: C 7. gün futility, ≥ 28 gün GO;
  B n ≥ 100 ve ≥ 4 blok. Sıfır ortalamalı tabanda B'nin işi seçici filtre: iyi/kötü işlemi ayırabiliyor mu (AUC).
- Yeni deterministik aile ön kaydı şimdilik yok. Kullanıcıyla konuşulacak: A1 (ML meta-labeler) karşılaştırma kolu.
- Senden: paper kullanıcının ayrı PowerShell penceresinde (Claude uygulaması kapanınca terminal paneli ölüyordu,
  20:00–21:00 UTC arası yeniden başlatıldı, tick kaybı yok). Görev Zamanlayıcı'ya taşıma hâlâ açık iş.
