# Agen Analis Nasdaq

Agen membaca berita pasar dan harga, menilai dampaknya ke Nasdaq, lalu menulis bias harian (bullish/bearish) ke `docs/report.json`. Dashboard `docs/index.html` membaca file itu dan tampil di GitHub Pages.

## Isi folder
- `agent.py`: agen (hanya pustaka standar Python).
- `docs/index.html`: dashboard live. Tanpa `report.json` ia menampilkan potret 27 Okt 2026.
- `.github/workflows/nasdaq-agent.yml`: menjalankan agen tiap 30 menit pada jam bursa AS.

## Uji lokal
```
python agent.py --demo     # tanpa internet, headline contoh
python agent.py            # berita dan harga live
python agent.py --loop 30  # ulangi tiap 30 menit
```
Lihat hasilnya: `cd docs && python -m http.server 8000`, lalu buka http://localhost:8000.

## Yang otomatis dan yang manual
- Otomatis: headline dan skor, bias, keyakinan, harga Nasdaq/yield/Brent, level support dan resistensi.
- Manual: suku bunga Fed. Ubah `FED_FUNDS` dan `FED_NOTE` (atau set sebagai variabel lingkungan) setelah tiap rapat Fed.
- Opsional: isi secret `ANTHROPIC_API_KEY` agar Claude menulis ringkasan alasan di dashboard.

Bukan nasihat investasi.
