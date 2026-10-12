# committee.v1 — çok ajanlı yatırım komitesi v1'e bir şey katıyor mu? (ön kayıt, 2026-10-13)

Kullanıcı hedefi: dünyayı (merkez bankaları, büyük oyuncular, jeopolitik, kripto) izleyip gerekçeli karar veren,
"ne durumdayız" sorusuna cevap veren profesyonel masa. Geçmiş testi yapılamaz (modeller geçmişi bilir) → ileriye dönük.

## Kurulum
- Rutin `jevai-macro-committee` (cumartesi 13:00, Claude): 4 analist alt ajanı (makro, akislar, jeopolitik, kripto;
  model sonnet, web araması) + Jev'in 5 günlük olasılıkları (gece görevi, JEV_API_KEY ile) → PM kararı, istem
  docs/committee_prompt.md (sabit).
- `committee` hesabı (1x): v1 ağırlığı × {−1: 0.5, 0: 1, +1: 1.5} (overlay.v1 ile aynı mekanik), dosya gelince sonraki
  gece uygulanır. overlay.v1 (llm hesabı) aynen devam eder; iki hesap birbirinden bağımsız.

## Değerlendirme (haftalık getiri farkı committee − 1x)
- 13. hafta futility: ortalama fark ≤ 0 → kapanır.  26. hafta GO: 4 haftalık blok bootstrap %95 CI alt > 0.
- Karne (bilgi): her üyenin (4 analist, PM, llm, Jev) sıfır olmayan görüşleri haftalık gerçekleşen getiriyle puanlanır
  (isabet, ortalama işaretli getiri). Ağırlık değişikliği yalnız 26. hafta sonrası, yeni ön kayıtla.
- İstem, eğim ölçeği ve kurallar bu süre boyunca değişmez.

## Değişiklik (13.10, değerlendirme başlamadan; W41 çalışması deneme sayılır, tüm eğimler 0'dı)
- GPT üyesi eklendi: `gpt-<hafta>.json` (docs/gpt_member_prompt.md, Codex yerelde yazar); PM istemine Jev + GPT
  üyeleri bölümü eklendi (committee_prompt.md adım 3). PM = rutinin modeli (kullanıcı Opus seçer); analistler sonnet.
- Değerlendirme penceresi W42'den (16.10 rebalansı) başlar: 13. hafta futility, 26. hafta GO aynen.
