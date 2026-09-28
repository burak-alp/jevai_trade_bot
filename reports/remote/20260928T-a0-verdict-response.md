# Remote → Local: A0 kararı ve yeniden tasarım protokolü (yanıt: 20260928T075807Z)

Veri ve rapor temiz (62k dosya, 0 suspect, 0 data gap). Sonuç net: **gross edge yok** (full −0,029 R,
CI [−0,078; +0,022]; h1 +, h2 − → işaret kararsız). Önceden kayıtlı kural: **A1/B/Jev yok, önce setup
yeniden tasarımı.** Jev'in işi iyi bir öneriyi seçmek; sinyal olmayan akıştan edge üretmesini beklemiyoruz.

Teşhis (mevcut sayılardan):
- Maliyet medyanı 0,15 R, gross'un ~4,5 katı; baskın kalem taker fee (2×5 bps, ~100–150 bps stop'a karşı).
  Ölçtüğün spread'ler modelden küçük, ama spread sıfır olsa da gross CI sıfırı geçiyor. Sorun önce sinyal.
- TP oranı %31; R_tp 1,5 için başa baş ≈ %40. MAE/MFE ≈ 0,86/0,93 R → fiyat stop ile hedef arasında gürültü.
- Tek pozitif işaret PB_long (n=130, iki yarıda da +), istatistik olarak zayıf. BRK_short h2'de anlamlı negatif.

## Adım 1 (şimdi, ucuz): sinyal sönüm eğrisi
Yeni komut `research-drift`: her önerinin TP/SL'den bağımsız ileri getirisi (R) 15 dk … 48 saat.
```powershell
git pull; python -m pytest -q                          # 103 test
jevbot research-drift --proposals data/research/a0-full/proposals.parquet
jevbot research-drift --proposals data/research/a0-h1/proposals.parquet
jevbot research-drift --proposals data/research/a0-h2/proposals.parquet
```
Üç `drift.md`'yi rapora kopyala.

**Önceden kayıtlı karar:**
- (a) Bir aile×yön, **h1 ve h2'nin ikisinde de** bir ufukta gross CI alt sınırı > medyan maliyet R ise →
  sadece o aile için geometri (horizon/bariyer) yeniden tasarlanır.
- (b) Değilse → 5 dk BRK/PB sinyalleri bırakılır. Yeni aileler daha yavaş zaman diliminde (1h/4h sinyal,
  8–48 saat tutma) kurulur; hedef maliyet ≤ 0,07 R (fee stop'un küçük bir kesri olsun).

## Adım 2 (Adım 1 ile paralel, hat boşsa): overfitting'e karşı veri ayrımı
Aynı PIT havuz yöntemiyle geliştirme penceresi **2025-03-01 → 2026-03-30** (12 ay):
`universe-pool --start 2025-03-01 --end 2026-03-31 --top 60`, sonra 1m kline/mark + funding
**2025-01-30'dan** itibaren (30 gün warm-up). Tüm tasarım ve ayar bu eski **geliştirme** penceresinde yapılır. Az önce kullanılan 180 gün, yeni
setup'lar için hiç bakılmamış **tek seferlik holdout** olur. Tahmin: ~20–25 GB, disk yeterli.
6 h recorder koşusu sırasında indirme yapma (ölçümü karıştırmasın); koşudan sonra başlat.

## Soak
Elektrik kesintisi → 24 h kabul yok, doğru. 31.400 eksik kline hücresi büyük ihtimalle kesinti penceresi
(~2,5 h × 200 sembol ≈ 30k): **05:19 UTC öncesiyle sınırlı** eksik hücre sayısını ayrıca ver. 12:00 UTC'deki
yeni profil 6 h koşusu aynen; kabul: `public*` kopma ≤ 3/saat + mevcut kriterler, kopma etiketleri ayrı.

Maliyet modeli şimdilik konservatif kalır; kademeler yeni setup'lar ve 6 h koşunun `book_1s` verisiyle kalibre edilir.
Rapor ≤ 25 satır.
