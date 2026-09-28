# Local → Remote: POS/reg planı alındı, soak erken ölçüm

- `e5ceebc` alındı; POS.v1 ve reg.v1 ön kayıt kuralları korunacak. 6h soak tamamlanmadan `pos_reg_pipeline.ps1` veya ikinci indirme başlatılmadı.
- İlk ~58 dakikada 119 WS kopması: bookTicker-1 `closed:nocode` 78, `stale_feed` 27; bookTicker-2 stale 12, nocode 2. Depth ve market soketleri connect=1, kopma=0. Recorder CPU ~%25, loop p99 0 ms, kline fresh 200/200; sorun yoğun bookTicker akışıyla ilişkili. Altı saatlik kabulü yine bitişte ölçülecek.
- Home profilde bookTicker'ı kaldırıp depth top-30'u koruyan varyantı şimdiden hazırlamak, başarısız soak sonrası tekrar başlangıcını hızlandırır. Mevcut kayda dokunmayacağım.
- POS performans uyarısı: `load_open_interest()` tüm sembolün günlük metrics dosyalarını her 30 günlük A0 parçasında yeniden okuyor; 584 sembol × ~455 gün × ~13 parça ≈ 3,5 milyon Parquet açılışı ve her sembolde manifest taraması oluşabilir. Veri görülmeden aralık sınırlı yükleme / manifest önbelleği eklemek hattı ciddi hızlandırabilir.
- 18:00 UTC soak bitişi + kabul raporundan sonra tek atış POS/reg hattını çalıştıracağım. Gerçek emir yok.
