# Remote → Local: funding carry `carry.v1` — ön kayıt (sonuç görülmeden, 2026-09-29)

Fikir: spot long + perp short, eşit notional → fiyat yönü nötr; gelir = perp short'un aldığı pozitif funding.
Yön tahmini yok. Kaynak tezi: BIS WP 1087 "Crypto carry". Eski projede hiç test edilmedi.

## Veri
- Funding: `fundingRate` (havuzlar için mevcut). Baz: `premiumIndexKlines` 1h (indirilecek) — premium ≈ (perp − spot)/spot.
  Uzun spot + kısa perp getirisi ≈ −Δpremium (+ funding). Spot fiyatı ayrıca gerekmez.
- Evren: günlük PIT havuzları (pool-2024 ∪ pool-dev ∪ pool-180d, top-60), perp listing ≥ 30 g, **Binance'te spot USDT
  çifti olan** semboller (1000/1000000 önekleri eşlenir). Dönem 2024-01-01 → 2026-09-27.

## Kurallar (ayar yok)
- Karar günlük 00:00 UTC, yalnız o anda bilinen veriyle. Sinyal: son 7 günde ödenen funding'lerin 8 saate normalize
  ortalaması, yıllık = ort × 3 × 365.
- **carry.v1 (seçici):** yıllık ≥ %15 → gir; yıllık < %5 **veya** sembol havuzdan çıktı → çık. En çok 10 pozisyon,
  en yüksek funding önce; her pozisyon sermayenin 1/10'u.
- **carry.base (referans):** BTC + ETH sürekli açık, sinyalsiz.
- Sermaye: notional N başına spot N + perp teminatı N/3 (3x) → 1.333 N. Boş yuva nakit (getiri 0).
- Maliyet (her giriş ve çıkış, çift başına): spot taker 10 bps + perp taker 5 bps + her bacağa 3 bps kayma = 21 bps.
- Teminat: perp girişten +%20 yükselirse yeniden dengeleme sayılır, ek 26 bps maliyet, referans sıfırlanır.
- Getiri: günlük sermaye getirisi = Σ(funding alınan + baz PnL − maliyetler) / sermaye.

## Karar kuralı
- **GEÇER:** carry.v1 tüm dönemde yıllık net getiri > 0 ve günlük getirilerin 7 günlük blok bootstrap %99 CI alt sınırı
  > 0; iki yarıda (→ 2025-05-15, → 2026-09-27) net > 0; sermaye üzerinde maks. düşüş ≤ %10.
- Geçerse: kilitli paper (spot + perp, emirsiz) → testnet → küçük gerçek sermaye. Jev: "bu çifte carry açılsın mı" vetosu (B).
- Geçmezse: bu maliyet yapısında (VIP0 taker) carry de yok; maker/VIP maliyetiyle yeniden ön kayıt tek seçenek.
- Beklenti (dürüst): geçse bile yıllık tek hane – düşük çift hane; "büyük kâr" değil, yapısal ve yönsüz gelir.
