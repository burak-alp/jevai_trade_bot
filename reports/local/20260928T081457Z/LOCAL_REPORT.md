# Local → Remote: drift sonucu ve kayıt raporu düzeltmesi

- Branch: `claude/sleepy-goldberg-ypz1k2`; Opus `b213956` fast-forward alındı.
- `research-drift --all`: full 3979, h1 2054, h2 1924 etiketli öneri; üç tablo yan dosyalarda.
- Önceden belirtilen eşik hiçbir aile×yön/ufukta sağlanmadı: CI alt sınırı − medyan maliyet R için en iyi değer h1'de −0,190; h2'de −0,212 (aileler arası en iyi).
- Karar: BRK/PB 5 dk sinyalleri bırakılmalı; A1/B/Jev açılmamalı. Yeni yavaş aileler yalnızca ayrı geliştirme verisinde tasarlanmalı.
- Önemli metodoloji notu: 2026-03-31→2026-09-27 bloğuna A0 ve drift için bakıldı; yeni aile seçimini etkilediği için artık tam anlamıyla `untouched holdout` değil. Prospektif, kilitli ek test gerekecek.
- Eski soak `kline_completeness.cells_missing=31400` rapor hatasıydı: 225 farklı sembolün tamamı 1256 dakikanın her birinde beklenmiş. Ham veride her dakikada tam 200 benzersiz kline var (251200 satır); PIT evren snapshot'larıyla hesaplayınca 0 eksik.
- `report.py` PIT üyeliğini kline kapanış dakikasına göre hesaplayacak şekilde düzeltildi. 24 saat kabulü yine yok: elektrik kesintisi, unhealthy ve feed stall sorunları sürüyor.
- Doğrulama: 103/103 genel test (Opus commit'i), 12/12 entegrasyon ve yeni kapsama testi geçti; eski soak raporu yeniden hesaplanıp 0 eksik doğrulandı.
- 6 saatlik yeni profil koşusu için PID 13440 hâlâ 12:00 UTC başlangıcını bekliyor; eşzamanlı indirme açılmadı. Eski geliştirme penceresi indirmesi 6 saatlik koşudan sonra başlayabilir.
