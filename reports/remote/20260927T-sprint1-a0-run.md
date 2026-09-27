# Remote → Local: Sprint 1 — Arm A0 replay on real data

Kod hazır (`git pull`). Soru tek: **deterministic setup'lar (BRK, PB) maliyet öncesi/sonrası edge taşıyor mu?**
A0 = her scanner önerisi, filtre yok, Jev yok. Soak'u etkilemez (tek process, dakikalar).

## Önkoşul
Son 180 gün, top-50 (BTCUSDT dahil): `klines 1m`, `markPriceKlines 1m`, `fundingRate` indirilmiş ve
`jevbot verify --root data/hist/um` temiz (önceki talimat). Metrics bu koşu için gerekmez.

## Koşu (3 komut)
```powershell
python -m pytest -q                                   # 92 test
jevbot research-a0 --start <D-180> --end <D-1> --tradable-top 50 --out data/research/a0-full
jevbot research-a0 --start <D-180> --end <D-90> --tradable-top 50 --out data/research/a0-h1
jevbot research-a0 --start <D-90>  --end <D-1>  --tradable-top 50 --out data/research/a0-h2
```
(`--end` hariç tutulur. `--hist` varsayılanı `data/hist/um`; farklıysa ekle.)

## Rapor (≤ 30 satır) → `reports/local/<ts>/LOCAL_REPORT.md`
- Üç koşunun `summary.md` dosyalarını `reports/local/<ts>/` altına kopyala (**`proposals.parquet` commit etme**).
- Her koşu için: proposals / labelled / data_gaps, verdict (gross/net edge), `all`, `BRK_long/short`, `PB_long/short`
  satırlarının mean gross R, mean net R ve CI'ları, median cost R, portföy satırı (trades/day, sum net R, max DD R, PF).
- h1 ile h2 aynı yönde mi? (işaret tutarlılığı)
- Veri sorunu (eksik sembol, data_gap oranı > %1, suspect dosya) varsa listele.
- Yorum/eleştiri en fazla 5 madde: özellikle maliyet modeli (spread/slip kademeleri: top10 1,5/1 bps, top30 3/2,
  diğer 5/4 bps; taker 5 bps) gerçek recorder `book_1s` spread medyanlarıyla uyumlu mu — soak verisinden 5 sembol örnekle.

Karar kuralı (önceden kayıtlı): gross CI alt sınırı ≤ 0 ise setup'lar Jev filtrelemesinden önce yeniden
tasarlanır; gross > 0 ama net ≤ 0 ise öncelik maliyet/geometri (horizon, R_tp, cost gate); net > 0 ise A1/B kollarına geçilir.
