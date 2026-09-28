# LOCAL_AGENT.md — Yerel ajan (GPT-6) görev dosyası

## Roller
- **Remote (Claude, git tarafı):** Kodun sahibi. Mimari, kod ve commit'ler bende. Branch: `claude/sleepy-goldberg-ypz1k2`.
- **Local (sen, GPT-6, yerel PC):** Gerçek ortamın sahibisin. Binance'e erişen makinede ölçüm yaparsın,
  sonuçları raporlarsın ve kodu eleştirirsin. **Ölçüm/ortam gerçeklerinde son söz sende, kodda son söz bende.**

## Kesin kurallar
- Gerçek emir yok. API key yok. Jev/LLM çağrısı yok. Sadece public market data.
- `data/`, `run/`, `logs/` commit edilmez (gitignore). Sadece `reports/local/**` commit edilir.
- `src/` altını doğrudan değiştirme. Düzeltme önerisini rapora yaz. Zorunlu hotfix gerekiyorsa
  `local/<konu>` adlı ayrı branch'e koy ve raporda linkini ver.
- Token tasarrufu: log yapıştırma; sayıları özetle, dosyaya referans ver.
- Geniş doküman/rapor okumadan önce `python scripts/rag.py query "aranan konu" --max-chars 1800`;
  dönen parçaları kaynak dosyada doğrula. İndeks yereldir, LLM çağrısı yapmaz.
- Sonuç uydurma. Bir adım çalışmadıysa "çalışmadı + hata" yaz.

## Ortam
Linux veya WSL2 önerilir (Windows native desteği sınırlı, test edilmedi). Python ≥ 3.11, saat senkronize
(chrony/timedatectl), Binance Futures'a yasal erişim.

```bash
git clone https://github.com/burak-alp/jevai_trade_bot.git && cd jevai_trade_bot
git checkout claude/sleepy-goldberg-ypz1k2 && git pull
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,fast]"
```

## Görev 1 — Doğrulama paketi (≈ 70 dk, tek komut)
```bash
scripts/local_validation.sh 3600          # smoke -> 1 saat record -> verify -> report -> hist indirme
```
Smoke başarısızsa script durur (exit 3); o durumda direkt Görev 3'e geç.
Çıktılar `reports/local/<ts>/` altında: `recording_report.md/json`, `smoke.json`, `verify.json`,
`warnings.jsonl`, `hist.txt`, `hist_quality.txt`, `env.txt`, `pytest.txt`.

## Görev 2 — Eleştiri (kanıtla)
Aşağıdakileri gerçek veriye bakarak kontrol et. Yanlış olabileceğini düşündüğüm yerler:
1. WS route eşlemesi (`config/base.yaml` → `binance.ws.stream_routes`): her stream gerçekten veri getiriyor mu?
2. `bookTicker` gerçek mesaj hızı (msgs/s p95/max) ve recorder CPU; tek process yeterli mi?
3. Feed latency (`t_recv − t_event`): gerçek değerler, clock offset etkisi.
4. OI polling: gerçek weight tüketimi ve `exchangeInfo.rateLimits`; 200 sembol/dk güvenli mi?
5. Universe: `min_quote_vol_24h=20M` ile kaç sembol çıkıyor, 200'e ulaşıyor mu?
6. Kline gap/backfill: eksik dakika var mı, `kline_grace_s=5` gerçek gecikmeye uygun mu?
7. Disk MB/saat (dataset bazında); `jevbot compact` sonrası boyut ne kadar düşüyor? (`jevbot compact` çalıştır, öncesi/sonrası `du -sh data/raw/*`)
8. Downloader: `metrics` ve `bookDepth` CSV kolon formatları kodla uyuşuyor mu? `quality.json` içinde
   haksız `warn/suspect` var mı (eşikler yanlış olabilir)?
9. `forceOrder`, `markPrice@arr` payload şemaları: schema error var mı?
10. Senin gördüğün başka her şey — "şöyle yapsak daha iyi" dahil.

## Görev 3 — Rapor
`reports/local/<ts>/LOCAL_REPORT.md` dosyasını **en fazla ~80 satır** olacak şekilde şu formatla yaz:

```markdown
# LOCAL_REPORT <ts>
## Sonuç: GO | NO-GO (Sprint 1 için)
## Ortam: OS, CPU, RAM, ağ/ülke, clock offset
## Acceptance (recording_report'tan): her madde ✅/❌ + değer
## Ana ölçümler: universe, bağlantı, msgs/s p50/p95/max, CPU/RAM, latency p50/p99, reconnect, gap, weight, disk MB/h
## Downloader: dataset başına ok/warn/suspect + sebep
## Eleştiri (öncelik sırasıyla, en fazla 10)
- [P1|P2|P3] dosya:satır veya config anahtarı — sorun — kanıt (sayı/dosya) — önerilen düzeltme
## Sorular (remote'a)
```

Sonra:
```bash
git add reports/local && git commit -m "local: validation report <ts>" && git push origin claude/sleepy-goldberg-ypz1k2
```
Kullanıcıya tek satır yaz: "Rapor hazır: reports/local/<ts>/LOCAL_REPORT.md".

## Anlaşma protokolü
- Remote raporu okur. Her eleştiriye **kabul (commit linki) / ret (1-2 cümle gerekçe)** ile
  `reports/remote/<ts>-response.md` dosyasında cevap verir.
- Anlaşmazlıkta en fazla bir tur daha. Sonra karar ölçüme dayanır: yeni ölçüm yapan haklıdır.
- GO'dan sonraki adım 24 saatlik soak: `docs/03_live_validation_runbook.md` §4. Rapor formatı aynı.
