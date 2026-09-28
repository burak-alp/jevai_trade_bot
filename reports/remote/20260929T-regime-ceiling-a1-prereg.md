# Remote → Local: rejim tavanı (kullanıcı tezi) + A1 ön kaydı

## Kullanıcı tezi: "boğada boğa, ayıda ayı stratejisi → kâr" — betimleyici ölçüm (`scripts/regime_oracle.py`)
Aynı 12,382 etiketli slow.v1 önerisi (2024-01 → 2026-03), BTC rejimine göre süzüldü; "uygun" = boğada long, ayıda short,
yatayda işlem yok. Kapı/parametre araması yok.

| rejim bilgisi | 30 g (±%5) net R/işlem [95 %, 7 g blok] | 7 g (±%3) net R/işlem |
|---|---|---|
| yok (tüm öneriler) | +0.016 [−0.049, +0.083] | +0.016 |
| **kahin** (gelecek BTC, sonradan bakınca) | **+0.248 [+0.117, +0.388]** | **+0.340 [+0.215, +0.483]** |
| kahinin tersi (sağlama) | −0.193 | −0.312 |
| **gecikmeli** (geçmiş BTC, o an bilinen) | −0.033 [−0.119, +0.054] | −0.075 [−0.179, +0.048] |

- Tezin geriye dönük gözlemi doğru: rejimi **bilirsen** kâr büyük. Ama o an bilinen rejim (geçmiş 30 g/7 g) gelecek rejimle
  yalnız %29 / %35 örtüşüyor — 3 sınıfta şans düzeyi (~%34). Rejim bu ölçekte kalıcı değil; kazanç tamamen **tahminde**.
- Gerekli isabet (kahin etiketi q olasılıkla doğru, yoksa yanlış iki etiketten biri): 30 g'de q=0.50 → +0.075 R,
  q=0.60 → +0.111 R; 7 g'de q=0.50 → +0.095 R. Hatalar bağımsız varsayıldı; gerçek tahmin hataları zamanda
  korelasyonlu → gerçek belirsizlik çok daha büyük.
- Anlamı: tez = "3 sınıflı haftalık/aylık BTC rejimini ≥ ~%45-50 isabetle tahmin et". Bu mimaride C kolunun sorusu.
  Uyarı: haftalık rejimde yılda ~52 bağımsız örnek var; %45 ile %34'ü ayırt etmek aylar-yıllar ister.

## A1 ön kaydı (ML meta-labeler; sonuç görülmeden)
- **Veri:** slow.v1 etiketli öneriler, holdout (2024-01 → 2025-03) + dev (2025-03 → 2026-03), 12,382 satır.
- **Hedef:** `y = net_r > 0` (maliyet sonrası kârlı). B ile kıyas için `y_success` AUC'u ayrıca raporlanır.
- **Özellikler (karar anında bilinen, yön-hizalı):** side × {ret_24h/atr, ret_7d/atr, rel_ret_7d, ema_trend, run_24h_atr,
  funding_bps_8h}, nötr {atr_pct, log qv_24h, vol_rank, log listing_age_d, cost_r, stop_dist_bps}, aile×yön dummy'leri.
- **Model:** L2 lojistik regresyon, standartlaştırılmış, λ = 1 (sabit, ayar yok).
- **Walk-forward:** aylık katlar; eğitim = çıkışı (`t_exit`) test ayı başlangıcından ≥ 2 gün önce biten tüm satırlar (purge +
  embargo); ilk test ayı 2024-07 (≥ 6 ay eğitim). Seçim: p ≥ eğitim p medyanı (üst %50).
- **GEÇER:** walk-forward test dönemi (2024-07 → 2026-03) boyunca A1-seçili net R ortalamasının %99 CI alt sınırı > 0
  (7 g blok) **ve** iki yarıda (→ 2025-05-01, → 2026-04-01) ortalama > 0. Karşılaştırma: tümü (A0) ve rastgele %50 (R).
- Geçmezse: deterministik taban + basit ML filtresinde de edge yok; A1 Jev'in (B) kıyas tabanı olarak kalır.
