# follow.v1 — büyükleri / siyasetçileri takip etmek bir şey katıyor mu? (ön kayıt, sonuç görülmeden, 2026-10-06)

Ham kongre işlem verisi (House/Senate Stock Watcher) erişilemiyor (403). Fikri gerçekte uygulayan ETF'ler vekil:
- GURU (Global X Guru): hedge fonların 13F'deki en yüksek kanaatli hisseleri, çeyreklik; veri 2012-06'dan.
- NANC (Unusual Whales Subversive Democratic Trading): Demokrat kongre üyelerinin bildirdiği işlemler; veri 2023-02'den.
- KRUZ (Cumhuriyetçi karşılığı) artık işlem görmüyor → dışarıda (hayatta kalan yanlılığı notu).

## Test
Günlük getiriler, nakit (^IRX) çıkarılmış. Regresyon: GURU ~ SPY + QQQ + IWM; NANC ~ SPY + QQQ (sektör eğilimini ayırmak için).
Yıllık alfa (sabit × 252) ve 20 g blok bootstrap %95 CI. Ücretler ETF getirisinin içinde (takip etmenin gerçek maliyeti).

## Karar
- Alfa CI alt > 0 → "takip" katmanı aday; ikinci adımda trend sistemine varlık olarak eklenip v1'e karşı test edilir.
- Değilse katman kapanır (takip, piyasa + sektör eğiliminden fazlasını vermiyor).
- NANC'ın 3.6 yılı düşük güç demek; "kanıt yok" ile "yok olduğu kanıtlandı" ayrı yazılır.
