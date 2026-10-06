# regime.map.v1 — hangi dönemde hangi strateji? (ön kayıt, sonuç görülmeden yazıldı, 2026-10-06)

Kullanıcı isteği: son 5 yılda dönem (rejim) → en uygun strateji eşlemesi. Kullanıcı seçimleri: kripto büyükler,
rejime göre dönemler, trend takibi + kırılım, günlük + 4 saatlik.

## Veri ve evren
- Binance USDⓈ-M, data.binance.vision klines 1d (tüm semboller, delist dahil) + 4h ve fundingRate (havuz).
- Pencere: 2021-10-01 → 2026-09-30 (5 yıl). Evren: her gün, önceki günün quote hacmine göre ilk 20 (PIT, delist dahil).
- Pozisyon: evrendeki her coin eşit ağırlık 1/20, toplam brüt ≤ 1x. Maliyet: her birim ciro için 6 bps (taraf başı).
  Funding: pozisyon × funding (long öder pozitifte). Sinyal bar kapanışında, işlem aynı kapanıştan (gecikme yok,
  bir sonraki barın getirisi kazanılır).

## Stratejiler (sabit ızgara, 20 adet)
- Günlük TREND: L ∈ {10, 30, 90} gün getirisi işareti; mod ∈ {long/short, yalnız long}.
- Günlük KIRILIM (Donchian): N ∈ {20, 55} gün; giriş kapanış > N-gün zirvesi (long) / < N-gün dibi (short);
  çıkış karşı N/2 kanalı; mod ∈ {long/short, yalnız long}.
- 4 saatlik TREND: L ∈ {2, 5, 10} gün (12/30/60 bar); aynı iki mod.
- 4 saatlik KIRILIM: N ∈ {20, 55} bar; aynı kurallar, iki mod.
- Referanslar (strateji sayılmaz): evren eşit ağırlık hep long, BTC hep long, nakit (0).

## Rejimler (BTC 1d)
- Nedensel (o an bilinen): BTC son 90 gün log getirisi r90: BOĞA r90 > +0.15, AYI r90 < −0.15, YATAY arada.
- Kahin (geriye bakınca): aynı eşikler, [t−45, t+45] gün getirisi (geleceği kullanır; yalnız betimleme).

## Çıktılar
1. Betimleyici harita: her strateji × kahin rejim ve × nedensel rejim için yıllık getiri, Sharpe, gün sayısı.
2. **Asıl test (örneklem dışı):** dönem 1 = 2021-10 → 2024-03, dönem 2 = 2024-04 → 2026-09.
   Dönem 1'de her nedensel rejim için en yüksek Sharpe'lı strateji seçilir (REJİM-GEÇİŞ), ayrıca tüm dönem 1'de en
   iyi tek strateji (TEK-EN-İYİ). Dönem 2'de ikisi de nedensel rejimle, değişiklik yapmadan çalıştırılır.
   - **GEÇER:** dönem 2'de REJİM-GEÇİŞ − TEK-EN-İYİ günlük farkının 7 günlük blok bootstrap %95 CI alt sınırı > 0
     **ve** REJİM-GEÇİŞ yıllık getirisinin CI alt sınırı > 0.
   - Ek bilgi: kahin rejimle aynı geçiş (tavan) ve evren hep long ile kıyas.
3. Eşik gevşetme yok; ızgara genişletilmez. Sonuç ne çıkarsa STATE'e yazılır.

## Netleştirme (sonuç görülmeden, kod yazılırken)
- Evren yalnız USDT kotalı, stabil olmayan bazlar (USDC/BUSD/TUSD/FDUSD/… hariç); BUSD çiftleri çift sayım olmasın diye dışarıda.
- Son gün: yerel 1d verisi 2026-09-26'da bitiyor → değerlendirme 2026-09-26'ya kadar.
- Rejim geçişinde strateji değişen gün 2 × 6 bps (tam ciro varsayımı) ek maliyet.
- TradFi/endeks kontratları (exchangeInfo underlyingType ≠ COIN: hisse, emtia, FX, endeks) evren dışı; kullanıcı kripto seçti.
