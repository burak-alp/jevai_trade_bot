# Local → Remote: Jev shadow bağlantısı ve recorder profili

- `bff58d0` alındı. Çalışan 6h recorder PID 5212 eski profili kullanıyor; yeni `home.yaml` yalnız sonraki koşuda geçerli. Kayda dokunulmadı.
- Kullanıcının başlattığı tek Jev shadow process PID 32352 (`run/paper-jev`); ikinci yazar açılmadı. Eski `run/paper` ve devre dışı Windows görevi ayrı kaldı.
- Yerel `jev-check --jev-base-url https://jev-ai.pro/api`: exit 0, models `jev-1.13.0 (versioned)`, yanıt `status=ok`, dönen model aynı, gecikme 477 ms, kullanım 797 giriş/56 çıkış token. Key değeri hiçbir yere yazılmadı.
- 30 sembol × 6 panel/gün ≈ 180 C çağrısı; bu örnek boyutuyla yaklaşık 143k giriş/10k çıkış token/gün (çıkarım, gerçek kullanım günlük rapordan izlenecek). Ön kayıtlı state/soru/panel değiştirilmedi.
- 6h soak sürüyor; 18:00 UTC sonrası kesin rapor + tek atış POS/reg hattı. Gerçek emir yok.
