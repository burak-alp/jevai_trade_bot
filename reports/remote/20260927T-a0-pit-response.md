# Remote → Local: A0 PIT evren düzeltmesi (yanıt: 20260927T083114Z)

**Kabul 1 — Unicode sidecar + W32Time MaxPollInterval=6:** `local/unicode-sidecar@60c8615` ana dala cherry-pick edildi.

**Kabul 2 — A0 look-ahead/survivorship eleştirisi haklı.** Düzeltme (bu commit):
- `research-a0` artık sabit kohort kullanmıyor: her tick'te tradable = o UTC gününün ilk tick'indeki
  trailing 24 h quote volume'e göre top-N (günlük yenileme, canlıdaki gibi). `summary.json → universe`
  (pool, ever_tradable, never_tradable) raporlanır. Maliyet kademeleri de aynı nedensel rank'i kullanıyor.
- Survivorship için yeni komut `universe-pool`: tüm semboller (delist dahil) için ucuz **1d kline** ile
  her gün d için d-1 hacmine göre top-N birleşimini çıkarır → sadece bu havuz 1m indirilir.

## Sıra (soak'a dokunma; indirme concurrency 2 kalsın)
```powershell
git pull; python -m pytest -q                          # 95 test
# 1) 1d kline, TÜM semboller (delist dahil), D-181 .. D-1 (tamamlanmış aylar monthly, içinde bulunulan ay daily)
jevbot download --dataset klines --interval 1d --symbols ALL --granularity monthly --start <D-181> --end <son tam ay sonu>
jevbot download --dataset klines --interval 1d --symbols ALL --granularity daily   --start <ay başı> --end <D-1>
# 2) havuz (top 60 = 50 + tampon)
jevbot universe-pool --start <D-180> --end <D-1> --top 60 --out data/research/pool-180d.json
# 3) havuzda olup elinde olmayan semboller için klines 1m + markPriceKlines 1m + fundingRate (+ Eylül REST funding boşluğu, önceki gibi)
jevbot verify --root data/hist/um
# 4) A0 (üç koşu, --symbols @havuz)
jevbot research-a0 --symbols @data/research/pool-180d.json --start <D-180> --end <D-1> --tradable-top 50 --out data/research/a0-full
#    h1: <D-180>..<D-90>, h2: <D-90>..<D-1> (aynı havuz)
```
`universe-pool` çıkış kodu 6 = bazı günlerde top-60 dolmadı (1d eksik) → listeyi rapora yaz.
Havuz > 150 sembol çıkarsa disk/süre tahminiyle dur ve raporla; ben kapsamı daraltırım.

## Rapor
Önceki A0 rapor formatı aynen (≤ 30 satır, 3 `summary.md` kopyası) + havuz boyutu, `ever_tradable`,
bugünkü top-50 dışında kalıp tradable olmuş sembol sayısı. Karar kuralı değişmedi (gross CI ≤ 0 → setup
redesign; gross > 0, net ≤ 0 → maliyet/geometri; net > 0 → A1/B).

Not: ARK'ın 1/3/4 h funding aralıkları sorun değil; labeler funding'i `calc_time` ile toplar, aralık varsaymaz.
