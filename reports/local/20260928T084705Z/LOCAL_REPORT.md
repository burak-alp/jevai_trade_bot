# Local → Remote: hızlandırma başlatıldı, paper zamanlama engeli

- `e6f99fb` fast-forward alındı. `slow_dev_pipeline.ps1 -Concurrency 8` PID 4280 ile 08:46 UTC'de başladı; 6 h recorder planı PID 13440 değişmedi.
- Pipeline'da A0 adımlarının exit kodu yutuluyordu; hata durumunda koşuyu durduracak şekilde script düzeltildi.
- Paper henüz başlatılmadı. `PaperEngine.step()` geç başlatmada mevcut saati geriye dönük işler: örn. 08:46'da 08:00 `t_tick`, aday evreni ise 08:46 REST verisinden. Bu PIT sızıntısı.
- Ayrıca normal 09:00:20 taraması onlarca REST çağrısı bitince karar yazıyor, ama label `t_decision=09:00` için 09:00 minute open'dan giriş varsayıyor. Karar kaydı ve gerçek yürütülebilir giriş sırası ters; bu haliyle prospektif getiri geçerli değil.
- `paper-report` kilitli kuraldaki 6 hafta/40 sonuç kapısından önce gross/net R ve CI basıyor; yalnızca sayıları göstermesi gerekiyor.
- Öneri: gecikmiş tick'i `skipped` kaydet; zamanında tick'te karar ve ilk uygulanabilir giriş dakikasını ayrı kaydet, settlement'ı o dakikadan hesapla; raporu kapı öncesi yalnızca sayılara sınırla. Sonra paper'ı başlatırım.
