# Remote → Local: yavaş aileler `slow.v1` — ön kayıt (yanıt: 20260928T081457Z)

**Kabul:** drift kararı (b) → 5 dk BRK/PB bırakıldı. `report.py` PIT kline düzeltmesi doğru, kabul.
Not: `src/` değişikliği için kural `local/<konu>` dalı; bu seferlik sorun değil, bundan sonra lütfen dal.
**Holdout notun haklı:** 2026-03-31 → 09-27 artık temiz değil. Nihai test = **ileriye dönük kilitli paper**.
180 gün yalnızca ikincil kontrol olarak kalıyor.

## Aileler ve parametreler — geliştirme verisi görülmeden sabitlendi, ayar yok
Kod: `src/jevbot/research/slow.py`, `jevbot research-a0 --arm slow`. Saatlik karar; stop = 3 × ATR(1h)
(fee ≈ 0,03 R); maliyet kapısı ≤ 0,10 R; PIT günlük top-50; listing ≥ 30 gün; 24 h hacim ≥ 20M.
- **TSM** — 7 günlük (168 × 1h) Donchian kanalının taze kırılımı, EMA50/EMA200 trend yönünde; TP 3 R, 48 h,
  sembol başına 24 h cooldown.
- **XSM** — her gün 00:00 UTC'de BTC'ye göre 7 günlük getiride en güçlü 3 long, en zayıf 3 short; 24 h zaman çıkışı.
- **FUND** — son ödenen funding (8 saate normalize) ≥ +5 bps ve 24 h yükseliş ≥ 6 ATR(1h) → short; aynasında
  (≤ −5 bps ve ≤ −6 ATR) → long; TP 2 R, 24 h.

## Koşu (6 h recorder koşusu bittikten sonra; geliştirme verisi indirilince)
```powershell
git pull; python -m pytest -q                       # 107 test
jevbot research-a0 --arm slow --symbols @data/research/pool-dev.json --start 2025-03-01 --end 2026-03-31 --out data/research/slow-dev
jevbot research-a0 --arm slow --symbols @data/research/pool-dev.json --start 2025-03-01 --end 2025-09-30 --out data/research/slow-dev-h1
jevbot research-a0 --arm slow --symbols @data/research/pool-dev.json --start 2025-09-30 --end 2026-03-31 --out data/research/slow-dev-h2
```
Her aile için 180 günlük veride de (`--start 2026-03-31 --end 2026-09-27`, mevcut pool dosyası) aynı komut
çalıştırılır; bu yalnızca ikincil kontroldür. Üç + bir `summary.md`'yi rapora kopyala.

## Önceden kayıtlı karar (aile × yön, toplam 6 test; %99 CI çoklu testi hesaba katar)
- **GEÇER**: geliştirme koşusunda net %99 CI alt sınırı > 0 (`pass_99` listesi, n ≥ 30) **ve** her iki
  geliştirme yarısında net ortalama > 0.
- Geçen aile/yön → 180 günde net ortalama > 0 (ikincil) → sonra A1 (ML) / B (Jev) tasarımı + kilitli paper.
- **Hiçbiri geçmezse**: bu bir sonuçtur, parametre oynanmaz. Sonraki seçenekler: (i) maker giriş ile
  maliyeti yarıya indirip aynı ön-kayıtla tekrar, (ii) OI/likidasyon gibi yeni veri — sadece recorder'dan
  ileriye dönük toplanabilir, (iii) bu donanım ve maliyet yapısında edge yok sonucu. Gerçek para yok.

Rapor ≤ 25 satır: 4 koşu × (aile×yön satırları: n, gross, net [95%], net 99% lo, cost R) + `pass_99` + yarılar tutarlı mı.
