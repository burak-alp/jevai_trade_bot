# jevai_trade_bot

Binance USDⓈ-M Futures üzerinde ~200 pair'i tarayan; deterministic setup'ların ürettiği trade önerilerini
Jev (TypeSafe) ile **olasılıksal olarak filtreleyen** ve tüm risk kararlarını deterministic kodda tutan
araştırma/trading sistemi.

**Durum:** Sprint 0.1 (hardening) — public market-data recorder + historical downloader. Gerçek para yok, API key yok, emir yok, Jev çağrısı yok.
Sprint 1'e geçiş kapısı: gerçek Binance'e karşı [`docs/03_live_validation_runbook.md`](docs/03_live_validation_runbook.md).

## Temel ilkeler

1. **Jev trader değildir.** Setup detector'ları side/entry/stop/TP'yi deterministic üretir; Jev yalnızca
   bu önerinin başarı olasılığını (meta-label) ve anormallik riskini (veto) değerlendirir.
2. **Risk engine AI'den bağımsızdır** — `risk/` modülü `judgment/`, `calibration/`, `ml/` import edemez.
3. **Fail-safe = NO NEW TRADE.** Belirsizlikte yeni pozisyon açılmaz; mevcut pozisyonlar exchange-native stop ile korunur.
4. **Her soru ölçülebilir bir label'a bağlıdır**; her olasılık kalibre edilir; her karar yeniden üretilebilir.
5. **Jev'in değeri kanıtlanmalıdır**: ML baseline ve random-filter kontrollerine karşı, forward (shadow) veride.

## Dokümanlar

- [`docs/01_architecture_review.md`](docs/01_architecture_review.md) — mimari eleştirisi, riskler, hedef mimari, roadmap, acceptance criteria
- [`docs/02_technical_spec.md`](docs/02_technical_spec.md) — implementasyon spesifikasyonu
- [`docs/03_live_validation_runbook.md`](docs/03_live_validation_runbook.md) — VPS'te smoke / 1 saat / 24 saat doğrulama komutları
- [`LOCAL_AGENT.md`](LOCAL_AGENT.md) — yerel ajan (GPT-6) görev + rapor + eleştiri protokolü; `scripts/local_validation.sh`

## Yerel RAG (token tasarrufu)

`python scripts/rag.py query "PIT evreni neden 486 sembol" --scope reports --max-chars 1800`
komutu `README`, `docs/`, `reports/`, `src/`, `config/` ve `scripts/` içindeki ilgili
parçaları dosya/satır referansıyla getirir. İndeks `run/rag/index.sqlite` içinde
artımlı güncellenir; model, API anahtarı ve ağ çağrısı kullanmaz. `--scope docs|reports|code|all`
ve `--limit` ile bağlamı daraltın. Sonuçları karar vermeden önce kaynak dosyada doğrulayın.

---

## Kurulum

Python ≥ 3.11 (Linux önerilir).

```bash
uv venv .venv && . .venv/bin/activate      # veya: python -m venv .venv
uv pip install -e ".[dev,fast]"             # fast = uvloop (opsiyonel)
pytest -q                                   # unit + integration (internet gerektirmez)
```

## Recorder (`jevbot record`)

Trader'dan bağımsız çalışan public market-data kaydedicisi. Sadece public endpoint'ler; API key okunmaz.

```bash
jevbot smoke                        # WS route'ları + REST: bağlantı, SUBSCRIBE ack, ilk event, şema, ping
jevbot record                       # config/base.yaml ile sürekli kayıt (Ctrl-C / SIGTERM = graceful shutdown)
jevbot record --duration 900        # 15 dk test koşusu
jevbot verify                       # Parquet sha256 + manifest + okunabilirlik kontrolü
jevbot recording-report             # throughput, CPU/RAM, latency, gaps, disk MB/h, integrity, acceptance
jevbot compact                      # biten saatlerin küçük part'larını birleştir (doğrulamalı, atomik)
jevbot config --set recorder.depth.top_n_by_volume=10   # çözülmüş config'i göster
```

Config katmanlıdır: `--config config/base.yaml --config config/local.yaml` (sonraki öncekini ezer),
tek değer için `--set a.b.c=value`. Bilinmeyen anahtar hata verir.

**Endpoint'ler config'tedir** (`binance.ws.base`, `binance.ws.routes`, `binance.ws.stream_routes`,
`binance.rest_base`). 2026 route ayrımına göre varsayılanlar:

| Stream | Route | URL |
|---|---|---|
| `<sym>@bookTicker`, `<sym>@depth20@500ms` | public | `wss://fstream.binance.com/public/stream?streams=...` |
| `<sym>@kline_1m`, `!markPrice@arr@1s`, `!forceOrder@arr` | market | `wss://fstream.binance.com/market/stream?streams=...` |

REST (weight): `/fapi/v1/time` (1), `/fapi/v1/exchangeInfo` (1), `/fapi/v1/ticker/24hr` (40, tüm semboller),
`/fapi/v1/openInterest` (1, round-robin ~200/dk), `/fapi/v1/klines` (limit'e göre 1–10, sadece gap backfill).
Kullanılan weight `X-MBX-USED-WEIGHT-1M` header'ından, limit `exchangeInfo.rateLimits`'ten runtime'da okunur.

Başlangıç sırası: orphan `.tmp` dosyalarını karantinaya al → clock sync → universe → **smoke test**
(başarısızsa çıkış kodu 3) → WS pool → pollers. Smoke test geçmeden recorder `HEALTHY` sayılmaz.

### Kaydedilen veri

```
data/raw/<dataset>/date=YYYY-MM-DD/<dataset>-<window>-<pid>-<n>.parquet   (+ .sha256, _manifest.jsonl)
data/raw/exchange_info/date=.../exchangeInfo-*.json.gz                     (saatlik PIT metadata)
data/quarantine/                                                            (crash'ten kalan .tmp dosyaları)
run/recorder_health.json, run/smoke_report.json
```

| Dataset | İçerik | Frekans |
|---|---|---|
| `kline_1m` | kapanmış 1m kline (ws; eksikse REST backfill, `source` kolonu) | sembol × dk |
| `mark_price` | mark, index, predicted funding, next funding (universe üyeleri) | sembol × 1 s |
| `book_1s` | bookTicker → 1 s bar: mid OHLC, spread bps mean/min/max, son bid/ask/qty, update sayısı | sembol × 1 s |
| `depth20` | depth20@500ms, `sample_interval_s` (5 s) örnekleme | BTC, ETH + top-N |
| `open_interest` | REST OI | sembol × ~1 dk |
| `force_order` | liquidation snapshot'ları (**eksik veri**, sadece log) | olay |
| `latency_1m` | `t_recv − t_event` p50/p90/p99/max, family × route | dk |
| `gaps` | ws kopma aralıkları, kline gap'leri ve backfill sonucu | olay |
| `health` | durum, msg/s, CPU, RSS, freshness, yazılan satır/byte | 10 s |
| `universe` | point-in-time sembol metadata + universe üyeliği + dışlanma nedeni | saatlik |

Şemalar: [`src/jevbot/recorder/schemas.py`](src/jevbot/recorder/schemas.py) (`rec.v2`, Parquet metadata'sında).

**Event-time partitioning:** her satır dataset'in zaman kolonuna (`TIME_COLUMN`) göre `sink.rotate_s` (180 s)
penceresine yazılır; pencere `window_end + late_grace_s` (60 s) sonra kapanır. Geç gelen event'ler ve REST
backfill kendi pencerelerinin ek part'ına gider; manifest'te `window_start/window_end` ile `t_min/t_max` tutarlıdır.
Crash'te en fazla açık pencereler (≈ rotate + grace) + `flush_s` kaybolur.

**Compaction:** `jevbot compact` biten saatlerin part'larını (symbol, zaman) sıralı tek dosyada birleştirir;
kaynaklar önce doğrulanır (sha256/manifest/okunabilirlik), yeni dosya fsync + sha256 + atomik rename + geri
okuma ile kontrol edilir, kaynaklar ancak bundan sonra silinir (manifest tombstone + crash journal).

### Sağlık ve izleme

`run/recorder_health.json` her 10 s güncellenir: `status` ∈ STARTING / HEALTHY / DEGRADED / UNHEALTHY / STOPPED,
`detail` sorunları listeler (ör. `disconnected:1`, `kline_stale:3`). Aynı satırlar `health` dataset'ine yazılır.

Çıkış kodları: `0` normal, `3` smoke test başarısız, `4` startup hatası (REST/universe), `5` verify hatası, `6` download hatası.

### systemd örneği

```ini
[Unit]
Description=jevbot market-data recorder
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jevai_trade_bot
ExecStart=/opt/jevai_trade_bot/.venv/bin/jevbot record --config config/base.yaml --config config/local.yaml
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
```

NTP (chrony) zorunludur; clock offset > 250 ms uyarı üretir.

## Historical downloader (`jevbot download`)

data.binance.vision (`data/futures/um`) → checksum doğrulamalı Parquet (`data/hist/um/...`).

```bash
# 1m klines, günlük dosyalar
jevbot download --dataset klines --interval 1m --symbols BTCUSDT,ETHUSDT --start 2026-08-01 --end 2026-08-31
# aylık dosyalar (tamamlanmış aylar için daha az istek)
jevbot download --dataset klines --interval 1m --symbols BTCUSDT --start 2026-01-01 --end 2026-07-31 --granularity monthly
jevbot download --dataset fundingRate --symbols BTCUSDT --start 2026-01-01 --end 2026-08-31   # sadece aylık
jevbot download --dataset metrics --symbols BTCUSDT --start 2026-08-01 --end 2026-08-31       # OI/LS 5m, sadece günlük
jevbot download --dataset premiumIndexKlines --interval 1m --symbols ALL --start 2026-08-01 --end 2026-08-02
# point-in-time sembol listesi (delist olanlar dahil, ilk/son gün)
jevbot download --dataset pit-listing --interval 1m
```

Var olan dosyalar atlanır (`--force` ile yeniden). 404 = o dönemde sembol yok → `_missing.jsonl`.

Ölçeklenebilirlik: işler bounded queue + sabit worker havuzu ile üretilir/tüketilir; ZIP'ler geçici dosyaya
stream edilir (sha256 incremental), CSV 4 MB'lık satır hizalı parçalarla Parquet'e çevrilir — bellek dosya
boyutundan bağımsızdır.

**Semantic validation:** her dosya için `ok | warn | suspect` kalite raporu (`*.quality.json` + manifest):
klines (monotonic/duplicate/continuity/OHLC/volume), metrics (5m cadence, create_time kayması, duplicate),
bookDepth (frozen/constant band, cadence, kümülatif derinlik), funding (duplicate, delist sonrası gözlem),
aggTrades (streaming id/zaman kontrolleri). `suspect` dosyalar silinmez, `_suspect/` altına taşınır;
`jevbot.hist.catalog.list_files` bunları varsayılan olarak döndürmez (replay kullanmaz).

## İnternetsiz geliştirme: fake exchange

```bash
jevbot fake-exchange --symbols 200 --book-rate 4000 &          # ws :18766, http :18765
jevbot record --config config/base.yaml --config config/fake.yaml --duration 300
curl -X POST localhost:18765/__admin/drop                      # tüm WS bağlantılarını düşür
curl "localhost:18765/__admin/silence?s=20"                    # bağlantıyı kapatmadan veri kes
curl "localhost:18765/__admin/skip_klines?symbol=BTCUSDT&n=2"  # kline gap → REST backfill
```

Fake exchange sentetik veri üretir; performans ölçümleri gerçek Binance yükünü yalnızca yaklaşık temsil eder.

Burst load test (çok process'li fake + gerçek recorder process + faz analizi, ~13 dk):

```bash
jevbot loadtest --workdir /tmp/lt --scenario default    # 5k×5dk, 15k×2dk, 30k×30s, burst ortasında drop, 30 s rotation
jevbot loadtest --workdir /tmp/lt-q --scenario quick    # ~2 dk
```

Her burst fazı için: offered/received oranı, feed latency p99'un sınırlı ve büyümüyor olması, sink backlog /
writer queue sınırı, sonrasında baseline'a dönüş, reconnect süresi; rapor `loadtest_report.{md,json}`.

## Güvenlik

Sprint 0 kodu hiçbir API key okumaz, signed endpoint çağırmaz ve emir göndermez.
