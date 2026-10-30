# Agen Analis Nasdaq (v2)

Agen membaca berita dan harga pasar, menilai dampaknya ke Nasdaq, lalu menulis bias harian ke `docs/report.json`. Dashboard `docs/index.html` (GitHub Pages) membaca file itu.

## Model
- 9 faktor berbobot: berita 30, futures NQ 20, futures ES 5, SOX 10, yield 10 tahun 10, VIX 10, Brent 5, DXY 5, tren 5 sesi 5.
- Berita: kamus 24 pola dengan penjelasan "kenapa berdampak", saham terdampak, horizon, negasi, kredibilitas sumber, usia berita, penggabungan berita kembar.
- Peluang naik: awalnya heuristik; setelah 30 hari dinilai di `docs/history.json`, agen otomatis mengkalibrasi (regresi logistik dengan prior, tanpa tanda terbalik) dan menampilkan skor Brier vs tebakan laju dasar. Dibatasi 18-82%.
- Level: pivot klasik, rentang 1 sigma dari VXN, perkiraan pembukaan dari futures.
- Kalender risiko (`EVENTS`, manual) dan rekam jejak (`docs/history.json`, bermakna setelah 30 hari).

## Uji lokal
    python agent.py --demo
    python agent.py
    python agent.py --fixture f.json --no-history --out /tmp/out

## Manual
Ubah `FED_FUNDS`, `FED_NOTE`, dan `EVENTS` di `agent.py` secara berkala. Opsional: secret `ANTHROPIC_API_KEY` untuk ringkasan Claude.

Bukan nasihat investasi.
