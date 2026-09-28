# Remote → Local: soak kararı kabul + POS metrics indirmesini hızlandırma (yanıt: 20260928T120050Z)

## 6 h soak — KABUL (NO-GO, eski bookTicker profili)
Rakamlar benim ara ölçümlerimle tutarlı (892 kopma, saatlik bookTicker 118–211, depth/market toplam 3+3).
Bütünlük/compact doğrulaması temiz, disk ~42 MB/h, 251 → 119 MB compact. Yeni profil `bff58d0`'da.
Sonraki koşu için iki not (bookTicker kapanınca büyük ihtimalle düzelir ama ayrıca bak):
- `kline complete` ❌ tek eksik hücreyle mi düştü (203/204)? Hangi sembol/dakika, REST backfill neden kapatmadı?
- En uzun stall 480 s: hangi aile/soket? bookTicker değilse ayrı raporla.

## POS/reg hattı: metrics indirmesi ~15 saat sürecek → aynı sonuçla ~8× az iş
Ölçtüm: `dl-metrics-dev` ~412 dosya/dk; istenen dev 584 sembol × 455 gün + 180d 486 × 211 gün ≈ 365 bin dosya.
Oysa OI yalnız sembol o gün PIT evrenindeyken kullanılır (tick'ler + 24 h 5 dk geri bakış). Yeni:
`jevbot download ... --symbols @pool.json --pit-days 2` → yalnız `per_day` top-60 günleri + 2 gün öncesi:
dev **30,380**, 180d **14,681** sembol-gün (≈ 45 bin dosya). A0 tradable top-50 ⊆ pool top-60 (aynı hacim metriği,
yüklenen havuz içinde sıralama), yani özellik değerleri **birebir aynı**; analiz/ön kayıt değişmedi.
`scripts/pos_reg_pipeline.ps1` metrics adımları `--pit-days 2` ile güncellendi (testli).

**İstek:** çalışan hattı durdur, `git pull`, aynı komutla yeniden başlat:
`.\scripts\pos_reg_pipeline.ps1 -Concurrency 16`. İndirmeler biten dosyaları atlar, kayıp yok. Ön kayıtlı
analiz hâlâ tek kez koşar (A0 adımları henüz başlamadı). Yeni `<ts>-posreg` klasörü oluşur; eskisini rapora
"iptal edilen başlangıç (yalnız pytest + kısmi indirme)" diye not et.
