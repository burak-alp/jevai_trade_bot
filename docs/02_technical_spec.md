# 02 — Technical Specification (v0.1)

> Bu doküman `01_architecture_review.md`'deki kararların implementasyon spesifikasyonudur.
> Etiketler: `[CAL]` empirical calibration gerekir · `[FIX]` güvenlik sabiti, optimize edilmez · `[VERIFY]` dış API detayı, güncel dokümandan doğrulanmalı.

İçindekiler
0. Conventions · 1. Event model & flows · 2. Clock & latency · 3. Market data · 4. Feature spec · 5. Scanner · 6. TradeProposal & labels · 7. Jev judgment layer · 8. Calibration · 9. Policy & persistence · 10. Risk engine · 11. Execution · 12. Persistence & storage · 13. Replay engine · 14. Experiments & statistics · 15. Observability · 16. Testing · 17. Failure matrix · 18. Config · 19. Code layout · 20. First backlog

---

## 0. Conventions

| Konu | Kural |
|---|---|
| Zaman | `int` ms, UTC epoch. Her event iki zaman taşır: `t_event` (exchange `E`/`T`) ve `t_recv` (local monotonic → wall clock'a map). Kararlar `t_recv` bazlı "available time" ile alınır. |
| Kimlik | ULID (zaman sıralı). `proposal_id`, `decision_id`, `request_id`, … |
| Sayılar | Feature'lar `float64` (numpy). Emir fiyat/miktarı `Decimal`, `tickSize`/`stepSize`'a **aşağı** yuvarlanır (stop için güvenli yöne). |
| Birimler | Getiri: log-return. Mesafeler: `*_atr` (ATR katı), `*_bps`, `*_R` (1R = stop mesafesi). İsimde birim zorunlu. |
| Yön | `side ∈ {+1, −1}`. "Aligned" feature = `side × feature` (pozitif = pozisyon lehine). |
| Versiyon | Her bileşen `*_version` string taşır: `feature_schema`, `family`, `questionset`, `prompt`, `policy`, `risk`, `label`, `calibrator`. Her karar kaydı bunların hepsini referans alır. |
| Saflık | Feature, scanner, policy, risk, sizing, label fonksiyonları **pure** (I/O yok, `now()` yok; zaman parametre). Tüm I/O adapter'larda. |

---

## 1. Event Model & Flows

### 1.1 Event tipleri (frozen dataclass, `slots=True`)

```python
# core/events.py
@dataclass(frozen=True, slots=True)
class BarClosed:            # 1m, kline stream'de k.x == True
    symbol: str; open_time: int; close_time: int
    o: float; h: float; l: float; c: float
    base_vol: float; quote_vol: float; taker_buy_base: float; taker_buy_quote: float
    n_trades: int; t_event: int; t_recv: int

@dataclass(frozen=True, slots=True)
class BookTop:              # bookTicker (son değer tutulur, 1s'de aggregate)
    symbol: str; bid: float; bid_qty: float; ask: float; ask_qty: float
    update_id: int; t_event: int; t_recv: int

@dataclass(frozen=True, slots=True)
class BookSnapshot:         # depth20@500ms (sadece Tier-2)
    symbol: str; bids: np.ndarray; asks: np.ndarray   # shape (20, 2): price, qty
    t_event: int; t_recv: int

@dataclass(frozen=True, slots=True)
class MarkUpdate:           # !markPrice@arr@1s
    symbol: str; mark: float; index: float; funding_rate: float
    next_funding_time: int; t_event: int; t_recv: int

@dataclass(frozen=True, slots=True)
class OIUpdate:             # REST poll
    symbol: str; oi_base: float; t_server: int; t_recv: int

# Karar zinciri
DecisionTick(t)             → FeatureFrame → [TradeProposal] → [JudgeResult]
→ [CalibratedJudgment] → [PolicyDecision per arm] → [RiskVerdict] → [OrderIntent]
# Execution
OrderEvent, FillEvent, PositionUpdate, FundingEvent
# Sistem
HealthEvent(component, status, detail), RiskStateChange(old, new, reason), KillSwitch(action)
```

### 1.2 Karar döngüsü (decision clock = 5m bar close) `[CAL: 1m/5m/15m karşılaştırılır]`

```
t = 5m boundary (ör. 12:05:00.000)
│
├─ t+0..1500ms   : 1m bar'ların gelmesi beklenir (grace = 1500 ms [CAL])
│                  gelmeyen sembol → bu tick için STALE (ineligible)
├─ t+1500ms      : DecisionTick(t) yayınlanır
│   ├─ FeatureEngine.compute(t)            ~5-20 ms   (200×F vectorized)
│   ├─ Scanner.run(frame)                  ~1-5 ms    → K ≤ 12 proposal
│   ├─ MLMetaLabeler.predict(proposals)    ~1 ms
│   ├─ JevJudge.judge_all(proposals)       ≤ 1500 ms  (asyncio.gather, semaphore=16, hard deadline)
│   ├─ Calibrator.apply(...)               <1 ms
│   ├─ for arm in arms: Policy.decide(...) <1 ms
│   ├─ for arm in arms: Risk.check(...)    <1 ms
│   └─ Execution.submit(approved)          ~50-200 ms
│
├─ deadline      : t + 4000 ms [FIX]. Deadline'ı aşan karar zinciri iptal edilir (HOLD, reason=DEADLINE).
└─ persist       : tüm artefaktlar async writer kuyruğuna
```

Pozisyon yönetimi (time stop, reconciliation, protective order doğrulama) decision clock'tan **bağımsız** 1 s'lik task'ta çalışır.

### 1.3 Proses modeli

```
Process 1: jevbot record    — WS → Parquet (bağımsız, en stabil bileşen)
Process 2: jevbot live      — tek asyncio event loop (uvloop)
             ├─ ws tasks (N connection)
             ├─ rest pollers (OI, exchangeInfo, 24h ticker, fundingInfo, time)
             ├─ decision loop (5m)
             ├─ position manager loop (1s)
             ├─ reconciler loop (30s)
             ├─ labeler loop (1m; vadesi gelen proposal'ları label'lar)
             └─ writer thread (Parquet + Postgres batch)
Process 3: jevbot replay | analyze | calibrate | report   — offline
```

Neden recorder ayrı: trader'daki bir bug veya restart veri kaybına yol açmamalı; recorder Faz 0'da trader yokken çalışmaya başlar.

---

## 2. Clock & Latency

- `/fapi/v1/time` her 60 s: `offset = server_time − (t_send + t_recv)/2`, RTT > 500 ms olan ölçümler atılır, EWMA(α=0.2).
- `|offset| > 250 ms` → WARN; `> 500 ms` → yeni entry yok (`CAUTION`) `[FIX]`. NTP (chrony) zorunlu.
- Stream lag: `lag = t_recv_wall − t_event`; stream başına p50/p99 izlenir. `lag_p99(1m) > 2000 ms` → stream degraded.
- Replay için **latency modelleri** canlıdan ölçülür ve Parquet'e yazılır: `kline_delay` (close_time → t_recv), `jev_latency`, `order_ack_latency`, `fill_latency`. Replay bu empirical dağılımlardan örnekler (seed'li).

---

## 3. Market Data

### 3.1 Stream planı `[VERIFY: USDⓈ-M WS endpoint path'leri (2025–26'da /public, /market, /private ayrımı duyuruldu), stream/connection limitleri, 10 msg/s subscribe limiti]`

| Stream | Kapsam | Kullanım | Tahmini yük |
|---|---|---|---|
| `<sym>@kline_1m` | 200 sembol, 2 connection | OHLCV + taker buy volume (flow/CVD'nin 1m proxy'si) | ~200 msg/s (kline her ~250ms update) — sadece `x==true` işlenir, diğerleri drop |
| `!markPrice@arr@1s` | tüm semboller, 1 stream | mark, index, funding, next funding | 1 msg/s (büyük array) |
| `<sym>@bookTicker` | 200 sembol, 2-4 connection | spread, top-of-book, microprice | majors'ta çok yüksek frekans → handler sadece "latest" günceller, 1 s'de aggregate |
| `<sym>@depth20@500ms` | Tier-2 (≤ 40 sembol: scanner pre-score top 30 + açık pozisyonlar + BTC/ETH), dinamik SUBSCRIBE | depth@5/10/25bps, imbalance | 80 msg/s |
| `!forceOrder@arr` | tümü | sadece log (eksik veri, feature değil) | düşük |
| User Data Stream | hesap | ORDER_TRADE_UPDATE, ACCOUNT_UPDATE | düşük |

`aggTrade` MVP'de **yok** (1m kline taker buy volume yeterli; aggTrades tarihsel olarak zaten indirilebilir). Faz 3+ ablation ile değeri ölçülür.

**Tier-2 seçimi:** her 5m tick'te `prescore` (rvol_z, |resid_ret_1h_atr|, |funding_xs_z| rank ortalaması) top 30 + açık pozisyonlar. Değişiklik ≤ 10 sembol/tick (subscribe rate limit). Yeni subscribe edilen sembolün depth feature'ı ilk 10 s `NaN` → o tick'te microstructure gerektiren kontroller "unknown" = fail-safe (ineligible).

### 3.2 REST pollers

| Endpoint | Periyot | Not |
|---|---|---|
| `GET /fapi/v1/exchangeInfo` | 1 h + startup | status, filters (tickSize, stepSize, minNotional), onboardDate, deliveryDate |
| `GET /fapi/v1/fundingInfo` | 1 h | funding interval (1h/4h/8h) & cap/floor `[VERIFY]` |
| `GET /fapi/v1/ticker/24hr` (all) | 5 m | universe volume rank |
| `GET /fapi/v1/openInterest?symbol=` | round-robin: 200 sembol / 60 s ≈ 3.3 req/s | weight ~1 `[VERIFY]`; limit 2400/min'in çok altında |
| `GET /fapi/v1/klines` | reconnect/gap sonrası backfill | 1500 bar/istek |
| `GET /fapi/v1/time` | 60 s | clock offset |
| `GET /fapi/v2/positionRisk`, `/fapi/v1/openOrders` | 30 s (reconciler) | |

Rate-limit yönetimi: response header `X-MBX-USED-WEIGHT-1M` izlenir; > %70 → poller'lar yavaşlar; 429 → exponential backoff + `CAUTION`; 418 (IP ban) → `REDUCE_ONLY`, alarm.

### 3.3 Bağlantı sağlığı

```python
class StreamHealth:
    last_recv: dict[str, int]         # stream_key → t_recv
    expected_interval_ms: dict[str, int]

    def status(self, key, now) -> Literal["OK", "DEGRADED", "STALE"]:
        gap = now - self.last_recv[key]
        exp = self.expected_interval_ms[key]
        return "OK" if gap < 3*exp else "DEGRADED" if gap < 10*exp else "STALE"
```

- Kline: `close_time + 3000 ms` içinde final bar yoksa → sembol o tick STALE; `10 s` → REST backfill.
- Silent stall: connection açık ama o connection'daki *tüm* stream'ler > 10 s sessiz → forced reconnect.
- Her connection 23 h'de planlı yeniden bağlanır (Binance 24 h limiti `[VERIFY]`), iki connection aynı anda değil (overlap: yeni bağlan → eskiyi kapat).
- Reconnect sonrası: kline backfill, gap `[t_last, t_now]` `data_gaps` tablosuna, etkilenen sembol warmup kontrolünden tekrar geçer.

### 3.4 Universe (point-in-time)

Saatlik yeniden hesap; kural (hepsi):
- `contractType == PERPETUAL`, `quoteAsset == USDT`, `status == TRADING`
- `deliveryDate` uzak gelecekte (yakın tarihli deliveryDate = delist sinyali `[VERIFY]`)
- `listing_age ≥ 14 gün` `[CAL 7–30]`
- 24h quote volume ≥ $20M `[CAL 10–50M]`, rank ≤ 200
- Stable/stable ve index-benzeri pariteler hariç (USDCUSDT vb.)
- **Hysteresis:** giriş rank ≤ 200, çıkış rank > 230 (churn önleme)
- Her saatlik universe `symbols_pit` tablosuna yazılır (replay'de survivorship bias önleme).

### 3.5 Bar & buffer yapısı

1m bar stream'den; 5m/15m/1h bar'lar **1m'den aggregate** edilir (ayrı stream subscribe edilmez → tutarlılık).

```python
class SymbolBuffers:   # numpy structured ring buffers
    m1:  Ring(capacity=2_880)    # 2 gün 1m
    m15: Ring(capacity=2_880)    # 30 gün 15m
    h1:  Ring(capacity=2_160)    # 90 gün 1h
    book_1s: Ring(capacity=3_600)  # 1 saat, 1s aggregate: mid_c, spread_mean, spread_max, bid_qty, ask_qty
    oi_1m: Ring(capacity=10_080) # 7 gün
    # fields: o,h,l,c,v,qv,tbv,tbqv,n + mark_c + funding
```
Bellek ≈ 200 × (2880+2880+2160) × ~12 × 8 B ≈ 150 MB. Tamamen kabul edilebilir.

---

## 4. Feature Spec (`feature_schema = f.v1`)

Kurallar:
1. **Sadece trailing pencere.** Full-sample istatistik yasak.
2. Robust z: `rz(x) = clip((x − median_W) / (1.4826·MAD_W + ε), −5, 5)`.
3. Percentile: kendi geçmiş penceresine göre empirical CDF.
4. Warmup yetersizse `NaN` → sembol ineligible (NaN asla 0'a doldurulmaz).
5. `Avail`: **H** = tarihsel indirilebilir (data.binance.vision), **R** = sadece recorder ile.

| Grup | Feature | Formül / tanım | Pencere | Avail |
|---|---|---|---|---|
| Vol | `atr_15m` | Wilder ATR(14) on 15m | 14×15m | H |
| | `atr_pct` | `atr_15m / close` | | H |
| | `rv_1h` | std(1m log ret, 60) · √60 | 60×1m | H |
| | `rv_pctile_30d` | pct rank of `rv_1h` (15m sampled) vs own 30d | 2880×15m | H |
| Return | `ret_{5m,15m,1h,4h}_atr` | `ln(c_t/c_{t−h}) / atr_pct` | | H |
| | `resid_ret_1h_atr` | `(ret_1h − β·ret_1h_BTC) / atr_pct` | | H |
| | `beta_btc`, `corr_btc` | OLS/corr, 5m returns | 7d (2016×5m) | H |
| Trend | `er_1h`, `er_4h` | Kaufman: `|c_t − c_{t−n}| / Σ|Δc|` (1m / 15m) | | H |
| | `ema_spread_atr` | `(EMA20_15m − EMA50_15m)/atr_15m` | | H |
| | `dist_ema20_atr` | `(c − EMA20_15m)/atr_15m` | | H |
| | `dist_vwap4h_atr` | `(c − VWAP_4h)/atr_15m`, VWAP rolling 240×1m | | H |
| | `adx_15m` | ADX(14) — **ablation adayı** (er ile korelasyon izlenir) | | H |
| Structure | `donch_pos_32` | `(c − min L_32)/(max H_32 − min L_32)`, 15m, current bar hariç | 32×15m (8h) | H |
| | `dist_swing_hi_atr`, `dist_swing_lo_atr` | son onaylı 3-bar fractal pivot'a mesafe (15m) | ≤ 96×15m | H |
| Volume/Flow | `rvol_z_15m` | `rz(ln qv_15m)` vs son 7g aynı saat dilimi değil, son 672×15m | 7d | H |
| | `taker_imb_15m`, `taker_imb_1h` | `(2·tbqv − qv) / qv` | | H |
| | `cvd_z_1h` | `Σ_{60×1m}(2·tbqv − qv)` / std(aynı toplam, 7d) | 7d | H |
| Derivs | `funding_bps` | predicted funding (markPrice stream) ×1e4 | | R (H: settled) |
| | `funding_xs_z` | universe genelinde cross-sectional robust z | anlık | R/H≈ |
| | `funding_ts_z` | kendi 30g geçmişine göre rz | 30d | H |
| | `mins_to_funding` | `(next_funding_time − t)/60000` | | H |
| | `oi_chg_15m_pct`, `oi_chg_1h_pct` | `100·(OI_t/OI_{t−h} − 1)` | | R (1m) / H (5m metrics) |
| | `oi_z_1h` | `rz(oi_chg_1h_pct)` vs 7d | 7d | R/H |
| | `basis_bps` | `(mark − index)/index·1e4` | | H (premium/mark klines) |
| Micro | `spread_bps` | median spread son 60 s | 60 s | R |
| | `spread_pctile_24h` | spread_bps'in kendi 24h (1 m örneklem) içindeki pct | 24h | R |
| | `depth10_bid_usd`, `depth10_ask_usd` | mid ±10 bps içindeki notional (depth20'den) | anlık | R (Tier-2) |
| | `book_imb_10bps` | `(bid − ask)/(bid + ask)` depth10 | 60 s median | R (Tier-2) |
| | `microprice_dev_bps` | `(μ − mid)/mid·1e4`, `μ = (bid·askQ + ask·bidQ)/(bidQ+askQ)` | 60 s mean | R |
| Market | `btc_ret_1h_atr`, `btc_ret_4h_atr`, `btc_er_1h`, `btc_rv_pctile` | BTCUSDT için aynı formüller | | H |
| | `breadth_1h` | universe'de `ret_1h > 0` oranı | anlık | H |
| | `breadth_ema` | universe'de `c > EMA50_15m` oranı | anlık | H |
| | `xs_mom_pct_4h` | `ret_4h_atr`'in cross-sectional percentile'ı | anlık | H |
| Meta | `listing_age_d`, `cost_rt_bps` (§5.4), `atr_pct` | | | H/R |

**Deterministic regime (Jev'e sorulmaz, slicing & risk için):**
```
btc_trend = up    if btc ema_spread_atr > +0.5 and btc_er_4h > 0.3
          = down  if btc ema_spread_atr < −0.5 and btc_er_4h > 0.3
          = flat  otherwise                                     [CAL]
vol_state = low | normal | high  by btc_rv_pctile tercile (<0.33, <0.80, ≥0.80)  [CAL]
regime    = f"{btc_trend}/{vol_state}"   (9 hücre)
```

**Redundancy kontrolü (Faz 1 çıktısı):** feature'lar arası Spearman korelasyon matrisi; |ρ| > 0.85 olan çiftlerden biri Jev state'inden çıkarılır (ML modeli hepsini görebilir).

---

## 5. Scanner

### 5.1 Pipeline

```python
def scan(frame: FeatureFrame, ctx: ScanContext) -> list[TradeProposal]:
    eligible = eligibility_mask(frame, ctx)                     # §5.2
    raw = [p for det in ctx.detectors for p in det.detect(frame, eligible, ctx)]   # §5.3
    raw = [p for p in raw if geometry_ok(p) and p.cost_R <= ctx.cfg.max_cost_R]    # §5.4
    raw = dedup_and_cooldown(raw, ctx.recent_proposals)          # §5.5
    for fam in groupby(raw, "family"):
        cross_sectional_rank(fam)                                # §5.6
    return diversify_top_k(raw, ctx.clusters, ctx.cfg)           # §5.7
```

### 5.2 Eligibility (tradability, strateji-bağımsız; hepsi AND)

| Kural | Başlangıç | Etiket |
|---|---|---|
| Universe üyesi, status TRADING, warmup tamam, NaN yok | — | [FIX] |
| Data freshness: son 1m bar mevcut; book age ≤ 5 s; OI age ≤ 180 s; mark age ≤ 5 s | — | [FIX] |
| `spread_bps ≤ min(8, 3 × spread_median_24h)` | 3–10 bps | [CAL] |
| `atr_pct ∈ [0.15%, 3.0%]` | alt: cost dominasyonu, üst: disorderly | [CAL] |
| `rv_pctile_30d ≤ 0.98` | | [CAL] |
| `min(depth10_bid, depth10_ask) ≥ 10 × notional_max` (Tier-2 değilse: `quote_vol_1h ≥ 200 × notional_max`) | 5–20× | [CAL] |
| `mins_to_funding ≥ 3` | funding anı çevresi anormallik | [CAL] |
| Sembolde açık pozisyon yok (herhangi bir arm'da değil, **o arm'da**) & exit cooldown (30 dk) bitmiş | | [CAL] |

### 5.3 Setup detector'ları (MVP: 2 aile)

Her detector hem long hem short için simetrik çalışır (`s ∈ {+1,−1}`); her koşul değeri `reasons` içine loglanır.

**BRK — range breakout continuation (`family_version = brk.v1`)**
```
H32 = max(high_15m[-33:-1]); L32 = min(low_15m[-33:-1])   # current 15m bar hariç
long  trigger: close_5m > H32
short trigger: close_5m < L32
AND  rvol_z_15m ≥ 1.0                         [CAL 0.5–2.0]
AND  s · taker_imb_15m ≥ 0.05                 [CAL 0–0.15]
AND  s · ret_15m_atr ≤ 2.5                    [CAL 1.5–3.5]   # aşırı uzamış hareketi kovalama
AND  er_1h ≥ 0.30                             [CAL]
AND  (H32 − L32)/atr_15m ≥ 2.0                [CAL]           # anlamlı range
structural_stop = (H32 if s>0 else L32) − s · 0.5·atr_15m     # range içine geri dönüş = invalidation
horizon = 120 min                             [CAL 60–240]
```

**PB — trend pullback resumption (`pb.v1`)**
```
trend:     s · ema_spread_atr ≥ 0.5 AND er_4h ≥ 0.30
pullback:  son 12×15m içinde s·dist_ema20_atr ≥ 1.0 olmuş
           AND şimdi s·dist_ema20_atr ∈ [−0.5, +0.3]
trigger:   s·(close_5m − extreme_prev_5m) > 0     # long: close > önceki 5m high
           AND s · taker_imb_15m > 0
structural_stop = pullback swing extreme − s · 0.25·atr_15m
horizon = 180 min                             [CAL 90–360]
```

### 5.4 Geometry & cost

```
entry_ref  = mid (decision anı)
stop_dist  = |entry_ref − structural_stop|
stop_dist  = max(stop_dist, k_min·atr_15m)                 k_min = 1.0  [CAL 0.75–1.5]
reject if stop_dist > k_max·atr_15m                         k_max = 2.5  [CAL]
reject if stop_dist_bps < 4 × (spread_bps + slip_est_bps)   [FIX]
tp         = entry_ref + s · R_tp · stop_dist               R_tp = 1.5  [CAL 1.2–2.5]

cost_rt_bps = 2·fee_taker_bps + spread_bps + 2·slip_est_bps + funding_exp_bps
  fee_taker_bps = 5.0 (VIP0)                                           [VERIFY]
  slip_est_bps  = impact_bps(notional, depth10)                        # half-spread'ler zaten spread_bps terimi içinde
  impact_bps    = 10 · notional / depth10_side_usd                     [CAL; Tier-2 değilse 3 bps sabit taban]
  funding_exp_bps = max(0, s · funding_bps) · P(funding within horizon)
cost_R = cost_rt_bps / stop_dist_bps
gate:  cost_R ≤ 0.25                                                    [CAL 0.15–0.30]
```
Breakeven kazanma olasılığı (timeout'u yok sayarak): `p* = (1 + cost_R)/(R_tp + 1)`. R_tp=1.5, cost_R=0.2 → p* = 0.48. Bu sayı, filtrenin ulaşması gereken hedefi netleştirir.

### 5.5 Dedup & cooldown
Aynı `(symbol, family, side)` 30 dk `[CAL]` içinde yeniden önerilmez; istisna: fiyat, önceki proposal'ın trigger seviyesini `s·0.5·atr` daha ileri geçmişse (yeni bilgi). HOLD edilen proposal'lar da cooldown'a girer (tekrar tekrar aynı soruyu sormamak için).

### 5.6 Ranking (aile içi, cross-sectional)
```
BRK score = mean(pct(rvol_z_15m), pct(s·taker_imb_15m), pct(s·resid_ret_1h_atr), pct(−cost_R))
PB  score = mean(pct(s·ema_spread_atr), pct(er_4h), pct(s·taker_imb_15m), pct(−cost_R))
```
Percentile rank, o tick'teki aynı aile adayları + son 7 günün aynı aile adayları havuzuna göre (tick'te tek aday varsa da anlamlı olsun diye).

### 5.7 Diversification
- Korelasyon cluster'ları: saatlik, 7d 5m return korelasyonu, average-linkage hierarchical clustering, kesim `ρ ≥ 0.75` `[CAL]`.
- Greedy seçim: score'a göre sırala; kabul koşulu: `total < K (12)`, `family_count < 8`, `cluster_same_side_count < 2`.
- **Scanner recall kontrolü (opsiyonel, Faz 2):** her tick'te seçilmeyen eligible adaylardan 3 rastgele proposal da label'lanır (Jev'e sorulmaz) → scanner'ın ranking'inin değer katıp katmadığı ölçülür.

---

## 6. TradeProposal & Labels

### 6.1 Veri yapısı

```python
@dataclass(frozen=True, slots=True)
class TradeProposal:
    proposal_id: str
    t_decision: int
    symbol: str
    family: str; family_version: str
    side: int                    # +1 / −1
    entry_ref: float; stop_price: float; tp_price: float
    stop_dist_bps: float; r_tp: float; horizon_s: int
    cost_rt_bps: float; cost_R: float
    scanner_score: float; scanner_rank: int
    regime: str                  # deterministic, §4
    reasons: Mapping[str, float] # detector koşul değerleri
    features: Mapping[str, float]# bu sembolün f.v1 snapshot'ı
    feature_schema: str
```

### 6.2 Triple-barrier labeler (`label_version = lbl.v1`)

Label, **trade exit mantığı ile aynı kod** kullanılarak hesaplanır (§10.6 ile birebir).

```python
def label(p: TradeProposal, path: PricePath, cfg) -> Label:
    # path: t_decision + entry_latency'den itibaren mark price 1m (varsa 1s) bar'ları + last price
    entry = p.entry_ref * (1 + p.side * cfg.entry_slip_bps/1e4)       # modeled fill
    sl, tp = p.stop_price, p.tp_price
    for bar in path.until(p.t_decision + p.horizon_s*1000):
        hit_sl = (bar.mark_low <= sl) if p.side > 0 else (bar.mark_high >= sl)
        hit_tp = (bar.high >= tp)     if p.side > 0 else (bar.low <= tp)   # TP: limit/last price
        if hit_sl and hit_tp:
            first = resolve_intrabar(bar, path.fine) or "SL"               # çözülemezse pesimist
        elif hit_sl: first = "SL"
        elif hit_tp: first = "TP"
        else: continue
        return make_label(first, exit_price=..., stop_slip=model_stop_slip(bar))
    return make_label("TIME", exit_price=path.close_at(horizon_end))

# Label alanları:
y_success   = 1 if exit == "TP" else 0
realized_R  = side·(exit_price − entry)/stop_dist
net_R       = realized_R − cost_R_realized − funding_R
mae_R, mfe_R (path boyunca, stop yokmuş gibi H'ye kadar da ayrıca: mae_R_full)
y_abnormal  = 1 if (mae_R_full ≥ 2.0) or (max_spread_H ≥ 4 × spread_median_24h)
                  or (stop_slip_R ≥ 0.3)                                  [CAL tanım]
y_direction = up/flat/down: ret_H/atr_pct > +0.5 / arada / < −0.5         (Bot C)
```

- Label hesaplama zamanı: `t_decision + horizon + 5 dk` (veri gecikmesi payı).
- **Tüm** proposal'lar label'lanır (trade edilen/edilmeyen, tüm arm'lardan bağımsız). Calibration ve A/B bu tabloyu kullanır → selection bias yok.
- Stop tetikleme **mark price** ile (live'da `workingType=MARK_PRICE`), fill **last price** + slippage. Label da aynı.

---

## 7. Jev Judgment Layer

### 7.1 Canonical state builder (`state.v1`)

İlkeler: anonim (sembol/tarih/mutlak fiyat yok), side-canonicalized (pozitif = pozisyon lehine), yuvarlanmış (2 ondalık), sabit anahtar sırası, birim anahtar adında, ≤ ~400 token.

```python
ALIGNED = {"ret_5m_atr","ret_15m_atr","ret_1h_atr","ret_4h_atr","resid_ret_1h_atr",
           "ema_spread_atr","dist_ema20_atr","dist_vwap4h_atr","taker_imb_15m","taker_imb_1h",
           "cvd_z_1h","book_imb_10bps","microprice_dev_bps","basis_bps",
           "btc_ret_1h_atr","btc_ret_4h_atr"}
def build_state(p: TradeProposal) -> dict:
    f, s = p.features, p.side
    a = {k: round(s * f[k], 2) for k in ALIGNED_ASSET}           # aligned
    a |= {k: round(f[k], 2) for k in NEUTRAL_ASSET}              # er, rvol_z, rv_pctile, spread_pctile, oi_chg, beta...
    a["funding_carry_against_bps"] = round(s * f["funding_bps"], 2)   # >0: pozisyon funding öder
    a["oi_chg_1h_pct_x_side_ret_sign"] = ...                     # price/OI quadrant, aligned
    m = {"btc_ret_1h_atr_aligned": round(s*f["btc_ret_1h_atr"],2), ...,
         "breadth_1h_aligned": round(f["breadth_1h"] if s>0 else 1-f["breadth_1h"], 2)}
    return {"schema": "state.v1",
            "proposal": {"family": FAMILY_NEUTRAL_NAME[p.family],   # "range_break" | "trend_pullback"
                         "stop_dist_atr": ..., "tp_R": p.r_tp, "horizon_min": p.horizon_s//60,
                         "cost_R": round(p.cost_R, 2)},
            "asset": a, "market": m}
```

Örnek (≈ 350 token):
```json
{"schema":"state.v1",
 "proposal":{"family":"range_break","stop_dist_atr":1.4,"tp_R":1.5,"horizon_min":120,"cost_R":0.14},
 "asset":{"ret_5m_atr":0.42,"ret_15m_atr":1.1,"ret_1h_atr":1.9,"ret_4h_atr":2.3,"resid_ret_1h_atr":1.2,
          "er_1h":0.46,"ema_spread_atr":0.8,"dist_ema20_atr":1.6,"dist_vwap4h_atr":1.9,
          "rv_pctile_30d":0.71,"rvol_z_15m":2.1,"taker_imb_15m":0.18,"cvd_z_1h":1.7,
          "book_imb_10bps":0.22,"spread_pctile_24h":0.4,"oi_chg_1h_pct":2.4,
          "funding_carry_against_bps":1.0,"funding_xs_z":0.6,"basis_bps":3.1,"beta_btc":1.3},
 "market":{"btc_ret_1h_atr_aligned":0.3,"btc_ret_4h_atr_aligned":-0.2,"btc_er_1h":0.2,
           "btc_rv_pctile":0.5,"breadth_1h_aligned":0.58}}
```

**State variant'ları** (`judge_requests.variant`):
- `canonical` — Bot B/B+ için standart.
- `raw` — side-aligned değil, proposal side'ı açıkça yazılı (bias ve Bot C için).
- `identified` — sembol + tarih eklenmiş (**sadece contamination testi**, %5 örneklem, policy'de asla kullanılmaz).

### 7.2 Question set `qs.v1`

```yaml
questionset: qs.v1
questions:
  - id: trade_success
    type: noul
    text: >
      A position has just been opened in the proposal direction. Every direction-dependent
      field in the state is expressed so that positive values favor this position.
      Exit rules: take-profit at +{tp_R}R, stop-loss at -1R (1R = {stop_dist_atr} ATR),
      otherwise exit after {horizon_min} minutes.
      Will the take-profit be reached before the stop-loss and before the time exit?
    label: labels.y_success
  - id: abnormal_risk
    type: noul
    text: >
      Within the next {horizon_min} minutes, will this market become disorderly for this
      position: an adverse move larger than 2R, a stop fill slippage above 0.3R,
      or a spread widening above 4x its recent median?
    label: labels.y_abnormal
# Sadece Bot C (variant=raw):
  - id: direction_h
    type: choice
    options: [up, flat, down]
    text: >
      Over the next {horizon_min} minutes, will the price change be above +0.5 ATR (up),
      below -0.5 ATR (down), or in between (flat)?
    label: labels.y_direction
```
Metinlerde yargı sözcüğü yok ("strong", "bullish", "healthy" vb. yasak — lint kuralı, §16).

### 7.3 Request / response

Jev API "State, Model, Questions → typed results by question ID" modelini kullanır; Noul = P(yes), Choice/Score kendi probability/confidence alanlarını döndürür. Tam alan adları `[VERIFY]` — adapter katmanı bizim iç şemamıza map'ler:

```python
@dataclass(frozen=True, slots=True)
class JudgeRequest:
    request_id: str; proposal_id: str; variant: str
    questionset_version: str; prompt_hash: str
    model_id: str; model_version: str; sampling: Mapping[str, Any]   # temperature/seed varsa sabit [VERIFY]
    state: Mapping[str, Any]; state_hash: str
    t_sent: int; deadline: int

@dataclass(frozen=True, slots=True)
class JudgeResult:
    request_id: str; status: Literal["ok","timeout","error","late","invalid","cache_hit"]
    probs: Mapping[str, float]          # noul: {"trade_success": 0.61}; choice: {"direction_h.up": 0.5, ...}
    confidence: Mapping[str, float]     # API sağlıyorsa
    raw: Mapping[str, Any]; latency_ms: int; t_received: int
```

Validasyon (pydantic): tüm beklenen `question_id`'ler mevcut, `0 ≤ p ≤ 1`, Choice olasılıkları toplamı `|Σ−1| < 0.02` (normalize edilir), aksi → `invalid` (= yokmuş gibi, HOLD).

### 7.4 Client davranışı

```python
class JevJudge:
    timeout_ms = 1500            # [CAL] canlı p99 latency × 1.3, max 2000 [FIX]
    max_concurrency = 16
    breaker = CircuitBreaker(fail_threshold=5, window_s=120, error_rate=0.2, cooldown_s=60)

    async def judge_all(self, proposals, t_tick) -> dict[str, JudgeResult]:
        if self.breaker.open: return {p.proposal_id: JudgeResult.down(p) for p in proposals}
        deadline = t_tick + self.timeout_ms
        tasks = [self._one(p, deadline) for p in proposals]
        return dict(await asyncio.gather(*tasks))       # _one asla raise etmez

    async def _one(self, p, deadline):
        req = build_request(p)
        if hit := self.cache.get(req.cache_key): return p.proposal_id, hit.as_cache_hit()
        try:
            async with self.sem:
                resp = await asyncio.wait_for(self.http.post(...), timeout=remaining(deadline))
        except (asyncio.TimeoutError, httpx.HTTPError) as e:
            self.breaker.record_failure(); return p.proposal_id, JudgeResult.fail(req, e)
        ...
```
- **Karar içinde retry yok** (deadline'ı aşar). Tek istisna: bağlantı-seviyesi hata ve kalan süre > 1000 ms.
- `t_received > deadline` → `late`: loglanır (latency istatistiği için), **karar için kullanılmaz** [FIX].
- Bir JudgeResult sadece üretildiği `t_decision` tick'inde geçerlidir; sonraki tick'te yeniden kullanılmaz [FIX]. (Stale Jev response ile asla entry yok.)
- Günlük çağrı bütçesi `max_calls_per_day` [CAL]; aşılırsa B arm'ları HOLD.

### 7.5 Cache

```
cache_key = sha256( canonical_json(state) ‖ questionset_version ‖ prompt_hash
                    ‖ model_id ‖ model_version ‖ canonical_json(sampling) )
value     = {raw_response, probs, latency_ms, t_first_seen, variant}
```
- Key'de ve value'da **outcome/label bilgisi yok**; `t_first_seen` sadece audit içindir.
- Live: yalnızca aynı-state dedup (nadiren isabet eder).
- Replay: (a) `cache_only` modu shadow döneminde kaydedilmiş cevapları *aynen* tekrar oynatır → farklı policy eşiklerini Jev'i yeniden çağırmadan test etmek (ana kullanım). (b) `model_version` uyuşmazlığı → miss (asla farklı sürümün cevabı kullanılmaz).
- Nondeterminism: cache replay'de reproducibility sağlar ama modelin varyansını gizler → test-retest ayrıca ölçülür (§8.6).

### 7.6 Ek tutarlılık testleri (offline, günlük örneklem)
- **Side bias:** aynı sembol-zaman için `raw` variant ile long ve short proposal sor; `mean(p_long) − mean(p_short)` vs gerçekleşen oranlar farkı. Sistematik offset → canonical variant'ın bias'ı gidermedeki değeri ölçülür.
- **Monotonicity sanity:** tek bir aligned feature'ı (ör. `taker_imb_15m`) +/− perturbe et; p'nin yönü ekonomik olarak tutarlı mı? (Tutarsızlık = model state'i okumuyor olabilir.)
- **Contamination:** `identified` vs `canonical` AUC farkı (acceptance: < 0.02).

---

## 8. Calibration

### 8.1 Veri
`proposals ⋈ judge_answers ⋈ labels`, `variant='canonical'`, `status='ok'`. **Tüm** proposal'lar (trade edilip edilmemesinden bağımsız).

### 8.2 Fit prosedürü (gece, 00:30 UTC)
```
window     = son 30 gün, sample weight = 0.5^(age_days/14)            [CAL]
purge      = label penceresi [t, t+H] fit/holdout sınırını kesen örnekler atılır
embargo    = 1 gün
holdout    = son 5 gün (fit'e girmez; yeni calibrator yalnızca holdout log-loss'u
             aktif calibrator'dan ≤ 0.002 kötü değilse aktive edilir, aksi alarm)
method     = n <  300           → identity (raw p) + UYARI (policy B'de entry kapalı)
             300 ≤ n < 2000     → Platt:     p_cal = σ(a·logit(p_raw) + b)
             n ≥ 2000           → Isotonic (out_of_bounds="clip"), + Platt ile 50/50 blend
                                   eğer holdout'ta isotonic tek başına daha kötüyse
clip       = p_raw ∈ [1e-4, 1 − 1e-4]
```
Choice (direction_h, 3 sınıf): temperature scaling on log-probs (tek parametre), n ≥ 3000'de Dirichlet/vector scaling denenir.

### 8.3 Regime-dependent calibration (hierarchical shrinkage)
```
p_global = C_global(p_raw)
p_slice  = C_slice(p_raw)            # slice = regime (9 hücre) veya family
w        = n_slice / (n_slice + k),  k = 500                               [CAL]
p_cal    = w·p_slice + (1 − w)·p_global
```
Slice calibrator yalnızca `n_slice ≥ 300` ise fit edilir.

### 8.4 Uncertainty (policy'nin kullandığı alt sınır)
Calibrated p'nin düştüğü equal-mass bucket'ın (10 bucket) Wilson %80 alt sınırı ile noktasal tahminin farkı: `p_lb = p_cal − (bucket_hit_rate − wilson_lb_80)`. Basit, muhafazakâr; az veride otomatik olarak girişleri azaltır.

### 8.5 Metrikler (her question × source(jev/ml/stack) × slice)

| Metrik | Formül |
|---|---|
| Brier | `BS = (1/N) Σ (p_i − y_i)²` ; Murphy: `BS = REL − RES + UNC` (RES = ayırt etme gücü — asıl önemli olan) |
| Log loss | `−(1/N) Σ [y ln p + (1−y) ln(1−p)]` |
| ECE | `Σ_b (n_b/N)·|ȳ_b − p̄_b|`; hem 10 equal-width hem 10 equal-mass bucket |
| Calibration slope/intercept | `logit P(y=1) = α + β·logit(p)`; ideal β=1, α=0 |
| AUC | + day-block bootstrap CI |
| Bucket raporu | bucket, n, mean_p, hit_rate (Wilson CI), mean realized_R, mean net_R, EV_pred = `p·R_tp + (1−p)·m_fail − cost_R`, EV_realized, win_rate(net_R>0) |

Not: Jev olasılıkları dar bir aralıkta kümelenebilir (ör. hepsi 0.55–0.75). Bu yüzden equal-mass bucket'lar ve AUC, sabit 0.5–1.0 bucket'larından daha bilgilendiricidir.

### 8.6 Test-retest & drift
- Günlük 50 rastgele geçmiş state × 3 tekrar (cache bypass) → `std(p)` dağılımı. Medyan std > 0.05 → k-sample averaging (maliyet × k) değerlendirilir.
- **PSI** (haftalık, raw p dağılımı, 10 bin, referans = önceki 4 hafta): `PSI = Σ (a_i − e_i)·ln(a_i/e_i)`; > 0.10 WARN, > 0.25 ALARM → B arm'ları CAUTION + refit.
- `model_version` değişikliği (API response'unda raporlanıyorsa) → otomatik ALARM, B arm'ları yeni entry durdurur, yeni sürüm için shadow kalibrasyonu baştan.

---

## 9. Policy Engine & Persistence/Hysteresis

### 9.1 EV gate (magic weighted score yerine)

```
p      = calibrated success probability (arm'ın kaynağından: prior | ml | jev | stack)
m_fail = E[net realized_R | not TP]   (family bazında, son 30g label'lardan; başlangıç −0.85)   [CAL]
EV_R   = p·R_tp + (1 − p)·m_fail − cost_R
EV_lb  = p_lb·R_tp + (1 − p_lb)·m_fail − cost_R
```
`m_fail` timeout'lu çıkışları (−1R'den iyi) ve stop slippage'ı (−1R'den kötü) empirik olarak içerir; "−1R" varsayımı yapılmaz.

### 9.2 Karar ağacı (pure function)

```python
def decide(p: TradeProposal, j: CalibratedJudgment | None, arm: ArmConfig,
           armed: ArmedState | None, t: int) -> PolicyDecision:
    # 0) kaynak yoksa
    if arm.source in ("jev", "stack") and (j is None or j.status != "ok" or j.t_decision != t):
        return HOLD("JUDGE_UNAVAILABLE")
    # 1) veto (asimetrik: sadece engeller)
    if arm.use_abnormal_veto and j.p_abnormal_cal >= arm.theta_abnormal:      # 0.30 [CAL 0.2–0.5]
        return HOLD("ABNORMAL_VETO")
    # 2) calibrator yeterli mi
    if arm.source != "prior" and j.calibrator_n < 300:
        return HOLD("UNCALIBRATED")
    # 3) EV
    ev, ev_lb = ev_r(...), ev_lb_r(...)
    if ev_lb >= arm.eps_high:                    # 0.20R [CAL 0.10–0.35]
        return ENTER("EV_HIGH", ev, ev_lb)
    if ev_lb >= arm.eps_in:                      # 0.05R [CAL 0.00–0.15]
        return ARM("EV_OK_AWAIT_CONFIRM", ev, ev_lb, expires=t + arm.confirm_ticks*TICK)
    return HOLD("EV_LOW", ev, ev_lb)
```

Arm kaynakları:

| Arm | `source` | Not |
|---|---|---|
| A0 | none | Gate yok: eligibility + geometry + cost_R geçen her proposal ENTER (risk limitleri dahilinde). Setup'ların ham edge'i. |
| A1 | `ml` | Walk-forward logistic regression (Faz 2), sonra LightGBM; aynı f.v1 feature'ları + family + regime |
| R | `random` | `P(enter) = pass_rate_B` (son 7g), `u = hash(proposal_id, seed) / 2^64` → reproducible |
| B | `jev` | Jev `trade_success` calibrated + `abnormal_risk` veto |
| B+ | `stack` | logistic: `logit p = w0 + w1·logit(p_ml) + w2·logit(p_jev)` (+ veto) |
| C | `jev_direction` | Side = Jev `direction_h`'dan (`P(up) − P(down) ≥ δ=0.2` [CAL]); geometri family template'inden; p = calibrated P(side yönünde up/down) ile aynı EV formülü (R_tp/stop aynı) |

### 9.3 Persistence / confirmation (hysteresis)

Tasarım prensibi: **teyit = yeni piyasa bilgisi**, aynı state'in tekrar sorulması değil.

```
ARMED durumu (proposal başına, arm başına):
  her decision tick'te (5m):
    1) invalidation: family'nin invalidation kuralı bozulduysa → DROP
         BRK: close_5m tekrar range içine döndü (long: close < H32)
         PB : yeni swing extreme stop seviyesini ihlal etti
    2) market confirmation:
         BRK: close_5m, trigger seviyesinin ötesinde kaldı AND s·taker_imb_5m ≥ 0
         PB : close_5m > trigger bar'ın extreme'i (long) AND yeni lower-low yok
    3) state değişimi: yeni 5m bar olduğu için feature'lar yenilenir;
       Jev yeniden çağrılır (yeni tick = yeni bilgi; eski cevap kullanılmaz)
    4) geometri yeniden hesaplanır (entry = yeni mid, stop = aynı structural stop,
       stop_dist ATR sınırları & cost_R yeniden kontrol; entry trigger'dan > 0.5R uzaklaştıysa → DROP "CHASE")
    5) EV_lb ≥ eps_in AND confirmation → ENTER
  confirm_ticks = 2 (10 dk) sonra hâlâ ENTER yoksa → EXPIRED    [CAL 1–3]
```

**Opportunity cost ölçümü (shadow/replay'de zorunlu):** her ARM kararı için iki hipotetik trade label'lanır: `immediate` (t'de giriş) ve `confirmed` (gerçekleşen teyit anında giriş, teyit gelmezse "no trade"). Karşılaştırma:
```
Δ_confirm = mean(net_R | confirmed path) − mean(net_R | immediate)   (aynı ARM kümesi, no-trade = 0R)
```
`Δ_confirm < 0` anlamlıysa teyit kaldırılır (eps_high = eps_in). Bu, "2-of-3" tartışmasını veriyle çözer.

**Neden EWMA değil:** EWMA ardışık Jev cevaplarını ortalar; cevaplar aynı state'e yakın olduğundan korelasyonludur ve gecikme ekler. Yukarıdaki yapı daha az parametre ile gecikmeyi sınırlar (max 10 dk) ve teyidi piyasaya bağlar. EWMA varyantı yine de ayrı arm olarak test edilebilir (`λ = 0.5`, `s_t = λ·s_{t−1} + (1−λ)·EV_lb,t`).

### 9.4 Exit policy (MVP)
Jev exit kararı vermez. Çıkış: native SL, native TP, bot-side time stop, risk engine/kill switch. "Abnormal spike'ta erken çıkış" Faz 3+ ayrı arm.

---

## 10. Risk Engine

### 10.1 State machine

```
            ┌──────── otomatik ────────┐
NORMAL ──▶ CAUTION ──▶ REDUCE_ONLY ──▶ HALTED
  ▲           │             │             │
  └─ otomatik (koşul 30 dk temiz) ┘      └── sadece manuel (CLI + sebep notu)
```
| State | Yeni entry | Risk çarpanı | Mevcut pozisyon |
|---|---|---|---|
| NORMAL | evet | 1.0 | normal |
| CAUTION | evet | 0.5 | normal |
| REDUCE_ONLY | hayır | — | stop/TP korunur, time stop çalışır |
| HALTED | hayır | — | REDUCE_ONLY + (config'e göre) flatten |

Risk engine her arm için ayrı *portfolio* state tutar, fakat **sistem-seviyesi** guard'lar (feed, exchange, clock, DB, kill) tüm arm'lara aynı anda uygulanır.

### 10.2 Position sizing

```
r_base      = 0.25% equity                                   [CAL 0.1–0.5%, paper MVP]
r           = r_base × m_state × m_streak                   (m_state: CAUTION 0.5; m_streak §10.4)
risk_usd    = equity × r
stop_slip   = expected stop slippage (price)                 # model: max(2·slip_est, 0.1·atr_15m) [CAL]
qty_risk    = risk_usd / (|entry − stop| + stop_slip)
notional    = qty_risk × entry
qty         = min(qty_risk,
                  cap_liquidity / entry,       cap_liquidity = 0.10 × min(depth10_bid, depth10_ask)   [CAL 0.05–0.2]
                  cap_volume / entry,          cap_volume    = 0.005 × quote_vol_1h                    [CAL]
                  cap_position / entry)        cap_position  = 1.0 × equity                            [CAL]
qty         = floor_to_step(qty); reject if qty·entry < minNotional or qty < minQty
```
Vol-normalization zaten stop-based sizing ile sağlanır (geniş stop → küçük pozisyon). Fractional Kelly: yalnızca L.2 sağlandıktan ve ≥ 500 trade ile `edge/variance` tahmini CI'ı dar olduktan sonra, `f = 0.1–0.25 × Kelly` ve `r_base` üst sınırı ile.

### 10.3 Portfolio limitleri

| Limit | Formül | Başlangıç |
|---|---|---|
| Max open positions | count | 3 [FIX MVP] |
| Per symbol | 1 pozisyon, arm başına | [FIX] |
| Total open risk | `Σ qty_i·(|entry_i − stop_i| + stop_slip_i)` | ≤ 0.75% equity [CAL] |
| Beta-weighted net risk | `|Σ side_i · β_i · risk_i|` | ≤ 2 × r·equity [CAL] |
| Cluster | aynı cluster, aynı side pozisyon sayısı | ≤ 2 [CAL] |
| Gross notional | `Σ notional_i` | ≤ 3.0 × equity [CAL] |
| Gap stress | `Σ qty_i·(|entry−stop| + 3·stop_slip_i)` (korelasyonlu gap senaryosu) | ≤ 1.5% equity [CAL] |
| Daily trade cap | yeni entry/gün | ≤ 20 [CAL] — overtrading sigortası |

### 10.4 Loss limitleri

| Limit | Tetik | Aksiyon |
|---|---|---|
| Daily loss | realized + unrealized (UTC gün) ≤ −1.5% equity (≈ −6R) | REDUCE_ONLY → ertesi UTC 00:00 NORMAL [CAL] |
| Weekly loss | ≤ −4% | HALTED (manuel) [CAL] |
| Global drawdown | equity peak'ten ≤ −8% | HALTED (manuel + post-mortem) [CAL 6–10%] |
| Consecutive losses | 5 ardışık net_R < 0 | m_streak = 0.5, 24 h [CAL] |
| | 8 ardışık | HALTED [CAL] |

Not: p_loss = 0.55 iken 5 ardışık kayıp olasılığı her başlangıç noktası için ≈ %5 — yani *beklenen* bir olay; bu guard sinyal değil, "sistem bozuldu mu?" sigortasıdır.

### 10.5 Pre-trade checks (her emirden hemen önce; biri fail → reject + reason)

```python
CHECKS = [
  ("risk_state_allows_entry", lambda c: c.state in ("NORMAL","CAUTION")),
  ("kill_switch_off",         lambda c: not c.kill),
  ("judge_fresh",             lambda c: c.arm.source not in JEV_SOURCES or c.judge.t_decision == c.t_tick),
  ("book_fresh",              lambda c: c.now - c.book.t_recv <= 2_000),                          # [FIX]
  ("bar_fresh",               lambda c: c.last_bar_close >= c.t_tick - 60_000),
  ("clock_ok",                lambda c: abs(c.clock_offset) <= 500),                               # [FIX]
  ("spread_ok",               lambda c: c.spread_now <= min(3*c.spread_med_1h, 10)),             # [CAL]
  ("slippage_ok",             lambda c: c.expected_slip_R <= 0.10),                                # [CAL]
  ("min_stop_dist",           lambda c: c.stop_dist_bps >= 4*(c.spread_bps + c.slip_bps)),        # [FIX]
  ("liq_distance",            lambda c: c.liq_dist_pct >= 3 * c.stop_dist_pct),                    # [FIX]
  ("exchange_healthy",        lambda c: c.err5xx_60s < 3 and c.last_429_age_s > 300),
  ("user_stream_alive",       lambda c: c.user_stream_ok),
  ("reconciled_recently",     lambda c: c.now - c.last_reconcile_ok <= 60_000),
  ("db_healthy",              lambda c: c.db_lag_s <= 10),
  ("portfolio_limits",        portfolio_limits_ok),          # §10.3
  ("loss_limits",             loss_limits_ok),               # §10.4
  ("circuit_breakers_clear",  breakers_clear),               # §10.7
]
```
Her check'in `value`, `limit`, `pass` değeri `risk_verdicts.checks` JSONB'ye yazılır.

### 10.6 Stop / TP / time stop

**Başlangıç tasarımı (MVP):**
- Initial stop: §5.4 (structure ∨ ATR, `[k_min, k_max]` ATR bandı).
- TP: sabit `R_tp = 1.5R` `[CAL]` — native.
- Time stop: `horizon` sonunda reduce-only market close (bot-side).
- Breakeven/trailing yok.

Gerekçe: exit mantığı label ile **birebir aynı** → tahmin edilen şey = trade outcome; kalibrasyon doğrudan EV'ye çevrilebilir; parametre sayısı minimum.

**Karşılaştırma (Faz 3'te ayrı arm'lar, aynı proposal'lar üzerinde label-level simülasyonla ucuza test edilir):**

| Variant | Kural | Artı | Eksi |
|---|---|---|---|
| T1 fixed R (MVP) | TP 1.5R, SL 1R, time H | Label uyumu, basit | Trend'lerde kazancı keser |
| T2 ATR TP | TP = k·ATR | Vol-adaptive | R-multiple değişken → EV hesabı karmaşık |
| T3 structure TP | Sonraki swing/range hedefi | Piyasa yapısına uygun | Tanım subjektif, az örnek |
| T4 partial | %50 @1R + stop→BE, kalan @2.5R | Win-rate ↑, varyans ↓ | 2× fee/emir; ortalama kazanç ↓; BE stop gürültüde tetiklenir |
| T5 trailing | +1R sonrası chandelier 3×ATR | Fat-tail yakalar | Trend azsa geri verir; en çok parametre |

Karar kriteri: net R/trade ve R-cinsinden maxDD; win-rate değil.

### 10.7 Circuit breakers `[CAL]`

| Breaker | Tetik | Aksiyon |
|---|---|---|
| BTC shock | `|btc_ret_5m| > 4σ_5m(7d)` | 30 dk yeni entry yok |
| Market spread shock | universe medyan `spread_pctile_24h > 0.95` | yeni entry yok (koşul + 10 dk) |
| Stale universe | STALE sembol oranı > %10 | CAUTION; > %25 → REDUCE_ONLY |
| Realized slippage drift | son 20 fill ortalama slippage > 2 × model | CAUTION; > 3× → REDUCE_ONLY |
| Jev drift | PSI > 0.25 veya model_version değişti | B/B+/C arm'ları yeni entry yok |
| Funding extreme | sembol `|funding| > 0.3%/interval` | o sembol ineligible |

### 10.8 Leverage

```
mmr       = maintenance margin rate (symbol leverage bracket'ından)        [VERIFY: /fapi/v1/leverageBracket]
lev_liq   = floor( 1 / (3·stop_pct + mmr + 0.005) )                        # liq mesafesi ≥ 3× stop  [FIX]
lev_tier  = {low: 5, normal: 3, high: 2}[vol_state]                         [CAL]
lev       = max(1, min(lev_liq, lev_tier, bracket_max))
margin    = notional / lev  ≤ free_margin × 0.8
```
Margin mode: **ISOLATED** [FIX]. Position mode: **one-way** [FIX]. Leverage hiçbir koşulda Jev çıktısının fonksiyonu değildir (kod review kuralı + unit test: `lev` fonksiyonunun imzasında judgment tipi yok).

### 10.9 Kill switch

Kaynaklar: `KILL` dosyası (`$JEVBOT_RUN_DIR/KILL`, 1 s polling), `SIGUSR1`, `jevbot kill [--flatten]`, opsiyonel Telegram komutu (allowlist'li chat id).
```
on_kill(flatten):
    state := HALTED (persist)
    cancel all non-reduce-only open orders
    verify protective stop exists for each position (yoksa yerleştir)
    if flatten: reduce-only market close, sembol sırası = en büyük riskten küçüğe,
                her kapatmada slippage > 1R ise durup alarm (flash crash'te panik satışı önleme)
```

---

## 11. Execution

### 11.1 Adapter arayüzü

```python
class ExecutionAdapter(Protocol):
    async def submit_entry(self, o: ApprovedOrder) -> OrderAck: ...
    async def place_protection(self, pos: Position) -> ProtectionAck: ...   # SL + TP
    async def close_position(self, symbol: str, reason: str) -> OrderAck: ...  # reduce-only market
    async def cancel(self, client_order_id: str) -> None: ...
    async def snapshot(self) -> ExchangeSnapshot: ...                         # positions + open orders
    events: AsyncIterator[OrderEvent | FillEvent | AccountEvent]
# Implementasyonlar: PaperAdapter (live feed + fill model), SimAdapter (replay), BinanceAdapter(testnet|live)
```

### 11.2 Entry → protection akışı (Binance)

```
1. set leverage & margin type (idempotent, sembol başına cache)
2. entry: LIMIT IOC @ entry_ref·(1 + s·max_slip_bps/1e4)       max_slip_bps = min(0.1R_bps, 15)  [CAL]
   (market yerine IOC: slippage üst sınırlı; partial fill kabul)
3. ORDER_TRADE_UPDATE fill(s) → filled_qty, avg_price
4. hemen: STOP_MARKET  reduceOnly/closePosition, workingType=MARK_PRICE, priceProtect=TRUE
          TAKE_PROFIT_MARKET aynı şekilde                                 [VERIFY: Binance conditional
          order'ları 2025 sonunda Algo Order API'ye taşıdığını duyurdu; endpoint & parametreleri doğrula]
5. stop ACK ≤ 2 s bekle; yoksa 1 kez retry; 5 s'de hâlâ yoksa → reduce-only market close + CAUTION   [FIX]
6. SL veya TP fill → diğer protective order'ı cancel; position CLOSED
7. time stop: t_entry + horizon → cancel protections → reduce-only market close
```
`newClientOrderId = f"{arm}{leg}-{proposal_id[-16:]}-{attempt}"` (≤ 36 karakter `[VERIFY]`) → idempotency ve restart sonrası eşleşme.

Birden fazla arm aynı gerçek hesapta **çalıştırılmaz**: testnet/live'da tek arm (seçilen); diğer arm'lar paper olarak aynı feed üzerinde paralel devam eder.

### 11.3 Paper / Sim fill modeli (pesimist)

```
entry (taker IOC):
  book varsa: depth20'yi qty kadar yürü (VWAP), + latency: order_latency ~ empirical (50–200 ms)
              ve o ana en yakın book snapshot'ı kullan; limit fiyatını aşan kısım dolmaz (partial)
  book yoksa (historical): fill = mid·(1 + s·(spread_bps/2 + impact_bps + 1)/1e4)   # +1 bps pesimizm
stop (market after mark trigger):
  fill = max-adverse(last_price_at_trigger, trigger_price) − s·stop_slip
  stop_slip_bps = max(entry impact model, 0.25 × bar'ın stop ötesindeki kısmı)       [CAL canlı veriyle]
TP (TAKE_PROFIT_MARKET): TP'ye dokunuş → market fill + normal slip
fees: taker 5.0 bps, maker 2.0 bps (VIP0)                                            [VERIFY]
funding: her funding anında cash = −s·qty·mark·funding_rate
```
Live'a geçişte **implementation shortfall** = live fill − paper-model fill (aynı sinyal) sürekli ölçülür.

### 11.4 Reconciler (30 s + her user-stream reconnect'te)

| Durum | Aksiyon |
|---|---|
| Exchange'de pozisyon var, local yok | adopt (qty/side/entry exchange'den), protective stop yoksa ATR-bazlı acil stop koy, state ≥ REDUCE_ONLY, alarm |
| Local var, exchange'de yok | fills/`userTrades` sorgula → kapanışı kaydet |
| Qty uyuşmazlığı | exchange doğru kabul edilir; local düzeltilir; alarm |
| Pozisyonda protective stop yok | hemen yerleştir; başarısızsa reduce-only close |
| Yetim reduce-only emir (pozisyon yok) | cancel |
| 3 ardışık reconcile hatası | REDUCE_ONLY |

User data stream: listenKey keepalive 30 dk'da bir `[VERIFY: 60 dk expiry]`; keepalive hatası → yeni listenKey + reconcile.

---

## 12. Persistence & Storage

### 12.1 PostgreSQL şeması (OLTP, `schema v1`)

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK (mode IN ('replay','shadow','paper','testnet','live')),
  git_sha TEXT NOT NULL, config_hash TEXT NOT NULL, config JSONB NOT NULL,
  prereg_hash TEXT, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, notes TEXT);

CREATE TABLE symbols_pit (
  t_asof TIMESTAMPTZ, symbol TEXT, status TEXT, onboard_date DATE, delivery_date TIMESTAMPTZ,
  tick_size NUMERIC, step_size NUMERIC, min_notional NUMERIC, quote_vol_24h NUMERIC,
  vol_rank INT, in_universe BOOLEAN, PRIMARY KEY (t_asof, symbol));

CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs, t_decision BIGINT NOT NULL,
  symbol TEXT NOT NULL, family TEXT NOT NULL, family_version TEXT NOT NULL, side SMALLINT NOT NULL,
  entry_ref DOUBLE PRECISION, stop_price DOUBLE PRECISION, tp_price DOUBLE PRECISION,
  stop_dist_bps REAL, r_tp REAL, horizon_s INT, cost_rt_bps REAL, cost_r REAL,
  scanner_score REAL, scanner_rank SMALLINT, regime TEXT, is_recall_control BOOLEAN DEFAULT FALSE,
  reasons JSONB, features JSONB, feature_schema TEXT, frame_ref TEXT);
CREATE INDEX ON proposals (t_decision); CREATE INDEX ON proposals (symbol, t_decision);

CREATE TABLE judge_requests (
  request_id TEXT PRIMARY KEY, proposal_id TEXT REFERENCES proposals, variant TEXT NOT NULL,
  questionset_version TEXT, prompt_hash TEXT, model_id TEXT, model_version TEXT, sampling JSONB,
  state JSONB NOT NULL, state_hash TEXT NOT NULL, t_sent BIGINT, t_received BIGINT, latency_ms INT,
  status TEXT NOT NULL, raw_response JSONB, error TEXT);
CREATE INDEX ON judge_requests (state_hash);

CREATE TABLE judge_answers (
  request_id TEXT REFERENCES judge_requests, question_id TEXT, option TEXT DEFAULT '',
  p_raw DOUBLE PRECISION NOT NULL, confidence DOUBLE PRECISION,
  PRIMARY KEY (request_id, question_id, option));

CREATE TABLE ml_predictions (
  proposal_id TEXT REFERENCES proposals, model_id TEXT, question_id TEXT, p_raw DOUBLE PRECISION,
  PRIMARY KEY (proposal_id, model_id, question_id));

CREATE TABLE calibrators (
  calibrator_id TEXT PRIMARY KEY, source TEXT, question_id TEXT, slice TEXT, method TEXT,
  fit_start BIGINT, fit_end BIGINT, n INT, params JSONB, holdout_metrics JSONB,
  created_at TIMESTAMPTZ DEFAULT now(), active BOOLEAN DEFAULT FALSE);

CREATE TABLE decisions (
  decision_id TEXT PRIMARY KEY, proposal_id TEXT REFERENCES proposals, arm TEXT NOT NULL,
  policy_version TEXT NOT NULL, t_decision BIGINT, action TEXT NOT NULL
    CHECK (action IN ('ENTER','ARM','HOLD','DROP','EXPIRED')),
  p_raw DOUBLE PRECISION, p_cal DOUBLE PRECISION, p_lb DOUBLE PRECISION, p_abnormal_cal DOUBLE PRECISION,
  calibrator_ids TEXT[], ev_r REAL, ev_lb_r REAL, reasons TEXT[], thresholds JSONB,
  parent_decision_id TEXT);                 -- ARM → ENTER zinciri
CREATE INDEX ON decisions (arm, t_decision);

CREATE TABLE risk_verdicts (
  verdict_id TEXT PRIMARY KEY, decision_id TEXT REFERENCES decisions, arm TEXT, risk_version TEXT,
  approved BOOLEAN NOT NULL, risk_state TEXT, checks JSONB NOT NULL,
  qty NUMERIC, notional NUMERIC, leverage SMALLINT, risk_usd NUMERIC);

CREATE TABLE orders (
  order_id TEXT PRIMARY KEY, client_order_id TEXT UNIQUE NOT NULL, exchange_order_id TEXT,
  arm TEXT, verdict_id TEXT, position_id TEXT, symbol TEXT, side SMALLINT, leg TEXT
    CHECK (leg IN ('ENTRY','SL','TP','TIME','KILL','RECONCILE')),
  type TEXT, qty NUMERIC, price NUMERIC, stop_price NUMERIC, reduce_only BOOLEAN,
  status TEXT, t_submit BIGINT, t_ack BIGINT, t_done BIGINT, raw JSONB);

CREATE TABLE fills (
  fill_id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders, t BIGINT, price NUMERIC, qty NUMERIC,
  fee NUMERIC, fee_asset TEXT, is_maker BOOLEAN, ref_price NUMERIC, slippage_bps REAL,
  model_price NUMERIC, raw JSONB);        -- model_price: implementation shortfall için

CREATE TABLE positions (
  position_id TEXT PRIMARY KEY, arm TEXT, proposal_id TEXT, symbol TEXT, side SMALLINT,
  qty NUMERIC, entry_price NUMERIC, stop_price NUMERIC, tp_price NUMERIC, leverage SMALLINT,
  opened_at BIGINT, closed_at BIGINT, exit_reason TEXT, gross_pnl NUMERIC, fees NUMERIC,
  funding NUMERIC, net_pnl NUMERIC, r_multiple REAL, mae_r REAL, mfe_r REAL);

CREATE TABLE funding_payments (position_id TEXT, t BIGINT, rate DOUBLE PRECISION, amount NUMERIC,
  PRIMARY KEY (position_id, t));

CREATE TABLE labels (
  proposal_id TEXT REFERENCES proposals, label_version TEXT, entry_mode TEXT DEFAULT 'immediate',
  y_success SMALLINT, y_abnormal SMALLINT, y_direction TEXT, exit_type TEXT,
  realized_r REAL, net_r REAL, mae_r REAL, mfe_r REAL, mae_r_full REAL, t_exit BIGINT,
  computed_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (proposal_id, label_version, entry_mode));

CREATE TABLE equity_curve (arm TEXT, t BIGINT, equity NUMERIC, open_risk NUMERIC,
  gross_notional NUMERIC, drawdown REAL, risk_state TEXT, PRIMARY KEY (arm, t));

CREATE TABLE ops_events (t BIGINT, level TEXT, component TEXT, kind TEXT, payload JSONB);
CREATE TABLE data_gaps (symbol TEXT, stream TEXT, t_start BIGINT, t_end BIGINT, backfilled BOOLEAN);
```

Hacim tahmini: ~3–5k proposal/gün × (proposal + 1–2 request + 5 decision) ≈ 30k satır/gün → yıllık ~10M satır; tek Postgres için rahat. TimescaleDB yalnızca `equity_curve`/`ops_events` hypertable'ı için opsiyonel.

### 12.2 Parquet layout (recorder + frames)

```
data/
  raw/                         # recorder (R — indirilemeyen)
    book1s/date=YYYY-MM-DD/part-HH.parquet      # 200 sembol × 1s: mid_o/h/l/c, spread_mean/max, bid/ask qty
    depth20/date=.../part-HH.parquet            # Tier-2, 5 s örnekleme, 20×2 seviye
    mark1s/date=...                             # mark, index, funding, next_funding
    oi1m/date=...                               # REST OI
    forceorder/date=...
    latency/date=...                            # kline_delay, jev, order ack ölçümleri
  hist/binance_vision/um/{klines_1m,aggTrades,metrics,fundingRate,premiumIndexKlines_1m,markPriceKlines_1m}/...
  frames/date=.../part-HH.parquet               # her decision tick'teki 200×F feature matrix (f.v1)
```
- Sıkıştırma zstd(level 3), row group ~128 MB, sıralama `(symbol, t)`.
- Yazım: writer thread, 5 s veya 50k satırda flush; saat sonunda part kapatılır (crash'te en fazla 1 saatlik *açık* part kaybı → part'lar 5 dk'lık da olabilir `[CAL]`).

### 12.3 Retention & tahmini boyut

| Veri | Tahmini boyut | Retention |
|---|---|---|
| book1s (200 sym) | ~17M satır/gün ≈ 200–400 MB/gün | süresiz (aggregate) |
| depth20 5s (Tier-2 ~40 sym) | ~0.7M snapshot/gün ≈ 150–300 MB/gün | 30 gün, sonra sil (türetilmiş feature'lar kalır) |
| mark1s, oi1m | < 100 MB/gün | süresiz |
| frames (288 tick × 200 × ~45) | ~2.6M değer/gün ≈ 15 MB/gün | süresiz |
| hist (klines/aggTrades) | aggTrades 200 sym ≈ 1–3 GB/gün | ihtiyaç kadar; yeniden indirilebilir |
| Postgres | ~10M satır/yıl | süresiz |

Bookticker tick'leri ham olarak **saklanmaz** (1 s aggregate yeterli, ham veri ~10× büyük).

### 12.4 DB failure davranışı
Writer kuyruğu → Postgres başarısızsa lokal append-only JSONL WAL (`wal/*.jsonl`). DB down > 60 s veya WAL > 500 MB → **REDUCE_ONLY** (denetlenemeyen karar = yeni trade yok) [FIX]. DB geri gelince WAL replay (idempotent PK'lar sayesinde tekrar güvenli).

---

## 13. Replay Engine

### 13.1 Mimari

```python
class ReplayEngine:
    def __init__(self, sources: list[EventSource], core: TradingCore, clock: SimClock,
                 judge: JudgeClient,  # Off | CacheOnly | LiveOnHistorical(flagged)
                 execution: SimAdapter, latency: LatencyModel, seed: int): ...

    def run(self):
        heap = merge(sources, key=lambda e: (e.t_available, e.seq))
        # t_available = t_event + sampled feed latency (kline: close_time + kline_delay)
        for ev in heap:
            self.clock.advance_to(ev.t_available)
            self.core.on_event(ev)           # canlıdaki ile AYNI core
```
`TradingCore` hiçbir yerde `time.time()` çağırmaz; sadece `clock.now()`.

### 13.2 Realizm kuralları

| Konu | Kural |
|---|---|
| Lookahead | Bar sadece `close_time + kline_delay` sonrasında görünür; forming bar kullanılmaz |
| Normalization | Tüm istatistikler trailing; warmup'sız sembol ineligible |
| Universe | `symbols_pit`; recorder öncesi dönem için data.binance.vision günlük dosyalarından yeniden kurulur (o gün var olan semboller, önceki 24h hacmine göre rank) — **delisted dahil** |
| Fees | Config'ten, tarih bazlı (fee değişiklikleri) |
| Funding | fundingRate history'den gerçek zaman damgalarında cash flow |
| Latency | Jev latency canlı empirical dağılımdan (veya cache kaydındaki gerçek latency); order ack & fill latency empirical |
| Fill | §11.3 pesimist model; intra-bar SL/TP belirsizliği → SL (veya aggTrades ile çöz) |
| Partial fill | IOC limitin ötesindeki depth dolmaz; book yoksa `qty ≤ cap_liquidity` şartı |
| Data gaps | Gap'li sembol gap + warmup boyunca ineligible; açık pozisyon gap'e girerse sonraki ilk fiyatta SL/TP kontrolü (gap fill pesimist) |
| Delisting | Delist tarihinde açık pozisyon settlement fiyatından kapanır |

### 13.3 Jev in replay

| Mod | Kullanım | Kanıt değeri |
|---|---|---|
| `off` | A0, A1, R arm'ları; historical dönem | — |
| `cache_only` | Shadow/paper döneminde kaydedilen cevaplarla policy/risk varyasyonlarını yeniden simüle et | **Yüksek** (forward, contamination yok) |
| `live_on_historical` | Contamination çalışması; Jev yayın öncesi tarih | Düşük; asla acceptance'a sayılmaz, rapor başlığında uyarı |

### 13.4 Walk-forward & multiple testing
- ML meta-labeler ve calibrator: rolling walk-forward, train `[t0, t1)`, purge `H`, embargo 1 gün, test `[t1+1d, t1+1d+30d)`.
- Her replay config çalıştırması `runs`'a yazılır → **N_trials** sayılır → Deflated Sharpe hesaplanır.
- Final holdout: son 20% dönem, parametre seçimi bittikten sonra **bir kez** çalıştırılır (runs'ta `holdout=true` bayrağı; ikinci kez çalıştırma CLI tarafından engellenir).

### 13.5 Leakage testleri (CI'da zorunlu)
- **Future poisoning:** t sonrasındaki tüm veriyi rastgele bozup t'ye kadarki karar hash'inin değişmediğini doğrula.
- **Shuffle label:** label'lar karıştırılınca ML/Jev AUC ≈ 0.5 olmalı (pipeline'da gizli sızıntı yok).
- **Time-shift:** feature'ları 1 tick ileri kaydırınca performansın *artması* = sızıntı alarmı.

---

## 14. Experiments & Statistics

### 14.1 Çalıştırma
Tüm arm'lar aynı `TradingCore` içinde; aynı proposal ve judgment'ları görür; her arm'ın kendi `PortfolioState` + `RiskEngine` instance'ı (aynı config) vardır. Sistem-seviyesi guard'lar ortaktır.

### 14.2 Pre-registration
`experiments/prereg/EXP-001.md`: hipotezler, birincil/ikincil metrikler, örneklem büyüklüğü, analiz yöntemi, durma kuralı. Veri toplama başlamadan commit edilir; hash `runs.prereg_hash`'e yazılır.

### 14.3 Birincil analiz (decision-level, paired)

```
Aynı proposal kümesi P (canonical Jev status=ok olanlar), holdout dönemi:
  H1 (incremental info): Δ LogLoss = LL(A1) − LL(B+) > 0
      → day-block bootstrap (10k), one-sided 95%; + Diebold-Mariano (günlük loss farkı, HAC)
  H2 (filter value vs random): lift_B = mean(net_R | B accept) − mean(net_R | all)
      → 1000 random filter (aynı pass rate) dağılımında lift_B'nin percentile'ı > 95
  H3 (Jev vs ML): lift_B vs lift_A1 aynı pass rate'e ayarlanmış (threshold matching)
Destekleyici:
  - Stacked logistic: y ~ logit(p_ml) + logit(p_jev); w_jev katsayısı, gün-cluster'lı SE
  - Distillation: GBM(features) → p_jev; ρ(fidelity) ve residual'ın y'yi açıklama gücü
  - Regime bazında aynı testler (shrinkage'lı, keşifsel — birincil değil)
```
Neden decision-level: günde binlerce proposal vs birkaç trade → çok daha yüksek istatistiksel güç, portfolio path-dependency'den bağımsız. Dikkat: overlapping label'lar ve aynı tick'teki korelasyonlu sembol'ler → **bağımsız örnek sayısı nominalden çok düşük**; tüm CI'lar gün-block bootstrap ile.

### 14.4 Güç (kaba planlama)
- AUC: nominal n = 3000 (≈ %40 pozitif) → SE ≈ 0.011; etkin n ≈ n/3 ile SE ≈ 0.019 → AUC 0.55'i 0.50'den ayırt etmek için etkin ~3000, nominal ~**9000** proposal gerekebilir. (2–4 hafta shadow.)
- Trade-level expectancy: `n ≈ (z·σ_R/μ_R)²`; μ=0.10R, σ=1.2R → ~550 trade (%95), %90 için ~390.
- Bu sayılar *plan* içindir; gerçek varyans Faz 2 verisinden yeniden hesaplanır.

### 14.5 Portfolio metrikleri (ikincil, her arm)

| Metrik | Tanım |
|---|---|
| Net PnL, CAGR (≥ 6 ay veri varsa) | |
| Sharpe / Sortino | günlük net getiri, ×√365 |
| PSR / DSR | Probabilistic & Deflated Sharpe (N_trials = runs sayımı) |
| Max DD (% ve R) | equity peak'ten |
| Profit factor | Σ kazanç / |Σ kayıp| |
| Expectancy | mean net_R, mean net USD |
| Win rate, avg win R, avg loss R | |
| Turnover | Σ notional / ortalama equity / gün |
| Fee ratio | toplam fee / gross PnL (ve / |gross|) |
| Slippage | ortalama bps, R cinsinden, model vs realized |
| Exposure / time in market | ortalama gross notional/equity, pozisyonlu zaman oranı |
| MAE/MFE dağılımı | stop/TP kalibrasyonu için |
| Trade count, ARM→ENTER dönüşüm, EXPIRED oranı | |

Jev etkisi soruları için rapor: B vs A0/A1/R'de (i) DD farkı, (ii) filtrelenen trade'lerin net_R ortalaması (kötü trade'ler mi elendi?), (iii) turnover & fee farkı, (iv) Sharpe/Sortino farkı — her biri bootstrap CI ile.

---

## 15. Observability

- Log: `structlog` JSON, her satırda `run_id`, `component`, ilgili id'ler.
- Metrikler (Prometheus client, opsiyonel Grafana; MVP'de Postgres + günlük HTML rapor yeterli):

| Metrik | Alarm |
|---|---|
| `ws_lag_ms{stream}` p99 | > 2000 ms |
| `stale_symbols_ratio` | > 0.10 |
| `decision_latency_ms` (tick → son karar) | > 4000 ms |
| `jev_latency_ms` p50/p99, `jev_error_rate`, `jev_late_rate` | p99 > timeout, err > 5% |
| `calibration_psi{question}` | > 0.25 |
| `risk_state{arm}` | değişimde bildirim |
| `unprotected_positions` | **> 0 → kritik** |
| `reconcile_mismatch_total` | > 0 |
| `db_queue_depth`, `wal_bytes` | eşik |
| `daily_pnl_R{arm}`, `open_risk_pct{arm}` | limit yaklaşımı |
- Bildirim: Telegram bot (outbound), seviyeler INFO/WARN/CRIT; CRIT tekrar eder (acknowledged olana kadar 5 dk'da bir).
- Günlük rapor (`jevbot report --day`): arm karşılaştırma, calibration bucket tablosu, reliability diagram, en kötü 10 trade'in tam karar zinciri.

---

## 16. Testing Methodology

| Katman | İçerik | Araç |
|---|---|---|
| Unit | Her feature formülü vs yavaş ama açık pandas referans implementasyonu (golden values); sizing; stop geometry; EV | pytest |
| Property | `size()` hiçbir girdide limitleri aşmaz; `lev()` imzası judgment almaz; canonicalization simetrisi: fiyat serisi aynalanınca (`p → 1/p`) long state == short state; risk state machine sadece izinli geçişler | hypothesis |
| Label | Sentetik fiyat yolları (bilinen SL/TP sırası, gap, aynı-bar ikisi) | pytest |
| Leakage | Future poisoning, shuffle label, time-shift (§13.5) | CI |
| Parity | Kayıtlı 1 günlük event stream → live core (fake WS) vs replay core: karar stream'lerinin hash'i eşit | CI |
| Golden replay | Sabit 3 günlük dataset → decision hash snapshot; değişiklik bilinçli güncelleme ister | CI |
| Prompt lint | Question/state metinlerinde yasaklı yargı sözcükleri; state'te sembol/tarih/mutlak fiyat yok | CI |
| Chaos (paper/testnet) | WS drop, gecikme, duplicate, out-of-order mesaj; REST 5xx/429/418; Jev timeout/çöp cevap; DB down; **entry fill sonrası, stop öncesi `kill -9`** → restart'ta reconciler ≤ 5 s içinde stop koyar | fault-injection proxy |
| Testnet E2E | partial fill, reduce-only, closePosition, leverage/margin set, listenKey expiry | senaryo scriptleri |

Not: Binance testnet fiyat/likiditesi gerçekçi değildir — testnet **sadece tesisat** (order lifecycle) içindir, performans ölçümü değil.

---

## 17. Failure Matrix

| Failure | Tespit | Anında aksiyon | Recovery |
|---|---|---|---|
| Jev timeout/hata | per-request | O proposal HOLD (B/B+/C) | breaker cooldown sonrası half-open |
| Jev breaker open | 5 fail / %20 err | B arm'ları yeni entry yok | 60 s sonra probe |
| Jev drift/version | PSI, version alanı | B arm'ları CAUTION/entry yok | shadow recalibration |
| Kline stream stall | health | Sembol STALE, ineligible | reconnect + REST backfill |
| Tüm WS kopuk | health | REDUCE_ONLY | reconnect, backfill, warmup kontrol, 5 dk temiz → NORMAL |
| User data stream kopuk | health/keepalive | yeni entry yok | yeni listenKey + reconcile |
| REST 429 | status | backoff, CAUTION | weight normale dönünce |
| REST 418 | status | REDUCE_ONLY, CRIT | manuel |
| Clock drift > 500 ms | time sync | yeni entry yok | NTP düzelince |
| Stop yerleştirilemedi | ack timeout 5 s | reduce-only market close | alarm, inceleme |
| Reconcile mismatch | reconciler | §11.4 tablo | otomatik + alarm |
| Pozisyon bot dışı değişti | reconciler | adopt + REDUCE_ONLY | manuel |
| DB down | writer | WAL; 60 s → REDUCE_ONLY | WAL replay |
| Disk dolu | writer error | recorder raw depth yazımını durdur, trader REDUCE_ONLY | retention job |
| Process crash | systemd | restart; başlangıçta **önce reconcile**, sonra trade | başlangıç state = CAUTION (30 dk) |
| Exchange maintenance | announcement/5xx | REDUCE_ONLY | manuel/otomatik |
| Symbol delist duyurusu | exchangeInfo deliveryDate | ineligible; açık pozisyon time-stop'u öne çek | — |
| Flash crash / BTC shock | breaker | 30 dk yeni entry yok; native stop'lar çalışır | otomatik |
| Kill switch | dosya/sinyal/CLI | HALTED (+flatten opsiyonel) | manuel |
| Risk engine exception | try/except en üstte | exception = reject; 3 exception/10 dk → HALTED | manuel [FIX] |

Başlangıç sırası (her restart): `load config → connect DB → reconcile exchange (positions/orders) → ensure protection → start market data → warmup check → state=CAUTION → 30 dk temiz → NORMAL`.

---

## 18. Config (örnek, `config/`)

```yaml
# config/base.yaml
run: {mode: shadow, decision_tf: 5m, bar_grace_ms: 1500, decision_deadline_ms: 4000}
universe: {min_quote_vol_24h: 20_000_000, max_rank: 200, exit_rank: 230, min_listing_age_d: 14}
fees: {taker_bps: 5.0, maker_bps: 2.0}          # [VERIFY]
jev:
  model_id: "jev"            # [VERIFY]
  model_version: "pinned-..." # [VERIFY]
  questionset: qs.v1
  state_schema: state.v1
  timeout_ms: 1500
  max_concurrency: 16
  max_calls_per_day: 20000   # [CAL]
  identified_variant_rate: 0.05
arms: [A0, A1, R, B, B_plus, C]
```

```yaml
# config/policy/pol.v1.yaml
pol.v1:
  defaults: {eps_in: 0.05, eps_high: 0.20, confirm_ticks: 2, chase_max_R: 0.5,
             min_calibrator_n: 300, m_fail_init: -0.85}
  arms:
    A0:     {source: none}
    A1:     {source: ml}
    R:      {source: random, seed: 1729, pass_rate_from: B, window_d: 7}
    B:      {source: jev, use_abnormal_veto: true, theta_abnormal: 0.30}
    B_plus: {source: stack, use_abnormal_veto: true, theta_abnormal: 0.30}
    C:      {source: jev_direction, delta_min: 0.20}
```

```yaml
# config/risk/risk.v1.yaml
risk.v1:
  r_base: 0.0025
  max_positions: 3
  max_total_open_risk: 0.0075
  max_beta_net_risk_mult: 2.0
  max_cluster_same_side: 2
  max_gross_notional_x: 3.0
  max_gap_stress: 0.015
  max_entries_per_day: 20
  loss: {daily: -0.015, weekly: -0.04, global_dd: -0.08, streak_caution: 5, streak_halt: 8}
  sizing: {cap_liquidity_depth_frac: 0.10, cap_volume_1h_frac: 0.005, cap_position_x: 1.0}
  checks: {book_age_ms: 2000, clock_offset_ms: 500, spread_mult: 3.0, spread_cap_bps: 10,
           max_expected_slip_R: 0.10, min_stop_cost_mult: 4, liq_to_stop_mult: 3}
  leverage: {tier_caps: {low: 5, normal: 3, high: 2}, margin: ISOLATED, position_mode: ONE_WAY}
  stops: {k_min_atr: 1.0, k_max_atr: 2.5, r_tp: 1.5, stop_ack_timeout_ms: 2000, stop_fail_close_ms: 5000}
  breakers: {btc_shock_sigma: 4.0, btc_shock_cooldown_min: 30, market_spread_pctile: 0.95,
             stale_ratio_caution: 0.10, stale_ratio_reduce: 0.25, slip_drift_caution: 2.0, slip_drift_reduce: 3.0}
```
Config hash her `runs` kaydında; config değişikliği = yeni run.

---

## 19. Code Layout (modular monolith)

```
jevai_trade_bot/
├─ pyproject.toml                 # python 3.12+, uv; deps: uvloop, websockets, httpx[http2], orjson,
│                                 # numpy, polars, pyarrow, duckdb, asyncpg, pydantic, structlog,
│                                 # scikit-learn, lightgbm, scipy, hypothesis, pytest
├─ config/  base.yaml · policy/pol.v1.yaml · risk/risk.v1.yaml · questions/qs.v1.yaml
├─ docs/    01_architecture_review.md · 02_technical_spec.md
├─ experiments/prereg/EXP-001.md
├─ src/jevbot/
│  ├─ core/          events.py · types.py · clock.py · ids.py · config.py · bus.py · core.py (TradingCore)
│  ├─ marketdata/    binance_ws.py · binance_rest.py · health.py · universe.py · bars.py · buffers.py · book.py
│  ├─ recorder/      recorder.py · parquet_sink.py
│  ├─ hist/          binance_vision.py (downloader) · pit_universe.py
│  ├─ features/      registry.py · technical.py · flow.py · derivatives.py · micro.py · context.py
│  │                 normalize.py · regime.py · frame.py
│  ├─ scanner/       eligibility.py · geometry.py · cost.py · ranking.py · diversify.py · clusters.py
│  │                 detectors/{base.py, brk.py, pb.py}
│  ├─ labels/        triple_barrier.py · paths.py · labeler_service.py
│  ├─ judgment/      state_builder.py · questions.py · jev_client.py · jev_adapter.py · cache.py
│  │                 breaker.py · fake.py (test/replay) · consistency.py
│  ├─ ml/            meta_labeler.py · stacking.py · distill.py · walkforward.py
│  ├─ calibration/   calibrators.py · shrinkage.py · metrics.py · drift.py · service.py
│  ├─ policy/        ev.py · decide.py · confirm.py · arms.py
│  ├─ risk/          engine.py · state_machine.py · sizing.py · limits.py · checks.py · breakers.py
│  │                 leverage.py · killswitch.py
│  ├─ execution/     adapter.py · paper.py · sim.py · binance.py · fill_model.py · reconciler.py
│  │                 position_manager.py · client_ids.py
│  ├─ portfolio/     state.py · ledger.py · funding.py
│  ├─ persistence/   schema.sql · db.py · writer.py · wal.py · parquet.py
│  ├─ replay/        engine.py · sources.py · latency_model.py · runner.py
│  ├─ analysis/      stats.py (bootstrap, DM, DSR/PSR) · reports.py · experiment.py
│  ├─ ops/           metrics.py · alerts.py · logging.py
│  └─ cli.py         jevbot {record,download,live,replay,calibrate,report,kill}
└─ tests/  unit/ · property/ · labels/ · leakage/ · parity/ · golden/ · chaos/
```

Bağımlılık yönü (import kuralı, CI'da `import-linter` ile zorlanır):
```
core ← features ← scanner ← policy ← risk ← execution
         ↑           ↑        ↑
      marketdata   labels   judgment, calibration, ml
risk  ─X→ judgment         (risk modülü judgment/calibration/ml import EDEMEZ)   [FIX]
execution ─X→ judgment
```
Bu kural "AI risk limitlerini override edemez" ilkesinin **kod seviyesinde** garantisidir.

---

## 20. İlk Backlog (Faz 0–1)

**Sprint 0 (hemen — veri zamanla birikir, gecikmesi pahalı):**
1. `recorder`: kline_1m (200), markPrice arr, bookTicker → book1s aggregate, OI poll 1m, forceOrder, depth20 (BTC/ETH + top 30 hacim, statik), latency ölçümleri → Parquet. systemd unit, günlük gap raporu.
2. `hist/binance_vision`: klines_1m, markPriceKlines_1m, premiumIndexKlines_1m, fundingRate, metrics, aggTrades (seçili) + checksum doğrulama; point-in-time universe kurulumu.
3. `persistence/schema.sql` + `runs` kaydı.

**Sprint 1:**
4. `features` f.v1 + pandas referans testleri + redundancy raporu.
5. `labels/triple_barrier` + sentetik testler.
6. `scanner` (eligibility, BRK, PB, geometry, cost, diversify).
7. `replay/engine` + `SimAdapter` + fill model + leakage testleri.
8. Arm A0 historical replay raporu: family × regime bazında gross/net R, cost_R dağılımı, MAE/MFE → **Go/No-go: setup'larda maliyet öncesi pozitif edge var mı?**

**Sprint 2:**
9. `judgment` (state builder, qs.v1, client, cache, fake client), prompt lint.
10. `ml/meta_labeler` (logistic walk-forward).
11. `jevbot live --mode shadow`: gerçek zamanlı proposal → Jev + ML → label; calibration service; günlük rapor.
12. `experiments/prereg/EXP-001.md` commit (shadow veri toplama başlamadan önce).

Bu noktadan sonra Faz 2 kapısı (L.1) veriyle değerlendirilir.
