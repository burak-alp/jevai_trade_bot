# Remote → Local: `/public` disconnect'leri (yanıt: 20260927T115843Z)

**Değerlendirme:** Soak baştan beri aynı PC/hatta indirme ile birlikte koşuyor (08:22'den beri 1m monthly
zip'ler, sonra ~9k küçük daily dosya). Gate 2'nin 1 saatlik temiz koşusunda indirme yoktu. Kopmaların
yalnızca en yoğun sokette (`public-4`: 50 bookTicker + 12 depth) toplanması ve saatlik artış, hat/CPU
çekişmesine işaret ediyor; ama bu henüz ölçülmüş bir sonuç değil. Soak'u durdurma, eşik gevşetme yok.

**Kod (bu commit, sadece sonraki recorder koşusunda etkili):** `closed:nocode` iki farklı şeyi
karıştırıyordu. Artık: `closed:<kod>` = sunucu close frame gönderdi; `closed:local_1011` = bizim keepalive
ping'imiz 20 s'de pong görmedi (backlog/ölü hat); `closed:nocode` = close frame'siz TCP EOF/reset.

## Ölçüm (mevcut veriyle, kod gerekmez)
1. Her `ws_disconnected` için en yakın health örneğinden: `loop_lag_p99_ms`, `cpu_pct`, `bytes_per_s`,
   `lat_p99_ms`. Özet: kopmalardaki medyanlar vs tüm soak medyanı.
2. Kopma/saat ve DEGRADED oranı, pencerelere göre: (a) indirme aktif, (b) indirme bitti + A0 koşuyor
   (sadece CPU), (c) ikisi de yok. Pencere sınırlarını log zaman damgalarından al.

## Önceden kayıtlı karar
- (c) penceresinde `public` kopma ≤ 1/saat ve loop lag p99 < 500 ms → neden hat çekişmesi. Aksiyon:
  kayıt/canlı sırasında indirme yok (operasyon kuralı), recorder'a dokunulmaz. Gate kabulü indirme-siz
  24 h (ya da soak'un (c) penceresi ≥ 12 h ise o kısım) üzerinden verilir.
- (c)'de hâlâ > 3/saat → recorder iterasyonu: home profilde bookTicker ve depth ayrı bağlantılar +
  bookTicker 50→30, sonra 6 h tekrar ölçüm.
- Kopmalarda loop lag p99 > 500 ms ise (her pencerede) → kendi CPU'muz; bunu ayrıca raporla.

Rapor ≤ 20 satır; A0 sonuçları ayrı rapor olabilir.
