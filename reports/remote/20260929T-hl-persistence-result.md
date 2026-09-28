# Remote → Local: hlp.v1 sonucu (ön kayıt 20260929T-hl-persistence-prereg.md; kod f4aeb35, sonuçtan önce)

3,477 portföy çekildi; iki dönemde de ROI hesaplanabilen: rastgele 682, görünür kazananlar 174.

| grup | Spearman(P1, P2) [99 %] | P1 üst ondalık → P2 ROI ort. [99 %] | üst ondalık P2 medyan | P2 kârlı payı (üst / tümü) |
|---|---|---|---|---|
| **rastgele (birincil)** | **+0.005 [−0.116, +0.122]** | +0.16 [−0.33, +0.71] | **−0.95** | 0.41 / 0.42 |
| görünür kazananlar (allTime ilk 500) | +0.235 [+0.015, +0.423] | +1.74 [+0.65, +2.98] | +1.08 | 0.83 / 0.53 |

Ciro tercilleri (rastgele): Spearman −0.11 / +0.02 / +0.01 — hiçbirinde kalıcılık yok.

## Karar
- **Birincil (yansız örneklem): beceri kalıcılığı YOK.** Mart–Mayıs'ın en iyi %10'u Haziran–Ağustos'ta diğerlerinden
  ayırt edilemiyor (kârlı payı 0.41 vs 0.42); medyanı −%95 — çoğu kazandığından fazlasını geri verdi / battı.
- "Görünür kazananlar" grubu güçlü kalıcılık gösteriyor ama **geçersiz**: bu grup *bugünkü* allTime PnL'e göre seçildi,
  yani P2'de kazananlar zaten seçime dahil (sonuca göre seçim). Liderlik tablosuna bakan birinin gördüğü yanılsama tam bu.
- Kopya/takip yolu kapanır: kazananı önceden (P1 verisiyle) seçmek P2'de avantaj vermiyor.
- Sınırlama: tek dönem çifti, örneklemde büyük ölçüde küçük hesaplar; çok büyük hesaplar için yansız test ~46 k portföy
  (~13 saat) ister — şimdilik yapılmadı.
