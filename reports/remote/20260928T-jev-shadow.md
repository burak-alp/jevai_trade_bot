# Remote → Local: Jev shadow açıldı (kullanıcı onayı 2026-09-28) — B ve C kolları

Kural değişikliği: kullanıcı Jev çağrısına **yalnız shadow modda** izin verdi. Emir yok, Binance key yok.
Jev hiçbir kararı, zamanlamayı, boyutu değiştirmez; cevaplar ayrı defterde (`<state>/jev.jsonl`).

## Kod (`src/jevbot/judgment/`, spec §7)
- `state.py` — `state.slow.v1`: anonim (sembol/tarih/fiyat yok), 2 ondalık. `canonical` = yön-hizalı
  (model yönü görmez/seçmez; long/short ayna state birebir aynı — testli). `raw` = yönsüz, direction için.
- `questions.py` — `qs.slow.v1`: `trade_success` (noul, B), `direction_h` (choice up/flat/down, C;
  24 h, düz bant ±0.5·√24 = ±2.45 ATR(1h)). Yargı sözcüğü yok. `PROMPT_HASH` her run satırında.
- `client.py` — TypeSafe `POST /v1/systemone`, `GET /v1/models` (OpenAPI ile doğrulandı). Key yalnız
  `JEV_API_KEY` ortam değişkeninden (Windows'ta kullanıcı registry'si de okunur); hiçbir yere yazılmaz.
  Cevap doğrulama, breaker (5 hata → 60 s), günlük bütçe 2000. Model: tarihli isim varsa o (`versioned`).
- `shadow.py` — paper tick'i diske yazıldıktan **sonra**: her öneri için `trade_success`; öneri sembolleri
  + 00/04/08/12/16/20 UTC'de top-30 hacim sembolü için `direction_h` (panel). Soru/state cevaptan önce
  yazılır. `late` = cevap paper giriş dakikasından sonra geldi (B için kullanılmaz). Direction 24 h sonra
  1h kapanışla yerleşir. Tahmini yük ≈ 210 çağrı/gün.
- CLI: `jevbot jev-check`, `jevbot paper --jev-shadow`, `jevbot jev-report`. 125 test geçti.

## Senden (sırayla)
1. Kullanıcı `JEV_API_KEY`'i kendi terminalinde `setx` ile tanımlar. Sen key'i hiçbir dosyaya/rapora yazma.
2. `git pull; jevbot jev-check` → model listesi, `pinning`, gecikme, status. Rapor et (key yok).
3. Sürekli: `jevbot paper --state-dir run/paper-jev --jev-shadow` (Görev Zamanlayıcı; eski `run/paper`
   ledger'ına dokunma). Soak ile çakışması sorun değil (saatte bir REST).
4. Her gün 1 kez `jevbot jev-report --state-dir run/paper-jev` → 10 satırlık özet: çağrı/ok/hata/late,
   latency p50/p95, token, model_returned, B n, C n. Oranlara göre hiçbir şey değiştirme.

## Ön kayıt — Jev değerlendirmesi (sonuç görülmeden; `qs.slow.v1` + `state.slow.v1` dondu)
- **C (direction, panel):** 3. gün yalnız bilgi. **7. gün karar:** `auc_up_vs_down` gün-blok %95 CI alt
  > 0.5 **ve** sinyal (|P(up) − P(down)| ≥ 0.2) net ortalama (12 bps maliyet sonrası) CI alt > 0 →
  C kolu kendi kilitli paper kapısına (≥ 6 hafta, ≥ 40 işlem) alınır. Aksi → C kapanır.
- **B (trade_success):** n ≥ 100 yerleşmiş, late olmayan cevapta karar: AUC CI alt > 0.5 → B/B+ tasarımı
  (kalibrasyon + eşik, ayrı ön kayıtla). Ekonomik ikincil: p ≥ medyan alt kümesinin net R'si vs tümü.
- Soru/state/eşik değişikliği = yeni versiyon (`qs.slow.v2`), sayaç sıfırdan; eski veri karışmaz.
- Açık iş (sonraya): `identified` varyantıyla contamination testi, test-retest (aynı state iki kez).
