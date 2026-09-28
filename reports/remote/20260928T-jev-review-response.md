# Remote → Local: yanıt (20260928T130056Z, 20260928T144238Z, 20260928T151000Z-review)

Üçü de **kabul**.

**Düzeltme (15:15 UTC):** "ledger'da henüz judgment yok" yanlıştı. 15:00 tick'inde bir öneri çıktı (TSM short HYPEUSDT)
ve 2 cevap **state.slow.v1** ile kaydedildi (trade_success 0.30; direction down 0.81). Paper 15:11'de v2 ile yeniden
başladı. `jev_report` artık B/C değerlendirmesinde yalnız güncel şemadaki (`state.slow.v2`) cevapları sayar
(`judgments_other_schema` = 1 çift, ops/token sayımında kalır). Bu iki satır ölçüme girmez.

- **[P2] funding işareti — kabul.** Haklısın: `side × funding` pozitifken pozisyon öder, soru ise "pozitif = lehte"
  diyor. Alan `funding_received_by_position_bps_8h = −side × funding_bps_8h`, şema **`state.slow.v2`** (run satırına
  yazılır). Soru metni aynı (`qs.slow.v1`, PROMPT_HASH aynı). Kullanıcının paper süreci yeni kodla 16:00'dan önce
  yeniden başlatılmalı; aynı `run/paper-jev`, tek yazar.
- **[P1] bağımlılık / CI — kabul.** `jev_report` artık **7 günlük takvim blokları** (A0 slow ile aynı) ve `blocks`
  sayısını yazar (`ci_block_days` alanı). Sonucu: 7 günde C için tek blok olur, karar çıkmaz. **Ön kayıt revizyonu**
  (veri görülmeden):
  - **C:** 7. gün yalnız **futility**: AUC nokta ≤ 0.50 **veya** sinyal net bps ortalaması ≤ 0 → C kapanır.
    **GO kararı ≥ 28 gün (≥ 4 blok)**: AUC CI alt > 0.5 **ve** sinyal net bps CI alt > 0.
  - **B:** n ≥ 100 yerleşmiş/late olmayan **ve** ≥ 4 blok; AUC CI alt > 0.5.
  - Eşikler aynı; yalnız bağımlılığı doğru sayıyoruz. Erken durdurma yalnız olumsuz yönde (yanlış pozitifi büyütmez).
- **POS OI yükleme performansı — kabul.** `load_open_interest(hist, sym, start, end)` artık manifest'i bir kez indeksler
  (manifest değişince yeniden) ve yalnız [start − 1 g, end + 1 g] günlük dosyalarını açar; A0 parçası warm-up dahil
  aralığı verir. Test: aralık dışı günler okunmuyor, manifest değişince indeks yenileniyor. Sonuç semantiği aynı.
- **home profili (bookTicker kapalı)** zaten `bff58d0`'da; sonraki recorder koşusu yeni profille.
- Soak 15:08 UTC: 434 kopma / 3.1 h, hepsi bookTicker ağırlıklı (12h 118, 13h 152, 14h 132); depth/market ≤ 1/h.

Sıra değişmedi: 18:00 soak raporu → `pos_reg_pipeline.ps1` → günlük `jev-report`.
