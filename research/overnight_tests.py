"""prereg_overnight.json 의 E1~E3. 외부 요청 없음."""
import json, random, math, bisect
from pathlib import Path
from statistics import mean, median
SP = Path(__file__).parent
EVAL = SP / "data" / "raw" / "028_startDateTime-199901010000.json"
ks_raw = json.load(open(EVAL, encoding="utf-8"))
K = []
for r in ks_raw:
    d = r["localDate"]; d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    K.append({"date": d, "o": r["openPrice"], "h": r["highPrice"], "l": r["lowPrice"], "c": r["closePrice"]})
K.sort(key=lambda x: x["date"])
US = json.load(open(SP / "data" / "us_daily.json", encoding="utf-8"))
us_dates = {s: sorted(v) for s, v in US.items()}

def last_before(sym, d):
    ds = us_dates[sym]; i = bisect.bisect_left(ds, d) - 1
    return ds[i] if i >= 0 else None

rows = []
for i in range(2, len(K)):
    t, p = K[i], K[i - 1]
    if t["date"] < "2008-09-01":
        continue
    row = {"date": t["date"],
           "gap": (t["o"] / p["c"] - 1) * 100,
           "intra": (t["c"] / t["o"] - 1) * 100,
           "range": (t["h"] - t["l"]) / p["c"] * 100,
           "k_prev": (p["c"] / K[i - 2]["c"] - 1) * 100,
           "open_eq_prev": abs(t["o"] - p["c"]) < 1e-9}
    ok = True
    for sym, key in (("^GSPC", "spx"), ("^SOX", "sox"), ("EWY", "ewy"), ("KRW=X", "fx")):
        a, b = last_before(sym, p["date"]), last_before(sym, t["date"])
        if not a or not b or a == b:
            ok = False; break
        row[key] = (US[sym][b] / US[sym][a] - 1) * 100
    if not ok:
        continue
    row["ewy_ex"] = row["ewy"] - row["k_prev"]
    rows.append(row)

# 데이터 품질: 시가 = 전일 종가 비율(연도별)
q = {}
for r in rows:
    y = r["date"][:4]; q.setdefault(y, [0, 0]); q[y][0] += r["open_eq_prev"]; q[y][1] += 1
quality = {y: f"{a}/{b}" for y, (a, b) in sorted(q.items()) if a}

def corr(x, y):
    n = len(x); mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x); syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else float("nan")

def boot(idx_rows, stat, reps=1000, block=20, seed=11):
    rnd = random.Random(seed); n = len(idx_rows); vals = []
    for _ in range(reps):
        pick = []
        while len(pick) < n:
            s = rnd.randrange(0, max(1, n - block)); pick.extend(range(s, min(n, s + block)))
        sample = [idx_rows[j] for j in pick[:n]]
        v = stat(sample)
        if v == v: vals.append(v)
    vals.sort()
    return [round(vals[int(len(vals) * 0.05)], 3), round(vals[int(len(vals) * 0.95) - 1], 3)]

def hit(sample, xk):
    n = len(sample)
    h = sum(1 for r in sample if (r[xk] > 0) == (r["intra"] > 0)) / n
    px = sum(1 for r in sample if r[xk] > 0) / n; py = sum(1 for r in sample if r["intra"] > 0) / n
    return h - (px * py + (1 - px) * (1 - py))

def period(lo, hi):
    return [r for r in rows if lo <= r["date"] <= hi and not r["open_eq_prev"]]

P = {"OOS": ("2009-03-23", "2023-08-30"), "SITE": ("2023-08-31", "2026-10-02"),
     "A_2009_2014": ("2009-03-23", "2014-12-31"), "B_2015_2019": ("2015-01-01", "2019-12-31"),
     "C_2020_2023": ("2020-01-01", "2023-08-30")}
res = {"n_rows": len(rows), "open_eq_prevclose_by_year": quality, "periods": {}}
for name, (lo, hi) in P.items():
    S = period(lo, hi)
    out = {"n": len(S), "from": S[0]["date"], "to": S[-1]["date"]}
    for xk in ("spx", "sox", "ewy_ex", "fx"):
        cg = corr([r[xk] for r in S], [r["gap"] for r in S])
        ci = corr([r[xk] for r in S], [r["intra"] for r in S])
        e = {"gap_corr": round(cg, 3), "gap_R2": round(cg * cg, 3),
             "intra_corr": round(ci, 3),
             "intra_hit_minus_chance_pp": round(hit(S, xk) * 100, 2)}
        if name in ("OOS", "SITE"):
            e["gap_corr_ci90"] = boot(S, lambda s, xk=xk: corr([r[xk] for r in s], [r["gap"] for r in s]))
            e["intra_corr_ci90"] = boot(S, lambda s, xk=xk: corr([r[xk] for r in s], [r["intra"] for r in s]))
            e["intra_hit_ci90_pp"] = [round(v * 100, 2) for v in boot(S, lambda s, xk=xk: hit(s, xk))]
        out[xk] = e
    # E3: |ewy_ex| 상위 10% 날 변동폭 배수
    thr = sorted(abs(r["ewy_ex"]) for r in S)[int(len(S) * 0.9)]
    def ratio(s, thr=thr):
        big = [r["range"] for r in s if abs(r["ewy_ex"]) >= thr]; rest = [r["range"] for r in s if abs(r["ewy_ex"]) < thr]
        return mean(big) / mean(rest) if big and rest else float("nan")
    out["E3_range_ratio_top10"] = round(ratio(S), 3)
    if name in ("OOS", "SITE"):
        out["E3_range_ratio_ci90"] = boot(S, ratio)
        out["E3_thr_abs_ewy_ex"] = round(thr, 2)
    out["mean_abs_gap"] = round(mean(abs(r["gap"]) for r in S), 3)
    out["mean_abs_intra"] = round(mean(abs(r["intra"]) for r in S), 3)
    out["median_range"] = round(median(r["range"] for r in S), 3)
    res["periods"][name] = out

# 최근 사례(설명용): 10/2 US → 10/6 개장 전 상황
res["latest_overnight_example"] = {s: {"last": us_dates[s][-1], "prev": us_dates[s][-2],
                                       "chg_pct": round((US[s][us_dates[s][-1]] / US[s][us_dates[s][-2]] - 1) * 100, 2)}
                                   for s in US}
json.dump(res, open(SP / "results" / "overnight_results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1)[:9000])
