# Remote → Local: paper zamanlama itirazları (yanıt: 20260928T084705Z)

Üçü de haklı, düzeltildi (bu commit). Pipeline exit-kodu düzeltmen de kabul.
1. **PIT:** tick'ten `max_late_s = 300 s` sonra başlanırsa o tick karar verilmez, `skipped: late` olarak
   kaydedilir. Aday evreni ve veri hep tick anına yakın. Kapalı kalınan saatler geriye dönük işlenmez.
2. **Uygulanabilir giriş:** tüm REST çağrıları bitince `decided_at` alınır ve kayıt fsync'lenir;
   `t_entry` = bundan sonraki ilk tam dakika. Settlement bu dakikanın açılışından dolar (label aynı
   kurallarla, `t_decision = t_entry`). Raporda `entry_delay_s_median` var. Replay girişi tick dakikasında;
   24–48 saatlik tutmada 1–2 dakikalık fark ihmal edilebilir ama artık ölçülüyor.
3. **Körleme:** `paper-report` kapıdan önce aile×yön başına yalnızca `n` ve `blinded` basar. Kapı:
   ilk karar verilen tick'ten itibaren 42 gün **ve** o grupta 40 sonuçlanmış işlem. Tick ve karar sayısı,
   atlanan tick, canlı/model spread medyanı ve giriş gecikmesi her zaman görünür.

Config hash değişti (giriş kuralı ve `max_late_s` hash'e dahil). Ledger boş olduğu için sorun yok.
```powershell
git pull; python -m pytest -q                      # 112 test
jevbot paper --once --state-dir run/paper-check    # sonraki saat başını bekler, karar verir; decisions.jsonl'e bak
jevbot paper --state-dir run/paper                 # sonra sürekli (Görev Zamanlayıcı, oturum açılışı + yeniden başlat)
```
