#!/usr/bin/env python3
"""
Agen Analis Nasdaq v2
---------------------
Menggabungkan berita dan data pasar menjadi satu bias harian (bullish / bearish)
beserta alasan, probabilitas, level harga, kalender risiko, dan rekam jejak.

Cara kerja:
  1. Berita (Google News RSS) dinilai dengan kamus pola. Setiap pola punya penjelasan
     mekanisme kenapa berita itu berdampak. Berita kembar digabung, bobotnya mengikuti
     kredibilitas sumber dan kesegaran.
  2. Data pasar (Yahoo Finance): futures Nasdaq-100 dan S&P 500, SOX, VIX, VXN, yield 10 tahun,
     Brent, indeks dolar, tren Nasdaq. Tiap faktor diberi sinyal -2 sampai +2.
  3. Sinyal digabung berbobot menjadi skor -10 sampai +10 lalu diubah menjadi probabilitas
     indeks naik. Probabilitas ini heuristik, BELUM dikalibrasi. Kalibrasi datang dari
     rekam jejak: tiap prediksi pra-buka disimpan lalu dibandingkan dengan hasil sesi.
  4. Hasil ditulis ke docs/report.json, docs/report.md, dan docs/history.json.

Pakai:
    python agent.py                    # berita dan harga live
    python agent.py --demo             # tanpa internet, headline contoh
    python agent.py --fixture f.json   # data tetap (untuk uji)
    python agent.py --loop 30          # ulangi tiap 30 menit

Hanya memakai pustaka standar Python. Paket `anthropic` opsional.
Bukan nasihat investasi.
"""
import argparse
import datetime as dt
import difflib
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = 2
UTC = dt.timezone.utc
WIB = dt.timezone(dt.timedelta(hours=7))
MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

# Edit dua baris ini setelah tiap rapat Fed (angka suku bunga tidak bisa diambil otomatis).
FED_FUNDS = os.environ.get("FED_FUNDS", "3,75–4,00%")
FED_NOTE = os.environ.get("FED_NOTE", "Rapat berikut 9 Des")

QUERIES = [
    "Nasdaq stocks today",
    "Federal Reserve rate Warsh",
    "Treasury yields 10-year",
    "Nvidia semiconductor stocks",
    "AI spending capex data center financing",
    "oil prices Hormuz Iran",
    "Big Tech earnings guidance",
    "Anthropic OpenAI IPO",
    "chip export controls tariffs",
    "US CPI inflation jobs report payrolls",
    "China Taiwan semiconductor trade tariffs",
    "Microsoft Alphabet Amazon Meta Apple earnings",
    "CoreWeave Oracle debt data center bonds",
    "SpaceX Tesla stock",
]

# Kalender risiko. PERBARUI tiap bulan. Waktu dalam ET (None = jam belum diketahui).
# confirmed=False berarti tanggal berasal dari sumber pihak ketiga dan belum dikonfirmasi perusahaan.
EVENTS = [
    ("2026-10-30", None, "Employment Cost Index kuartal III", "sedang", True,
     "Ukuran tekanan upah. Angka di atas perkiraan (+0,8%) menguatkan alasan Fed menaikkan suku bunga."),
    ("2026-11-02", "16:00", "Laporan laba Apple (setelah penutupan)", "tinggi", False,
     "Apple adalah salah satu bobot terbesar indeks. Panduan dan permintaan iPhone menggerakkan seluruh Nasdaq."),
    ("2026-11-03", "10:00", "JOLTS lowongan kerja (September)", "sedang", True,
     "Tanda panasnya pasar tenaga kerja. Angka kuat menambah peluang kenaikan suku bunga."),
    ("2026-11-06", "08:30", "Laporan tenaga kerja Oktober (nonfarm payrolls)", "tinggi", True,
     "Data tunggal paling berpengaruh bagi arah suku bunga. Terlalu panas menaikkan yield, terlalu lemah memicu kekhawatiran resesi."),
    ("2026-11-10", "08:30", "CPI Oktober", "tinggi", True,
     "Inflasi menentukan apakah Fed menaikkan suku bunga 9 Desember. Angka panas menekan saham teknologi lewat yield."),
    ("2026-11-13", "08:30", "PPI Oktober", "sedang", True,
     "Harga di tingkat produsen. Memberi petunjuk awal arah inflasi konsumen berikutnya."),
    ("2026-11-17", "08:30", "Penjualan ritel Oktober", "sedang", True,
     "Mengukur kekuatan konsumen. Data kuat memperkuat sikap Fed yang ketat."),
    ("2026-11-18", "14:00", "Risalah rapat FOMC (rapat 27-28 Okt)", "sedang", True,
     "Menunjukkan seberapa dekat Fed pada kenaikan Desember dan alasan dua anggota yang menginginkan kenaikan sekarang."),
    ("2026-11-18", "16:00", "Laporan laba Nvidia (setelah penutupan, perkiraan)", "tinggi", False,
     "Barometer permintaan AI dan penggerak terbesar semikonduktor. Hasilnya bisa mengubah arah seluruh tema AI."),
    ("2026-11-25", "08:30", "PCE Oktober dan PDB kuartal III (estimasi kedua)", "tinggi", True,
     "PCE adalah ukuran inflasi pilihan Fed. Hasilnya menjadi masukan terakhir sebelum rapat Desember."),
    ("2026-12-09", "14:00", "Keputusan suku bunga FOMC", "tinggi", True,
     "Pasar sudah menghargai sekitar 85% peluang kenaikan 25 bp. Kejutan ke arah mana pun menggerakkan saham teknologi."),
]

# ---------------------------------------------------------------------------
# Kamus berita: pola, skor (positif = bullish untuk Nasdaq), mekanisme dampak.
# ---------------------------------------------------------------------------
LEXICON = [
    dict(label="Risiko kenaikan suku bunga", score=-2,
         pat=r"\brate hikes?\b|hawkish|hotter[- ]than[- ]expected|inflation (rises|rose|accelerat|surges?|jumps?|pressures?|stays? (high|elevated))|vote[sd]? for a hike|dissent.{0,30}hike",
         why="Kenaikan suku bunga menaikkan biaya modal dan tingkat diskonto. Saham growth seperti teknologi, yang labanya jauh di masa depan, kehilangan nilai paling besar, jadi Nasdaq lebih sensitif daripada Dow.",
         who=["QQQ", "NVDA", "MSFT", "TSLA"], horizon="Beberapa hari sampai rapat Fed berikutnya"),
    dict(label="Pelonggaran ditunda", score=-1, pat=r"\bno_cut\b",
         why="Harapan pelonggaran memudar, sehingga biaya modal tetap tinggi lebih lama dan valuasi growth sulit naik.",
         who=["QQQ", "IGV"], horizon="Beberapa pekan"),
    dict(label="Imbal hasil naik", score=-2,
         pat=r"yields? (jump|surge|climb|rise|rose|spike|top|hit|reach)|bond (selloff|sell-off|rout)|highest since",
         why="Yield obligasi yang tinggi membuat obligasi bersaing dengan saham dan menurunkan nilai kini laba teknologi. Di sekitar 5% tekanan pada valuasi tinggi meningkat.",
         who=["QQQ", "IGV", "ARKK"], horizon="Hari ini sampai beberapa pekan"),
    dict(label="Imbal hasil turun", score=2,
         pat=r"(?<!safety )(?<!haven )yields? (fall|fell|drop|dropped|slip|slipped|ease|eased|plunge|plunged|decline|declined|retreat)(?!.*(flight to safety|safe[- ]haven))|lower yields|bond rally",
         why="Yield turun mengurangi tekanan diskonto pada saham growth. Jika turunnya karena lari ke aset aman saat saham jatuh, efek positifnya hanya sebagian.",
         who=["QQQ", "IGV"], horizon="Hari ini sampai beberapa pekan"),
    dict(label="Harapan pelonggaran", score=2,
         pat=r"rate cuts?\b|dovish|cooler[- ]than[- ]expected|inflation (cools|cooled|eases|eased|slows|slowed)|disinflation",
         why="Peluang pelonggaran menurunkan biaya modal dan menopang valuasi saham growth.",
         who=["QQQ", "ARKK"], horizon="Beberapa pekan"),
    dict(label="Data kerja lemah menurunkan peluang hike", score=1,
         pat=r"unemployment (rises|rose|climbs|climbed|jumps|jumped|up)|jobless claims (rise|rose|jump|jumped)|weak (jobs|payrolls?|hiring|labou?r|employment)|(jobs|payrolls?|hiring) (miss|disappoint|slow|cool|weaken)|(payrolls?|jobs report).{0,40}below",
         why="Data kerja yang lebih lemah mengurangi peluang kenaikan suku bunga sehingga baik bagi valuasi. Efeknya terbatas jika terlalu lemah dan memicu kekhawatiran resesi.",
         who=["QQQ"], horizon="Beberapa hari"),
    dict(label="Data ekonomi kuat", score=0,
         pat=r"(strong|solid|robust|hot|hotter)[- ](jobs|payrolls?|hiring|labou?r|employment|retail sales|gdp|economy|data)|(jobs|payrolls?|retail sales|gdp) (beat|beats|top|tops|surge|surged|rise more|stronger)|payrolls? (above|exceed)",
         why="Data ekonomi kuat berarti ekonomi sehat tetapi mengurangi alasan Fed melonggarkan suku bunga. Arah dampaknya bergantung rezim pasar: saat pasar takut suku bunga, kabar kuat justru menekan saham.",
         who=["QQQ"], horizon="Beberapa hari"),
    dict(label="Tekanan pendanaan AI", score=-2,
         pat=r"\bcapex\b|capital (crunch|constraint)|(biggest|major|main) constraint|cash outflow|negative free cash flow|free cash flow.{0,25}negative|cash burn|burning cash|raise capital|equity (raise|offering|issuance)|non-voting shares?|share issuance|debt[- ]laden|heavily indebted|(bond|credit) (yields?|spreads?).{0,30}(record|widen)|financing (concerns?|worries|risk)|ai (spending|investment|capex|cost|costs) (worries|concerns|fears|doubts|anxiety)|bigger ai (spending|capex)|higher capex",
         why="Belanja AI yang besar dengan arus kas tipis memaksa perusahaan berutang atau menerbitkan saham. Investor mulai mempertanyakan imbal hasil investasi, sehingga seluruh rantai AI (chip, pusat data, listrik) ikut tertekan.",
         who=["NVDA", "AVGO", "MU", "ORCL", "CRWV"], horizon="Berhari-hari; memengaruhi tema, bukan satu saham"),
    dict(label="Earnings atau panduan mengecewakan", score=-2,
         pat=r"guidance (cut|cuts|lower|disappoint|below|weak)|(miss|misses|missed) (earnings|estimates|expectations|forecasts?)|earnings (miss|disappoint)|(cuts?|lowers?) (its )?(annual |full-year )?(forecast|outlook|guidance)|disappointing|(profit|earnings|revenue) (warning|shortfall)|(results|earnings|profit) (fall|fell) short|below (wall street )?(estimates|expectations)|weak(er)? (outlook|forecast|guidance)|profit misses|misses? (revenue|sales|profit|earnings)? ?(estimates|expectations)|misses? (on )?(revenue|sales|profit|earnings)",
         why="Panduan lemah memotong estimasi laba ke depan. Mega-cap berbobot besar di indeks, jadi satu saham yang jatuh dalam bisa menyeret Nasdaq dan sentimen sektornya.",
         who=["QQQ"], horizon="Hari reaksi dan 1 sampai 3 hari berikutnya"),
    dict(label="Earnings atau panduan kuat", score=2,
         pat=r"\bbeats?\b|beat (estimates|expectations|forecasts?)|(raises?|lifts?|boosts?) (its )?(annual |full-year )?(forecast|outlook|guidance)|record (revenue|profit|earnings|sales)|upbeat|strong (earnings|results|guidance|demand)|tops (estimates|expectations)|better[- ]than[- ]expected",
         why="Laba dan panduan di atas perkiraan menaikkan estimasi laba dan sering memicu pembelian institusi. Dampak ke indeks besar bila emitennya mega-cap.",
         who=["QQQ"], horizon="Hari reaksi dan beberapa hari"),
    dict(label="Risiko regulasi atau tarif", score=-2,
         pat=r"export (curb|control|restriction|ban)s?|tariffs?|trade war|(chip|semiconductor) (ban|curbs?)|entity list|blacklist",
         why="Pembatasan ekspor dan tarif memotong pasar dan margin perusahaan chip dan perangkat keras, serta menaikkan biaya rantai pasok.",
         who=["NVDA", "AMD", "AVGO", "TSM", "AAPL"], horizon="Hari ini sampai beberapa pekan"),
    dict(label="Minyak naik", score=-2,
         pat=r"(oil|brent|crude|wti)( prices?)? (surge|surges|jump|jumps|climb|climbs|spike|spikes|rise|rises|rose|rally|rallies|soar|soars|top|tops|hit|hits)|hormuz (closure|closed|shut|blockade)|energy shock",
         why="Minyak mahal memperbesar inflasi, mendorong Fed tetap ketat, dan mengurangi daya beli. Saham teknologi terkena lewat suku bunga yang lebih tinggi lebih lama.",
         who=["QQQ", "SPY"], horizon="Hari ini sampai beberapa pekan"),
    dict(label="Minyak turun atau de-eskalasi", score=2,
         pat=r"(oil|brent|crude|wti)( prices?)? (fall|falls|fell|drop|drops|dropped|slide|slides|slump|slumps|sink|sinks|plunge|plunges|tumble|tumbles|decline|declines)|ceasefire|peace (deal|talks|agreement)|hormuz reopen|de-?escalat",
         why="Minyak lebih murah dan risiko Timur Tengah yang mereda meredakan inflasi, membuka ruang bagi Fed, dan menaikkan selera risiko.",
         who=["QQQ", "SPY"], horizon="Hari ini sampai beberapa pekan"),
    dict(label="Eskalasi geopolitik", score=-2,
         pat=r"military (options|action|strikes?)|air ?strikes?|missiles?|(?<![\w-])escalat|invasion|(attacks?|strikes?) on (iran|israel|saudi|ships?|tankers?)|briefed on .{0,25}(strike|military)",
         why="Eskalasi militer mendorong harga minyak dan memicu lari ke aset aman, sehingga saham bervaluasi tinggi tertekan.",
         who=["QQQ", "SPY", "XLE"], horizon="Hari ini sampai beberapa hari"),
    dict(label="Aksi jual besar", score=-2,
         pat=r"sell-?off|plunge[sd]?|tumble[sd]?|\btank(s|ed)?\b|\brout\b|\bsinks?\b|\bslump(s|ed)?\b|crash(es|ed)?|wipe[sd]? (out|off)|worst (day|week|session)|biggest (drop|fall|decline)|steepest",
         why="Aksi jual besar memicu pengurangan leverage dan jual otomatis oleh dana sistematis, sehingga penurunan bisa berlanjut beberapa hari sampai harga cukup murah.",
         who=["QQQ"], horizon="1 sampai 3 hari"),
    dict(label="Rebound atau reli", score=1,
         pat=r"\brall(y|ies|ied)\b|rebound(s|ed)?|record high",
         why="Pemulihan harga menandakan pembeli masuk. Rebound setelah aksi jual besar sering tidak berlanjut tanpa katalis baru.",
         who=["QQQ"], horizon="Hari ini"),
    dict(label="Optimisme IPO AI", score=1,
         pat=r"\bipo\b.{0,60}(filing|files|filed|optimism|surge|boost|lift|revive)|(filing|files|filed).{0,40}\bipo\b",
         why="IPO perusahaan AI memvalidasi valuasi sektor dan menarik dana ke tema AI. Namun pasokan saham baru juga menyedot likuiditas.",
         who=["NVDA", "MSFT", "GOOGL"], horizon="Beberapa hari"),
    dict(label="Pasokan saham baru", score=-1,
         pat=r"share supply|new supply|dilution|ipo (glut|supply)|lock-?up expir",
         why="Penerbitan saham baru dalam jumlah besar menyerap dana dari saham yang sudah ada dan bisa menekan harganya.",
         who=["QQQ"], horizon="Beberapa pekan"),
    dict(label="Kekhawatiran ekonomi", score=-1,
         pat=r"layoffs?|job cuts|recession|slowdown|stagflation|consumer (spending|confidence) (falls|drops|weak)",
         why="Pemutusan kerja dan perlambatan menurunkan estimasi laba dan menambah risiko resesi.",
         who=["QQQ", "SPY"], horizon="Beberapa pekan"),
    dict(label="Peretasan atau risiko keamanan", score=-1,
         pat=r"\bhack(ed|ers?|ing)?\b|breach|heist|cyber-?attack|ransomware",
         why="Peretasan besar mengguncang kepercayaan pada aset digital dan menurunkan selera risiko, terutama di kripto dan saham terkait. Dampak ke Nasdaq biasanya terbatas kecuali menyangkut perusahaan besar.",
         who=["COIN", "MSTR", "HOOD", "IBIT"], horizon="Beberapa hari"),
    dict(label="Peringkat analis diturunkan", score=-1,
         pat=r"downgrade[sd]?|cut to (sell|underperform|neutral|hold)|lowers? (price target|rating)",
         why="Penurunan peringkat menggerakkan arus dana institusi keluar dari saham itu. Dampak ke indeks kecil kecuali emitennya mega-cap.",
         who=[], horizon="1 sampai 2 hari"),
    dict(label="Peringkat analis dinaikkan", score=1,
         pat=r"upgrade[sd]?|raised to (buy|outperform|overweight)|raises? price target|initiat(es|ed) (at|with) (buy|outperform)",
         why="Kenaikan peringkat menarik arus dana institusi ke saham itu. Dampak ke indeks kecil kecuali emitennya mega-cap.",
         who=[], horizon="1 sampai 2 hari"),
    dict(label="Pembelian kembali atau stock split", score=1,
         pat=r"buy-?backs?|repurchase|dividend (hike|increase|raise)|special dividend|stock split",
         why="Buyback mengurangi jumlah saham beredar dan menopang laba per saham. Stock split memperluas basis investor ritel.",
         who=[], horizon="Beberapa hari"),
    dict(label="Rotasi keluar dari teknologi", score=-1,
         pat=r"rotat(e|es|ed|ion|ing) (out of|away from) (tech|megacap|mega-cap|growth)|equal[- ]weight (outperforms?|beats?)|megacap (selling|weakness)",
         why="Dana berpindah dari mega-cap ke saham lain. Indeks berbobot kapitalisasi seperti Nasdaq tertinggal walau pasar luas naik.",
         who=["QQQ", "RSP"], horizon="Beberapa hari sampai pekan"),
    dict(label="Kekhawatiran valuasi", score=-1,
         pat=r"bubble|overvalued|stretched valuations?|valuation (concerns|worries|fears)|minsky",
         why="Kekhawatiran valuasi membuat investor lebih cepat menjual saat ada kabar buruk dan menahan pembelian saat kabar baik.",
         who=["QQQ", "NVDA"], horizon="Pekan sampai bulan"),
]
for _e in LEXICON:
    _e["rx"] = re.compile(_e["pat"], re.I)

# Frasa yang memuat "rate hike" tetapi bukan kabar kenaikan, mis. "wait on another rate hike".
NEGATED_HIKE = re.compile(
    r"(wait on|waits on|hold off on|holds off on|pause|pauses|skip|skips|unlikely to (raise|hike)|no|rule out|rules out)"
    r"\s+(another |a |further |more )?(rate )?(hike|hikes|increase)")
NEGATED_CUT = re.compile(
    r"(no|rules? out|unlikely to|not expect(ing)?|end of|ends?)\s+(any |a |another |further |more )?rate cuts?")

LEVERAGE = [
    (r"\bnvidia|nvda|broadcom|micron|tsmc|semiconductor|chip|intel|\bamd\b", 1.4),
    (r"\bfed\b|fomc|warsh|powell", 1.5),
    (r"\bmeta\b|microsoft|alphabet|google|amazon|apple|tesla", 1.3),
    (r"\bcoreweave|oracle|neocloud|bond", 1.3),
]

TICKERS = [
    (r"nvidia", "NVDA"), (r"alphabet|google", "GOOGL"), (r"amazon", "AMZN"), (r"\bmeta\b", "META"),
    (r"microsoft", "MSFT"), (r"apple", "AAPL"), (r"tesla", "TSLA"), (r"intel", "INTC"),
    (r"broadcom", "AVGO"), (r"micron", "MU"), (r"\bamd\b", "AMD"), (r"tsmc|taiwan semi", "TSM"),
    (r"oracle", "ORCL"), (r"coreweave", "CRWV"), (r"binance|bitcoin", "BTC"), (r"sandisk", "SNDK"),
]

THEMES = [
    ("Semikonduktor", r"nvidia|nvda|broadcom|micron|tsmc|semiconductor|chip|intel|\bamd\b|sandisk"),
    ("Pendanaan AI", r"capex|capital|financ|debt|bond|coreweave|oracle|free cash flow|cash outflow|ai spending"),
    ("Fed dan yield", r"\bfed\b|fomc|warsh|rate (hike|cut)|yield|treasury|inflation|cpi|pce|payroll|jobs"),
    ("Energi dan geopolitik", r"oil|brent|crude|hormuz|iran|israel|military|ceasefire"),
    ("Earnings Big Tech", r"alphabet|google|amazon|microsoft|meta\b|apple|tesla|earnings|guidance"),
    ("Kripto", r"bitcoin|crypto|binance|ether"),
]

MAJOR = ("reuters", "bloomberg", "cnbc", "wall street journal", "wsj", "financial times",
         "associated press", "ap news", "new york times", "barron")
MID = ("yahoo", "marketwatch", "benzinga", "seeking alpha", "investing.com", "forbes", "fortune",
       "business insider", "economist", "axios", "kiplinger", "fxempire", "morningstar",
       "investopedia", "tipranks", "nasdaq.com", "abc news", "bbc", "guardian")

# key: (nama, bobot, penjelasan umum, alasan jika bearish, alasan jika bullish)
FINFO = {
    "news": ("Berita berbobot", 30,
             "Skor rata-rata berita yang sudah digabung per kejadian, dibobot kredibilitas sumber dan kesegaran. Menangkap katalis yang belum tercermin penuh di harga.",
             "Arus berita lebih banyak membawa dampak negatif ke valuasi dan laba.",
             "Arus berita lebih banyak membawa dampak positif ke valuasi dan laba."),
    "nq_fut": ("Futures Nasdaq-100", 20,
               "Penanda terdekat arah pembukaan sesi AS. Diperdagangkan hampir 24 jam sehingga bereaksi lebih dulu pada berita.",
               "Futures di bawah penutupan sebelumnya, artinya sesi AS diperkirakan dibuka lebih rendah.",
               "Futures di atas penutupan sebelumnya, artinya sesi AS diperkirakan dibuka lebih tinggi."),
    "es_fut": ("Futures S&P 500", 5,
               "Pembanding luas. Jika S&P ikut turun, tekanan bukan hanya di teknologi.",
               "S&P 500 ikut melemah, jadi tekanan meluas melampaui teknologi.",
               "S&P 500 ikut menguat, jadi kenaikan didukung pasar yang lebih luas."),
    "sox": ("Semikonduktor (SOX)", 10,
            "Chip adalah penggerak terbesar Nasdaq. Arah SOX sering menentukan arah indeks.",
            "Indeks semikonduktor turun, dan sektor ini berbobot besar di Nasdaq.",
            "Indeks semikonduktor naik, dan sektor ini berbobot besar di Nasdaq."),
    "yield": ("Yield obligasi 10 tahun", 10,
              "Yield tinggi menekan valuasi saham growth. Kenaikan tajam dalam sehari bersifat bearish, dan level di atas 5% menambah tekanan.",
              "Yield naik atau berada di level tinggi, sehingga nilai kini laba teknologi tertekan.",
              "Yield turun, sehingga tekanan diskonto pada saham growth berkurang."),
    "vix": ("VIX (volatilitas)", 10,
            "Mengukur ketakutan pasar. VIX tinggi atau melonjak berarti investor membayar mahal untuk perlindungan, umumnya tanda risk-off.",
            "Volatilitas tinggi atau naik, tanda investor membeli perlindungan.",
            "Volatilitas rendah atau turun, tanda investor tenang dan berani mengambil risiko."),
    "oil": ("Minyak Brent", 5,
            "Minyak mahal menaikkan inflasi dan menahan Fed melonggarkan. Minyak turun meredakan tekanan itu.",
            "Minyak tinggi atau naik, memperbesar tekanan inflasi dan suku bunga.",
            "Minyak turun, meredakan tekanan inflasi dan suku bunga."),
    "dxy": ("Indeks dolar", 5,
            "Dolar kuat mengetatkan kondisi keuangan global dan menekan laba perusahaan multinasional.",
            "Dolar menguat, mengetatkan kondisi keuangan dan menekan laba multinasional.",
            "Dolar melemah, melonggarkan kondisi keuangan global."),
    "trend": ("Tren Nasdaq 5 sesi", 5,
              "Momentum jangka pendek. Tren turun cenderung berlanjut beberapa hari, dan jarak dari puncak menunjukkan seberapa dalam koreksinya.",
              "Indeks melemah selama lima sesi terakhir, dan momentum jual cenderung berlanjut.",
              "Indeks menguat selama lima sesi terakhir, dan momentum beli cenderung berlanjut."),
}
FACTOR_ORDER = ["news", "nq_fut", "es_fut", "sox", "yield", "vix", "oil", "dxy", "trend"]

SYMS = {"nq_fut": "NQ=F", "es_fut": "ES=F", "sox": "^SOX", "vix": "^VIX", "vxn": "^VXN",
        "yield10": "^TNX", "brent": "BZ=F", "dxy": "DX-Y.NYB", "nvda": "NVDA",
        "irx": "^IRX", "fvx": "^FVX", "qqqe": "QQQE", "soxx": "SOXX", "qqq_y": "QQQ"}

DEMO = [
    {"title": "Meta posts first cash outflow in a decade, plans non-voting share issue to raise capital", "source": "Reuters"},
    {"title": "Broadcom, Intel CEOs say capital is the biggest AI constraint", "source": "Benzinga"},
    {"title": "Philadelphia semiconductor index plunges 6.7% as AI spending doubts return", "source": "Reuters"},
    {"title": "Fed likely to wait on another rate hike as inflation pressures mount", "source": "Yahoo Finance"},
    {"title": "September payrolls well below consensus, boosting case for Fed pause", "source": "Reuters"},
    {"title": "Oil prices fall as Brent slides, easing inflation fears before Fed", "source": "CNBC"},
    {"title": "Stocks rebound as Anthropic IPO filing lifts Nasdaq", "source": "Bloomberg"},
    {"title": "Treasury 10-year yield slips from near 5% as investors seek safety", "source": "Reuters"},
]


# ---------------------------------------------------------------------------
# Utilitas
# ---------------------------------------------------------------------------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def fnum(x, d=2, sign=False):
    """Format angka gaya Indonesia: 26.184,72 dan tanda minus sungguhan."""
    if x is None:
        return "–"
    s = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if x < 0 and float(f"{abs(x):.{d}f}") != 0:
        return "−" + s
    return ("+" + s) if sign and float(f"{abs(x):.{d}f}") != 0 else s


def _zone():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo("America/New_York")
    except Exception:
        return None


ET_ZONE = _zone()


def to_et(t):
    if ET_ZONE is not None:
        return t.astimezone(ET_ZONE)
    off = -4 if 3 < t.month < 11 else -5
    return t.astimezone(dt.timezone(dt.timedelta(hours=off)))


def et_to_utc(date_iso, hhmm):
    d = dt.datetime.strptime(date_iso + " " + hhmm, "%Y-%m-%d %H:%M")
    if ET_ZONE is not None:
        return d.replace(tzinfo=ET_ZONE).astimezone(UTC)
    off = -4 if 3 < d.month < 11 else -5
    return (d - dt.timedelta(hours=off)).replace(tzinfo=UTC)


def parse_time(s):
    if not s:
        return None
    try:
        t = parsedate_to_datetime(s)
    except Exception:
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def session_date(now):
    """Tanggal sesi AS yang sedang atau akan berjalan (libur bursa tidak diperhitungkan)."""
    d = to_et(now).date()
    if d.weekday() < 5 and now < et_to_utc(d.isoformat(), "16:00"):
        return d
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Pengambilan data
# ---------------------------------------------------------------------------
def _http(url, timeout=20, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nasdaq-agent/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def fetch_rss(query, limit=12):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(_http(url))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        if title:
            out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                        "published": (it.findtext("pubDate") or "").strip(), "source": src})
        if len(out) >= limit:
            break
    return out


def _yahoo(symbol, rng, interval="1d"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval={interval}")
    return json.loads(_http(url))["chart"]["result"][0]


def _series(res):
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        if q["open"][i] is None or q["high"][i] is None or q["low"][i] is None:
            continue
        rows.append({"t": t, "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": c,
                     "v": (q.get("volume") or [None] * (i + 1))[i]})
    return rows


def _quote(symbol):
    """Harga terakhir dan penutupan sesi sebelumnya (dua penutupan terakhir pada deret harian)."""
    res = _yahoo(symbol, "1mo")
    rows = _series(res)
    last = res["meta"].get("regularMarketPrice") or rows[-1]["c"]
    prev = rows[-2]["c"] if len(rows) >= 2 else None
    if symbol == "^TNX" and last > 20:  # beberapa data lama memakai skala x10
        last, prev = last / 10, (prev / 10 if prev else None)
    chg = round((last / prev - 1) * 100, 2) if prev else None
    out = {"last": round(last, 4), "prev": round(prev, 4) if prev else None, "change_pct": chg}
    if len(rows) >= 6 and rows[-6]["c"]:
        out["change5_pct"] = round((last / rows[-6]["c"] - 1) * 100, 2)
    if symbol == "^TNX" and prev:
        out["change_bps"] = round((last - prev) * 100, 1)
        if len(rows) >= 6:
            p5 = rows[-6]["c"] / (10 if rows[-6]["c"] > 20 else 1)
            out["change5_bps"] = round((last - p5) * 100, 1)
    return out


# ---------------------------------------------------------------------------
# Fed Speak, Magnificent 7, COT, validator makro
# ---------------------------------------------------------------------------
FED_RX = re.compile(r"\b(fed|federal reserve|fomc|powell|warsh|bostic|waller|williams|jefferson|barr|bowman|cook|logan|"
                    r"goolsbee|kashkari|daly|harker|musalem|hammack|schmid|collins|rate cuts?|rate hikes?|inflation)\b")
FED_LABELS = {"Risiko kenaikan suku bunga", "Pelonggaran ditunda", "Harapan pelonggaran"}
MACRO_LABELS = FED_LABELS | {"Imbal hasil naik", "Imbal hasil turun", "Data kerja lemah menurunkan peluang hike", "Data ekonomi kuat",
                             "Minyak naik", "Minyak turun atau de-eskalasi", "Eskalasi geopolitik", "Kekhawatiran ekonomi",
                             "Risiko regulasi atau tarif"}
MAG7 = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "META": "Meta", "GOOGL": "Alphabet", "TSLA": "Tesla"}
FED_OVERLAY_MAX = 5.0
COT_CODE = "209742"  # NASDAQ MINI (E-mini Nasdaq-100) di laporan TFF CFTC


def fed_tone(hits):
    return sum(h["score"] for h in hits if h["label"] in FED_LABELS)


def fed_radar(clusters, earnings_week=False):
    """Pisahkan berita Fed Speak dan hitung overlay skor. Hawkish (tone negatif) = penalti, dovish = bonus.
    Bobot memuat kredibilitas sumber dan usia berita, jadi berita basi otomatis kecil."""
    rows, num, den = [], 0.0, 0.0
    for c in clusters:
        rep = c["rep"]
        if not FED_RX.search(rep["title"].lower()):
            continue
        tone = fed_tone(rep["hits"])
        scored = tone != 0
        rows.append({"title": rep["title"], "source": rep.get("source"), "link": rep.get("link"), "age_h": rep["age_h"],
                     "tone": tone, "label": "Hawkish" if tone < 0 else "Dovish" if tone > 0 else "Netral",
                     "weight": round(c["weight"], 2), "scored": scored, "sources_count": c["n"]})
        if scored:
            num += c["weight"] * tone; den += c["weight"]
    avg = num / den if den else 0.0
    strength = min(1.0, den / 1.0)
    overlay = clamp(avg / 2 * FED_OVERLAY_MAX, -FED_OVERLAY_MAX, FED_OVERLAY_MAX) * strength
    if earnings_week:
        overlay *= 0.5
    rows.sort(key=lambda r: -abs(r["tone"]) * r["weight"])
    return {"items": rows[:10], "n_scored": sum(1 for r in rows if r["scored"]), "avg_tone": round(avg, 2),
            "strength": round(strength, 2), "overlay": round(overlay, 2),
            "meter": round(clamp(avg / 3, -1, 1), 2), "earnings_week_halved": earnings_week}


def earnings_week(market, now):
    """Mag7 yang melapor dari 3 hari sebelum sampai 1 hari sesudah: berita makro biasa dikurangi 50%."""
    act = []
    for e in market.get("mag7_earnings") or []:
        t = dt.datetime.fromtimestamp(e["ts"], UTC)
        h = (t - now).total_seconds() / 3600
        if -24 <= h <= 72:
            act.append({"ticker": e["ticker"], "name": MAG7.get(e["ticker"], e["ticker"]), "start_utc": t.isoformat(timespec="minutes"),
                        "hours_until": round(h, 1)})
    return act


def mag7_events(market, now):
    out = []
    for e in market.get("mag7_earnings") or []:
        t = dt.datetime.fromtimestamp(e["ts"], UTC)
        if t < now - dt.timedelta(hours=24):
            continue
        d = to_et(t).date().isoformat()
        out.append({"date": d, "start_utc": t.isoformat(timespec="minutes"), "time_known": True,
                    "name": f"Laporan laba {MAG7.get(e['ticker'], e['ticker'])} ({e['ticker']})", "impact": "tinggi",
                    "confirmed": e.get("confirmed", False), "mag7": True,
                    "why": f"{MAG7.get(e['ticker'], e['ticker'])} termasuk 7 saham yang memegang lebih dari 40% bobot Nasdaq-100. Selama pekan laporan, hasil perusahaan lebih menggerakkan indeks daripada data makro, sehingga bobot berita makro dikurangi 50%.",
                    "hours_until": round((t - now).total_seconds() / 3600, 1)})
    return out


def fetch_mag7_earnings():
    """Tanggal laporan laba tiap saham Mag7 dari Yahoo (quoteSummary/calendarEvents)."""
    op, crumb = _yahoo_crumb()
    out, errs = [], []
    for tk in MAG7:
        try:
            url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{tk}?modules=calendarEvents&crumb={urllib.parse.quote(crumb)}"
            r = json.loads(op.open(url, timeout=15).read())["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]
            dates = [d["raw"] for d in r.get("earningsDate", []) if d.get("raw")]
            if dates:
                out.append({"ticker": tk, "ts": min(dates), "confirmed": len(dates) == 1})
        except Exception as e:  # noqa: BLE001
            errs.append(f"{tk}: {str(e)[:40]}")
    if not out:
        raise RuntimeError("; ".join(errs[:2]) or "tidak ada tanggal")
    return out


def fetch_cot():
    """Laporan COT (TFF, futures only) kontrak Nasdaq Mini dari API publik CFTC. Terbaru di urutan pertama."""
    url = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json?cftc_contract_market_code=" + COT_CODE +
           "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=8")
    rows = json.loads(_http(url, timeout=25))
    out = []
    for r in rows:
        try:
            al, ash = int(float(r["asset_mgr_positions_long"])), int(float(r["asset_mgr_positions_short"]))
            ll, ls = int(float(r["lev_money_positions_long"])), int(float(r["lev_money_positions_short"]))
            out.append({"date": r["report_date_as_yyyy_mm_dd"][:10], "am_long": al, "am_short": ash, "am_net": al - ash,
                        "lev_long": ll, "lev_short": ls, "lev_net": ll - ls,
                        "oi": int(float(r.get("open_interest_all", 0)))})
        except (KeyError, ValueError):
            continue
    if len(out) < 3:
        raise RuntimeError("data COT kurang dari 3 minggu")
    return out


def load_cot(path, now, injected=None):
    """Cache di docs/cot.json; ambil ulang bila lebih tua dari 12 jam (rilis mingguan, Jumat sore ET)."""
    if injected:
        return {"rows": injected, "fetched_at": now.isoformat(timespec="minutes"), "stale": False}
    cache = None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001
            cache = None
    fa = parse_time(cache.get("fetched_at")) if cache else None
    if cache and fa and (now - fa).total_seconds() < 12 * 3600:
        return {**cache, "stale": False}
    try:
        rows = fetch_cot()
        res = {"rows": rows, "fetched_at": now.isoformat(timespec="minutes")}
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
        return {**res, "stale": False}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] COT: {e}", file=sys.stderr)
        return {**cache, "stale": True, "err": str(e)[:80]} if cache else {"rows": [], "err": str(e)[:80], "stale": True}


def analyze_cot(cot):
    rows = (cot or {}).get("rows") or []
    if len(rows) < 3:
        return None
    n0, n1, n2 = rows[0]["am_net"], rows[1]["am_net"], rows[2]["am_net"]
    d1, d2 = n0 - n1, n1 - n2
    trend = "distribusi" if d1 < 0 and d2 < 0 else "akumulasi" if d1 > 0 and d2 > 0 else "campuran"
    text = {"distribusi": "Asset manager mengurangi net long dua minggu berturut-turut: uang besar sedang distribusi.",
            "akumulasi": "Asset manager menambah net long dua minggu berturut-turut: uang besar sedang akumulasi.",
            "campuran": "Posisi asset manager naik-turun, tidak ada tren dua minggu yang jelas."}[trend]
    return {"report_date": rows[0]["date"], "trend": trend, "text": text, "am_net": n0, "d_week1": d1, "d_week2": d2,
            "lev_net": rows[0]["lev_net"], "lev_d": rows[0]["lev_net"] - rows[1]["lev_net"],
            "series": [{"date": r["date"], "am_net": r["am_net"], "lev_net": r["lev_net"]} for r in rows[:6]][::-1],
            "stale": bool((cot or {}).get("stale")), "fetched_at": (cot or {}).get("fetched_at")}


def apply_gates(score, market, cot_an, structure_breaker_active=False):
    """Pengunci bias: yield/DXY (validator makro) dan batas tren institusi (COT). Mengembalikan (skor, daftar gerbang)."""
    gates = []
    y = (market.get("yield10") or {}).get("change_pct")
    d = (market.get("dxy") or {}).get("change_pct")
    nq = (market.get("nq_fut") or {}).get("change_pct")
    y_up, y_dn = y is not None and y > 0.5, y is not None and y < -0.5
    d_up, d_dn = d is not None and d > 0.3, d is not None and d < -0.3
    nq_up = nq is not None and nq > 0.3
    g = {"key": "validator", "name": "Validator makro (yield dan dolar)", "active": False,
         "detail": "Yield 10 tahun " + (fnum(y, 2, True) + "%" if y is not None else "n/a") + ", DXY " + (fnum(d, 2, True) + "%" if d is not None else "n/a"),
         "effect": None}
    if score >= 5 and y_up:
        g.update(active=True, effect="Skor bullish dipangkas ke +2",
                 note="Anomali fundamental: berita positif, tetapi yield obligasi naik. NQ berisiko tertekan.")
        score = min(score, 2.0)
    elif (y is not None and y > 0.2) and (d is not None and d > 0.2) and (nq is not None and nq > 0.2) and score > 2:
        g.update(active=True, effect="Skor dibatasi maksimum +2",
                 note="Anomali berbahaya: NQ naik bersamaan dengan dolar dan yield. Korelasi normalnya terbalik, hindari memaksakan bias bullish.")
        score = 2.0
    elif score <= -5 and y_dn and d_dn:
        g.update(active=True, effect="Skor bearish dilunakkan ke −2",
                 note="Berita negatif, tetapi yield dan dolar sama-sama turun sehingga gravitasi valuasi justru mendukung NQ.")
        score = max(score, -2.0)
    gates.append(g)
    c = {"key": "cot", "name": "Tren institusi mingguan (COT)", "active": False, "detail": "data COT tidak tersedia", "effect": None}
    if cot_an:
        c["detail"] = f"{cot_an['trend']}; net asset manager {cot_an['am_net']:+,} ({cot_an['d_week1']:+,} minggu ini, {cot_an['d_week2']:+,} minggu lalu)"
        if cot_an["trend"] == "distribusi" and score > 5:
            c.update(active=True, effect="Skor bullish dibatasi maksimum +5", note=cot_an["text"]); score = 5.0
        elif cot_an["trend"] == "akumulasi" and score < -5:
            c.update(active=True, effect="Skor bearish dibatasi minimum −5", note=cot_an["text"]); score = -5.0
    gates.append(c)
    return score, gates


def analyze_structure(bars):
    """Filter mikrostruktur dari bar per jam NQ: liquidity sweep dan Fair Value Gap (FVG).
    Sweep high: bar baru menembus high 24 bar sebelumnya tetapi ditutup kembali di bawahnya (penolakan).
    Bearish FVG: low bar[i-2] > high bar[i]; ditolak bila harga masuk celah lalu terakhir ditutup di bawah tengahnya.
    Sisi bullish adalah cerminannya."""
    res = {"available": False, "sweep_high": None, "sweep_low": None, "bear_fvg": None, "bull_fvg": None, "bearish": False, "bullish": False, "notes": []}
    if not bars or len(bars) < 30:
        return res
    res["available"] = True
    n = len(bars)
    for i in range(n - 6, n):
        lvl_h = max(b["h"] for b in bars[i - 24:i]); lvl_l = min(b["l"] for b in bars[i - 24:i])
        b = bars[i]
        if b["h"] > lvl_h and b["c"] < lvl_h and bars[-1]["c"] < lvl_h:
            res["sweep_high"] = {"level": round(lvl_h, 2), "high": round(b["h"], 2), "bars_ago": n - 1 - i}
        if b["l"] < lvl_l and b["c"] > lvl_l and bars[-1]["c"] > lvl_l:
            res["sweep_low"] = {"level": round(lvl_l, 2), "low": round(b["l"], 2), "bars_ago": n - 1 - i}
    last = bars[-1]
    for i in range(n - 12, n):
        a, c = bars[i - 2], bars[i]
        if a["l"] > c["h"]:  # celah turun
            top, bot = a["l"], c["h"]
            later = bars[i + 1:]
            if later and max(x["h"] for x in later) >= bot and last["c"] < (top + bot) / 2:
                res["bear_fvg"] = {"top": round(top, 2), "bottom": round(bot, 2), "bars_ago": n - 1 - i}
        if a["h"] < c["l"]:  # celah naik
            bot, top = a["h"], c["l"]
            later = bars[i + 1:]
            if later and min(x["l"] for x in later) <= top and last["c"] > (top + bot) / 2:
                res["bull_fvg"] = {"top": round(top, 2), "bottom": round(bot, 2), "bars_ago": n - 1 - i}
    if res["sweep_high"]:
        res["notes"].append(f"Liquidity sweep di atas {res['sweep_high']['level']:,.0f} lalu ditolak ({res['sweep_high']['bars_ago']} jam lalu).")
    if res["bear_fvg"]:
        res["notes"].append(f"Harga tertolak di bearish FVG {res['bear_fvg']['bottom']:,.0f}–{res['bear_fvg']['top']:,.0f}.")
    if res["sweep_low"]:
        res["notes"].append(f"Liquidity sweep di bawah {res['sweep_low']['level']:,.0f} lalu ditolak ({res['sweep_low']['bars_ago']} jam lalu).")
    if res["bull_fvg"]:
        res["notes"].append(f"Harga tertahan di bullish FVG {res['bull_fvg']['bottom']:,.0f}–{res['bull_fvg']['top']:,.0f}.")
    res["bearish"] = bool(res["sweep_high"] or res["bear_fvg"])
    res["bullish"] = bool(res["sweep_low"] or res["bull_fvg"])
    return res


def fetch_alpaca(symbol="QQQ"):
    """Harga saham/ETF nyaris realtime dari Alpaca (feed IEX gratis). Kunci hanya dari variabel lingkungan."""
    kid, sec = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not kid or not sec:
        return None
    req = urllib.request.Request(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/snapshot?feed=iex",
        headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec, "User-Agent": "nasdaq-agent/2.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    last = (d.get("latestTrade") or {}).get("p")
    prev = (d.get("prevDailyBar") or {}).get("c")
    if not last:
        return None
    return {"symbol": symbol, "last": round(last, 2), "prev": prev,
            "change_pct": round((last / prev - 1) * 100, 2) if prev else None,
            "time": (d.get("latestTrade") or {}).get("t"), "feed": "IEX"}



# ---------------------------------------------------------------------------
# Makro, breadth, opsi, volume
# ---------------------------------------------------------------------------
def _fed_mid():
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+[.,]\d+", FED_FUNDS)]
    return sum(nums[:2]) / len(nums[:2]) if len(nums) >= 2 else None


def rate_expectations(market):
    """Proksi ekspektasi suku bunga dari yield T-bill 13 minggu (^IRX) vs titik tengah target Fed.
    Bukan CME FedWatch (tidak ada API gratis resmi). Selisih ±15 bps kira-kira setara satu langkah 25 bps
    yang sepenuhnya diperhitungkan untuk rapat dalam ~5 pekan."""
    irx, mid = (market.get("irx") or {}).get("last"), _fed_mid()
    if irx is None or mid is None:
        return None
    diff = (irx - mid) * 100
    ph = clamp(diff / 15, 0, 1)
    pc = clamp(-diff / 15, 0, 1)
    label = "Pasar memperhitungkan kenaikan" if diff >= 7 else "Pasar memperhitungkan pemangkasan" if diff <= -7 else "Pasar memperkirakan suku bunga ditahan"
    ten = (market.get("yield10") or {}).get("last")
    return {"tbill13w": round(irx, 3), "fed_mid": round(mid, 3), "diff_bps": round(diff, 1),
            "p_hike": round(ph, 2), "p_cut": round(pc, 2), "p_hold": round(1 - max(ph, pc), 2), "label": label,
            "slope_10y_13w_bps": round((ten - irx) * 100, 0) if ten else None,
            "note": "Proksi dari T-bill 13 minggu, bukan angka CME FedWatch."}


def breadth(market):
    q, e, so = market.get("qqq_y"), market.get("qqqe"), market.get("soxx")
    if not (q and e):
        return None
    rel1 = round((e.get("change_pct") or 0) - (q.get("change_pct") or 0), 2)
    rel5 = round((e.get("change5_pct") or 0) - (q.get("change5_pct") or 0), 2) if e.get("change5_pct") is not None and q.get("change5_pct") is not None else None
    ref = rel5 if rel5 is not None else rel1
    state = "menyempit" if ref <= -1.0 else "melebar" if ref >= 0.5 else "seimbang"
    text = {"menyempit": "QQQE tertinggal dari QQQ: pergerakan ditopang segelintir megacap, kurang sehat.",
            "melebar": "QQQE mengungguli QQQ: kenaikan atau penurunan melibatkan banyak saham, lebih tahan lama.",
            "seimbang": "QQQE dan QQQ bergerak searah: tidak ada anomali megacap yang menonjol."}[state]
    return {"qqq": q.get("change_pct"), "qqqe": e.get("change_pct"), "soxx": (so or {}).get("change_pct"),
            "qqq5": q.get("change5_pct"), "qqqe5": e.get("change5_pct"), "soxx5": (so or {}).get("change5_pct"),
            "rel1": rel1, "rel5": rel5, "state": state, "text": text}


def volume_anomaly(bars):
    """Proksi jejak blok besar: lonjakan volume per jam NQ relatif terhadap 48 jam sebelumnya."""
    vs = [b for b in (bars or []) if b.get("v")]
    if len(vs) < 30:
        return None
    last = vs[-1]; base = [b["v"] for b in vs[-49:-1]]
    mu = sum(base) / len(base); sd = (sum((x - mu) ** 2 for x in base) / len(base)) ** 0.5 or 1
    z = (last["v"] - mu) / sd
    big = [b for b in vs[-6:] if (b["v"] - mu) / sd >= 2.5]
    d = "naik" if last["c"] >= last["o"] else "turun"
    return {"z": round(z, 2), "last_volume": last["v"], "avg_volume": round(mu), "spike": z >= 2.5 or bool(big),
            "direction": d, "note": ("Lonjakan volume pada bar terakhir (" + d + "): kemungkinan transaksi institusi besar." if z >= 2.5 else
                                     "Ada lonjakan volume beberapa jam terakhir." if big else "Volume normal, tidak ada jejak blok besar yang menonjol."),
            "caveat": "Data dark pool/blok asli berbayar. Ini proksi dari volume futures."}


def volume_profile(bars, bins=60):
    vs = [b for b in (bars or []) if b.get("v")]
    if len(vs) < 20:
        return None
    lo, hi = min(b["l"] for b in vs), max(b["h"] for b in vs)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for b in vs:
        i0 = int((b["l"] - lo) / step); i1 = min(bins - 1, int((b["h"] - lo) / step))
        n = i1 - i0 + 1
        for i in range(i0, i1 + 1):
            vol[i] += b["v"] / n
    poc = max(range(bins), key=lambda i: vol[i])
    total = sum(vol); acc = vol[poc]; a = b_ = poc
    while acc < 0.7 * total and (a > 0 or b_ < bins - 1):
        up = vol[b_ + 1] if b_ < bins - 1 else -1
        dn = vol[a - 1] if a > 0 else -1
        if up >= dn:
            b_ += 1; acc += vol[b_]
        else:
            a -= 1; acc += vol[a]
    price = lambda i: round(lo + (i + 0.5) * step, 1)
    return {"poc": price(poc), "vah": round(lo + (b_ + 1) * step, 1), "val": round(lo + a * step, 1),
            "range_low": round(lo, 1), "range_high": round(hi, 1), "bars": len(vs)}


def fvg_map(bars, lookback=48):
    """Daftar Fair Value Gap yang belum terisi dan posisi harga terhadap premium/discount."""
    if not bars or len(bars) < 30:
        return None
    n = len(bars); last = bars[-1]["c"]
    rng = bars[-lookback:]
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    eq = (hi + lo) / 2
    gaps = []
    for i in range(max(2, n - lookback), n):
        a, c = bars[i - 2], bars[i]
        if a["l"] > c["h"]:
            top, bot, kind = a["l"], c["h"], "bearish"
            filled = any(x["h"] >= top for x in bars[i + 1:])
        elif a["h"] < c["l"]:
            top, bot, kind = c["l"], a["h"], "bullish"
            filled = any(x["l"] <= bot for x in bars[i + 1:])
        else:
            continue
        if not filled and (top - bot) / last > 0.0004:
            gaps.append({"kind": kind, "top": round(top, 1), "bottom": round(bot, 1), "bars_ago": n - 1 - i})
    gaps.sort(key=lambda g: abs((g["top"] + g["bottom"]) / 2 - last))
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    zone = "premium" if pos > 0.55 else "discount" if pos < 0.45 else "equilibrium"
    return {"gaps": gaps[:5], "range_high": round(hi, 1), "range_low": round(lo, 1), "equilibrium": round(eq, 1),
            "position_pct": round(pos * 100), "zone": zone, "price": round(last, 1)}


def _bs_gamma(S, K, T, iv):
    if S <= 0 or K <= 0 or T <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * iv * math.sqrt(T))


def _yahoo_crumb():
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 nasdaq-agent/2.0")]
    try:
        op.open("https://fc.yahoo.com", timeout=10)
    except Exception:  # noqa: BLE001
        pass  # fc.yahoo.com membalas 404 tetapi tetap memasang cookie
    crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10).read().decode()
    return op, crumb


def fetch_option_chain(symbol="QQQ", max_exp=3):
    """Rantai opsi dari Yahoo untuk beberapa expiry terdekat. Hasil: {spot, contracts:[{t,K,oi,vol,iv,T}]}"""
    op, crumb = _yahoo_crumb()
    base = f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}?crumb={urllib.parse.quote(crumb)}"
    first = json.loads(op.open(base, timeout=15).read())["optionChain"]["result"][0]
    exps = first.get("expirationDates") or []
    spot = first["quote"].get("regularMarketPrice")
    out, now_ts = [], time.time()
    for ex in exps[:max_exp]:
        res = first if ex == first["options"][0]["expirationDate"] else json.loads(op.open(base + f"&date={ex}", timeout=15).read())["optionChain"]["result"][0]
        T = max((ex - now_ts) / (365 * 86400), 0.5 / 365)
        for side, key in (("C", "calls"), ("P", "puts")):
            for c in res["options"][0].get(key, []):
                out.append({"t": side, "K": c["strike"], "oi": c.get("openInterest") or 0, "vol": c.get("volume") or 0,
                            "iv": c.get("impliedVolatility") or 0, "T": T, "exp": ex})
    return {"spot": spot, "contracts": out, "expiries": exps[:max_exp]}


def analyze_options(chain, spot_hint, nq_last, now):
    """PCR dan peta Gamma Exposure dari rantai opsi QQQ, dipetakan ke harga NQ. Konvensi umum:
    market maker long gamma di call (+) dan short gamma di put (-). Ini perkiraan, bukan posisi sebenarnya."""
    if not chain or not chain.get("contracts"):
        return None
    S = chain.get("spot") or spot_hint
    cs = [c for c in chain["contracts"] if c["iv"] > 0.01]
    if not S or not cs:
        return None
    if sum(c["oi"] for c in cs) < 1000 or len({c["K"] for c in cs if c["oi"] > 0}) < 8:
        return None  # open interest kosong atau jarang: hasil GEX/PCR akan menyesatkan
    put_oi = sum(c["oi"] for c in chain["contracts"] if c["t"] == "P"); call_oi = sum(c["oi"] for c in chain["contracts"] if c["t"] == "C")
    put_v = sum(c["vol"] for c in chain["contracts"] if c["t"] == "P"); call_v = sum(c["vol"] for c in chain["contracts"] if c["t"] == "C")
    gex = {}
    for c in cs:
        g = _bs_gamma(S, c["K"], c["T"], c["iv"]) * c["oi"] * 100 * S * S * 0.01
        gex[c["K"]] = gex.get(c["K"], 0.0) + (g if c["t"] == "C" else -g)
    ks = sorted(gex)
    near = [k for k in ks if 0.9 * S <= k <= 1.1 * S]
    cw = max(near, key=lambda k: gex[k]) if near else None
    pw = min(near, key=lambda k: gex[k]) if near else None
    # gamma flip: strike tempat GEX kumulatif (dari bawah) berganti tanda, paling dekat dengan spot
    cum, flip = 0.0, None
    prev_sign = None
    cand = []
    for k in near:
        cum += gex[k]
        sg = cum >= 0
        if prev_sign is not None and sg != prev_sign:
            cand.append(k)
        prev_sign = sg
    if cand:
        flip = min(cand, key=lambda k: abs(k - S))
    ratio = (nq_last / S) if nq_last else None
    mp = lambda k: round(k * ratio, 0) if (k is not None and ratio) else None
    net = sum(gex.values())
    top = sorted(near, key=lambda k: -abs(gex[k]))[:6]
    pcr = round(put_oi / call_oi, 2) if call_oi else None
    pcrv = round(put_v / call_v, 2) if call_v else None
    sentiment = None
    if pcr is not None:
        sentiment = ("Takut berlebihan (sinyal kontrarian bullish)" if pcr >= 1.3 else "Pesimis, lindung nilai meningkat" if pcr >= 1.0 else
                     "Serakah/terlalu optimis (kontrarian waspada)" if pcr <= 0.6 else "Netral")
    return {"underlying": "QQQ", "spot": round(S, 2), "nq_ratio": round(ratio, 3) if ratio else None,
            "pcr_oi": pcr, "pcr_vol": pcrv, "sentiment": sentiment,
            "call_wall": cw, "put_wall": pw, "gamma_flip": flip,
            "call_wall_nq": mp(cw), "put_wall_nq": mp(pw), "gamma_flip_nq": mp(flip),
            "net_gex_musd": round(net / 1e6, 1), "regime": "gamma positif (meredam gerak)" if net > 0 else "gamma negatif (memperbesar gerak)",
            "top": [{"strike": k, "nq": mp(k), "gex_musd": round(gex[k] / 1e6, 1)} for k in sorted(top)],
            "expiries": len(chain.get("expiries") or []),
            "note": "Perkiraan dari open interest dan IV opsi QQQ (Yahoo), dipetakan ke NQ dengan rasio harga. Bukan data posisi dealer sebenarnya."}


def strategy_metrics(entries):
    """Metrik kuantitatif: ikuti bias (bullish=long, bearish=short, netral=diam) pada hasil close-to-close."""
    rows = [e for e in entries if e.get("outcome") and not e.get("late")]
    rets = []
    for e in rows:
        pos = 1 if "BULL" in e["bias"] else -1 if "BEAR" in e["bias"] else 0
        if pos:
            rets.append(pos * e["outcome"]["change_pct"])
    if not rets:
        return {"n_trades": 0, "n_days": len(rows)}
    wins = [r for r in rets if r > 0]; losses = [r for r in rets if r < 0]
    eq = peak = dd = 0.0
    for r in rets:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
    gl = -sum(losses)
    return {"n_trades": len(rets), "n_days": len(rows), "win_rate": round(len(wins) / len(rets), 3),
            "profit_factor": round(sum(wins) / gl, 2) if gl > 0 else None,
            "max_drawdown_pct": round(dd, 2), "total_return_pct": round(eq, 2),
            "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss_pct": round(-gl / len(losses), 2) if losses else None}


def front_contract(now):
    """Kontrak MNQ bulan terdekat (H, M, U, Z). Berganti 8 hari sebelum kedaluwarsa (Jumat ketiga),
    jadi kode otomatis maju: MNQZ2026 -> MNQH2027 -> MNQM2027 -> ..."""
    codes = {3: "H", 6: "M", 9: "U", 12: "Z"}
    d = now.astimezone(UTC).date()
    for y in (d.year, d.year + 1):
        for m in (3, 6, 9, 12):
            first = dt.date(y, m, 1)
            third_fri = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
            if d < third_fri - dt.timedelta(days=8):
                return {"code": f"MNQ{codes[m]}{y}", "tv": f"CME_MINI:MNQ{codes[m]}{y}",
                        "yahoo": f"MNQ{codes[m]}{str(y)[2:]}.CME", "expiry": third_fri.isoformat()}
    raise RuntimeError("kontrak tidak ditemukan")


def fetch_market(now):
    m = {"fed": {"range": FED_FUNDS, "note": FED_NOTE}}
    feeds = m["_feeds"] = {}

    def mark(name, ok, err=None):
        feeds[name] = {"ok": ok, "at": dt.datetime.now(UTC).astimezone(WIB).isoformat(timespec="seconds"),
                       "err": (str(err)[:80] if err else None)}
    try:
        res = _yahoo("^IXIC", "3mo")
        rows = _series(res)
        bars = []
        for r in rows[-40:]:
            d = to_et(dt.datetime.fromtimestamp(r["t"], UTC)).date().isoformat()
            bars.append({"d": d, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]})
        last, prev = bars[-1]["c"], bars[-2]["c"]
        closes = []
        for b in bars[-6:]:
            y, mo, dd = b["d"].split("-")
            closes.append([f"{int(dd)} {MONTHS[int(mo) - 1]}", round(b["c"], 2)])
        m["nasdaq"] = {
            "last": round(last, 2), "prev_close": round(prev, 2),
            "change_pct": round((last / prev - 1) * 100, 2),
            "high52": res["meta"].get("fiftyTwoWeekHigh"),
            "ret5_pct": round((last / bars[-6]["c"] - 1) * 100, 2) if len(bars) >= 6 else None,
            "closes": closes, "bars": bars[-30:],
        }
        mark("nasdaq", True)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Nasdaq: {e}", file=sys.stderr); mark("nasdaq", False, e)
    for key, sym in SYMS.items():
        try:
            if key == "nq_fut":
                fc = front_contract(now)
                m[key] = _quote(sym)  # NQ=F: kontrak depan Yahoo, berganti sendiri tiap kuartal
                m[key]["contract"] = "NQ=F (" + fc["code"] + ")"
                m[key]["tv"] = fc["tv"]; m[key]["expiry"] = fc["expiry"]
                mark(key, True)
                continue
            m[key] = _quote(sym)
            mark(key, True)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {sym}: {e}", file=sys.stderr); mark(key, False, e)
    try:
        hb = _series(_yahoo("NQ=F", "5d", "1h"))
        m["nq_hourly"] = [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r.get("v")} for r in hb[-120:]]
        mark("nq_hourly", True)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] NQ=F per jam: {e}", file=sys.stderr); mark("nq_hourly", False, e)
    hx = {}
    for nm, sym in (("ES", "ES=F"), ("DXY", "DX-Y.NYB"), ("TNX", "^TNX"), ("SOX", "^SOX")):
        try:
            hx[nm] = {r["t"]: r["c"] for r in _series(_yahoo(sym, "5d", "1h")) if r.get("c")}
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {sym} per jam: {e}", file=sys.stderr)
    if hx:
        m["hourly_x"] = hx
    try:
        a = fetch_alpaca("QQQ")
        if a:
            m["qqq"] = a
            mark("alpaca", True)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Alpaca: {e}", file=sys.stderr); mark("alpaca", False, e)
    try:
        m["mag7_earnings"] = fetch_mag7_earnings()
        mark("mag7_earnings", True)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] kalender laba Mag7: {e}", file=sys.stderr); mark("mag7_earnings", False, e)
    try:
        spot = (m.get("qqq_y") or {}).get("last")
        m["options"] = analyze_options(fetch_option_chain("QQQ"), spot, (m.get("nq_fut") or {}).get("last"), now)
        mark("options", bool(m["options"]), None if m["options"] else "OI kosong atau rantai tidak lengkap")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] opsi: {e}", file=sys.stderr); mark("options", False, e)
    if "yield10" in m and "change_bps" not in m["yield10"] and m["yield10"].get("prev"):
        m["yield10"]["change_bps"] = round((m["yield10"]["last"] - m["yield10"]["prev"]) * 100, 1)
    return m


# ---------------------------------------------------------------------------
# Berita
# ---------------------------------------------------------------------------
REGIME_MAP = {
    "Data kerja lemah menurunkan peluang hike": {"rate_fear": 2, "growth_fear": -1},
    "Kekhawatiran ekonomi": {"rate_fear": 1, "growth_fear": -2},
    "Data ekonomi kuat": {"rate_fear": -2, "growth_fear": 1},
}


def detect_regime(market):
    """Rezim pasar dari level dan tren yield 10 tahun (FedWatch tidak punya API gratis, yield adalah pendekatannya).
    rate_fear: pasar takut suku bunga tinggi -> kabar ekonomi lemah dianggap baik (peluang hike turun).
    growth_fear: pasar takut pertumbuhan -> kabar lemah dianggap buruk."""
    y = market.get("yield10") or {}
    last, ch5 = y.get("last"), y.get("change5_bps")
    if last is None:
        return {"name": "normal", "label": "Rezim netral (data yield tidak tersedia)", "why": ""}
    if last >= 4.5 or (ch5 is not None and ch5 >= 15 and last >= 4.0):
        return {"name": "rate_fear", "label": "Rezim sensitif suku bunga",
                "why": f"Yield 10 tahun {last:.2f}%" + (f" ({ch5:+.0f} bps dalam 5 sesi)" if ch5 is not None else "") +
                       ". Kabar ekonomi lemah dibaca baik (mengurangi peluang hike), kabar kuat dibaca buruk."}
    if last < 3.9 or (ch5 is not None and ch5 <= -20 and last < 4.3):
        return {"name": "growth_fear", "label": "Rezim cemas pertumbuhan",
                "why": f"Yield 10 tahun {last:.2f}%" + (f" ({ch5:+.0f} bps dalam 5 sesi)" if ch5 is not None else "") +
                       ". Kabar ekonomi lemah dibaca buruk (resesi), kabar kuat dibaca baik."}
    return {"name": "normal", "label": "Rezim netral", "why": f"Yield 10 tahun {last:.2f}% tidak ekstrem."}


def apply_regime(score, hits, regime):
    """Balikkan atau sesuaikan bobot kabar ekonomi sesuai rezim. Mengembalikan skor baru dan hits yang disesuaikan."""
    if regime["name"] == "normal":
        return score, hits
    out, delta = [], 0
    for h in hits:
        m = REGIME_MAP.get(h["label"])
        if m and regime["name"] in m and m[regime["name"]] != h["score"]:
            delta += m[regime["name"]] - h["score"]
            h = {**h, "score": m[regime["name"]],
                 "why": h["why"] + " Penyesuaian rezim: " + regime["label"].lower() + ", sehingga bobot dan arahnya disesuaikan."}
        out.append(h)
    if not delta:
        return score, out
    return round(score + delta, 2), out


MACRO_RX = re.compile(r"\b(jobs|payrolls?|nonfarm|employment|unemployment|jobless|retail sales|gdp|cpi|ppi|pce|inflation)\b")


def score_headline(title):
    """Mengembalikan (skor, daftar entri kamus yang cocok). Skor positif = bullish."""
    t = NEGATED_HIKE.sub(" ", title.lower())
    t = NEGATED_CUT.sub(" no_cut ", t)
    hits = [e for e in LEXICON if e["rx"].search(t)]
    if MACRO_RX.search(t):  # berita data makro: kata "beat/miss" bukan soal laba perusahaan
        hits = [e for e in hits if not e["label"].startswith("Earnings")]
    raw = clamp(sum(e["score"] for e in hits), -4, 4)
    mult = 1.0
    for pat, m in LEVERAGE:
        if re.search(pat, t):
            mult = max(mult, m)
    hits.sort(key=lambda e: -abs(e["score"]))
    return round(raw * mult, 2), hits


def credibility(src):
    s = (src or "").lower()
    if any(k in s for k in MAJOR):
        return 1.0
    if any(k in s for k in MID):
        return 0.85
    return 0.65


def tokens(title):
    stop = {"that", "with", "from", "this", "will", "after", "amid", "over", "into", "stocks", "stock",
            "shares", "says", "their", "have", "than", "about", "more", "as", "the"}
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3 and w not in stop}


def theme_of(title, hits):
    t = title.lower()
    for name, pat in THEMES:
        if re.search(pat, t):
            return name
    return "Sentimen pasar"


def analyze_news(raw, now, regime=None):
    items = []
    seen = set()
    for r in raw:
        key = re.sub(r"\W+", " ", r["title"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        s, hits = score_headline(r["title"])
        if regime:
            s, hits = apply_regime(s, hits, regime)
        pub = parse_time(r.get("published"))
        age = max(0.0, (now - pub).total_seconds() / 3600) if pub else 12.0
        cred = credibility(r.get("source"))
        rec = max(0.2, 0.5 ** (age / 10))
        items.append({**r, "score": s, "hits": hits, "age_h": round(age, 1), "cred": cred,
                      "quality": cred * rec, "tok": tokens(r["title"]), "norm": re.sub(r"\W+", " ", r["title"].lower()).strip(), "pub": pub})
    # gabungkan berita kembar (kejadian yang sama diberitakan banyak sumber)
    items.sort(key=lambda i: -i["quality"])
    clusters = []
    for it in items:
        for c in clusters:
            inter = len(it["tok"] & c["rep"]["tok"])
            union = len(it["tok"] | c["rep"]["tok"]) or 1
            jac = inter / union
            ratio = difflib.SequenceMatcher(None, it["norm"], c["rep"]["norm"]).ratio()
            if (jac >= 0.45 and (it["score"] >= 0) == (c["rep"]["score"] >= 0)) or jac >= 0.7 or ratio >= 0.8:
                c["members"].append(it)
                if abs(it["score"]) > abs(c["rep"]["score"]) and it["quality"] >= 0.6 * c["rep"]["quality"]:
                    c["rep"] = it
                break
        else:
            clusters.append({"rep": it, "members": [it]})
    out = []
    for c in clusters:
        rep, n = c["rep"], len(c["members"])
        mult = min(1.5, 1 + 0.15 * (n - 1))
        w = rep["quality"] * mult
        out.append({"rep": rep, "n": n, "mult": mult, "weight": w, "impact": w * rep["score"], "members": c["members"]})
    return out


def news_stats(clusters):
    scored = [c for c in clusters if c["rep"]["score"] != 0]
    den = sum(c["weight"] for c in scored)
    avg = sum(c["impact"] for c in scored) / den if den else 0.0
    if len(scored) < 4:
        avg *= len(scored) / 4
    bull = sum(c["weight"] for c in scored if c["rep"]["score"] > 0)
    bear = sum(c["weight"] for c in scored if c["rep"]["score"] < 0)
    return {"avg": round(avg, 3), "n_scored": len(scored), "n_total": len(clusters),
            "bull_share": round(bull / (bull + bear), 3) if bull + bear else 0.5}


def news_items_out(clusters, limit=40):
    out = []
    for c in sorted(clusters, key=lambda c: -abs(c["impact"]))[:limit]:
        rep, n = c["rep"], c["n"]
        s = rep["score"]
        hits = rep["hits"]
        fed_item = bool(FED_RX.search(rep["title"].lower())) and fed_tone(hits) != 0
        who = []
        for pat, tk in TICKERS:
            if re.search(pat, rep["title"].lower()) and tk not in who:
                who.append(tk)
        for h in hits:
            for tk in h["who"]:
                if tk not in who:
                    who.append(tk)
        strength = "kuat" if abs(s) >= 3 else "sedang" if abs(s) >= 1.5 else "lemah"
        small = abs(s) >= 2 and rep["cred"] < 1.0 and n == 1
        out.append({
            "title": rep["title"], "link": rep.get("link", ""), "source": rep.get("source", ""),
            "published": rep["pub"].isoformat(timespec="minutes") if rep["pub"] else None,
            "age_h": rep["age_h"], "score": s, "weighted": round(c["impact"], 2),
            "direction": "bullish" if s > 0 else "bearish" if s < 0 else "netral", "strength": strength,
            "theme": theme_of(rep["title"], hits), "fed_speak": fed_item, "sources_count": n,
            "sources": sorted({m.get("source") for m in c["members"] if m.get("source")}),
            "labels": [h["label"] for h in hits],
            "impacts": [{"label": h["label"], "direction": "bullish" if h["score"] > 0 else "bearish",
                         "why": h["why"]} for h in hits[:2]],
            "who": who[:6], "horizon": hits[0]["horizon"] if hits else None,
            "small": small,
            "small_note": ("Hanya satu sumber non-utama yang memuat pola berdampak kuat ini. Bisa jadi sinyal dini, "
                           "tetapi belum terkonfirmasi, jadi bobotnya diturunkan.") if small else None,
            # kompatibel dengan dashboard versi 1
            "why": " · ".join(h["label"] for h in hits) if hits else "Tidak ada pola dampak yang dikenali",
            "tags": [h["label"] for h in hits], "theme_v1": None,
        })
    return out


# ---------------------------------------------------------------------------
# Faktor pasar dan vonis
# ---------------------------------------------------------------------------
def build_factors(ns, market):
    F = []

    def add(key, signal, detail, note=None):
        name, w, general, bear, bull = FINFO[key]
        F.append({"key": key, "name": name, "weight": w,
                  "signal": None if signal is None else round(float(signal), 2),
                  "detail": detail, "what": general, "why_bear": bear, "why_bull": bull, "note": note})

    n = ns["n_scored"]
    add("news", clamp(ns["avg"] / 1.2, -2, 2),
        f"skor rata-rata {fnum(ns['avg'], 2, True)} dari {n} kejadian bernilai; {round(ns['bull_share'] * 100)}% bobot bullish")

    def pct_factor(key, label, scale):
        q = market.get(key)
        if not q or q.get("change_pct") is None:
            add(key, None, "data tidak tersedia")
            return
        add(key, clamp(q["change_pct"] / scale, -2, 2),
            f"{fnum(q['change_pct'], 2, True)}% dari penutupan sebelumnya ({fnum(q['last'], 2)})")

    pct_factor("nq_fut", "NQ", 0.5)
    pct_factor("es_fut", "ES", 0.4)
    pct_factor("sox", "SOX", 1.0)
    nq_pct = (market.get("nq_fut") or {}).get("change_pct")

    y = market.get("yield10")
    if y and y.get("change_bps") is not None:
        sig = clamp(-y["change_bps"] / 4, -2, 2)
        note = None
        if y["last"] >= 5.0:
            sig -= 0.5
        elif y["last"] <= 4.0:
            sig += 0.5
        if sig > 0 and nq_pct is not None and nq_pct <= -1:
            sig *= 0.4
            note = "Yield turun karena investor lari ke aset aman saat saham jatuh, jadi sinyal positifnya dikurangi."
        add("yield", clamp(sig, -2, 2),
            f"{fnum(y['last'], 3)}% ({fnum(y['change_bps'], 1, True)} bp dari sesi sebelumnya)", note)
    else:
        add("yield", None, "data tidak tersedia")

    v = market.get("vix")
    if v and v.get("last") is not None:
        lv = v["last"]
        sig = 1.0 if lv <= 14 else 0.5 if lv <= 18 else -0.5 if lv <= 22 else -1.2 if lv <= 28 else -2.0
        c = v.get("change_pct")
        if c is not None:
            sig += -0.5 if c >= 10 else 0.5 if c <= -10 else 0
        vxn = market.get("vxn") or {}
        extra = f"; VXN (Nasdaq-100) {fnum(vxn['last'], 2)}" if vxn.get("last") else ""
        add("vix", clamp(sig, -2, 2), f"{fnum(lv, 2)} ({fnum(c, 1, True)}% dari sesi sebelumnya){extra}")
    else:
        add("vix", None, "data tidak tersedia")

    b = market.get("brent")
    if b and b.get("last") is not None:
        sig = clamp(-(b.get("change_pct") or 0) / 2, -1.5, 1.5)
        sig += -0.5 if b["last"] > 100 else 0.5 if b["last"] < 75 else 0
        add("oil", clamp(sig, -2, 2),
            f"${fnum(b['last'], 2)} ({fnum(b.get('change_pct'), 2, True)}% dari sesi sebelumnya)")
    else:
        add("oil", None, "data tidak tersedia")

    pct_factor("dxy", "DXY", 0.4)

    nas = market.get("nasdaq")
    if nas and nas.get("ret5_pct") is not None:
        dd = (1 - nas["last"] / nas["high52"]) * 100 if nas.get("high52") else 0
        sig = clamp(nas["ret5_pct"] / 1.5, -1.5, 1.5) - (0.5 if dd > 10 else 0)
        add("trend", clamp(sig, -2, 2),
            f"{fnum(nas['ret5_pct'], 2, True)}% dalam 5 sesi; {fnum(dd, 1)}% di bawah puncak 52 minggu")
    else:
        add("trend", None, "data tidak tersedia")

    F.sort(key=lambda f: FACTOR_ORDER.index(f["key"]))
    return F


def upcoming_events(now, extra=None, limit=8):
    out = list(extra or [])
    has_aapl = any("(AAPL)" in e["name"] for e in out)
    for date, hhmm, name, impact, confirmed, why in EVENTS:
        start = et_to_utc(date, hhmm or "09:00")
        end_of_day = et_to_utc(date, "23:59")
        if end_of_day < now or (has_aapl and "Apple" in name):
            continue
        out.append({"date": date, "start_utc": start.isoformat(timespec="minutes"), "time_known": hhmm is not None,
                    "name": name, "impact": impact, "confirmed": confirmed, "why": why,
                    "hours_until": round((start - now).total_seconds() / 3600, 1)})
    out.sort(key=lambda e: e["start_utc"])
    return out[:limit]


PRIOR_A, PRIOR_B = 0.16, 0.16
CAL_MIN_N = 30


def calibrate(entries):
    """Kalibrasi Platt dengan prior: cocokkan P(naik) = sigmoid(a + b*skor) ke hasil nyata.
    Aktif hanya bila sudah ada CAL_MIN_N hari dinilai; di bawah itu tetap memakai parameter heuristik."""
    data = [(e["score10"], 1.0 if e["outcome"]["change_pct"] > 0 else 0.0) for e in entries
            if e.get("outcome") and not e.get("late") and e.get("score10") is not None]
    n = len(data)
    info = {"active": False, "n": n, "min_n": CAL_MIN_N, "a": PRIOR_A, "b": PRIOR_B,
            "brier": None, "brier_naive": None, "base_rate": None}
    if n:
        base = sum(y for _, y in data) / n
        info["base_rate"] = round(base, 3)
        info["brier_naive"] = round(sum((base - y) ** 2 for _, y in data) / n, 4)
        ps = [clamp(1 / (1 + math.exp(-(PRIOR_A + PRIOR_B * x))), 0.18, 0.82) for x, _ in data]
        info["brier"] = round(sum((p - y) ** 2 for p, (_, y) in zip(ps, data)) / n, 4)
    first = min((e["date"] for e in entries if e.get("outcome") and not e.get("late")), default=None)
    last = max((e["date"] for e in entries if e.get("outcome") and not e.get("late")), default=None)
    info.update(first_date=first, last_date=last)
    if n < CAL_MIN_N:
        return info
    la, lb = 5.0, 200.0  # kekuatan prior: menahan parameter dekat nilai awal saat data masih sedikit
    a, b = PRIOR_A, PRIOR_B
    for _ in range(50):
        ga, gb = la * (a - PRIOR_A), lb * (b - PRIOR_B)
        haa, hab, hbb = la, 0.0, lb
        for x, y in data:
            p = 1 / (1 + math.exp(-(a + b * x)))
            ga += (p - y); gb += (p - y) * x
            w = p * (1 - p)
            haa += w; hab += w * x; hbb += w * x * x
        det = haa * hbb - hab * hab
        if det <= 1e-9:
            break
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-7:
            break
    a, b = clamp(a, -0.6, 0.6), clamp(b, 0.0, 0.5)  # tanda negatif dilarang: skor bearish tidak boleh menaikkan peluang
    ps = [clamp(1 / (1 + math.exp(-(a + b * x))), 0.18, 0.82) for x, _ in data]
    info.update(active=True, a=round(a, 4), b=round(b, 4),
                brier_fit=round(sum((p - y) ** 2 for p, (_, y) in zip(ps, data)) / n, 4))
    return info


def compose(factors, events, now, calib=None, structure=None, fed_overlay=0.0, market=None, cot_an=None):
    avail = [f for f in factors if f["signal"] is not None]
    wsum = sum(f["weight"] for f in factors) or 1  # faktor hilang menyumbang 0, skor menyusut ke netral
    for f in factors:
        f["contribution"] = round(f["signal"] * f["weight"] / wsum * 5, 2) if f["signal"] is not None else None
    score10 = round(sum(f["contribution"] for f in avail), 2)
    base_score = score10
    score10 = round(clamp(score10 + fed_overlay, -10, 10), 2)
    pre_gate = score10
    score10, gates = apply_gates(score10, market or {}, cot_an)
    breaker = {"active": False}
    if structure and structure.get("available"):
        if score10 >= 5 and structure["bearish"]:
            breaker = {"active": True, "raw_score": score10, "side": "bullish_dibatalkan",
                       "reason": "Bias bullish kuat, tetapi struktur harga NQ berbalik: " + " ".join(structure["notes"][:2])}
            score10 = 2.0
        elif score10 <= -5 and structure["bullish"]:
            breaker = {"active": True, "raw_score": score10, "side": "bearish_dibatalkan",
                       "reason": "Bias bearish kuat, tetapi struktur harga NQ berbalik: " + " ".join(structure["notes"][:2])}
            score10 = -2.0
    ca, cb = (calib["a"], calib["b"]) if calib and calib.get("active") else (PRIOR_A, PRIOR_B)
    p_up = clamp(1 / (1 + math.exp(-(ca + cb * score10))), 0.18, 0.82)

    side = 1 if score10 >= 0 else -1
    active = [f for f in avail if abs(f["signal"]) >= 0.25]
    aw = sum(f["weight"] for f in active)
    agree = sum(f["weight"] for f in active if f["signal"] * side > 0) / aw if aw else 0.5

    penalties = []
    soon = [e for e in events if e["impact"] == "tinggi" and -2 <= e["hours_until"] <= 24]
    if soon:
        penalties.append((min(15, 8 * len(soon)), "Ada rilis berdampak tinggi dalam 24 jam: " + ", ".join(e["name"] for e in soon)))
    vix = next((f for f in factors if f["key"] == "vix"), None)
    if vix and vix["signal"] is not None and vix["signal"] <= -1.2:
        penalties.append((5, "Volatilitas tinggi membuat arah lebih mudah berbalik"))
    if len(avail) < 5:
        penalties.append((15, "Data harga tidak lengkap, hanya " + str(len(avail)) + " dari 9 faktor tersedia"))
    conf = 30 + 45 * agree + 15 * min(1, abs(score10) / 6) + 5 * min(1, len(avail) / 9) - sum(p for p, _ in penalties)
    conf = int(clamp(round(conf), 20, 80))

    if score10 >= 5:
        bias = "BULLISH"
    elif score10 >= 1.5:
        bias = "BULLISH TIPIS"
    elif score10 > -1.5:
        bias = "NETRAL"
    elif score10 > -5:
        bias = "BEARISH TIPIS"
    else:
        bias = "BEARISH"
    return {"bias": bias, "score10": score10, "p_up": round(p_up, 3), "p_down": round(1 - p_up, 3),
            "confidence_pct": conf, "agreement_pct": round(agree * 100),
            "factors_available": len(avail), "factors_total": len(factors),
            "warnings": [t for _, t in penalties] + ([breaker["reason"] + f" Skor dipangkas dari {breaker['raw_score']:+.1f} menjadi {score10:+.1f}."] if breaker["active"] else [])
                        + [g["note"] for g in gates if g["active"]],
            "breaker": breaker, "base_score": base_score, "fed_overlay": round(fed_overlay, 2),
            "pre_gate_score": pre_gate, "gates": gates}


def news_feed_out(clusters, limit=150):
    """Semua berita (termasuk yang netral), terbaru di atas, untuk halaman berita."""
    out = []
    for c in clusters:
        rep = c["rep"]
        pubs = [m["pub"] for m in c["members"] if m.get("pub")]
        pub = max(pubs) if pubs else None
        top = max(rep["hits"], key=lambda h: abs(h["score"]), default=None)
        out.append({"title": rep["title"], "source": rep.get("source"), "link": rep.get("link"),
                    "published": pub.isoformat(timespec="minutes") if pub else None,
                    "score": rep["score"], "direction": "bullish" if rep["score"] > 0 else "bearish" if rep["score"] < 0 else "netral",
                    "sources": sorted({m.get("source") for m in c["members"] if m.get("source")}),
                    "theme": theme_of(rep["title"], rep["hits"]),
                    "label": top["label"] if top else None, "why": top["why"] if top else None,
                    "fed": bool(FED_RX.search(rep["title"].lower()))})
    out.sort(key=lambda x: x["published"] or "", reverse=True)
    return out[:limit]


def build_forecast(market, levels, verdict, factors, clusters_out):
    """Proyeksi harga NQ dari skor faktor. Heuristik: skor +10 setara geseran +0,8 sigma harian.
    Bukan ramalan pasti; dipakai untuk memberi gambaran arah, besaran, dan rentang."""
    nq, nas = market.get("nq_fut"), market.get("nasdaq")
    ref_src = nq if nq else nas
    if not ref_src or not levels:
        return None
    ref = ref_src["last"]
    sigma_pct = levels["sigma_pct"]
    sig_pts = ref * sigma_pct / 100
    per_point = sig_pts * 0.08
    mu = clamp(per_point * verdict["score10"], -0.9 * sig_pts, 0.9 * sig_pts)
    def hz(label, days, mult):
        sg = sig_pts * math.sqrt(days)
        m = mu * mult
        return {"label": label, "exp_pts": round(m, 1), "exp_pct": round(m / ref * 100, 2),
                "target": round(ref + m, 1), "low": round(ref + m - sg, 1), "high": round(ref + m + sg, 1),
                "sigma_pct": round(sigma_pct * math.sqrt(days), 2)}
    fac = next((f for f in factors if f["key"] == "news"), None)
    news_mu = per_point * fac["contribution"] if fac and fac["contribution"] is not None else 0.0
    tot = sum(abs(i["weighted"]) for i in clusters_out if i["score"]) or 1
    moves = sorted(({"title": i["title"], "pts": round(i["weighted"] / tot * sig_pts * 0.24, 1), "direction": i["direction"],
                     "horizon": i.get("horizon")} for i in clusters_out if i["score"]),
                   key=lambda x: -abs(x["pts"]))[:6]
    return {"ref": round(ref, 2), "ref_name": nq.get("contract") if nq else "Nasdaq Composite",
            "sigma_pct": sigma_pct, "p_up": verdict["p_up"], "news_pts": round(news_mu, 1),
            "total_pts": round(mu, 1),
            "horizons": [hz("Sisa sesi / 1 hari", 1, 1.0), hz("5 sesi ke depan", 5, 2.0)],
            "news_moves": moves,
            "note": "Heuristik: skor +10 setara geseran sekitar 0,8 sigma harian (sigma dari VXN). Rentang = 1 sigma. Dampak berita per item adalah porsi kasar dari sumbangan faktor berita, bukan harga yang dijamin."}


def make_reasons(factors):
    ranked = sorted([f for f in factors if f["signal"] is not None and abs(f["contribution"]) >= 0.3],
                    key=lambda f: -abs(f["contribution"]))[:5]
    out = []
    for f in ranked:
        bull = f["signal"] > 0
        text = f"{f['detail'][0].upper() + f['detail'][1:]}. {f['why_bull'] if bull else f['why_bear']}"
        if f.get("note"):
            text += " " + f["note"]
        out.append({"dir": "bullish" if bull else "bearish", "title": f["name"], "text": text,
                    "contribution": f["contribution"]})
    return out


def build_levels(market, now):
    nas = market.get("nasdaq")
    if not nas or not nas.get("bars"):
        return None
    today = to_et(now).date().isoformat()
    done = [b for b in nas["bars"] if b["d"] < today or (b["d"] == today and now >= et_to_utc(today, "16:00"))]
    if not done:
        return None
    b = done[-1]
    h, l, c = b["h"], b["l"], b["c"]
    p = (h + l + c) / 3
    r1, s1 = 2 * p - l, 2 * p - h
    r2, s2 = p + (h - l), p - (h - l)
    vxn = (market.get("vxn") or {}).get("last")
    vix = (market.get("vix") or {}).get("last")
    if vxn:
        sigma, src = vxn / math.sqrt(252), "VXN"
    elif vix:
        sigma, src = vix * 1.25 / math.sqrt(252), "VIX x 1,25"
    else:
        sigma, src = None, None
    fut = (market.get("nq_fut") or {}).get("change_pct")
    out = {"basis_date": b["d"], "close": round(c, 2), "high": round(h, 2), "low": round(l, 2),
           "pivot": round(p, 2), "r1": round(r1, 2), "r2": round(r2, 2), "s1": round(s1, 2), "s2": round(s2, 2),
           "last": nas["last"]}
    if sigma:
        out.update(sigma_pct=round(sigma, 2), sigma_source=src,
                   band_low=round(c * (1 - sigma / 100), 2), band_high=round(c * (1 + sigma / 100), 2))
    if fut is not None:
        out.update(implied_open=round(c * (1 + fut / 100), 2), implied_open_pct=fut)
    return out


def make_scenarios(market, levels, events, factors):
    bull, bear = [], []
    nq = (market.get("nq_fut") or {}).get("change_pct")
    if nq is not None:
        bull.append(f"Futures Nasdaq-100 memangkas penurunan atau berbalik naik sebelum pembukaan AS (sekarang {fnum(nq, 2, True)}%).")
    y = market.get("yield10")
    if y and y.get("last"):
        bull.append(f"Yield 10 tahun turun di bawah {fnum(y['last'] - 0.05, 2)}% bukan karena lari ke aset aman.")
        if y["last"] < 5.0:
            bear.append("Yield 10 tahun menembus 5,00%, level yang menekan valuasi saham growth.")
        else:
            bear.append(f"Yield 10 tahun naik lebih jauh dari {fnum(y['last'], 2)}%.")
    v = (market.get("vix") or {}).get("last")
    if v:
        bull.append("VIX turun di bawah 17 dan tetap di sana saat sesi berjalan." if v > 17 else "VIX tetap di bawah 17.")
        bear.append("VIX naik di atas 22, tanda investor mulai membeli perlindungan secara agresif." if v < 22 else f"VIX bertahan di atas {fnum(v, 0)}.")
    if levels:
        bull.append(f"Nasdaq menutup di atas pivot {fnum(levels['pivot'], 0)}, lalu menguji {fnum(levels['r1'], 0)}.")
        bear.append(f"Nasdaq menutup di bawah {fnum(levels['s1'], 0)}, membuka jalan ke {fnum(levels['s2'], 0)}.")
    b = (market.get("brent") or {}).get("last")
    if b:
        bear.append(f"Brent naik di atas ${fnum(b + 5, 0)} karena eskalasi Timur Tengah.")
    bull.append("Tidak ada berita baru soal penerbitan saham atau utang besar untuk membiayai belanja AI.")
    nxt = next((e for e in events if e["impact"] == "tinggi" and e["hours_until"] > -2), None)
    if nxt:
        bear.append(f"{nxt['name']} lebih panas dari perkiraan.")
    return {"bull": bull[:5], "bear": bear[:5]}


# ---------------------------------------------------------------------------
# Rekam jejak
# ---------------------------------------------------------------------------
def load_history(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("entries", []) if isinstance(d, dict) else []
    except Exception:  # noqa: BLE001
        return []


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("state", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def load_calib_log(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("calib_log", [])
    except Exception:  # noqa: BLE001
        return []


def update_history(entries, now, verdict, bars):
    """Simpan prediksi pra-buka (dibekukan saat bursa buka) dan evaluasi sesi yang sudah selesai."""
    T = session_date(now).isoformat()
    opened = now >= et_to_utc(T, "09:30")
    pred = {"bias": verdict["bias"], "score10": verdict["score10"], "p_up": verdict["p_up"],
            "at": now.isoformat(timespec="minutes")}
    e = next((x for x in entries if x["date"] == T), None)
    if e is None:
        entries.append({"date": T, **pred, "late": opened})
    elif not opened and not e.get("outcome"):
        e.update(pred)
    byd = {b["d"]: b for b in bars or []}
    ds = sorted(byd)
    for e in entries:
        if e.get("outcome") or e["date"] not in byd or now < et_to_utc(e["date"], "16:05"):
            continue
        i = ds.index(e["date"])
        if i == 0:
            continue
        chg = (byd[e["date"]]["c"] / byd[ds[i - 1]]["c"] - 1) * 100
        e["outcome"] = {"change_pct": round(chg, 2), "close": round(byd[e["date"]]["c"], 2)}
        e["hit"] = None if e["bias"] == "NETRAL" else ((e["p_up"] > 0.5) == (chg > 0))
    cutoff = (now.date() - dt.timedelta(days=7)).isoformat()
    entries[:] = [e for e in entries if e.get("outcome") or e["date"] >= cutoff]
    entries.sort(key=lambda x: x["date"])
    del entries[:-2000]  # simpan bertahun-tahun; kalibrasi memakai seluruh riwayat
    return entries


def track_record(entries):
    scored = [e for e in entries if e.get("outcome") and not e.get("late") and e.get("hit") is not None]
    hits = sum(1 for e in scored if e["hit"])
    recent = [e for e in entries if e.get("outcome")][-10:]
    return {"n": len(scored), "hits": hits, "hit_rate": round(hits / len(scored), 3) if scored else None,
            "min_n": 30,
            "recent": [{"date": e["date"], "bias": e["bias"], "p_up": e["p_up"], "late": e.get("late", False),
                        "change_pct": e["outcome"]["change_pct"], "hit": e.get("hit")} for e in recent],
            "pending": next(({"date": e["date"], "bias": e["bias"], "p_up": e["p_up"]}
                             for e in reversed(entries) if not e.get("outcome")), None)}


# ---------------------------------------------------------------------------
# Keluaran
# ---------------------------------------------------------------------------
def session_info(now):
    T = session_date(now).isoformat()
    open_utc = et_to_utc(T, "09:30")
    phase = "berjalan" if now >= open_utc else "pra-buka"
    wd = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][dt.date.fromisoformat(T).weekday()]
    y, mo, dd = T.split("-")
    return {"date": T, "phase": phase, "open_wib": open_utc.astimezone(WIB).strftime("%H:%M"),
            "label": f"{wd} {int(dd)} {MONTHS[int(mo) - 1]}"}


def llm_summary(report):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("[info] paket anthropic belum terpasang, ringkasan LLM dilewati", file=sys.stderr)
        return None
    v = report["verdict"]
    fac = "\n".join(f"- {f['name']}: {f['detail']} (sinyal {f['signal']})" for f in report["factors"] if f["signal"] is not None)
    top = "\n".join(f"- ({i['score']:+}) {i['title']}" for i in report["items"][:12])
    prompt = (
        "Kamu analis fundamental Nasdaq. Tulis kesimpulan hari ini dalam Bahasa Indonesia, maksimal 200 kata: "
        f"bias {v['bias']} (peluang naik {round(v['p_up'] * 100)}%), 3 sampai 4 alasan utama dengan mekanisme sebab akibatnya, "
        "apa yang bisa membalik bias, dan 2 risiko. Utamakan berita kecil yang berdampak besar. "
        "Jangan memberi nasihat beli atau jual.\n\nFAKTOR:\n" + fac + "\n\nBERITA:\n" + top)
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(model="claude-sonnet-5-5", max_tokens=700,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "text", None))


def write_markdown(r, path):
    v = r["verdict"]
    L = [f"# Bias Nasdaq: {v['bias']}",
         f"_Diperbarui {r['generated_at']} · sesi {r['session']['label']} ({r['session']['phase']})_", "",
         f"Peluang indeks naik **{round(v['p_up'] * 100)}%**, turun **{round(v['p_down'] * 100)}%** "
         f"(skor {fnum(v['score10'], 1, True)} dari ±10, keyakinan {v['confidence_pct']}%, "
         f"{v['agreement_pct']}% bobot faktor searah). Estimasi " + ("terkalibrasi dari hasil nyata." if v.get("calibrated") else "heuristik, belum dikalibrasi."), ""]
    if v["warnings"]:
        L += ["**Catatan:** " + "; ".join(v["warnings"]), ""]
    if r.get("llm_summary"):
        L += ["## Ringkasan analis", r["llm_summary"], ""]
    L += ["## Alasan utama"] + [f"- **{x['title']}** ({fnum(x['contribution'], 2, True)}): {x['text']}" for x in r["reasons"]]
    L += ["", "## Papan faktor", "| Faktor | Nilai | Sinyal | Bobot | Kontribusi |", "| --- | --- | --- | --- | --- |"]
    for f in r["factors"]:
        L.append(f"| {f['name']} | {f['detail']} | {fnum(f['signal'], 2, True)} | {f['weight']}% | {fnum(f['contribution'], 2, True)} |")
    lv = r.get("levels")
    if lv:
        L += ["", "## Level hari ini",
              f"Pivot {fnum(lv['pivot'], 0)}; resistensi {fnum(lv['r1'], 0)} / {fnum(lv['r2'], 0)}; "
              f"support {fnum(lv['s1'], 0)} / {fnum(lv['s2'], 0)}."]
        if lv.get("implied_open"):
            L.append(f"Perkiraan pembukaan dari futures: {fnum(lv['implied_open'], 0)} ({fnum(lv['implied_open_pct'], 2, True)}%).")
        if lv.get("band_low"):
            L.append(f"Rentang wajar 1σ ({lv['sigma_source']}): {fnum(lv['band_low'], 0)} sampai {fnum(lv['band_high'], 0)}.")
    L += ["", "## Berita paling berdampak"]
    for i in r["items"][:12]:
        why = i["impacts"][0]["why"] if i["impacts"] else i["why"]
        L.append(f"- **{fnum(i['weighted'], 1, True)}** {i['title']} ({i['source']}). {why}")
    L += ["", "## Kalender risiko"] + [f"- {e['date']}: {e['name']} ({e['impact']})" for e in r["events"]]
    tr = r["track_record"]
    L += ["", "## Rekam jejak",
          f"{tr['hits']} benar dari {tr['n']} prediksi terevaluasi." if tr["n"] else "Belum ada prediksi yang terevaluasi.",
          "", "_Bukan nasihat investasi._"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def _side(bias):
    return 1 if "BULL" in bias else -1 if "BEAR" in bias else 0


def check_alert(state, verdict, now, top_title=None):
    """Bandingkan dengan keadaan laporan sebelumnya. Peringatan bila bias berbalik arah (bullish<->bearish),
    atau skor bergeser >=4 poin dalam <=3 jam. Jeda minimal 30 menit antar peringatan."""
    prev = state.get("last")
    cur = {"bias": verdict["bias"], "score10": verdict["score10"], "at": now.isoformat(timespec="seconds")}
    alert = None
    if prev:
        pt = parse_time(prev["at"])
        hrs = (now - pt).total_seconds() / 3600 if pt else 99
        flip = _side(prev["bias"]) * _side(cur["bias"]) == -1
        jump = abs(cur["score10"] - prev["score10"]) >= 4 and hrs <= 3
        last_alert = parse_time(state.get("last_alert_at") or "")
        cooled = not last_alert or (now - last_alert).total_seconds() >= 1800
        if (flip or jump) and cooled:
            alert = {"at": cur["at"], "from": prev["bias"], "to": cur["bias"], "from_score": prev["score10"],
                     "to_score": cur["score10"], "kind": "balik arah" if flip else "geser tajam",
                     "trigger": top_title, "sent": False}
    state["last"] = cur
    return alert


def send_webhook(alert):
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return False
    text = (f"Peringatan bias Nasdaq ({alert['kind']}): {alert['from']} ({alert['from_score']:+.1f}) -> "
            f"{alert['to']} ({alert['to_score']:+.1f})" + (f". Pemicu berita: {alert['trigger']}" if alert.get("trigger") else ""))
    body = json.dumps({"content": text, "text": text}).encode()  # cocok untuk Discord dan Slack
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "nasdaq-agent/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] webhook: {e}", file=sys.stderr)
        return False


def _third_friday(y, m):
    first = dt.date(y, m, 1)
    return first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)


def _corr(a, b):
    n = len(a)
    if n < 20:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def analyze_intermarket(market):
    """Status dan korelasi 24 jam (return per jam) NQ terhadap ES, DXY, US10Y, SOX, plus deteksi divergensi."""
    nq, es, dx = market.get("nq_fut") or {}, market.get("es_fut") or {}, market.get("dxy") or {}
    y10, y5 = market.get("yield10") or {}, market.get("fvx") or {}
    ST = []
    def st(key, tone, label, text):
        ST.append({"key": key, "tone": tone, "label": label, "text": text})
    d = dx.get("change_pct")
    if d is not None:
        if d >= 0.5:
            st("dxy", "bear", "DXY breakout naik", f"Dolar +{abs(d):.2f}%: tekanan turun untuk NQ; sweep likuiditas sering terjadi saat DXY menembus.")
        elif d >= 0.2:
            st("dxy", "bear", "DXY bullish (tekanan turun untuk NQ)", f"Dolar +{d:.2f}%, menekan likuiditas dan laba multinasional.")
        elif d <= -0.5:
            st("dxy", "bull", "DXY breakdown turun", f"Dolar {d:.2f}%: kondisi longgar yang mendukung NQ.")
        elif d <= -0.2:
            st("dxy", "bull", "DXY bearish (mendukung NQ)", f"Dolar {d:.2f}%, kondisi likuiditas lebih longgar.")
        else:
            st("dxy", "neutral", "DXY netral", f"Dolar {d:+.2f}%, tidak memberi arah.")
    for key, nm, q in (("us10y", "US10Y", y10), ("us5y", "US5Y (proksi US2Y)", y5)):
        c = q.get("change_pct") if q else None
        if c is None:
            continue
        if c >= 0.5:
            st(key, "bear", f"{nm} naik tajam", f"Yield {c:+.2f}%: menekan valuasi saham teknologi berdurasi panjang.")
        elif c <= -0.5:
            st(key, "bull", f"{nm} turun tajam", f"Yield {c:+.2f}%: meringankan tekanan valuasi (kecuali turun karena ketakutan resesi).")
        else:
            st(key, "neutral", f"{nm} stabil", f"Yield {c:+.2f}%.")
    n, e = nq.get("change_pct"), es.get("change_pct")
    if n is not None and e is not None:
        diff = n - e
        if n * e < 0 and abs(n) >= 0.15 and abs(e) >= 0.15:
            st("esnq", "warn", "ES/NQ divergence terdeteksi", f"NQ {n:+.2f}% tetapi ES {e:+.2f}%: arah berlawanan, sinyal pasar tidak solid.")
        elif abs(diff) >= 0.4:
            st("esnq", "warn", "NQ lebih " + ("kuat" if diff > 0 else "lemah") + " dari ES", f"Selisih {diff:+.2f} poin persen: pergerakan didorong " + ("sektor teknologi saja." if diff > 0 else "oleh luar teknologi, teknologi tertinggal."))
        else:
            st("esnq", "neutral", "ES dan NQ selaras", f"NQ {n:+.2f}% vs ES {e:+.2f}%.")
    mat = []
    hx = market.get("hourly_x") or {}
    nqh = {b["t"]: b["c"] for b in (market.get("nq_hourly") or []) if b.get("c")}
    def corr_with(other):
        ts = sorted(set(nqh) & set(other))[-49:]
        if len(ts) < 21:
            return None, 0
        a = [math.log(nqh[ts[i]] / nqh[ts[i - 1]]) for i in range(1, len(ts))]
        b = [math.log(other[ts[i]] / other[ts[i - 1]]) for i in range(1, len(ts)) if other[ts[i - 1]] > 0 and other[ts[i]] > 0]
        if len(a) != len(b):
            return None, 0
        return _corr(a, b), len(a)
    for nm, label, exp in (("ES", "NQ vs ES (S&P 500)", 1), ("DXY", "NQ vs DXY", -1), ("TNX", "NQ vs US10Y", -1), ("SOX", "NQ vs SOX (semikonduktor)", 1)):
        c, n_ = corr_with({int(k): v for k, v in (hx.get(nm) or {}).items()})
        if c is None:
            continue
        broken = (c * exp) < -0.3
        mat.append({"pair": label, "corr": round(c, 2), "expected": "positif" if exp > 0 else "negatif", "n": n_,
                    "state": "terbalik dari biasanya" if broken else "normal" if abs(c) >= 0.2 else "lemah"})
        if broken:
            st("corr_" + nm.lower(), "warn", f"Korelasi {nm} terbalik", f"Biasanya {('positif' if exp > 0 else 'negatif')}, 24 jam terakhir {c:+.2f}: hubungan intermarket sedang tidak normal.")
    headwind = (1 if (d or 0) >= 0.2 else -1 if (d or 0) <= -0.2 else 0) + (1 if (y10.get("change_pct") or 0) >= 0.5 else -1 if (y10.get("change_pct") or 0) <= -0.5 else 0)
    summary = ("Gravitasi makro menekan NQ (dolar dan/atau yield naik)" if headwind >= 1 else
               "Gravitasi makro mendukung NQ (dolar dan/atau yield turun)" if headwind <= -1 else "Gravitasi makro netral")
    if not ST and not mat:
        return None
    return {"statuses": ST, "matrix": mat, "summary": summary, "headwind": headwind,
            "note": "Korelasi dihitung dari return per jam 24 jam terakhir; ambang 'terbalik' = berlawanan dengan hubungan normal pada |r| > 0,3."}


def build_entry_warnings(now, verdict, market, levels, structure, smc, vp, options, volume, regime, brd, events, radar, ew, feeds, items, inter=None):
    """Daftar peringatan sebelum entry: apa yang SEDANG terjadi dan apa yang AKAN terjadi di pasar Nasdaq."""
    W = []
    def add(when, sev, cat, title, text, action=None):
        W.append({"when": when, "sev": sev, "cat": cat, "title": title, "text": text, "action": action})
    nq, nas = market.get("nq_fut") or {}, market.get("nasdaq") or {}
    price = nq.get("last")
    bias_s = _side(verdict["bias"])
    et = to_et(now)
    t_min = et.hour * 60 + et.minute
    wd = et.weekday()

    # ---- SEDANG TERJADI ----
    for g in verdict.get("gates", []):
        if g["active"]:
            add("now", "kritis", "Pengunci bias", g["name"] + " aktif", g.get("note", ""), "Skor sudah dipangkas otomatis. Tunggu konfirmasi sebelum entry searah bias awal.")
    if verdict.get("breaker", {}).get("active"):
        add("now", "kritis", "Struktur harga", "Struktur NQ berlawanan dengan bias berita", verdict["breaker"]["reason"], "Jangan kejar arah berita; tunggu harga merebut kembali level yang tertolak.")
    for e in events:
        h = e.get("hours_until")
        if h is None or e.get("impact") != "tinggi":
            continue
        if -0.25 <= h <= 0.25:
            add("now", "kritis", "No-trade window", e["name"] + (" baru saja dirilis" if h <= 0 else " dirilis sebentar lagi"),
                e["why"], "Jendela terlarang: 15 menit sebelum sampai 15 menit sesudah rilis. Spread melebar dan harga melonjak dua arah.")
        elif 0.25 < h <= 3:
            add("now", "waspada", "Rilis data", f"{e['name']} dalam {h:.1f} jam", e["why"], "Entry sekarang berisiko terguncang oleh rilis. Pertimbangkan menunggu hasilnya.")
    vix, vxn = market.get("vix"), market.get("vxn")
    if vix:
        if vix["last"] >= 25:
            add("now", "kritis", "Volatilitas", f"VIX tinggi di {vix['last']:.1f}", "Pasar membayar mahal untuk perlindungan; gerak harian besar dan arah mudah berbalik.", "Perkecil ukuran posisi dan perlebar stop.")
        elif vix["last"] >= 20 or (vix.get("change_pct") or 0) >= 10:
            add("now", "waspada", "Volatilitas", f"VIX {vix['last']:.1f} ({fnum(vix.get('change_pct'), 1, True)}%)", "Volatilitas meningkat; arah lebih mudah berbalik dan sapuan stop lebih sering.", "Jangan memasang stop terlalu ketat.")
    if levels and price:
        rng = price * levels["sigma_pct"] / 100
        add("now", "info", "Rentang normal", f"Rentang gerak 1 sigma hari ini sekitar ±{rng:,.0f} poin NQ",
            f"Sekitar 68% hari bergerak dalam rentang ini ({levels['sigma_source']}, ±{levels['sigma_pct']:.2f}%).", "Stop yang lebih sempit dari sebagian kecil rentang ini mudah tersapu.")
    if nq and abs(nq.get("change_pct") or 0) >= 2:
        add("now", "kritis", "Gap besar", f"Futures NQ bergerak {fnum(nq['change_pct'], 2, True)}% dari penutupan", "Gerak sebesar ini biasanya disertai likuiditas tipis dan spread lebar.", "Tunggu harga mencari keseimbangan sebelum entry.")
    elif nq and bias_s and (nq.get("change_pct") or 0) * bias_s < -0.5:
        add("now", "waspada", "Konflik sinyal", f"Bias {verdict['bias'].lower()} tetapi futures NQ bergerak {fnum(nq['change_pct'], 2, True)}%", "Harga sedang berlawanan dengan kesimpulan berita; pasar belum membenarkan bias.", "Tunggu harga searah dengan bias sebelum entry.")
    if smc and bias_s:
        if bias_s > 0 and smc["zone"] == "premium":
            add("now", "waspada", "Lokasi harga", "Harga di zona premium", f"Harga di {smc['position_pct']}% rentang 48 jam. Long di area mahal berisiko terjepit.", "Lebih baik menunggu pullback ke discount atau equilibrium " + f"{smc['equilibrium']:,.0f}.")
        elif bias_s < 0 and smc["zone"] == "discount":
            add("now", "waspada", "Lokasi harga", "Harga di zona discount", f"Harga di {smc['position_pct']}% rentang 48 jam. Short di area murah berisiko memantul.", "Lebih baik menunggu rally ke equilibrium " + f"{smc['equilibrium']:,.0f}.")
    if options and price:
        for key, nm, bad_for in (("call_wall_nq", "call wall (resistensi opsi)", 1), ("put_wall_nq", "put wall (support opsi)", -1)):
            lv = options.get(key)
            if lv and abs(price - lv) / price <= 0.003 and bias_s == bad_for:
                add("now", "waspada", "Dinding opsi", f"Harga dekat {nm} {lv:,.0f}", "Hedging market maker di level ini sering menahan atau membalikkan harga.", "Beri ruang; jangan entry tepat di bawah/atas dinding.")
    if volume and volume.get("spike"):
        add("now", "waspada", "Volume", "Lonjakan volume terdeteksi", volume["note"], "Volume mendadak sering mendahului pembalikan atau sapuan likuiditas.")
    if structure and structure.get("available"):
        for n in structure["notes"][:2]:
            add("now", "waspada", "Struktur harga", n, "Sweep dan penolakan FVG sering mendahului pembalikan.", None)
    if verdict["agreement_pct"] < 60 or verdict["confidence_pct"] < 45:
        add("now", "waspada", "Keyakinan rendah", f"Faktor tidak sepakat (kesepakatan {verdict['agreement_pct']}%, keyakinan {verdict['confidence_pct']}%)", "Sinyal bercampur, sehingga bias lebih rapuh.", "Kurangi ukuran posisi atau tunggu sinyal lebih searah.")
    miss = [k for k in ("nasdaq", "nq_fut", "vix", "yield10", "nq_hourly") if feeds.get(k) and not feeds[k]["ok"]]
    if miss:
        add("now", "kritis" if "nq_fut" in miss else "waspada", "Data tidak lengkap", "Sumber data gagal: " + ", ".join(miss), "Skor dihitung tanpa data itu sehingga lebih tidak akurat.", "Cek harga langsung di platform Anda.")
    if inter:
        for stt in inter["statuses"]:
            if stt["key"] in ("esnq",) and stt["tone"] == "warn":
                add("now", "waspada", "Intermarket", stt["label"], stt["text"], "Tunggu ES dan NQ kembali searah sebelum entry.")
            elif stt["key"] == "dxy" and "breakout" in stt["label"].lower() and (not structure or structure.get("sweep_low") or structure.get("sweep_high") or True):
                add("now", "waspada", "Intermarket", stt["label"], stt["text"], "Waspadai sweep likuiditas NQ saat DXY bergerak tajam.")
            elif stt["key"].startswith("corr_"):
                add("now", "info", "Intermarket", stt["label"], stt["text"], None)
    if radar and abs(radar.get("overlay", 0)) >= 2:
        add("now", "waspada", "Fed", f"Nada pejabat Fed {'hawkish' if radar['overlay'] < 0 else 'dovish'} (overlay {fnum(radar['overlay'], 1, True)})", "Pernyataan Fed menggeser skor; pasar biasanya bereaksi cepat dan bisa berbalik.", None)
    if ew:
        add("now", "waspada", "Pekan laporan Mag7", "Pekan laporan: " + ", ".join(e["ticker"] for e in ew), "Hasil perusahaan besar lebih menggerakkan indeks daripada data makro; bobot berita makro dikurangi 50%.", "Waspadai gap setelah penutupan saat laporan keluar.")
    if brd and brd["state"] == "menyempit":
        add("now", "waspada", "Breadth", "Pergerakan ditopang segelintir saham", brd["text"], "Reli yang sempit lebih mudah patah.")
    if regime and regime["name"] == "rate_fear":
        add("now", "info", "Rezim pasar", regime["label"], regime["why"], None)
    y, d = (market.get("yield10") or {}).get("change_pct"), (market.get("dxy") or {}).get("change_pct")
    if y is not None and y >= 0.5:
        add("now", "waspada", "Yield", f"Yield 10 tahun naik {fnum(y, 2, True)}%", "Kenaikan yield menekan valuasi saham teknologi.", None)
    if d is not None and d >= 0.5:
        add("now", "waspada", "Dolar", f"Dolar menguat {fnum(d, 2, True)}%", "Dolar kuat menekan likuiditas global dan laba multinasional.", None)
    for it in items[:15]:
        if abs(it["score"]) >= 3 and (it.get("age_h") or 99) <= 2:
            add("now", "waspada", "Berita baru", it["title"], (it["impacts"][0]["why"] if it.get("impacts") else ""), "Berita segar yang kuat sering belum sepenuhnya tercermin di harga.")
            break
    # sesi
    if now.weekday() >= 5 and not (now.weekday() == 6 and et.hour >= 18):
        add("now", "kritis", "Sesi", "Pasar futures sedang tutup (akhir pekan)", "Harga tidak bergerak; pembukaan Minggu malam sering berselisih (gap).", "Tidak ada entry yang bisa dieksekusi sampai pasar buka.")
    elif 9 * 60 + 30 <= t_min < 9 * 60 + 45 and wd < 5:
        add("now", "waspada", "Sesi", "15 menit pertama setelah bursa AS buka", "Volume dan volatilitas memuncak, sering ada sapuan awal.", "Banyak trader menunggu 15–30 menit sebelum entry.")
    elif 15 * 60 + 45 <= t_min < 16 * 60 and wd < 5:
        add("now", "waspada", "Sesi", "15 menit terakhir sebelum penutupan bursa AS", "Order penutupan institusi bisa membuat harga bergerak tajam.", None)
    elif 17 * 60 <= t_min < 18 * 60 and wd < 5:
        add("now", "waspada", "Sesi", "Jeda pemeliharaan harian CME (17:00–18:00 ET)", "Perdagangan futures dijeda; likuiditas tipis di sekitar jam ini.", None)
    elif (t_min < 9 * 60 + 30 or t_min >= 16 * 60) and wd < 5:
        add("now", "info", "Sesi", "Di luar jam bursa AS", "Likuiditas futures lebih tipis dibanding sesi reguler sehingga gerak bisa tidak teratur.", None)
    if wd == 4 and t_min >= 14 * 60 and t_min < 17 * 60:
        add("now", "info", "Akhir pekan", "Menjelang penutupan Jumat", "Posisi yang dibawa ke akhir pekan menanggung risiko gap hari Minggu malam.", None)

    # ---- AKAN TERJADI ----
    for e in events:
        h = e.get("hours_until")
        if h is None or h < 0.5 or h > 72 or (e["impact"] == "rendah"):
            continue
        t = dt.datetime.fromisoformat(e["start_utc"]).astimezone(WIB)
        when = t.strftime("%a %d %b %H:%M").replace("Mon", "Sen").replace("Tue", "Sel").replace("Wed", "Rab").replace("Thu", "Kam").replace("Fri", "Jum").replace("Sat", "Sab").replace("Sun", "Min") if e.get("time_known") else e["date"] + " (jam belum pasti)"
        sev = "waspada" if (e["impact"] == "tinggi" and h <= 24) else "info"
        add("next", sev, "Mag7" if e.get("mag7") else "Jadwal", f"{e['name']} · {when}" + (" WIB" if e.get("time_known") else "") + (f" (dalam {h:.0f} jam)" if h < 48 else ""),
            e["why"], "Hindari entry besar sebelum rilis." if e["impact"] == "tinggi" else None)
    sess_open = et_to_utc(session_date(now).isoformat(), "09:30")
    hrs_open = (sess_open - now).total_seconds() / 3600
    if 0 < hrs_open <= 18:
        add("next", "info", "Sesi", f"Bursa AS buka {sess_open.astimezone(WIB).strftime('%H:%M')} WIB (dalam {hrs_open:.1f} jam)", "Pembukaan membawa lonjakan volume dan arah awal sesi.", None)
    d0 = now.astimezone(UTC).date()
    for off in range(0, 4):
        dd = d0 + dt.timedelta(days=off)
        if dd == _third_friday(dd.year, dd.month):
            quad = dd.month in (3, 6, 9, 12)
            add("next", "waspada" if quad else "info", "Opsi", ("Quad witching" if quad else "Kedaluwarsa opsi bulanan") + f" · {dd.isoformat()}", "Hari kedaluwarsa opsi/futures: volume besar, harga bisa tertarik ke level strike besar.", "Waspadai gerak tidak wajar di jam-jam akhir.")
    fc = front_contract(now)
    exp = dt.date.fromisoformat(fc["expiry"])
    days_to = (exp - d0).days
    if days_to <= 10:
        add("next", "waspada", "Kontrak", f"Kontrak NQ ganti dalam sekitar {max(days_to - 8, 0)} hari ({fc['code']})", "Volume berpindah ke kontrak berikutnya menjelang kedaluwarsa; harga kontrak lama bisa berbeda beberapa poin.", "Pastikan platform Anda memakai kontrak depan yang sama.")
    if wd == 4 and not (t_min >= 17 * 60):
        add("next", "info", "Akhir pekan", "Pasar tutup Jumat 17:00 ET (04:00 WIB Sabtu)", "Berita akhir pekan baru tercermin saat pembukaan Minggu malam.", None)

    rank = {"kritis": 0, "waspada": 1, "info": 2}
    W.sort(key=lambda x: (x["when"] != "now", rank[x["sev"]]))
    nk = sum(1 for w in W if w["sev"] == "kritis" and w["when"] == "now")
    nw = sum(1 for w in W if w["sev"] == "waspada" and w["when"] == "now")
    nu = sum(1 for w in W if w["sev"] in ("kritis", "waspada") and w["when"] == "next")
    if nk:
        status, label = "kritis", "Kondisi tidak ideal untuk entry sekarang"
    elif nw >= 3:
        status, label = "waspada", "Hati-hati: beberapa risiko aktif"
    elif nw or nu:
        status, label = "waspada", "Ada risiko yang perlu diperhatikan"
    else:
        status, label = "aman", "Tidak ada risiko menonjol saat ini"
    return {"status": status, "label": label, "n_kritis": nk, "n_waspada": nw, "n_next": nu, "items": W,
            "note": "Daftar periksa risiko otomatis, bukan saran beli atau jual."}


def build_report(raw, market, now, history_path=None, use_llm=True):
    regime = detect_regime(market)
    clusters = analyze_news(raw, now, regime)
    structure = analyze_structure(market.get("nq_hourly"))
    ew = earnings_week(market, now)
    if ew:  # pekan laporan Mag7: berita makro biasa dikurangi 50%
        for c in clusters:
            if any(h["label"] in MACRO_LABELS for h in c["rep"]["hits"]):
                c["weight"] *= 0.5; c["impact"] *= 0.5
    radar = fed_radar(clusters, bool(ew))
    fed_ids = {id(c) for c in clusters if FED_RX.search(c["rep"]["title"].lower()) and fed_tone(c["rep"]["hits"]) != 0}
    news_clusters = [c for c in clusters if id(c) not in fed_ids]  # Fed Speak dihitung lewat overlay, bukan dua kali
    ns = news_stats(news_clusters)
    factors = build_factors(ns, market)
    events = upcoming_events(now, mag7_events(market, now))
    entries = load_history(history_path) if history_path else []
    calib = calibrate(entries)
    cot_path = os.path.join(os.path.dirname(history_path) or ".", "cot.json") if history_path else None
    cot = load_cot(cot_path, now, market.get("cot_rows"))
    cot_an = analyze_cot(cot)
    verdict = compose(factors, events, now, calib, structure, radar["overlay"], market, cot_an)
    verdict["calibrated"] = calib["active"]
    # kompatibel dengan dashboard versi 1
    verdict.update(avg_score=ns["avg"], total=round(sum(c["impact"] for c in clusters), 2), scored_items=ns["n_scored"])
    levels = build_levels(market, now)
    items = news_items_out(clusters)
    tr = track_record([])
    hb = market.get("nq_hourly")
    extras = {"rates": rate_expectations(market), "breadth": breadth(market), "options": market.get("options"),
              "volume": volume_anomaly(hb), "volume_profile": volume_profile(hb), "smc": fvg_map(hb)}
    inter = analyze_intermarket(market)
    entry = build_entry_warnings(now, verdict, market, levels, structure, extras["smc"], extras["volume_profile"], market.get("options"),
                                 extras["volume"], regime, extras["breadth"], events, radar, ew, market.get("_feeds") or {}, items, inter)
    alerts = []
    if history_path:
        state = load_state(history_path)
        top = next((i["title"] for i in items if i["score"]), None)
        al = check_alert(state, verdict, now, top)
        if al:
            al["sent"] = send_webhook(al)
            state.setdefault("alerts", []).append(al)
            state["last_alert_at"] = al["at"]
        state["alerts"] = state.get("alerts", [])[-20:]
        alerts = state["alerts"]
        update_history(entries, now, verdict, (market.get("nasdaq") or {}).get("bars"))
        os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)
        calib = calibrate(entries)  # hitung ulang setelah hasil sesi yang baru selesai masuk
        log = load_calib_log(history_path)
        today = now.astimezone(WIB).date().isoformat()
        snap = {"date": today, "n": calib["n"], "active": calib["active"], "a": calib["a"], "b": calib["b"],
                "brier": calib["brier"], "brier_naive": calib["brier_naive"]}
        if log and log[-1].get("date") == today:
            log[-1] = snap
        else:
            log.append(snap)
        del log[:-400]
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": entries, "calib_log": log, "state": state}, f, ensure_ascii=False, indent=1)
        try:
            with open(os.path.join(os.path.dirname(history_path) or ".", "history.csv"), "w", encoding="utf-8") as f:
                f.write("date,bias,score10,p_up,late,change_pct,hit\n")
                for e in entries:
                    o = e.get("outcome") or {}
                    f.write(",".join(str(x) for x in [e["date"], e.get("bias", ""), e.get("score10", ""), e.get("p_up", ""),
                                                      e.get("late", False), o.get("change_pct", ""), e.get("hit", "")]) + "\n")
        except Exception as ex:  # noqa: BLE001
            print(f"[warn] history.csv: {ex}", file=sys.stderr)
        tr = track_record(entries)
        tr["metrics"] = strategy_metrics(entries)
        calib["log"] = log[-30:]
    calib["updated_at"] = now.astimezone(WIB).isoformat(timespec="minutes")
    tr["calibration"] = calib
    report = {
        "version": VERSION, "generated_at": now.astimezone(WIB).isoformat(timespec="seconds"),
        "session": session_info(now), "verdict": verdict, "factors": factors, "reasons": make_reasons(factors),
        "entry_warnings": entry, "intermarket": inter, "regime": regime, "structure": structure, "fed_radar": radar, "cot": cot_an, "earnings_week": ew, **extras, "alerts": alerts,
        "fetched_at": now.astimezone(WIB).isoformat(timespec="seconds"),
        "feeds": market.get("_feeds") or {}, "levels": levels, "forecast": build_forecast(market, levels, verdict, factors, items), "scenarios": make_scenarios(market, levels, events, factors), "events": events,
        "track_record": tr, "market": {k: v for k, v in market.items() if k not in ("nq_hourly", "options", "_feeds", "mag7_earnings", "cot_rows", "hourly_x")}, "news_stats": ns, "items": items, "news_feed": news_feed_out(clusters), "llm_summary": None,
        "methodology": {"weights": {k: FINFO[k][1] for k in FACTOR_ORDER}, "p_up_formula": "1/(1+exp(-(%.3f+%.3f*skor)))" % (calib["a"], calib["b"]) + (" (terkalibrasi)" if calib["active"] else " (heuristik awal)"),
                        "p_up_range": [0.18, 0.82]},
    }
    if use_llm:
        try:
            report["llm_summary"] = llm_summary(report)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] ringkasan LLM gagal: {e}", file=sys.stderr)
    return report


def run_once(args):
    fixture = None
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as f:
            fixture = json.load(f)
    now = parse_time(fixture["now"]) if fixture and fixture.get("now") else dt.datetime.now(UTC)
    if fixture:
        raw, market = fixture.get("headlines", []), fixture.get("market", {})
    elif args.demo:
        raw, market = DEMO, {}
    else:
        raw = []
        for q in QUERIES:
            try:
                raw += fetch_rss(q)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] gagal ambil '{q}': {e}", file=sys.stderr)
        market = fetch_market(now)
    if not raw and not market.get("nasdaq"):
        print("[error] tidak ada headline maupun data harga. Cek koneksi atau pakai --demo.", file=sys.stderr)
        return 1
    if not raw:
        print("[warn] tidak ada headline; laporan memakai data harga saja.", file=sys.stderr)
    hist = None if (args.demo or args.no_history) else os.path.join(args.out, "history.json")
    report = build_report(raw, market, now, hist, use_llm=not fixture)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    write_markdown(report, os.path.join(args.out, "report.md"))
    v = report["verdict"]
    print(f"Bias: {v['bias']} | skor {v['score10']:+.1f} | naik {round(v['p_up'] * 100)}% | "
          f"{v['factors_available']}/{v['factors_total']} faktor | {len(report['items'])} berita -> {args.out}")
    return 0


def main():
    p = argparse.ArgumentParser(description="Agen analis Nasdaq v2")
    p.add_argument("--demo", action="store_true", help="headline contoh tanpa internet")
    p.add_argument("--fixture", help="berkas JSON berisi now, headlines, market (untuk uji)")
    p.add_argument("--no-history", action="store_true", help="jangan menulis rekam jejak")
    p.add_argument("--loop", type=int, default=0, help="ulangi tiap N menit (0 = sekali)")
    p.add_argument("--out", default="docs", help="folder keluaran (docs/ dilayani GitHub Pages)")
    args = p.parse_args()
    if not args.loop:
        return run_once(args)
    while True:
        run_once(args)
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    sys.exit(main())
