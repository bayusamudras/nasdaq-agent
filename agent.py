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
         pat=r"weak (jobs|payrolls?|hiring|labou?r|employment)|(jobs|payrolls?|hiring) (miss|disappoint|slow|cool|weaken)|(payrolls?|jobs report).{0,40}below",
         why="Data kerja yang lebih lemah mengurangi peluang kenaikan suku bunga sehingga baik bagi valuasi. Efeknya terbatas jika terlalu lemah dan memicu kekhawatiran resesi.",
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
        "yield10": "^TNX", "brent": "BZ=F", "dxy": "DX-Y.NYB", "nvda": "NVDA"}

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


def _yahoo(symbol, rng):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval=1d")
    return json.loads(_http(url))["chart"]["result"][0]


def _series(res):
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        rows.append({"t": t, "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": c})
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
    if symbol == "^TNX" and prev:
        out["change_bps"] = round((last - prev) * 100, 1)
    return out


def fetch_market(now):
    m = {"fed": {"range": FED_FUNDS, "note": FED_NOTE}}
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
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Nasdaq: {e}", file=sys.stderr)
    for key, sym in SYMS.items():
        try:
            m[key] = _quote(sym)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {sym}: {e}", file=sys.stderr)
    if "yield10" in m and "change_bps" not in m["yield10"] and m["yield10"].get("prev"):
        m["yield10"]["change_bps"] = round((m["yield10"]["last"] - m["yield10"]["prev"]) * 100, 1)
    return m


# ---------------------------------------------------------------------------
# Berita
# ---------------------------------------------------------------------------
def score_headline(title):
    """Mengembalikan (skor, daftar entri kamus yang cocok). Skor positif = bullish."""
    t = NEGATED_HIKE.sub(" ", title.lower())
    t = NEGATED_CUT.sub(" no_cut ", t)
    hits = [e for e in LEXICON if e["rx"].search(t)]
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


def analyze_news(raw, now):
    items = []
    seen = set()
    for r in raw:
        key = re.sub(r"\W+", " ", r["title"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        s, hits = score_headline(r["title"])
        pub = parse_time(r.get("published"))
        age = max(0.0, (now - pub).total_seconds() / 3600) if pub else 12.0
        cred = credibility(r.get("source"))
        rec = max(0.2, 0.5 ** (age / 10))
        items.append({**r, "score": s, "hits": hits, "age_h": round(age, 1), "cred": cred,
                      "quality": cred * rec, "tok": tokens(r["title"]), "pub": pub})
    # gabungkan berita kembar (kejadian yang sama diberitakan banyak sumber)
    items.sort(key=lambda i: -i["quality"])
    clusters = []
    for it in items:
        for c in clusters:
            inter = len(it["tok"] & c["rep"]["tok"])
            union = len(it["tok"] | c["rep"]["tok"]) or 1
            if inter / union >= 0.45 and (it["score"] >= 0) == (c["rep"]["score"] >= 0) or inter / union >= 0.7:
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
        out.append({"rep": rep, "n": n, "mult": mult, "weight": w, "impact": w * rep["score"]})
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
            "theme": theme_of(rep["title"], hits), "sources_count": n,
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


def upcoming_events(now, limit=7):
    out = []
    for date, hhmm, name, impact, confirmed, why in EVENTS:
        start = et_to_utc(date, hhmm or "09:00")
        end_of_day = et_to_utc(date, "23:59")
        if end_of_day < now:
            continue
        out.append({"date": date, "start_utc": start.isoformat(timespec="minutes"), "time_known": hhmm is not None,
                    "name": name, "impact": impact, "confirmed": confirmed, "why": why,
                    "hours_until": round((start - now).total_seconds() / 3600, 1)})
    out.sort(key=lambda e: e["start_utc"])
    return out[:limit]


def compose(factors, events, now):
    avail = [f for f in factors if f["signal"] is not None]
    wsum = sum(f["weight"] for f in factors) or 1  # faktor hilang menyumbang 0, skor menyusut ke netral
    for f in factors:
        f["contribution"] = round(f["signal"] * f["weight"] / wsum * 5, 2) if f["signal"] is not None else None
    score10 = round(sum(f["contribution"] for f in avail), 2)
    p_up = clamp(1 / (1 + math.exp(-(0.16 + 0.16 * score10))), 0.18, 0.82)

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
            "warnings": [t for _, t in penalties]}


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
    del entries[:-200]
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
         f"{v['agreement_pct']}% bobot faktor searah). Estimasi heuristik, belum dikalibrasi.", ""]
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


def build_report(raw, market, now, history_path=None, use_llm=True):
    clusters = analyze_news(raw, now)
    ns = news_stats(clusters)
    factors = build_factors(ns, market)
    events = upcoming_events(now)
    verdict = compose(factors, events, now)
    # kompatibel dengan dashboard versi 1
    verdict.update(avg_score=ns["avg"], total=round(sum(c["impact"] for c in clusters), 2), scored_items=ns["n_scored"])
    levels = build_levels(market, now)
    items = news_items_out(clusters)
    tr = track_record([])
    if history_path:
        entries = load_history(history_path)
        update_history(entries, now, verdict, (market.get("nasdaq") or {}).get("bars"))
        os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": entries}, f, ensure_ascii=False, indent=1)
        tr = track_record(entries)
    report = {
        "version": VERSION, "generated_at": now.astimezone(WIB).isoformat(timespec="seconds"),
        "session": session_info(now), "verdict": verdict, "factors": factors, "reasons": make_reasons(factors),
        "levels": levels, "scenarios": make_scenarios(market, levels, events, factors), "events": events,
        "track_record": tr, "market": market, "news_stats": ns, "items": items, "llm_summary": None,
        "methodology": {"weights": {k: FINFO[k][1] for k in FACTOR_ORDER}, "p_up_formula": "1/(1+exp(-(0.16+0.16*skor)))",
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
