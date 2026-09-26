# 01 — Architecture Review & Target Design

> Durum: v0.1 (tasarım). Bu dokümandaki bütün sayısal eşikler **başlangıç aralığıdır**, "doğru değer" değildir.
> `[CAL]` etiketi: empirical calibration gerektirir. `[FIX]` etiketi: güvenlik/altyapı sabiti, optimize edilmez.
> `[VERIFY]` etiketi: dış sisteme (Binance/Jev API) ait, implementasyondan önce güncel dokümandan doğrulanmalı.

---

## A. Architecture Review

### A.1 Taslakta doğru olanlar (korunacak)

| Karar | Neden doğru |
|---|---|
| Jev ≠ trader; sizing/leverage/stop/limitler deterministic | LLM çıktısı non-stationary, sürüm değişebilir, kalibre değil. Risk parametreleri modelden bağımsız olmalı. |
| Fail-safe default = NO NEW TRADE | Asimetrik kayıp: kaçırılan trade'in maliyeti ≈ 0, kontrolsüz trade'in maliyeti sınırsız. |
| Neutral / observable state, framing yok | LLM'ler prompt framing'e çok duyarlı; "bullish breakout" gibi etiketler cevabı sızdırır. |
| Replay → paper → testnet → live | Doğru sıra. (Aşağıda "shadow" aşaması ekleniyor.) |
| Calibration + A/B | Projenin asıl değeri bu ölçüm altyapısı. |
| Modular monolith, tek VPS | 200 sembol için Python + asyncio yeterli; microservice gereksiz. |

### A.2 Taslaktaki yapısal hatalar

**1. İç çelişki: "Jev trader değildir" ama yön kararını Jev veriyor.**
Taslakta scanner "incelemeye değer coin"i seçiyor, yönü `direction_edge = P(up) − P(down)` belirliyor. Yani trade'in *side*'ı fiilen Jev'den geliyor; risk engine sadece büyüklüğü sınırlıyor. Bu, public deneylerde başarısız olan "Jev directional trader" modelinin yumuşatılmış hali.
→ **Düzeltme:** Side, entry, invalidation (stop) ve hedef deterministic *setup detector* tarafından önerilir (trade proposal). Jev yalnızca bu somut proposal'ın başarı olasılığını değerlendirir (**meta-labeling**, López de Prado). Jev bir *filtre/veto/ranker*'dır, sinyal kaynağı değildir. Directional Jev sadece deneysel Bot C'de kalır.

**2. Soruların çoğu ölçülebilir bir label'a bağlı değil → kalibre edilemez.**
"setup_quality = good", "flow_quality = strong", "momentum_persistence" — hangi horizon'da, hangi eşikte doğrulanacak? Label'ı olmayan olasılık kalibre edilemez, A/B'de de anlamlı değildir.
→ **Kural:** Jev'e sorulan her soru, önceden tanımlı bir outcome label'ına (horizon + barrier + eşik) birebir karşılık gelmeli. Karşılığı yoksa soru kaldırılır.

**3. Baseline çok zayıf: "rules-only vs rules+Jev" karşılaştırması Jev'i değil "herhangi bir filtre"yi ölçer.**
Jev'in yaptığı her şey girdi olarak verdiğimiz ~25 feature'ın bir fonksiyonu. Aynı feature'lar üzerinde eğitilmiş bir logistic regression / gradient boosting modeli de filtre yapabilir. Ayrıca sadece *daha az trade etmek* bile fee drag'i düşürüp metrikleri iyileştirir.
→ **Düzeltme:** İki zorunlu kontrol kolu: (a) **ML meta-labeler** (aynı state, aynı label, walk-forward eğitilmiş), (b) **random filter** (Jev ile aynı geçiş oranında rastgele filtre). Jev'in incremental value'su bunlara karşı ölçülür.

**4. Historical replay'de Jev için ciddi contamination (sızıntı) riski.**
Jev 15 Eylül 2026'da yayınlandı; training cutoff'u ve training corpus'u bilinmiyor. Geçmiş tarihli state'lerde model, fiyat yolunu/olayları (FTX, LUNA, ETF onayları, belirli coin pump'ları) *hatırlayabilir*. Sembol adı, tarih, mutlak fiyat gibi tanımlayıcılar bu hafızayı tetikler.
→ **Düzeltme:** (a) State tamamen anonimleştirilir (sembol yok, tarih yok, mutlak fiyat yok, sadece normalize edilmiş oranlar). (b) Jev'in *kanıt değeri taşıyan* değerlendirmesi yalnızca **forward shadow data** (Jev yayın tarihinden sonra, canlı toplanan) üzerinde yapılır. Historical replay, quant baseline ve altyapı doğruluğu içindir. (c) Contamination testi: aynı state'i tanımlayıcılı/tanımlayıcısız sor; performans farkı varsa sızıntı var.

**5. Order book / microstructure feature'ları geçmişe dönük backtest edilemez.**
Binance L2 order book geçmişini ücretsiz vermez. `data.binance.vision` üzerinden klines, aggTrades, trades, funding, premium/mark klines, 5m metrics (OI, L/S ratio) ve sınırlı bookDepth/bookTicker alınabilir (kapsama tarihleri dataset'e göre değişir `[VERIFY]`). Liquidation stream (`forceOrder`) sembol başına saniyede en fazla 1 snapshot verir — **eksik veridir**, tarihsel versiyonu yok.
→ **Düzeltme:** Faz 0'da hemen bir **recorder** çalıştırılır ve *yalnızca indirilemeyen* veriler kaydedilir (book feature'ları, 1m OI, mark/funding, forceOrder). Replay dataset'iniz bu kayıtla birikir. Liquidation feature'ı MVP'den çıkarılır (sadece log).

**6. Cost yapısı ile horizon uyumsuz → overtrading riski en büyük tehlike.**
Örnek deneyinizi inceleyelim: 731 trade, $1,650 fee → trade başı ~$2.26 fee. Round-trip taker ~10 bps varsayımıyla ortalama notional ≈ $2,250. Fee hariç kayıp ≈ −$1,500 → trade başı **≈ −9 bps gross**. Yani sinyal fee'den önce de negatif; bu tipik olarak *adverse selection*'dır (son hareketi kovalamak, spread/slippage ödemek). 200 coin × dakikalık tarama, kısa horizon'larla birleşirse aynı sonuç çıkar.
→ **Düzeltme:** Hedef horizon 30 dk–4 saat. Her proposal için `expected_move / round_trip_cost ≥ 3` `[CAL]` ön şartı. Trade frekansı bir *çıktı* olarak izlenir, günlük üst sınır `[FIX]` konur.

**7. "2-of-3 persistence" LLM gürültüsünü ölçer, piyasa teyidini değil.**
Aynı (veya neredeyse aynı) state üç kez sorulursa üç cevap bağımsız kanıt değildir; sadece modelin sampling varyansıdır. Gerçek teyit, *yeni piyasa bilgisidir* (bir sonraki bar kapanışında seviyenin korunması, flow'un devam etmesi).
→ **Düzeltme:** Persistence deterministic *market confirmation* + calibrated EV üzerinde hysteresis olarak tasarlanır (bkz. Spec §9). Jev, state materyal olarak değişmedikçe tekrar çağrılmaz.

**8. İstatistiksel güç (sample size) ciddi şekilde hafife alınmış.**
Trade başı beklenen net 0.10R, std 1.2R ise, expectancy'nin 0'dan farklı olduğunu %95 güvenle göstermek için n ≈ (1.96·1.2/0.10)² ≈ **550 trade** gerekir. Max 3 pozisyon ve günde ~5 trade ile bu ~110 gün eder. Portfolio seviyesinde A/B ile Jev'in katkısını ayırmak çok daha fazla ister.
→ **Düzeltme:** Birincil değerlendirme **decision-level**: trade edilsin edilmesin, Jev'e sorulan *her* proposal label'lanır. Günde yüzlerce proposal → haftalar içinde anlamlı güç. Portfolio sim ikincil doğrulamadır.

**9. 200 coin ≈ 1–3 faktör.**
Altcoin getirilerinin büyük kısmı BTC/market faktörüyle açıklanır. "Max 3 pozisyon" üç farklı coinde aynı BTC-long bahsi olabilir. Scanner ham momentum'a bakarsa BTC yükselirken sistematik olarak en yüksek beta'lı coinleri seçer (edge değil, kaldıraçlı beta).
→ **Düzeltme:** Scanner'da BTC-residual (idiosyncratic) return; risk'te beta-weighted net exposure ve correlation-cluster limitleri.

**10. Leverage kavram hatası riski.**
Risk, stop mesafesine göre sizing ile belirlenirse leverage *riski değiştirmez*; sadece margin kullanımını ve liquidation mesafesini değiştirir. "1x equivalent risk" ifadesi yerine: *risk = stop-based sizing; leverage = liquidation mesafesi kısıtını sağlayan minimum değer*.

**11. Secondary analyst (GPT/Claude) execution path'te olmamalı — "ambiguous durumda" bile.**
Disagreement tetiklemeli analyst: latency ekler, nadir tetiklendiği için kalibre edilemez, seçim yanlılığı yaratır ve subscription UI ile 7/24 bağımlılık imkânsız. → MVP'den tamamen çıkarılır; sadece offline haftalık post-mortem/raporlama aracı olabilir.

**12. Jev vendor riski.**
Model 11 günlük. Fiyat, API şeması, latency ve *model davranışı* sessizce değişebilir → kalibrasyon kırılır. → TypeSafe API ayrı bir `model_version` alanı sunmuyor (response yalnızca `model` string'i döndürüyor, alias'tan farklı olabilir), yani **tam pinning garanti değil**. Bu yüzden: `/v1/models` ile mümkünse versioned isim seçimi, her request'te response `model` değerinin saklanması, zorunlu çıktı dağılımı drift monitörü (PSI + test-retest) ve Jev'in "distilled" ML kopyası ayrı arm olarak (bkz. D.4, Spec §7.3).

### A.3 Soru bazında değerlendirme (taslaktaki 8 atomic question)

| # | Soru | Karar | Gerekçe |
|---|---|---|---|
| 1 | REGIME (trend_up/down/range/high_vol/disorderly) | **Kaldır → deterministic** | ADX, efficiency ratio, EMA eğimi, vol percentile ile hesaplanabilir. Ayrıca seçenekler mutually exclusive değil (trend_up ∧ high_volatility aynı anda olabilir) → kötü Choice tasarımı. Regime, calibration slicing için deterministic hesaplanır. |
| 2 | SHORT_TERM_DIRECTION | **Sadece Bot C'de, yeniden tanımlanarak** | Horizon ve eşik tanımsız. Bot C için: "H dakika sonra getiri > +0.5·ATR mı / < −0.5·ATR mı / arada mı" şeklinde label'lı. |
| 3 | SETUP_QUALITY | **Değiştir → `trade_success` (Noul)** | Vague. Yerine: "Bu proposal, TP'ye SL'den önce H içinde ulaşır mı?" — triple-barrier label ile birebir ölçülebilir. Sistemin ana sorusu. |
| 4 | MOMENTUM_PERSISTENCE | **Birleştir (#3 içine)** | #2 ve #3 ile yüksek korelasyonlu; trade_success zaten persistence'ı içeriyor. |
| 5 | BREAKOUT_QUALITY | **Setup-conditional olarak kalabilir, MVP'de yok** | Sadece breakout setup'ında anlamlı; trade_success ile büyük ölçüde örtüşür. Faz 3'te ablation ile test edilir. |
| 6 | FLOW_QUALITY | **Kaldır** | CVD, taker ratio, imbalance zaten hesaplanıyor; LLM'e yeniden özetletmek bilgi eklemez. |
| 7 | LIQUIDITY_OK | **Kaldır → deterministic** | Depth@10bps vs order notional, spread, expected slippage — tamamen hesaplanabilir. LLM'e bırakmak hata kaynağı. |
| 8 | ABNORMAL_MARKET | **Kalır, ama sadece veto yönünde** | Deterministic circuit breaker'lara ek, çok-değişkenli "garip durum" sezgisi. Asimetrik kullanılır: sadece trade'i engelleyebilir, asla açtıramaz. Label: "H içinde adverse excursion > 2×stop veya spread > 3× median". |

**Yeni önerilen sorular:**

| Soru | Tip | Label | Aşama |
|---|---|---|---|
| `trade_success` | Noul | Triple barrier: TP önce = 1, SL önce = 0, timeout = realized R ile ayrı | MVP |
| `abnormal_risk` | Noul | Adverse excursion / spread shock (bkz. Spec §6) | MVP |
| `adverse_excursion_bucket` | Score (low/med/high/extreme) | MAE (max adverse excursion) R cinsinden quantile | Faz 3 (stop kalibrasyonu için bilgi) |
| `event_risk` | Noul | Sadece metin girdisi (Binance announcements, delisting, unlock takvimi) verilirse | Faz 4+ opsiyonel — LLM'in *gerçek* karşılaştırmalı avantajı olabilecek tek alan |

MVP'de Jev'e **proposal başına 2 soru** (`trade_success`, `abnormal_risk`). Az soru = düşük latency, az multiple-testing, yüksek istatistiksel güç.

---

## B. Kritik Hatalar / Riskler (öncelik sırasıyla)

| # | Risk | Etki | Mitigasyon |
|---|---|---|---|
| 1 | **Fee/slippage drag + overtrading** | Edge'i tamamen yer (örnek deney) | Cost-aware EV gate, horizon ≥ 30dk, günlük trade cap, maker-first denemesi Faz 5+ |
| 2 | **LLM contamination / lookahead in replay** | Sahte edge → canlıda kayıp | Anonim state, sadece post-release forward data ile Jev değerlendirmesi, contamination testi |
| 3 | **Yetersiz sample size / multiple testing** | Şans eseri iyi sonucu edge sanmak | Decision-level eval, pre-registration, deflated Sharpe, holdout |
| 4 | **Market factor (BTC) konsantrasyonu** | Korelasyonlu çöküşte 3 pozisyon birden stop, slippage ile > 3R kayıp | Beta-weighted exposure, cluster limitleri, gap-risk budget |
| 5 | **Paper fill modeli fazla iyimser** | Paper kârlı, live zararlı | Pessimistic taker model, stop slippage modeli, live implementation-shortfall ölçümü |
| 6 | **Korumasız pozisyon** (stop gönderilemedi, listenKey düştü, reconnect) | Sınırsız kayıp | Native stop zorunlu, fill→stop onayı ≤ 2s yoksa market close, reconciliation loop |
| 7 | **Jev drift / API değişikliği** | Kalibrasyon sessizce bozulur | Versioned model adı (mümkünse) + response `model` loglama, zorunlu PSI/test-retest monitör, rolling calibration |
| 8 | **Survivorship bias** | Delist olan coinler dışlanınca backtest şişer | Point-in-time universe, delisted semboller dahil |
| 9 | **Silent WS stall** (bağlantı açık, veri yok) | Stale data ile karar | Stream başına heartbeat, event-time lag izleme |
| 10 | **Jev directional bias** (LLM'lerde sık görülen "bullish" bias) | Long/short asimetrisi | Side-canonicalized state + mirror consistency test |
| 11 | **Regime tek-dönem örneklemi** | 2 aylık boğa verisiyle "edge" | Acceptance criteria'da ≥ 2 farklı regime şartı |
| 12 | **Hukuki/erişim** | Binance Futures'ın ikamet ülkesinde kullanılabilirliği/KYC | Canlıdan önce yasal uygunluk kontrolü |

---

## C. Önerilen Final Architecture

```
                         Binance USDⓈ-M Futures
     ┌──────────────┬───────────────┬───────────────┬──────────────┐
  kline_1m (200)  !markPrice@arr   bookTicker/     REST poll       User Data Stream
  (taker buy vol)  (funding,mark)  depth20@500ms   OI 1m, exchInfo  (orders/fills/acct)
     │              │              (tiered)         │                    │
     └──────────────┴───────┬───────┴───────────────┘                    │
                            ▼                                            │
                 ┌─────────────────────┐        ┌──────────────────┐     │
                 │ Market Data Engine  │──────▶ │ Recorder         │     │
                 │ (health, clock,     │        │ (Parquet, only   │     │
                 │  per-symbol buffers)│        │  non-downloadable)│    │
                 └─────────┬───────────┘        └──────────────────┘     │
                           ▼  BarClosed(1m) event                         │
                 ┌─────────────────────┐                                 │
                 │ Feature Engine      │  200×F numpy matrix, incremental │
                 └─────────┬───────────┘                                 │
                           ▼                                              │
                 ┌─────────────────────┐                                 │
                 │ Deterministic       │  1) Eligibility (tradability)    │
                 │ Scanner             │  2) Setup detectors → proposals  │
                 │                     │  3) Rank + diversify → top K     │
                 └─────────┬───────────┘                                 │
                           ▼  TradeProposal(side, entry, stop, tp, H)     │
        ┌──────────────────┼──────────────────────────┐                  │
        ▼                  ▼                          ▼                  │
  ┌───────────┐     ┌──────────────┐          ┌──────────────┐          │
  │ Jev Judge │     │ ML Meta-     │          │ Labeler      │ (async,  │
  │ (2 Noul,  │     │ labeler      │          │ triple-      │  future) │
  │ cache,    │     │ (baseline)   │          │ barrier)     │          │
  │ timeout)  │     └──────┬───────┘          └──────┬───────┘          │
  └─────┬─────┘            │                         │                  │
        ▼                  ▼                         ▼                  │
  ┌──────────────────────────────┐          ┌────────────────┐          │
  │ Calibration Service          │◀─────────│ Decision Store │          │
  │ raw p → calibrated p         │  refit   │ (Postgres)     │          │
  └─────────────┬────────────────┘          └────────────────┘          │
                ▼                                                        │
  ┌──────────────────────────────┐                                      │
  │ Policy Engine (per arm)      │  veto → EV gate → confirmation /      │
  │ A / A1 / B / C / R           │  hysteresis                           │
  └─────────────┬────────────────┘                                      │
                ▼  OrderIntent                                           │
  ┌──────────────────────────────┐                                      │
  │ Risk Engine (AI-independent) │  sizing, stops, limits, breakers,     │
  │ state: NORMAL/CAUTION/       │  kill switch                          │
  │ REDUCE_ONLY/HALTED           │                                      │
  └─────────────┬────────────────┘                                      │
                ▼  ApprovedOrder                                         │
  ┌──────────────────────────────┐                                      │
  │ Execution Adapter            │  Paper | Sim(replay) | Testnet | Live │
  │ + Reconciler                 │◀─────────────────────────────────────┘
  └──────────────────────────────┘
```

Temel değişiklikler (taslağa göre):
1. Scanner → **TradeProposal** üretir (side + geometri deterministic).
2. Jev → proposal üzerinde **meta-label** (Noul) + **veto** (Noul). Direction sorusu yok (Bot C hariç).
3. **ML meta-labeler** ve **random filter** zorunlu kontrol kolları.
4. **Labeler** her proposal'ı (trade edilsin edilmesin) label'lar → calibration & A/B aynı veri üzerinde.
5. **Calibration Service** policy'den önce; policy sadece calibrated p görür.
6. **Policy** "magic weighted score" yerine **cost-aware expected value** kullanır.
7. **Recorder** ayrı process; trader çökse bile veri toplamaya devam eder.
8. **Shadow faz** (canlı veri, Jev çağrılır, trade yok) paper'dan önce.

---

## D. Jev'in Kesin Rolü

### D.1 Kontrat

```
Jev: (CanonicalState, QuestionSet vN) → {question_id: probability}
```
- Girdi: tek bir TradeProposal'a ait, anonim, side-canonicalized, normalize edilmiş ~25 feature **+ proposal geometrisi** (stop_dist_atr/bps, tp_R, horizon, cost_R, trigger_distance_atr). Geometri olmadan "TP, SL'den önce mi?" sorusu tanımsızdır.
- Çıktı: yalnızca olasılıklar. Fiyat, miktar, leverage, stop, TP **asla** istenmez/kabul edilmez.
- Kullanım yetkisi: **filtre (gate) ve veto**. Jev bir proposal'ı *geçirebilir* veya *engelleyebilir*; proposal *yaratamaz*, side'ı *değiştiremez*, size'ı *büyütemez*.
- Jev yokluğunda (timeout/hata/stale): Bot B için karar = HOLD (yeni pozisyon yok). Mevcut pozisyonlar deterministic yönetim + native stop ile devam.

### D.2 Directional forecasting için Jev kullanmak mantıklı mı?

Kısa cevap: **Birincil yol olarak hayır; ölçülecek hipotez olarak evet (Bot C).**
- Jev'e verdiğimiz state, deterministic hesaplanmış feature'ların alt kümesi. LLM bu feature'lar ile gelecekteki getiri arasındaki *bu piyasaya özgü* ilişkiyi hiç görmedi (sizin label'larınızla eğitilmedi). Aynı feature'lar üzerinde walk-forward eğitilmiş küçük bir model, prensipte bu ilişkiyi LLM'den daha iyi öğrenmeli.
- LLM'in olası avantajları: (a) çok-değişkenli "garip durum" sezgisi (veto), (b) genel piyasa bilgisinden gelen priors (ör. funding aşırılığında squeeze dinamiği), (c) metin/olay bilgisini işleme.
- Dolayısıyla Jev'in en umut verici rolleri sırasıyla: **veto (abnormal_risk) > meta-label filtre (trade_success) > event risk (metin) > direction**.

### D.3 Jev'in değer katıp katmadığının "distillation testi"

Jev'in çıktılarını hedef alan bir GBM'i aynı feature'lar üzerinde eğit (`p_jev ≈ f(features)`).
- R² / fidelity çok yüksekse (ör. Spearman ρ > 0.9 `[CAL]`), Jev feature'ların basit bir fonksiyonudur → onu distilled model ile değiştirebilirsiniz (daha hızlı, deterministic, ücretsiz).
- Jev'in residual'ı (`p_jev − f(features)`) outcome'u açıklıyorsa (residual ile label arasında anlamlı ilişki), Jev feature'ların ötesinde bilgi (priors) katıyordur. **Bu, "gerçek incremental value" için en temiz testtir.**

### D.4 Fallback hiyerarşisi (Bot B için)

```
Jev OK (latency < timeout, response model == referans model)  → calibrated Jev p
Jev down / timeout                                     → HOLD (yeni entry yok)
Jev drift alarmı (PSI > 0.25)                          → CAUTION: B arm yeni entry durdurur, kalibrasyon refit + inceleme
```
Distilled model **otomatik** fallback yapılmaz (farklı bir sistemdir, ayrı arm olarak ölçülür).

---

## E. Feature / Scanner Tasarımı (özet — detay Spec §4-5)

### E.1 Feature seti eleştirisi

**Redundant (birini tut):**
- EMA20, EMA50, distance-from-EMA → `ema_spread_atr = (EMA20−EMA50)/ATR` ve `dist_ema20_atr` yeterli.
- RSI14 ↔ normalize return'ler → RSI kalabilir ama ret_z ile korelasyonu izlenir; MVP'de ret_z yeterli.
- CVD ↔ taker buy/sell volume ↔ aggressor ratio → aynı bilgi. `taker_imbalance_N = (buy−sell)/(buy+sell)` + `cvd_z` yeterli.
- volume, relative volume, volume acceleration → `rvol_z` (log volume z-score) + `rvol_accel`.
- ATR ↔ volatility → `atr_pct` + `rv_pctile` (kendi geçmişine göre percentile).

**Eksik (ekle):**
- **Vol-normalizasyon**: tüm return'ler ATR veya σ cinsinden (coinler arası karşılaştırılabilirlik).
- **BTC-residual return & beta**: `resid_ret_15m = ret − β·ret_BTC`.
- **Kaufman efficiency ratio** (trend kalitesi, ADX'ten daha az gecikmeli).
- **VWAP distance** (session/rolling 4h).
- **Swing high/low mesafesi (ATR)** — stop yerleşimi ve breakout tanımı için.
- **Cost-to-trade estimate (bps)** — `spread/2 + impact(notional, depth)` + fee.
- **Funding: cross-sectional z-score + time-to-next-funding + funding interval** (Binance'te sembole göre 1h/4h/8h `[VERIFY]`).
- **OI delta normalize** (`oi_chg_15m / oi`) ve **price-OI quadrant** (price↑OI↑, price↑OI↓, …).
- **Top trader long/short ratio** (5m, REST) — opsiyonel.
- **Listing age** (yeni listelenenler farklı davranır; ilk 7–14 gün hariç tutulur `[CAL]`).
- **Time-of-day / funding window** (funding saatleri civarı anormallik).
- **Mark–last divergence** (stop tetikleme mark price ile yapılırsa önemli).

**Çıkar / MVP dışı:** Liquidation activity (veri eksik), L2 full-depth diff book (maliyetli; depth20@500ms snapshot yeterli).

### E.2 Scanner: 200 → 10-15

"Scanner strateji olmasın" isteğine kısmen itiraz: **ölçülebilirlik için proposal'ın bir hipotezi olmalı.** Çözüm: tek strateji değil, *pluggable setup detector aileleri*. MVP'de 2 aile (istatistiksel güç için az):

1. **Eligibility (hard filters)** — tradability, strateji-bağımsız.
2. **Setup detectors** — her biri `TradeProposal` üretir:
   - `BRK` Momentum breakout (Donchian-N kırılımı + rvol + flow teyidi)
   - `PB` Trend pullback (trend içinde EMA/VWAP'a geri çekilme + reversal flow)
   - (Faz 3+) `SQZ` Funding/OI squeeze, `MR` extreme mean-reversion
3. **Rank + diversify**: aile içi cross-sectional rank → birleşik skor → korelasyon cluster'ı başına max 2, aile başına max K/2, top K=10–15.

Ayrıca: **Scanner "interest score" ≠ trade score.** Scanner recall odaklıdır (iyi fırsatı kaçırma), precision'ı Jev/ML filtresi sağlar.

---

## F. Risk Architecture (özet — detay Spec §10)

Katmanlar (her biri tek başına trade'i durdurabilir, hiçbiri AI tarafından override edilemez):

1. **Pre-trade checks** (per order): data freshness, spread, slippage estimate, min stop distance, liquidity cap, liquidation distance, symbol state.
2. **Portfolio limits**: max open positions (3), max risk per trade (0.25–0.5% `[CAL]` paper'da), max symbol exposure, beta-weighted net exposure, cluster limit, gross notional cap.
3. **Loss limits**: daily (−1.5%…−2% `[CAL]`), weekly, global DD (−8%…−10%) → HALTED + manuel inceleme.
4. **Circuit breakers**: BTC 5m move > X·σ, market-wide spread shock, exchange error rate, Jev drift.
5. **Operational guards**: feed disconnect, clock drift, reconciliation mismatch, DB failure, listenKey expiry.
6. **Kill switch**: dosya bayrağı + sinyal + (opsiyonel) Telegram komutu; entry emirlerini iptal, stop'ları korur, opsiyonel flatten.

Risk engine **state machine**: `NORMAL → CAUTION → REDUCE_ONLY → HALTED` (yukarı geçiş otomatik, aşağı geçiş HALTED'dan sadece manuel).

**Stop/TP başlangıç önerisi:** Initial stop = structure (swing) ve ATR'nin *daha genişi*, `k_atr ∈ [1.0, 2.0]` `[CAL]`; TP = **sabit R-multiple (1.5–2.0R `[CAL]`) + time stop H**. Neden: label (triple barrier) ile trade exit mantığı *birebir aynı* olur → Jev/ML'in tahmin ettiği şey tam olarak trade outcome'udur. Partial TP ve trailing, baseline doğrulandıktan sonra **ayrı arm** olarak test edilir (her biri ek parametre = ek overfitting).

**Leverage:** risk'i değil liquidation mesafesini belirler. Kural: `liq_distance ≥ 3 × stop_distance` `[FIX]` sağlayan minimum leverage, isolated margin, vol-tier üst sınırı (low: 5x, normal: 3x, high: 2x `[CAL]`).

---

## G. Backtesting / Calibration Methodology (özet — detay Spec §8, §13)

**Üç ayrı değerlendirme katmanı:**

| Katman | Veri | Amaç | Jev? |
|---|---|---|---|
| Historical replay | data.binance.vision + recorder | Quant baseline (A), setup'ların gross/net edge'i, cost modeli, altyapı | Hayır (contamination) — sadece contamination testi için sınırlı |
| Shadow (forward) | Canlı feed, trade yok | Jev + ML decision-level değerlendirme, calibration | **Evet — ana kanıt** |
| Paper portfolio | Canlı feed, simüle fill | Portfolio seviyesinde A/A1/B/C/R | Evet |

**Replay prensipleri:** event-driven, `event_time + feed_latency ≤ decision_time`, point-in-time universe (delisted dahil), bar-close finality (`k.x == true`), rolling/expanding normalization (asla full-sample z-score), pessimistic taker fills, funding cash-flow'ları, stop slippage modeli, latency injection (ölçülen Jev latency dağılımından örnekleme).

**Calibration:** per question, **tüm** proposal'lar üzerinde (sadece trade edilenler değil — selection bias). n < 300: raw + monitor; 300 ≤ n < 2000: Platt/beta calibration; n ≥ 2000: isotonic. Regime slicing hierarchical shrinkage ile: `p = w·p_slice + (1−w)·p_global, w = n_slice/(n_slice + 500)` `[CAL]`. Metrikler: Brier (+ Murphy decomposition), log loss, ECE (equal-width *ve* equal-mass bins), AUC, reliability diagram, test-retest variance.

---

## H. A/B Experimental Methodology (özet — detay Spec §14)

**Kollar (aynı event stream, aynı cost/latency/slippage modeli, aynı risk engine):**

| Arm | Tanım | Soru |
|---|---|---|
| **A0** | Tüm proposal'lar (filtre yok), EV gate'te sadece prior p | Setup'ların kendisi net pozitif mi? |
| **A1** | A0 + ML meta-labeler (logistic → GBM), walk-forward | Güçlü quant baseline |
| **R** | A0 + random filter; geçiş oranı yalnızca karar anından **önceki** B kararlarından (trailing 7g, causal) | "Az trade etmek" etkisi |
| **B** | A0 + Jev calibrated p (+ abnormal veto) | Jev filtre değeri |
| **B+** | A1 modeline Jev p'yi feature olarak ekle (stacking) | Jev ML'e *incremental* bilgi katıyor mu? — **asıl soru** |
| **C** | Jev direction-led (Jev side seçer, geometri deterministic) | Directional Jev deneysel |

**Birincil hipotez (pre-registered):** B+ modelinin out-of-sample log loss'u A1'den düşüktür (day-block bootstrap, one-sided α=0.05) **ve** B'nin net R/trade'i R arm'ından büyüktür.

**Portfolio path-dependency:** Max 3 pozisyon paylaşıldığı için arm'ların trade setleri path'e bağlıdır; bu yüzden birincil test decision-level, portfolio metrikleri ikincil.

---

## I. Data / Database Architecture (özet — detay Spec §12)

| Veri | Store | Retention |
|---|---|---|
| Raw recorded market data (book features 1s, depth20 snapshot 5s, OI 1m, mark 1s, forceOrder) | Parquet, `date=/symbol=` partition, zstd | Raw depth20: 30 gün; features: süresiz |
| Downloaded historical (klines, aggTrades, metrics, funding, premium) | Parquet (data.binance.vision'dan) | Süresiz (yeniden indirilebilir) |
| Feature snapshots (karar anında) | Parquet, `snapshot_id` ile | Süresiz |
| Decisions, Jev req/resp, policy, risk, orders, fills, positions, labels, calibration | PostgreSQL (OLTP; Timescale opsiyonel) | Süresiz |
| Hot state (positions, risk state) | Process memory + Postgres checkpoint | — |
| Analiz | DuckDB (Parquet + Postgres scan) | — |

Redis MVP'de yok (tek process). Postgres yerine başlangıçta SQLite da olur; ama shadow + paper paralel yazım için Postgres önerilir.

---

## J. Project Folder / Service Architecture

3 process (aynı codebase):
1. `jevbot record` — recorder (en basit, en stabil; ilk gün başlar).
2. `jevbot live --mode {shadow,paper,testnet,live}` — trader (market data + features + scanner + judgment + policy (tüm arm'lar) + risk + execution).
3. `jevbot replay` / `jevbot analyze` — offline.

Klasör yapısı Spec §19'da. Kritik prensip: **Live ve replay aynı core'u çalıştırır**; sadece `Clock`, `MarketDataSource`, `JudgeClient`, `ExecutionAdapter` implementasyonları değişir. "Parity test": aynı event stream → live core ve replay core bit-for-bit aynı kararları üretmeli.

---

## K. MVP → Production Roadmap

| Faz | İçerik | Süre (tahmini) | Çıkış kapısı |
|---|---|---|---|
| **0** | Recorder + universe + historical downloader | 1 hafta | 7 gün kesintisiz kayıt, gap raporu < %0.5 |
| **1** | Feature engine + scanner + labeler + replay engine + cost model; Arm A0 replay | 2–3 hafta | Setup'lar için gross edge raporu; parity testi geçer |
| **2** | Jev client + state builder + cache; **shadow mode** (canlı, trade yok); ML meta-labeler | 4–8 hafta veri | ≥ 3,000 label'lı proposal; calibration raporu; contamination testi |
| **3** | Paper portfolio: A0, A1, R, B, B+, C paralel | ≥ 8 hafta | Acceptance criteria L.2 |
| **4** | Testnet execution: order lifecycle, native stops, reconciliation, chaos testleri | 2–4 hafta | L.3 (sıfır korumasız pozisyon) |
| **5** | Live micro: küçük sermaye, risk 0.1–0.25%/trade | ≥ 8 hafta | L.4 implementation shortfall |
| **6** | Ölçekleme (risk %, sermaye, yeni setup aileleri, maker execution) | — | Sürekli monitoring |

Not: Faz 1'de setup'ların **gross** edge'i yoksa (maliyet öncesi bile ≤ 0), Jev filtresinin kurtarma şansı düşüktür — önce setup'lar düzeltilir. Meta-labeling precision artırır, sıfır recall'u kurtaramaz.

---

## L. Acceptance Criteria

Tüm eşikler `[CAL]` değil, **önceden kayda geçirilmiş (pre-registered) kapılar**dır; sonuçlar görüldükten sonra değiştirilmez.

### L.1 Shadow → Paper geçişi (Jev değerlendirmesi)
- ≥ 3,000 label'lı proposal, ≥ 4 hafta, ≥ 2 farklı BTC regime'i (ör. trend + range; deterministic regime classifier'a göre her birinde ≥ %25 örnek).
- Jev `trade_success` calibrated ECE ≤ 0.05 (holdout).
- Jev AUC (holdout) ≥ 0.55 ve day-block bootstrap %95 CI alt sınırı > 0.50.
- Contamination testi: tanımlayıcılı vs anonim state arasında AUC farkı < 0.02.
- Test-retest: aynı state için p'nin std'si < 0.05 (yoksa k-sample averaging gerekir).
- Jev p99 latency < 1.5 s; availability ≥ %99.

### L.2 Paper → Testnet/Live kapısı (strateji)
- Net expectancy (tüm maliyetler dahil) > 0, trade-level bootstrap %90 CI alt sınırı > 0; n ≥ 300 trade (arm başına).
- Profit factor ≥ 1.2; max DD ≤ 10 × risk_per_trade (R cinsinden ≤ 10R).
- Sonuç ≥ 2 regime'de pozitif veya en kötü regime'de ≥ −0.05R/trade.
- Deflated Sharpe Ratio > 0.95 (test edilen config sayısı ile düzeltilmiş).
- Jev için: B+ vs A1 log-loss iyileşmesi anlamlı (one-sided p < 0.05) **ve** B net R/trade > R arm (p < 0.05). Sağlanmazsa: live'a **Jev'siz** (A1) geçilebilir; Jev B+ shadow'da kalır.

### L.3 Testnet kapısı (operasyon)
- 0 korumasız pozisyon (fill sonrası 2 s içinde stop onayı %100).
- Chaos testleri (WS kopması, REST 5xx, 429, listenKey expiry, process kill -9, DB down) hepsinde doğru safe-mode.
- Reconciliation mismatch = 0 (veya tamamı otomatik çözülmüş ve loglanmış).
- 14 gün kesintisiz çalışma.

### L.4 Live micro → ölçekleme
- Implementation shortfall (live fill vs paper model) ortalaması ≤ modelin %150'si.
- Live net R/trade, paper'ın CI'ı içinde.
- ≥ 150 live trade.

---

## Kaynaklar

- Jev (TypeSafe) API: Choice / Score / Noul; Noul = P(yes); Choice ve Score kendi probability/confidence alanlarını döndürür; yayın tarihi 2026-09-15 — [jevmodel.org/api](https://jevmodel.org/api/), [HF blog: Jev API tutorial](https://huggingface.co/blog/sora-2/jev-ai-api-tutorial-build-your-first-structured-de), [OpenRouter: What is Jev](https://openrouter.ai/blog/insights/what-is-jev/)
- Jev finance/trading projeleri taraması (2026-09-20): "Jev judges, code executes" deseni, kripto projelerinde 70–500 ms latency, **hiçbir projede raporlanmış edge kanıtı yok** — [gist: drillan survey](https://gist.github.com/drillan/6916b16e8ea31a8ec36c8f59d6483150)
- Meta-labeling, triple-barrier, purged CV, deflated Sharpe: López de Prado, *Advances in Financial Machine Learning* (2018); Bailey & López de Prado, "The Deflated Sharpe Ratio" (2014).
