"""Backtest faktor harga (tanpa berita): skor penutupan hari t memprediksi arah Nasdaq Composite hari t+1.

Hanya memakai faktor yang bisa direkonstruksi dari harga historis (NQ, ES, SOX, yield, VIX, minyak, DXY, tren 5 sesi).
Faktor berita tidak bisa direkonstruksi dan dihitung netral. Hasil ini menguji apakah KERANGKA FAKTOR HARGA punya nilai prediktif,
bukan seluruh model. Tanpa biaya transaksi dan slippage.
"""
import datetime as dt
import math

import agent

SERIES = {"nasdaq": "^IXIC", "nq_fut": "NQ=F", "es_fut": "ES=F", "sox": "^SOX", "yield10": "^TNX", "vix": "^VIX", "brent": "BZ=F", "dxy": "DX-Y.NYB"}


def _load(range_="5y"):
    out = {}
    for k, sym in SERIES.items():
        try:
            rows = agent._series(agent._yahoo(sym, range_))
            out[k] = {agent.to_et(dt.datetime.fromtimestamp(r["t"], agent.UTC)).date().isoformat(): r["c"] for r in rows}
        except Exception as e:  # noqa: BLE001
            print(f"[warn] backtest {sym}: {e}")
            out[k] = {}
    return out


def _pearson(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 3) if sx and sy else None


def _wilson(h, n):
    if not n:
        return None
    p, z = h / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    w = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return [round(max(0, c - w), 3), round(min(1, c + w), 3)]


def build_rows(data):
    """Kembalikan daftar (tanggal, skor10, sinyal_per_faktor, return_hari_berikutnya_pct)."""
    nas = data["nasdaq"]
    dates = sorted(nas)
    ser = {}
    for k, d in data.items():  # forward-fill hingga 3 hari agar selisih kalender futures tidak membuat lubang
        s, last, gap = [], None, 0
        for dd in dates:
            v = d.get(dd)
            if v is not None:
                last, gap = v, 0
            else:
                gap += 1
                if gap > 3:
                    last = None
            s.append(last)
        ser[k] = s
    rows = []
    for i in range(253, len(dates) - 1):
        def q(k):
            c, p = ser[k][i], ser[k][i - 1]
            if c is None or not p:
                return None
            if k == "yield10":
                if c > 20:
                    c, p = c / 10, p / 10
                return {"last": c, "prev": p, "change_pct": (c / p - 1) * 100, "change_bps": (c - p) * 100}
            return {"last": c, "prev": p, "change_pct": (c / p - 1) * 100}
        m = {k: q(k) for k in ("nq_fut", "es_fut", "sox", "yield10", "vix", "brent", "dxy")}
        n = ser["nasdaq"]
        hi = max(x for x in n[i - 251:i + 1] if x)
        m["nasdaq"] = {"last": n[i], "high52": hi, "ret5_pct": (n[i] / n[i - 5] - 1) * 100}
        fs = agent.build_factors({"avg": 0.0, "n_scored": 0, "bull_share": 0.5}, m)
        wsum = sum(f["weight"] for f in fs) or 1
        sc = sum(f["signal"] * f["weight"] / wsum * 5 for f in fs if f["signal"] is not None)
        nxt = (n[i + 1] / n[i] - 1) * 100 if n[i + 1] and n[i] else None
        if nxt is None:
            continue
        rows.append((dates[i], round(max(-10, min(10, sc)), 2), {f["key"]: f["signal"] for f in fs}, nxt))
    return rows


def analyze(rows):
    if len(rows) < 100:
        return None
    sc = [r[1] for r in rows]
    ret = [r[3] for r in rows]
    pairs = [(s, y) for s, y in zip(sc, ret) if s != 0]
    hits = sum(1 for s, y in pairs if (s > 0) == (y > 0))
    out = {"n": len(rows), "from": rows[0][0], "to": rows[-1][0], "ic": _pearson(sc, ret),
           "hit_rate": round(hits / len(pairs), 3), "hit_ci": _wilson(hits, len(pairs)),
           "base_up": round(sum(1 for y in ret if y > 0) / len(ret), 3)}
    buckets = []
    for lo, hi, name in ((-99, -4, "skor ≤ −4"), (-4, -1.5, "−4 s/d −1,5"), (-1.5, 1.5, "−1,5 s/d +1,5"), (1.5, 4, "+1,5 s/d +4"), (4, 99, "skor ≥ +4")):
        sel = [(s, y) for s, y in zip(sc, ret) if lo < s <= hi] if lo > -99 else [(s, y) for s, y in zip(sc, ret) if s <= hi]
        if len(sel) >= 10:
            buckets.append({"label": name, "n": len(sel), "up_pct": round(sum(1 for _, y in sel if y > 0) / len(sel) * 100, 1),
                            "mean_ret": round(sum(y for _, y in sel) / len(sel), 3)})
    out["buckets"] = buckets
    # strategi sederhana: posisi sesuai tanda skor (|skor| ≥ 1,5), tanpa biaya
    eq, peak, mdd, wins, losses, trades, gp, gl = 1.0, 1.0, 0.0, 0, 0, 0, 0.0, 0.0
    bh = 1.0
    for s, y in zip(sc, ret):
        bh *= 1 + y / 100
        if abs(s) < 1.5:
            continue
        r = (1 if s > 0 else -1) * y
        trades += 1
        eq *= 1 + r / 100
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        if r > 0:
            wins += 1; gp += r
        else:
            losses += 1; gl += -r
    out["strategy"] = {"trades": trades, "win_rate": round(wins / trades, 3) if trades else None, "total_pct": round((eq - 1) * 100, 1),
                       "profit_factor": round(gp / gl, 2) if gl else None, "max_dd_pct": round(mdd * 100, 1), "buy_hold_pct": round((bh - 1) * 100, 1)}
    keys = list(rows[0][2])
    byf = []
    for k in keys:
        pr = [(r[2][k], r[3]) for r in rows if r[2].get(k) is not None]
        if len(pr) > 100:
            byf.append({"key": k, "ic": _pearson([a for a, _ in pr], [b for _, b in pr]), "n": len(pr)})
    out["by_factor"] = sorted([b for b in byf if b["ic"] is not None and b["key"] != "news"], key=lambda b: -abs(b["ic"]))
    data = [(s, 1.0 if y > 0 else 0.0) for s, y in zip(sc, ret)]
    out["walk_forward"] = agent.walk_forward(data, min_train=120)
    ic = out["ic"] or 0
    lo = out["hit_ci"][0]
    out["verdict"] = ("Ada bukti lemah bahwa faktor harga memprediksi arah hari berikutnya." if lo > 0.5 and ic > 0.03 else
                      "Faktor harga belum bisa dibedakan dari tebakan acak untuk arah hari berikutnya." if lo <= 0.5 else
                      "Hasil campuran: akurasi arah di atas acak tetapi korelasi skor-return lemah.")
    out["caveat"] = "Hanya faktor harga (berita dihitung netral). Tanpa biaya transaksi. Hasil masa lalu tidak menjamin masa depan."
    return out


def run():
    return analyze(build_rows(_load()))


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=1))
