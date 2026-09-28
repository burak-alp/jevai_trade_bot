# Remote → Local: pos.v1 kodu hazır + reg.v1 ön kaydı (tek atış hattı)
(önceki: 20260928T-slow-verdict-response.md — oradaki elle `metrics` komutlarının yerine bu hat geçer)

## pos.v1 — kod (veri görülmeden; parametreler önceki dosyadaki gibi)
`jevbot research-a0 --arm pos` → `src/jevbot/research/slow.py` (`PosConfig`, `pos_config()`), OI yükleyici
`data.load_open_interest`. Uygulama ayrıntıları (sonuç görülmeden sabitlendi):
- OI = `metrics.sum_open_interest` (kontrat adedi; fiyat etkisi yok). Satır t anında yalnızca
  `create_time + 5 dk ≤ t` ise ve bundan en çok 30 dk eskiyse kullanılır; yoksa NaN → sinyal yok.
- Cooldown sembol × aile başına (yönden bağımsız), slow.v1 ile aynı. Maliyet kapısı ve etiketleyici aynı.
- slow.v1 paper config hash'i değişmedi (`pos` alanı yalnızca pos kolunda yazılır).
- Plumbing kontrolü: diskteki 3 günlük BTC metrics (2026-09-22..24, dev dışında) ile OI özellikleri sonlu;
  sinyal/sonuç bakılmadı. 39 research testi + tam suite geçti.

## reg.v1 — kullanıcının hipotezi: "piyasanın yönünü bil, ona uygun aileyi çalıştır"
Rejim etiketi karar anında yalnız kapanmış veriden: `btc_trend` = BTC 1h EMA50 − EMA200 işareti.
Kural: long önerisi yalnız `btc_trend = +1`, short yalnız `−1` iken alınır. slow.v1 sinyalleri **aynen**
(TSM/XSM/FUND, parametre değişmez); sadece rejime ters yönlüler atılır. Raporda `<aile>_<yön>@reg` satırları.

**Neden dev'de değil:** slow.v1 dev yarılarında TSM long h1'de, TSM short h2'de kazandı — bu zaten bir
rejim ipucu ve biz onu gördük. Aynı pencerede test kendini kandırmak olur. Temiz pencere:
**2024-01-01 → 2025-03-01 (hiç bakılmadı)**, 30 g warmup 2023-12. Yarılar: → 2024-08-01, → 2025-03-01.

**GEÇER (6 test, aile×yön @reg):** holdout'ta net %99 CI alt > 0 (n ≥ 30, 7 günlük blok) **ve** iki yarıda
net > 0. Geçen → kilitli prospektif paper. Geçen yoksa → BTC-trend rejim kapısı bu ailelerde kapanır;
bu holdout yakılmış sayılır, üzerinde başka rejim tanımı denenmez.
Ek kazanım: aynı koşu ungated slow.v1'in ilk gerçek örneklem dışı tekrarını da verir (yalnız bilgi).
Not: `btc_trend` etiketi artık her slow/pos önerisinde dolu; `btc_trend_up/down` satırları betimleyicidir.

## Koşu — soak 18:00 UTC'de bitip raporlandıktan sonra
```powershell
git pull; .\scripts\pos_reg_pipeline.ps1 -Concurrency 8
```
Adımlar: pytest → metrics (dev + 180d havuzu) → verify → A0-pos dev/h1/h2/180d → 2024 1d + PIT havuzu →
1m kline/mark/funding → verify → A0-slow holdout/h1/h2. Çıktı `reports/local/<ts>-posreg/`.
Rapor ≤ 30 satır: metrics kapsaması (sembol-gün %, warn/suspect) + pos `pass_99` + h1/h2 tablosu (4 satır) +
holdout `pass_99` ve `reg_pass_99` + @reg h1/h2 tablosu (6 satır). Karar kuralını uygula, eşik değiştirme.
