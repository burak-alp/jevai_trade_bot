# Remote → Local: TradFi gece/hafta sonu açığı `gap.v1` — ön kayıt (veriye bakılmadan, 2026-09-29)

Fikir (kullanıcı): Binance hisse perp'leri 7/24; ABD borsası kapalıyken fiyatı kripto yatırımcıları belirliyor. Açılışta
gerçek fiyata dönüyor mu (aşırı tepki → geri dönüş) yoksa perp doğru mu tahmin ediyor?

- **Evren:** Binance USDⓈ-M hisse/ETF perp'leri (pool-tradfi.json; metaller ve emtia hariç: XAU XAG XPD XPT PAXG COPPER NATGAS).
  Sembol listelendikten ≥ 7 gün sonra. Dönem 2026-01-28 → 2026-09-25. Fiyat = 1 dk kapanış (last).
- **Kapalı dönem:** önceki işlem günü 16:00 New York → bu işlem günü 09:30 New York (NYSE 2026 tatilleri hariç; hafta sonu dahil).
  P_c = kapanış anındaki fiyat, P_pre = açılıştan 5 dk önce, P_30 = açılıştan 30 dk sonra.
  Sapma d = ln(P_pre / P_c); sonuç r = ln(P_30 / P_pre).
- **Kural (birincil, geri dönüş):** |d| ≥ %0.5 ise açılıştan 5 dk önce −sign(d) yönünde gir, açılış + 30 dk'da çık.
  Maliyet 20 bps gidiş-dönüş (taker 2 × 5 + kayma 2 × 5).
- **GEÇER:** işlem başına net getiri ortalamasının haftalık blok bootstrap %99 CI alt sınırı > 0, iki yarıda
  (→ 2026-06-01, sonrası) ortalama > 0, n ≥ 200. Ters yön (devam) de raporlanır ama geçme kriteri geri dönüş kuralı içindir;
  devam anlamlı çıkarsa ayrı ön kayıt gerekir.
- **Betimleyici:** tüm gecelerde r ~ d eğimi ve korelasyon; hafta sonu vs hafta içi ayrı.
- Uyarılar: tek dönem (~8 ay), semboller aynı gecelerde korele (haftalık blok bu yüzden), TradFi perp ücretleri/spread
  kripto perp'lerden farklı olabilir; açılış anında likidite ince olabilir.
