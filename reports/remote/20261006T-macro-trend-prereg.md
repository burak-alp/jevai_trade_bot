# macro.trend.v1 — varlıklar arası trend (ön kayıt, sonuç görülmeden, 2026-10-06)

Kullanıcı hedefi: dünyaya göre dinamik varlık seçimi (kripto / ABD hisse / altın / gümüş / petrol / tahvil / nakit).
Katman 1 = kurallı temel. Sonraki katmanlar (takip, LLM) bu temele karşı ölçülecek.

## Veri
Yahoo Finance günlük düzeltilmiş kapanış (temettü dahil). Varlıklar: SPY, QQQ, GLD, SLV, USO, TLT, BTC-USD, ETH-USD.
Nakit getirisi ^IRX (3 aylık T-bill). Her varlık verisi başladığı günden itibaren evrene girer.
Pencere 2006-05-01 → 2026-09-30. Geliştirme D = 2006-05 → 2015-12, test T = 2016-01 → 2026-09.

## Strateji
- Sinyal (varlık başına, haftalık, cuma kapanışı): geçmiş getiri > nakit getirisi ise LONG, değilse o pay NAKİT.
  Izgara (yalnız D'de seçilir, T'ye dokunulmaz): geriye bakış ∈ {63, 126, 252 gün, 21/63/252 ortalaması}.
- Boyut: ters oynaklık (60 g), toplam brüt ≤ 1x (kaldıraç yok), kalan nakit. Short yok.
- Rebalans haftalık; sinyal cuma kapanışı, getiri sonraki işlem gününden. Maliyet: ciro başına 10 bps (taraf).
- Kripto 7/24 işlem görür: tüm seriler NYSE işlem günlerine hizalanır (kripto hafta sonu getirisi pazartesiye biner).

## Karşılaştırma
SPY al-tut, 60/40 (SPY/TLT aylık rebalans), tüm varlıklar eşit ağırlık al-tut (aylık rebalans). Hepsi aynı maliyetle.

## GO (T döneminde, üçü birden)
1. Yıllık getiri 20 günlük blok bootstrap %95 CI alt sınırı > 0.
2. Sharpe > 60/40 Sharpe ve > SPY Sharpe.
3. Maks. düşüş < SPY maks. düşüş.
Eşik gevşetme yok; ızgara genişletilmez.
