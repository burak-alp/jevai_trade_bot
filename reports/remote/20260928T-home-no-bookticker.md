# Remote → Local: home profili — bookTicker kapalı (kullanıcı kararı, 2026-09-28 14:10 UTC)

- Kanıt (6 h soak 20260928T120050Z, 12:02–14:06): 279 kopma, %97'si bookTicker (bt-1 ≈ 140/h, 2,200 msg/s tek akış).
  13:30 UTC ABD açılışında patlama: 13:30–13:50 arası 124 kopma, 13:50'den sonra ~0. depth-3 ≈ 1.5/h,
  market ≈ 1/h. CPU %29, loop lag ≤ 13 ms → yerel değil; kabul penceresi (public* ≤ 3/h) bookTicker yüzünden geçemez.
- `config/home.yaml`: `recorder.book_ticker: false`, `depth.top_n_by_volume: 10 → 30` (spread = depth top-of-book).
  Kabul kriterleri **değişmedi**; bu kapsam daraltması: slow/pos/reg ve Jev shadow tick-seviyesinde kotasyon istemiyor.
- Çalışan soak etkilenmez (config başta yüklendi); 18:00'de bitsin ve raporla. Sonraki recorder koşusu yeni profille,
  aynı kriterler; pos_reg hattı bittikten sonra başlat (indirme ile çakışmasın).
