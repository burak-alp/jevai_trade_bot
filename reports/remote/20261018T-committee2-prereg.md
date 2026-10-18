# committee.v2 — ileri görüşlü / daha cesur komite (ön kayıt, sonuç görülmeden, 2026-10-18)

Kullanıcı: "biraz daha agresif ve daha zeki / ileri görüşlü olalım". Kaldıraç etkisi ayrı ölçülüyor (2x hesap); v2 yalnız
karar zekâsını değiştirir (kaldıraçsız), böylece iki etki karışmaz.

## Kurulum
- Rutin `jevai-macro-committee-v2` (cumartesi 14:00): committee.v1'in analist dosyasını (aynı olgular) + bağlam + GPT
  okur; senaryolar, varlık başına p_up ve eğim ∈ {−2..+2} yazar (docs/committee2_prompt.md, sabit).
- `committee2` hesabı (1x): eğim çarpanı {−2: 0, −1: 0.5, 0: 1, +1: 1.5, +2: 2}; v1'in tutmadığı varlıkta +1/+2 = %5/%10
  yeni pozisyon; toplam > 1 → orantılı 1'e. Dosya gelince sonraki gece uygulanır.

## Değerlendirme
- Haftalık getiri farkı committee2 − 1x: 13. hafta futility (ortalama ≤ 0 → kapanır), 26. hafta GO (4 hafta blok
  bootstrap %95 CI alt > 0).
- İleri görüş ölçüsü: p_up tahminlerinin Brier skoru, taban (0.5 sabit = 0.25) ile kıyas; 26. haftada Brier < 0.25 ve
  20 haftalık blok bootstrap CI üst < 0.25 ise "ileri görüş var" denir. Karne `jevbot macro-status`ta.
- İstem ve kurallar bu süre boyunca değişmez.
