# overlay.v1 — LLM (Claude) haber/makro yorumu v1'e bir şey katıyor mu? (ön kayıt, 2026-10-06)

Geçmiş testi yapılamaz (model geçmişi bilir → bakış yanlılığı). Yalnız ileriye dönük paper.

## Kurulum
- v1 her haftalık rebalansta `run/macro-paper/context-<hafta>.json` yazar: v1 ağırlıkları, varlık başına 1/3/12 ay getiri,
  60 g oynaklık, son 36 saatin Yahoo başlıkları.
- Claude rutini (cumartesi) dosyayı + kendi web aramasını okuyup varlık başına eğim ∈ {−1, 0, +1} ve gerekçe yazar:
  `overlay-<hafta>.json`. Sabit istem sürümü `overlay.v1` (docs/overlay_prompt.md).
- `llm` hesabı (1x): ağırlık = v1 ağırlığı × {−1: 0.5, 0: 1.0, +1: 1.5}; v1'in tutmadığı varlık 0 kalır; toplam > 1 ise
  orantılı 1'e indirilir. Overlay dosyası gelene kadar v1 ile aynı; gelince sonraki günlük çalışmada uygulanır.

## Değerlendirme (haftalık getiri farkı llm − 1x)
- 13. hafta futility: ortalama fark ≤ 0 → kapanır.
- 26. hafta GO: farkın 4 haftalık blok bootstrap %95 CI alt sınırı > 0.
- İstem, eğim ölçeği ve kurallar bu süre boyunca değişmez.
