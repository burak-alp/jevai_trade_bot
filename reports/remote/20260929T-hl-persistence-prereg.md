# Remote → Local: Hyperliquid trader kalıcılığı `hlp.v1` — ön kayıt (sonuç görülmeden, 2026-09-29)

Soru (kullanıcı): "Gerçek insanlar trade ile kazanıyor." Kazanç **beceri** mi (dönemden döneme sürer) yoksa **şans** mı
(ortalamaya döner)? Sürerse: kopya / öğrenme adayı. Hyperliquid zincir üstü, sahtelenemez.

## Veri
- `stats-data.hyperliquid.xyz/Mainnet/leaderboard` (29.09 anlık, 46,713 hesap) → örneklem çerçevesi.
- Hesap başına `info {type: portfolio}` → `perpAllTime` pnlHistory + accountValueHistory (birikimli, ~3-4 günde bir nokta).
- **Hayatta kalma yanlılığını azaltmak için** güncel hesap değerine göre süzme yok. Örneklem: allTime hacim ≥ $1M olan
  hesaplardan rastgele 3,000 (seed 20260929) + allTime PnL'e göre ilk 500 (ayrı raporlanır, "görünür kazananlar").

## Dönemler ve ölçü
- P1 = 2026-03-01 → 2026-06-01, P2 = 2026-06-01 → 2026-09-01. Dönem PnL'i = birikimli PnL farkı (uç noktalara en yakın
  noktalar, ≤ 5 gün uzaklık). ROI = PnL / dönemdeki ortalama hesap değeri (ort. ≥ $1,000 olanlar).
- Birincil: P1 ROI'ye göre en üst ondalık dilimin P2 ROI ortalaması; hesaplar üzerinden bootstrap %99 CI.
- İkincil: P1–P2 ROI Spearman korelasyonu; üst dilim − medyan hesap farkı; ciro (hacim/değer) terciline göre kırılım
  (yüksek ciro ≈ piyasa yapıcı / maker iadesi).

## Karar
- **Beceri var:** üst ondalık P2 ROI %99 CI alt > 0 **ve** Spearman > 0 (%99 CI alt > 0) **ve** düşük/orta ciro
  tercilinde de aynı işaret (yalnız piyasa yapıcıların maker iadesi değil).
- Varsa sonraki adım (ayrı ön kayıt): `userFills` ile kopya gecikmesi + Binance maliyetiyle takip stratejisi testi.
- Yoksa: görünür kazananlar büyük ölçüde şans/beta; kopya yolu kapanır.
- Uyarı: tek piyasa dönemi (2026 Mart–Ağustos), 3 aylık pencereler; beta (boğada long) kontrolü yok → pozitif
  sonuç bile "long ağırlıklı hesaplar yükselen piyasada kazandı" olabilir; yön payı ayrıca raporlanır.
