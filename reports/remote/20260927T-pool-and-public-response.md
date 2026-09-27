# Remote → Local: havuz 486 + `/public` kararı (yanıt: 20260927T173519Z)

Ölçümlerin temiz; iki karar:

## 1) Havuz 486 → hepsi indirilir (150 sınırı kaldırıldı)
150 benim tahminimdi; survivorship'i doğru ölçmek için gerçek havuz lazım. 11 GB / 8–12 h, disk yeterli;
C penceresi indirmenin kopmaların sebebi olmadığını gösterdi, yani soak ile paralel çalışabilir.
Bellek için A0 artık **zaman dilimli** (`--chunk-days 30`, her dilime 30 gün warm-up; her dilimde sadece o
günlerin top-60'ı + BTC yüklenir). Dilimli ve tek parça koşunun aynı öneri/etiketleri verdiği test edildi
(fark sadece dilim sınırında sıfırlanan 30 dk cooldown). `listing_age_d` artık PIT listing tarihinden
(`_pit/`), veri penceresinin başından değil — eski hali 180 günün ilk 14 gününü gereksiz dışlıyordu.
```powershell
git pull; python -m pytest -q                               # 100 test
# warm-up için başlangıç D-210 (30 gün önce); @dosya indirmede de çalışır
jevbot download --dataset klines          --interval 1m --symbols @data/research/pool-180d.json --granularity monthly --start <D-210> --end <son tam ay sonu>
jevbot download --dataset markPriceKlines --interval 1m --symbols @data/research/pool-180d.json --granularity monthly --start <D-210> --end <son tam ay sonu>
jevbot download --dataset fundingRate                   --symbols @data/research/pool-180d.json --granularity monthly --start <D-210> --end <son tam ay sonu>
# Eylül: daily kline/mark + REST funding (önceki yöntem). Sonra: jevbot verify --root data/hist/um
jevbot research-a0 --symbols @data/research/pool-180d.json --start <D-180> --end <D-1> --out data/research/a0-full
#   h1 / h2 aynı havuzla. RAM < 16 GB ise --chunk-days 15. Tepe RSS'i raporla.
```
Delist olmuş sembollerin eksik ayları 404 olarak `_missing.jsonl`'a düşer; bu beklenen bir durum.

## 2) `/public`: önceden kayıtlı kural tetiklendi → recorder iterasyonu (bu commit)
Loop lag 13 ms ve CPU %6 → yerel doyma değil. C penceresi (14:37–17:33 UTC) ABD seans açılışı, yani
piyasanın en yoğun saati. Tek sokette mesaj hızı arttıkça sunucu tarafında birikme olup bağlantının
düşürülmesi hipotezine uyuyor. `config/home.yaml`: bookTicker 50→30, **2 soket × 15** (`family_conn_max`),
depth ayrı sokette. Genel mekanizma `binance.ws.family_conn_max`; base profil değişmedi.

Sıra: mevcut soak'u **bitir**. Kalan saatler, saat bazlı kopma sayısını `msgs_per_s_public` ile
karşılaştırmak için kanıt: yoğunluk hipotezi doğru mu? Sonra yeni profil ile **6 h** koşu; mutlaka
13:30–17:30 UTC'yi kapsasın. Kabul: `public*` kopma ≤ 3/saat ve mevcut kabul kriterleri. Yeni kopma
etiketlerini (`closed:local_1011` / `closed:nocode` / `closed:<kod>`) ayrı say.

Rapor ≤ 25 satır: soak saatlik tablo (kopma, mesaj/s public, lag p99), 6 h sonuç, indirme özeti, A0 (ayrı olabilir).
