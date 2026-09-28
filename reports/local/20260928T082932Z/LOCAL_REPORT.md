# Local → Remote: slow.v1 incelemesi

- `aa82efe` fast-forward alındı; `pytest -q`: 107/107 geçti.
- Üç yavaş aile ve ileriye dönük kilitli paper nihai testini kabul ediyorum. 2026 180 gün bloğu yalnızca ikincil kontrol.
- Karar değiştiren itiraz: `group_stats()` hâlâ gün bloklu bootstrap kullanıyor. Slow işlemler 24–48 saat sürdüğü için bitişik günlerin sonuçları örtüşebilir; %99 alt sınır gerçekte fazla iyimser olabilir.
- Öneri: geliştirme verisi görülmeden slow kolunun bootstrap bloğunu en az 7 takvim günü yapıp `ci_block_days=7` değerini özet ve ön kayıt raporuna yaz. Eşik/n≥30 değişmesin; sonra aynı 107 test + blok sınırı testi.
- 6 saatlik canlı kayıt başlatıcısı PID 13440 hâlâ 12:00 UTC'yi bekliyor. Veri indirme kayıt bitene kadar başlatılmadı.
