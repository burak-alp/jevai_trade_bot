# jevai_trade_bot

Binance USDⓈ-M Futures üzerinde ~200 pair'i tarayan; deterministic setup'ların ürettiği trade önerilerini
Jev (TypeSafe) ile **olasılıksal olarak filtreleyen** ve tüm risk kararlarını deterministic kodda tutan
araştırma/trading sistemi.

**Durum:** Tasarım aşaması. Gerçek para yok.

## Temel ilkeler

1. **Jev trader değildir.** Setup detector'ları side/entry/stop/TP'yi deterministic üretir; Jev yalnızca
   bu önerinin başarı olasılığını (meta-label) ve anormallik riskini (veto) değerlendirir.
2. **Risk engine AI'den bağımsızdır** — `risk/` modülü `judgment/`, `calibration/`, `ml/` import edemez.
3. **Fail-safe = NO NEW TRADE.** Belirsizlikte yeni pozisyon açılmaz; mevcut pozisyonlar exchange-native stop ile korunur.
4. **Her soru ölçülebilir bir label'a bağlıdır**; her olasılık kalibre edilir; her karar yeniden üretilebilir.
5. **Jev'in değeri kanıtlanmalıdır**: ML baseline ve random-filter kontrollerine karşı, forward (shadow) veride.

## Dokümanlar

- [`docs/01_architecture_review.md`](docs/01_architecture_review.md) — mimari eleştirisi, kritik riskler, hedef mimari, Jev'in rolü, roadmap, acceptance criteria (A–L)
- [`docs/02_technical_spec.md`](docs/02_technical_spec.md) — implementasyon spesifikasyonu: event flow, feature/scanner formülleri, Jev state & soru şeması, calibration, policy, risk, execution, DB şeması, replay, istatistiksel testler, failure matrix, config, kod yapısı

## Aşamalar

Recorder → historical replay (quant baseline) → **shadow** (canlı veri, Jev çağrılır, trade yok) →
paper (A0/A1/R/B/B+/C paralel) → testnet (tesisat) → live micro → ölçekleme.
