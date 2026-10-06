# STATE — tek sayfa proje durumu (önce bunu oku; ≤ 60 satır tutulur, her karar sonrası güncellenir)

Güncelleme: 2026-10-06 11:40 (Claude, yerel). Ayrıntı gerekirse: `python scripts/rag.py query "<konu>" --max-chars 1800`.

## Hedef
Kripto (Binance USDⓈ-M) için maliyet sonrası kârlı bot; kaldıraçlı/kaldıraçsız serbest. Mimari: deterministik
öneri → Jev meta-label/veto → deterministik risk. Jev boyut/kaldıraç/stop belirlemez. Gerçek para ancak kanıtla.
Kullanıcı: aylardır sonuç yok, 1-2 günde net karar istiyor. TradFi (hisse) ileride olası; şimdi kapsam dışı.

## GEÇEN (ilk)
- **macro.trend.v1** varlıklar arası trend (SPY QQQ GLD SLV USO TLT BTC ETH + nakit, ters oynaklık, haftalık, kaldıraçsız):
  D 2006-15 seçimi 252 g; **T 2016-01→2026-09: +%12.8/yıl [6.6, 20.0], Sharpe 1.09, maks DD %16** (SPY %14.9 / 0.75 / %34;
  60/40 %8.9 / 0.61 / %27) → GO. Sağlamlık: +1 g gecikme %13.2, 3× maliyet %11.9, kriptosuz %8.8 / 0.78 / %12.6.
  Eşit ağırlık al-tut T'de %26 (kripto) ama DD %40. Varlık listesi bugünden seçildi (seçim yanlılığı riski, kripto payı ~%5).
- macro.trend.v2 (geniş Binance evreni / short / hedef oynaklık): D seçimi TEMEL yalnız long; T Sharpe 1.03 < 1.09 → GEÇMEDİ,
  v1 temel kalır. Geniş evren ve short T'de kötüleştirdi. Kaldıraç tablosu (T, tavan 3x, finansman farkı TradFi %1 kripto %5):
  hedef %10: %14.1/yıl DD %20 | %15: %18.7 DD %29 | %20: %22.0 DD %35 en kötü ay −%16 | %30: %26.4 DD %42 en kötü ay −%23.

## Çalışan (06.10 15:00'ten itibaren)
- **macro.paper.v1** (`jevbot macro-paper --telegram`, durum `run/macro-paper`): v1 ağırlıkları backtest ile aynı kodla
  (`jevbot.research.macro`; eşdeğerlik testi), Binance perp fiyatları + gerçek funding, haftalık rebalans, hesaplar 1x ve 2x,
  boştaki nakit T-bill faizi. Günlük çalıştırma: kullanıcının kuracağı Windows görevi (01:30 TR). Gerçek emir yok.

## Kapanan hipotezler (ön kayıtlı, hepsi GEÇMEDİ — eşik gevşetme yok)
| hipotez | pencere | sonuç |
|---|---|---|
| 5 dk BRK/PB | 2026-03 → 09 | gross −0.03 R, net −0.19 R (maliyet) |
| slow.v1 TSM/XSM/FUND | dev 2025-03 → 2026-03 | 6/6 geçmedi, yarılarda işaret dönüyor |
| pos.v1 CROWD/FLUSH (OI) | dev | 4/4 geçmedi |
| reg.v1 BTC 1h trend kapısı | holdout 2024-01 → 2025-03 (yakıldı) | geçmedi |
| A1 lojistik meta-labeler | walk-forward 2024-07 → 2026-03 | AUC 0.525, getiri rastgele yarıyla aynı |
| rejim tavanı (betimleyici) | 2024 → 2026 | kahin rejim +0.25/+0.34 R; bilinen rejim −0.03/−0.07 R |
| carry.v1 seçici alt funding carry | 2024-01 → 2026-09 | +%1.0/yıl, h2 −%3.5 → geçmedi (maliyet funding'in %60'ı) |
| carry.base BTC+ETH (referans) | aynı | +%4.9/yıl [4.0, 5.9], DD %0.4; h2 %2.7 — nakit "earn" faizi mertebesi |
| hlp.v1 Hyperliquid kalıcılık | P1 Mar–May → P2 Haz–Ağu 2026 | yansız örneklemde Spearman +0.005; P1 kazananları P2 medyan −%95 → kopya yolu yok |
| gap.v1 TradFi hisse perp gece/hafta sonu açığı | 2026-01 → 09, 36 hisse | eğim ≈ 0; geri dönüş −13 bps net → geçmedi (verimli) |
| Jev C direction_h 24 h (canlı shadow) | 29.09 → 06.10, n 907, 2 blok | futility: AUC 0.342 [0.296, 0.348], sinyal net −92 bps [−119, −33], isabet %38 → kapandı |
| regime.map.v1 rejim→strateji (trend/kırılım, 1d+4h, top-20 PIT) | P1 2021-10→2024-03, P2 2024-04→2026-09 | P2 rejim-geçiş +%1.6/yıl [−53, +145], tek-en-iyi −%23; fark +8.7 bps/g [−11, +28] → geçmedi; kahin geçiş de +%1.8 (P1 eşlemesi P2'ye taşınmıyor) |
Bilgi (test değil): slow.v1 FUND 2024'te +0.15 R, 2025-26'da ~0 — tutarsız, kiraz toplanmaz.

## Çalışanlar
**06.10 11:45 kullanıcı kararıyla HER ŞEY KAPATILDI:** paper durdu (son tick 11:00 TR), Windows görevi "JevAI Paper"
ve Claude rutini `jevai-llm-claude-arm` devre dışı (silinmedi). Veri `run/paper-jev` içinde duruyor. Aşağısı arşiv.
- Jev shadow paper: kullanıcının ayrı PowerShell penceresi; `jevbot paper --state-dir run/paper-jev --jev-shadow
  --jev-base-url https://jev-ai.pro/api`; model `jev-1.13.0`; config `314c11b6a1e1727b`; state `state.slow.v2`.
  Uygulama terminalinde çalıştırma (uygulama kapanınca ölür). `jev-ai.pro` çağrılarını yalnız kullanıcı başlatır.
- Jev soruları: B `trade_success` (her öneri), C `direction_h` 24 h (top-30, 4 saatte bir), `btc_regime_7d` (günde 1).
- **llm.v1 (ABD hisse perp, Jev vs Claude):** paper sürecinin hook'u, hafta içi 14:00 UTC bağlam + Jev; Claude kolu zamanlanmış
  görev `jevai-llm-claude-arm` (17:08 TR) → `jevbot llm-record`. Referanslar: always_long, random. Rapor: `jevbot llm-report`.
- **Sanal hesap (`jevbot jev-account`):** 1000$ tek birikimli hesap, Jev kripto C + Jev hisse; Telegram saatlik pozisyon
  detayı + 21:00 tam rapor. `jev_dyn` (ön kayıt 02.10, yalnız rapor; C testi değişmez): aynı pozisyonlar, aynı coin için
  sonraki 4 saatlik panel ters sinyal verirse (|skor|≥0.2) o panelin fiyatından erken kapanır. 27.10'da jev ile kıyaslanır.

## Karar takvimi (Türkiye saati; ön kayıtlı)
- C: **06.10 futility tetiklendi → kapandı** (AUC ≤ 0.5 ve net ≤ 0, ikisi de). AUC < 0.5 tersine çevirmeye gerekçe değil
  (post-hoc; 30 coin aynı saatte korelasyonlu, 2 blok). B ara bilgi: n 45, TP %2, net −0.22 R (karar n ≥ 100).
- B: n ≥ 100 yerleşmiş ve ≥ 4 blok; AUC CI alt > 0.5 ve A1'i (0.525) geçmeli.
- btc_regime_7d: çakışmasız haftalık örneklemde isabet > taban oran; anlamlılık aylar sürer (yılda ~52 örnek).

## Recorder
6 h soak NO-GO (bookTicker ~140 kopma/saat, 13:30 ABD açılışı). `config/home.yaml`: bookTicker kapalı, depth top-30.
Yeni profil 6 h soak (2026-09-29) 9/9 GEÇTİ: 4 kopma, 0 eksik kline, CPU %7 → recorder GO (yalnız araştırma verisi).

## Kurallar
Gerçek emir/Binance key yok. `data/ run/ logs/` commit edilmez. Sonuç uydurma yok. Kararlar önce kayıt, sonra koşu.
Araştırma alt-ajanı açılırsa model: `sonnet`. Kullanıcıya saatler Türkiye saatiyle.
