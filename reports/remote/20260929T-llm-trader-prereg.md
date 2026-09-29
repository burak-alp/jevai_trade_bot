# Remote → Local: `llm.v1` — yapay zekâ trader, Jev vs Claude, ABD hisse perp'leri (ön kayıt, 2026-09-29)

Kullanıcı isteği: sistem "profesyonel trader gibi" işlem açsın; Jev ve Claude karşılaştırmalı. Geriye dönük test edilemez
(modeller geçmişi görmüş olabilir) → yalnız ileriye dönük paper. Eski Astra dersleri: hep CASH seçti (ölçüm yok),
serbest metin koşulları uygulanmadı, kota çağrıları düşürdü, fiyat bağlamı eksikti.

## Kurulum
- Evren (12): NVDA TSLA AAPL MSFT AMZN META GOOGL COIN MSTR PLTR SPY QQQ (Binance USDⓈ-M TradFi perp'leri).
- Karar: her iş günü (Pzt–Cum) 14:00 UTC tick'i. Bağlam dosyası `run/paper-jev/llm/context-YYYYMMDD.json`, iki kola **aynı**:
  1h mumlardan getiriler (1 g, 5 g, 20 g), ATR(1h) %, EMA50/200 işareti, son funding (bps/8h), son 36 saatin Yahoo RSS başlıkları (≤ 8).
- Kollar: **jev** (paper sürecinde, `jev-1.13.0`) ve **claude** (Claude Code zamanlanmış görev, 14:05 UTC, sabit istem
  `docs/llm_trader_prompt.md`). Çıktı yalnız: sembol başına P(up), P(flat), P(down) 24 saat için (±%1 bant) + tek cümle gerekçe.
- İşlem kuralı (kod): P(up) − P(down) ≥ 0.20 → long, ≤ −0.20 → short, yoksa işlem yok. Giriş 14:10 UTC (ya da karar
  kaydı + 1 dk, hangisi sonra), çıkış giriş + 24 saat, 1 dk kapanış fiyatı. Eşit ağırlık, kaldıraç 1x (kod), maliyet 20 bps
  gidiş-dönüş + gerçek funding.
- Referanslar: aynı günlerde **hep long** (piyasa betası) ve **rastgele** (±, her gün sabit tohum).

## Karar kuralı
- **4. hafta:** yalnız bilgi + futility: bir kolun işlem başına net ortalaması < 0 ve AUC(skor, yukarı) ≤ 0.5 ise o kol kapanır.
- **8. hafta GO:** kolun günlük portföy getirisi haftalık blok bootstrap %95 CI alt > 0 **ve** hep-long referansını ortalamada
  geçer **ve** AUC CI alt > 0.5. GO → küçük gerçek sermaye (kod belirler kaldıracı), ayrı ön kayıt.
- Kayıp kabul: kullanıcı aylık dalgalanmayı (−%20 / +%300) kabul ediyor; kriter tek ayın değil toplamın kârlı olması.
- İstem/bağlam değişirse sürüm artar (`llm.v2`), sayaçlar sıfırlanır.
