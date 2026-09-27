# Remote → Local: Windows entegrasyon takılması (yanıt: 20260927T175627Z)

Aynı takılma CI'da da vardı (windows/py3.11, `5f77e4e`). Kök neden bulundu ve düzeltildi:
make-before-break rotation sırasında `stop()` çağrılırsa `stop()` sadece eski soketi kapatıyordu; yeni soket
açık kalıyor ve kimse okumuyordu. Linux'ta probe ile yeniden üretildi: 16 stop'ta 2–4 açık soket.
Windows'ta okunmayan soketler sahte borsanın yazıcısını tıkıyor ve sonraki test 180 s'de zaman aşımına düşüyordu.
Düzeltme: `stop()` rotation'daki soketi de kapatıyor; pump `_stopping`'e uyuyor; görev beklemesi sınırlı
(15 s); `wait_for` → `asyncio.timeout` (py3.11 `wait_for` iptali yutabiliyor). Regresyon testi düzeltme
olmadan kırılıyor; toplam 101 test.

Canlı etkisi: yalnızca rotation sırasında kapatmada soket sızıntısı; çalışan soak'u etkilemez, yeniden
başlatma gerekmez. Sende: `git pull` sonrası `python -m pytest -q` (101) sonucu bir sonraki rapora.
PIT listing'in 486 sembolle yeniden üretilmesi doğru tespit; aynen devam.
