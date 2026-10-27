#!/usr/bin/env python3
"""
Agen Analis Nasdaq
------------------
Mengambil berita (RSS), menilai dampaknya ke Nasdaq dengan kamus kata kunci
berbobot, lalu menulis kesimpulan bias harian (bullish / bearish) ke
report.json dan report.md. Jika ANTHROPIC_API_KEY diisi, Claude ikut menulis
ringkasan alasan dari headline yang sudah dinilai.

Pakai:
    python agent.py              # ambil berita live
    python agent.py --demo       # tanpa internet, pakai headline contoh
    python agent.py --loop 30    # ulangi tiap 30 menit

Hanya memakai pustaka standar Python. Paket `anthropic` opsional.
Bukan nasihat investasi.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

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
]

# (pola regex, skor, label). Skor positif = bullish untuk Nasdaq.
LEXICON = [
    (r"\brate hike|hawkish|hotter[- ]than[- ]expected|inflation (rises|accelerat)", -2, "Risiko kenaikan suku bunga"),
    (r"\byields? (jump|surge|climb|rise|rose|spike)|highest since", -2, "Imbal hasil naik"),
    (r"\byields? (fall|fell|drop|slip|ease|plunge)|lower yields", 2, "Imbal hasil turun"),
    (r"\brate cut|dovish|cooler[- ]than[- ]expected|inflation (cools|eases)", 2, "Harapan pelonggaran"),
    (r"\bweak (jobs|payroll)|below consensus|below expectations", 1, "Data lemah menurunkan peluang hike"),
    (r"\bcapex|capital (crunch|constraint)|capital is the biggest|biggest constraint|cash outflow|raise capital|equity raise|debt[- ]laden", -2, "Tekanan pendanaan AI"),
    (r"\bguidance (cut|lower|disappoint)|misses?|disappointing|cuts? (forecast|outlook)", -2, "Panduan/earnings mengecewakan"),
    (r"\bbeats?|raises? (forecast|guidance|outlook)|record (revenue|profit)|upbeat", 2, "Earnings/panduan kuat"),
    (r"\bexport (curb|control|restriction)|tariff|ban\b", -2, "Risiko regulasi/tarif"),
    (r"\b(oil|brent|crude) (surge|jump|climb|spike|rise|rises)|hormuz (closure|closed|shut)", -2, "Minyak naik"),
    (r"\b(oil|brent|crude) (fall|falls|drop|slide|slump|sink)|ceasefire|peace (deal|talks)", 2, "Minyak turun/de-eskalasi"),
    (r"\bselloff|sell-off|plunge|tumble|rout|bubble", -2, "Aksi jual"),
    (r"\brally|rebound|record high|surge", 1, "Rebound/rally"),
    (r"\bipo\b.*(filing|files|filed|optimism)", 1, "Optimisme IPO AI"),
    (r"\bnew (share )?supply|share supply|dilution", -1, "Pasokan saham baru"),
    (r"\blayoffs?|recession|slowdown", -1, "Kekhawatiran ekonomi"),
]

# Kata penanda berita "kecil tapi besar": bobot ekstra jika menyangkut hal ini.
LEVERAGE = [
    (r"\bnvidia|nvda|broadcom|micron|tsmc|semiconductor|chip", 1.4),
    (r"\bfed\b|fomc|warsh|powell", 1.5),
    (r"\bmeta|microsoft|alphabet|google|amazon|apple|tesla", 1.3),
    (r"\bcoreweave|oracle|neocloud|bond", 1.3),
]

DEMO = [
    "Meta posts first cash outflow in a decade, plans non-voting share issue to raise capital",
    "Broadcom, Intel CEOs say capital is the biggest AI constraint",
    "Philadelphia semiconductor index plunges 6.7% as AI spending doubts return",
    "Fed expected to hold rates; markets price December rate hike",
    "September payrolls well below consensus, boosting case for Fed pause",
    "Oil prices fall as Brent slides, easing inflation fears before Fed",
    "Stocks rebound as Anthropic IPO optimism lifts Nasdaq",
    "Treasury 10-year yield slips from near 5% as investors seek safety",
]


# Edit dua baris ini setelah tiap rapat Fed (angka suku bunga tidak bisa diambil otomatis).
FED_FUNDS = os.environ.get("FED_FUNDS", "3,75–4,00%")
FED_NOTE = os.environ.get("FED_NOTE", "FOMC 27–28 Okt")

MAJOR_SOURCES = ("reuters", "bloomberg", "cnbc", "wall street journal", "wsj", "financial times", "associated press", "ap news")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def theme_of(title, tags):
    t = title.lower()
    if re.search(r"\bnvidia|nvda|broadcom|micron|tsmc|semiconductor|chip", t):
        return "Semikonduktor"
    joined = " ".join(tags)
    if re.search(r"pendanaan|Pasokan saham|IPO", joined):
        return "Pendanaan AI"
    if re.search(r"suku bunga|Imbal hasil|pelonggaran|hike", joined):
        return "Fed & yield"
    if re.search(r"Minyak", joined):
        return "Energi"
    return "Sentimen"


def _yahoo(symbol, rng="1mo"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nasdaq-agent/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["chart"]["result"][0]


def _quote(symbol):
    res = _yahoo(symbol, "5d")
    meta = res["meta"]
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    chg = round((last - prev) / prev * 100, 2) if last and prev else None
    return {"last": last, "change_pct": chg, "prev": prev}


def fetch_market():
    """Harga pasar dari Yahoo Finance. Setiap bagian gagal sendiri-sendiri."""
    m = {"fed": {"range": FED_FUNDS, "note": FED_NOTE}}
    try:
        res = _yahoo("^IXIC", "1mo")
        q = res["indicators"]["quote"][0]
        ts = res["timestamp"]
        rows = [(t, c, h, l) for t, c, h, l in zip(ts, q["close"], q["high"], q["low"]) if c is not None]
        last6 = rows[-6:]
        closes = []
        for t, c, _, _ in last6:
            d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
            closes.append([f"{d.day} {MONTHS[d.month - 1]}", round(c, 2)])
        recent = rows[-3:]
        meta = res["meta"]
        m["nasdaq"] = {
            "last": round(rows[-1][1], 2),
            "change_pct": round((rows[-1][1] - rows[-2][1]) / rows[-2][1] * 100, 2),
            "high52": meta.get("fiftyTwoWeekHigh"),
            "closes": closes,
            "resistance": round(max(r[2] for r in recent if r[2]), 2),
            "support": round(min(r[3] for r in recent if r[3]), 2),
            "prev_close": round(rows[-2][1], 2),
        }
    except Exception as e:
        print(f"[warn] Nasdaq: {e}", file=sys.stderr)
    for key, sym in (("yield10", "^TNX"), ("brent", "BZ=F"), ("vix", "^VIX")):
        try:
            m[key] = _quote(sym)
        except Exception as e:
            print(f"[warn] {sym}: {e}", file=sys.stderr)
    return m


def fetch_rss(query, limit=12):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query + " when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    req = urllib.request.Request(url, headers={"User-Agent": "nasdaq-agent/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src = (it.findtext("source") or "").strip()
        if title:
            out.append({"title": title, "link": link, "published": pub, "source": src})
        if len(out) >= limit:
            break
    return out


def score_headline(title):
    t = title.lower()
    raw, tags = 0, []
    for pat, s, label in LEXICON:
        if re.search(pat, t):
            raw += s
            tags.append(label)
    mult = 1.0
    for pat, m in LEVERAGE:
        if re.search(pat, t):
            mult = max(mult, m)
    return round(max(-4, min(4, raw)) * mult, 2), tags


def collect(demo=False):
    items, seen = [], set()
    if demo:
        raw = [{"title": h, "link": "", "published": "", "source": "demo"} for h in DEMO]
    else:
        raw = []
        for q in QUERIES:
            try:
                raw += fetch_rss(q)
            except Exception as e:  # satu sumber gagal tidak boleh menghentikan agen
                print(f"[warn] gagal ambil '{q}': {e}", file=sys.stderr)
    for r in raw:
        key = re.sub(r"\W+", " ", r["title"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        s, tags = score_headline(r["title"])
        src = (r.get("source") or "").lower()
        r.update(score=s, tags=tags, theme=theme_of(r["title"], tags),
                 small=(abs(s) >= 2 and not any(m in src for m in MAJOR_SOURCES)),
                 why=" · ".join(tags) if tags else "Tidak ada pola kunci")
        items.append(r)
    return items


def verdict(items):
    scored = [i for i in items if i["score"] != 0]
    total = sum(i["score"] for i in scored)
    n = max(1, len(scored))
    avg = total / n
    if avg >= 0.8:
        bias = "BULLISH"
    elif avg >= 0.25:
        bias = "BULLISH TIPIS"
    elif avg <= -0.8:
        bias = "BEARISH"
    elif avg <= -0.25:
        bias = "BEARISH TIPIS"
    else:
        bias = "NETRAL"
    conf = int(min(85, 40 + abs(avg) * 25 + min(len(scored), 20)))
    return {"bias": bias, "avg_score": round(avg, 2), "total": round(total, 2),
            "scored_items": len(scored), "confidence_pct": conf}


def llm_summary(items, v):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("[info] paket anthropic belum terpasang, ringkasan LLM dilewati", file=sys.stderr)
        return None
    top = sorted(items, key=lambda i: abs(i["score"]), reverse=True)[:15]
    lines = "\n".join(f"- ({i['score']:+}) {i['title']}" for i in top)
    prompt = (
        "Kamu analis fundamental Nasdaq. Berdasarkan headline bernilai (skor + bullish, - bearish) "
        f"dan bias otomatis {v['bias']}, tulis kesimpulan hari ini dalam Bahasa Indonesia: "
        "bias, 3-4 alasan utama (utamakan berita kecil berdampak besar), pemicu yang bisa membalik bias, "
        "dan 2 risiko. Maksimal 180 kata. Jangan memberi nasihat beli/jual.\n\n" + lines
    )
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(model="claude-sonnet-5-5", max_tokens=700,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "text", None))


def write_reports(items, v, summary, outdir, market=None):
    now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    items = sorted(items, key=lambda i: abs(i["score"]), reverse=True)[:40]
    data = {"generated_at": now, "verdict": v, "llm_summary": summary, "market": market, "items": items}
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    md = [f"# Bias Nasdaq: {v['bias']}", f"_Diperbarui {now}_", "",
          f"Skor rata-rata **{v['avg_score']:+}** dari {v['scored_items']} headline bernilai "
          f"(keyakinan {v['confidence_pct']}%).", ""]
    if summary:
        md += ["## Ringkasan analis", summary, ""]
    md += ["## Headline paling berdampak"]
    for i in items[:12]:
        tag = ", ".join(i["tags"]) or "-"
        link = f" ([sumber]({i['link']}))" if i["link"] else ""
        md.append(f"- **{i['score']:+}** {i['title']} _[{tag}]_{link}")
    md += ["", "_Bukan nasihat investasi._"]
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return data


def run_once(args):
    items = collect(demo=args.demo)
    if not items:
        print("[error] tidak ada headline. Cek koneksi atau pakai --demo.", file=sys.stderr)
        return 1
    v = verdict(items)
    summary = llm_summary(items, v)
    market = None if args.demo else fetch_market()
    write_reports(items, v, summary, args.out, market)
    print(f"Bias: {v['bias']} | skor {v['avg_score']:+} | {v['scored_items']} headline | -> {args.out}/report.md")
    return 0


def main():
    p = argparse.ArgumentParser(description="Agen analis Nasdaq")
    p.add_argument("--demo", action="store_true", help="pakai headline contoh tanpa internet")
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
