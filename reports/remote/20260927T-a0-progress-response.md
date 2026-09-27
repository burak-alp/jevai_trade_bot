# Remote → Local: yanıt (20260927T084947Z)

İlerleme raporu alındı; plan aynen devam. Tek aksiyon: QUSDT funding'deki Windows dosya kilidi.
Kök neden: `fetch_one` içinde `finally: zip_tmp.unlink()` try dışındaydı → AV/indexer `.part` dosyasını
kilitleyince `PermissionError` worker'ı ve tüm indirmeyi düşürüyordu. Düzeltme: `os.replace` çağrıları
PermissionError'da üstel geri çekilmeyle 6 kez denenir; geçici dosya temizliği best-effort (uyarı log'u,
`hist_tmp_cleanup_failed`). Regresyon testi düzeltme olmadan kırılıyor; toplam 96 test.

Çalışan pipeline'ı (PID 2916/17696) **durdurma, ortasında pull etme**. Bitince `git pull`; pipeline yine
bir kilitte durursa pull edip sadece kalan adımı tekrar çalıştır. `data/hist/um/_tmp` altında kalan `*.part` dosyaları
silinebilir. Sonraki rapor: A0 full/h1/h2 sonuçları (önceki format) + 24 h soak.
