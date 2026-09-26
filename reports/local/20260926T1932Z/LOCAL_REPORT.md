# LOCAL_REPORT 2026-09-26T19:32Z

## Sonuç: NO-GO (Sprint 1)

Gerçek Binance smoke testi bir kez geçti; recorder içindeki ikinci smoke `ws:public` timeout ile durdu. Bir saatlik temiz kayıt yapılmadı. Bu rapor ilk yerel karşılıktır, kabul testi değildir.

## Ortam

Windows native, Python 3.11.9, 16 logical CPU, 32 GiB RAM, D: yaklaşık 1.37 TB boş. WSL kurulu değil. `fapi.binance.com/fapi/v1/ping` ve `data.binance.vision` HTTP 200. Public data only; key, emir, Jev çağrısı yok. Git HEAD `7ad424a`.

## Acceptance / ölçümler

- İlk `jevbot smoke`: OK; REST 907 sembol, request weight limiti 2400/dk, clock offset +1003.5 ms; `ws:market` ve `ws:public` veri, SUBSCRIBE ACK ve ping gördü.
- `jevbot record --duration 75` içindeki smoke: FAIL. REST OK, clock offset +889.5 ms; market OK (kline 2, markPrice 2); public `TimeoutError()` (`run/smoke_report.json`). Recorder kayıt başlamadan durdu.
- Universe ön hesap: 100 üye (`min_quote_vol_24h=20M`, max 200); depth 22. 200 hedefe ulaşmadı. Uzun süreli throughput, latency, gap, OI ve disk ölçümleri YOK.
- Windows Time: `w32tm /query /status` = `Leap Indicator 3 (not synchronized)`, source `Local CMOS Clock`. Gecikme kabul ölçümü güvenilir değil. Sistemin saat ayarı değiştirilmedi.
- `tests/integration/test_recorder_fake.py::test_smoke_passes`: FAIL. `websockets==17.1` ile fake WS timeout; `13.1` ile kök hata açık: `Assembler() argument after * must be an iterable, not NoneType`.
- `pytest tests/unit -x -q`: 5 PASS, ardından `test_compaction_merges_sorts_and_tombstones` FAIL; writer `OSError [Errno 9] Bad file descriptor`. Tam suite tamamlanmadı.
- Historical downloader çalıştırılmadı: recorder gate ve saat durumu çözülmeden kapsamı genişletmedim.

## Eleştiri / istekler

1. [P1] `src/jevbot/recorder/integrity.py:56-60` — Windows'ta `os.open(..., O_RDONLY)` + `os.fsync(fd)` EBADF; sink finalize başarısız, `.parquet.tmp` kalıyor. Yazılabilir handle ile Windows testini düzelt; başarısız writer'ın ana sürece açıkça hata döndürmesini de kontrol et.
2. [P1] `src/jevbot/testing/fake_binance.py:136` — `serve(..., max_queue=None)` bu kurulumda WebSocket sunucusunu bozuyor; `websockets==13.1` traceback yukarıdaki `Assembler` hatası. Desteklenen açık queue değeri kullanıp Windows testini yeniden çalıştır.
3. [P2] `src/jevbot/recorder/smoke.py` — gerçek public route ilk smoke'ta geçti, ikinci smoke'ta `TimeoutError()` verdi; detay sözlüğü olayları ve timeout aşamasını kaydetmiyor. `recv`/`ping` aşamasını ve görülen event sayılarını hata halinde de raporla; sonra tekrar ölçelim.
4. [P2] `scripts/local_validation.sh` yalnızca Linux. Windows native desteklenecekse PowerShell eşdeğeri veya belgelenmiş manuel komutlar gerekli.

## Sorular (remote'a)

- Windows native resmi test hedefi mi? Değilse bu makinede WSL yok; Linux VPS'e geçiş için kullanıcı kararı gerekir.
- İlk düzeltme sonrası smoke + kısa record + verify'ı yeniden deneyeyim; saat senkronu sağlanınca 1 saat gate ve downloader'a geçelim mi?
