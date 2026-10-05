# -*- coding: utf-8 -*-
"""
사이트 핵심 신호의 표본 외(out-of-sample) 검증.

정의는 collector/collect.py 의 build_ant 를 그대로 따른다.
  - flow_basis / flow_scale : 강도 = 순매수 / 거래대금 × 100 (거래대금이 90% 이상 있으면)
  - '크게 산 날' : thr = sorted(v)[int(n*0.8)], v >= thr  (구간 안에서 임계값을 정함)
  - '크게 판 날' : cut = sorted(v)[int(n*0.2)], v <= cut  (contrarianRead 의 sell 버킷)
  - 20일 뒤 수익률 : 구간 안의 행만으로 C[i+20]/C[i]-1 (구간 끝 20일은 빠짐 — 사이트와 동일)
  - 초과수익 : 고른 날 평균 − 같은 구간 모든 날(fwd 가 있는 날) 평균
  - 90% 범위 : collect._excess_ci (이동 블록 20일, 600회, seed 7)

추가 검증(사이트 화면을 실제로 본 사람의 입장):
  - 실시간 판정 : 그날까지의 직전 750거래일 안에서 오늘 강도의 백분위(collect._percentile)가
    20 이하/80 이상이었던 날 (온도계가 실제로 그 문구를 띄웠을 날) → 20일 초과수익
  - 연도별 : 해마다 임계값을 새로 잡고(사이트 yearly 와 같은 방식) 부호가 얼마나 뒤집히는지
  - 다중비교 : 시드 민감도, 순환 이동(circular shift) 귀무분포로 본 p 값(신호 4개 중 최대값 포함)
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import collect  # noqa: E402

collect.ROOT = DATA / "_tmp"
collect.OUT = DATA / "_tmp" / "out"

PX: dict[str, float] = json.loads((DATA / "kospi_close_full.json").read_text(encoding="utf-8"))
ROWS: list[dict] = json.loads((DATA / "kospi_flows_full.json").read_text(encoding="utf-8"))
ACTORS = collect.ACTOR_KEYS


def parse_pre2009() -> list[dict]:
    """2005-01-03~2009-03-20: 7100(기타법인) 코드가 없는 행. 7100 을 0 으로 두고 같은 규칙으로 파싱.
    이 시기 투자자 합계는 0 으로 맞아 떨어지므로(=빠진 주체 없음) 개인·외국인·거래대금은 정의가 같다.
    다만 기관계에 들어가는 7000 이 이 시기에는 기타법인을 포함했을 가능성이 커 기관 숫자는 정의가 다르다."""
    by = {}
    for f in sorted((DATA / "raw").glob("*marketType-KOSPI*.json")):
        for c in json.loads(f.read_text(encoding="utf-8")).get("content", []):
            d = str(c.get("bizdate"))
            if d >= "20090323" or d in by:
                continue
            amt, traded = {}, 0
            for nn in c.get("netAmounts") or []:
                g = str(nn["investorGubun"])
                amt[g] = int(str(nn["diffValue"]).replace(",", ""))
                if g != "9999":
                    traded += int(str(nn.get("buyPrice") or 0).replace(",", ""))
            row = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}"}
            for key, codes in collect.INVESTOR_GROUPS.items():
                row[key] = float(round(sum(amt.get(cd, 0) for cd in codes) / 1e8))
            if traded > 0:
                row["tradingValue"] = float(round(traded / 1e8))
            row["_code7000"] = float(round(amt.get("7000", 0) / 1e8))
            by[d] = row
    return [by[k] for k in sorted(by)]


def ci_stats(fwd, heavy, block=20, reps=600, seed=7):
    """collect._excess_ci 와 같은 절차인데 분위수를 골라 쓸 수 있게 전체 통계 목록을 돌려준다."""
    m = len(fwd)
    if m < block * 3 or not any(heavy):
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        idx = []
        while len(idx) < m:
            st = rng.randrange(0, m - block + 1)
            idx.extend(range(st, st + block))
        idx = idx[:m]
        hv = [fwd[i] for i in idx if heavy[i]]
        if hv:
            stats.append(sum(hv) / len(hv) - sum(fwd[i] for i in idx) / m)
    stats.sort()
    return stats


def q(stats, p):
    return round(stats[int(len(stats) * p)], 2) if p < 0.5 else round(stats[int(len(stats) * p) - 1], 2)


def window_signals(rows: list[dict]) -> dict:
    """build_ant 와 같은 방식으로 구간 하나를 '사이트 표본'처럼 채점."""
    rows = [r for r in rows if r["date"] in PX]
    rows, basis = collect.flow_basis(rows)
    n = len(rows)
    C = [PX[r["date"]] for r in rows]
    V = {k: collect.flow_scale(rows, basis, k) for k in ACTORS}
    fwd = [(C[i + 20] / C[i] - 1) * 100 for i in range(n - 20)]
    base = sum(fwd) / len(fwd)
    out = {"from": rows[0]["date"], "to": rows[-1]["date"], "days": n, "basis": basis,
           "baseline20": round(base, 2), "signals": {}}

    def pack(flags_all, flags_fwd):
        ii = [i for i in range(n - 20) if flags_fwd[i]]
        r20 = round(sum(fwd[i] for i in ii) / len(ii), 2)
        excess = round(r20 - base, 2)
        ci = collect._excess_ci(fwd, flags_fwd)
        return {"n": sum(flags_all), "n20": len(ii), "r20": r20, "excess": excess,
                "ci90": list(ci) if ci else None, "significant": collect._significant(ci),
                "grade": collect._grade(excess, ci)}

    for k in ACTORS:
        v = V[k]
        thr = sorted(v)[int(n * 0.8)]
        out["signals"][f"{k}_top20"] = pack([x >= thr for x in v], [v[i] >= thr for i in range(n - 20)])
    iv = V["individual"]
    cut = sorted(iv)[int(n * 0.2)]
    out["signals"]["individual_bottom20"] = pack([x <= cut for x in iv], [iv[i] <= cut for i in range(n - 20)])
    out["_series"] = {"rows": rows, "C": C, "V": V, "fwd": fwd}
    return out


def shift_null(win: dict, min_gap: int = 40) -> dict:
    """순환 이동 귀무분포: 신호(4개 같이)를 수익률에 대해 k일 밀어 붙여 '아무 관계 없을 때' 초과수익 분포를 만든다.
    자기상관(신호 몰림, 20일 겹침)은 양쪽 모두 보존된다."""
    s = win["_series"]
    fwd, V = s["fwd"], s["V"]
    n = len(V["individual"])
    m = len(fwd)
    base = sum(fwd) / m
    flags = {}
    for k in ACTORS:
        v = V[k]
        thr = sorted(v)[int(n * 0.8)]
        flags[f"{k}_top20"] = [v[i] >= thr for i in range(m)]
    iv = V["individual"]
    cut = sorted(iv)[int(n * 0.2)]
    flags["individual_bottom20"] = [iv[i] <= cut for i in range(m)]

    def exc(fl, k):
        sel = [fwd[i] for i in range(m) if fl[(i + k) % m]]
        return sum(sel) / len(sel) - base

    obs = {name: exc(fl, 0) for name, fl in flags.items()}
    null = {name: [] for name in flags}
    null_max = []
    for k in range(min_gap, m - min_gap):
        row = {name: exc(fl, k) for name, fl in flags.items()}
        for name in flags:
            null[name].append(row[name])
        null_max.append(max(abs(x) for x in row.values()))
    res = {}
    for name in flags:
        o = obs[name]
        res[name] = {
            "obs": round(o, 2),
            "p_two_sided": round(sum(1 for x in null[name] if abs(x) >= abs(o)) / len(null[name]), 3),
            "p_family_max_abs": round(sum(1 for x in null_max if x >= abs(o)) / len(null_max), 3),
        }
    res["_shifts"] = len(null_max)
    return res


def rolling_realtime(rows: list[dict], lookback: int = 750) -> list[dict]:
    """그날까지 직전 lookback 거래일 분포에서 오늘 강도의 백분위(collect._percentile) — 온도계가 그날 보여줬을 값."""
    rows = [r for r in rows if r["date"] in PX]
    rows, basis = collect.flow_basis(rows)
    n = len(rows)
    C = [PX[r["date"]] for r in rows]
    V = {k: collect.flow_scale(rows, basis, k) for k in ACTORS}
    out = []
    for t in range(lookback - 1, n):
        rec = {"date": rows[t]["date"], "fwd20": (C[t + 20] / C[t] - 1) * 100 if t + 20 < n else None}
        for k in ACTORS:
            rec[f"p_{k}"] = collect._percentile(V[k][t - lookback + 1: t + 1], V[k][t])
        out.append(rec)
    return out


def eval_rolling(recs: list[dict], lo: str, hi: str) -> dict:
    rr = [r for r in recs if lo <= r["date"] <= hi and r["fwd20"] is not None]
    fwd = [r["fwd20"] for r in rr]
    base = sum(fwd) / len(fwd)
    defs = {
        "individual_top20": lambda r: r["p_individual"] >= 80,
        "foreign_top20": lambda r: r["p_foreign"] >= 80,
        "institution_top20": lambda r: r["p_institution"] >= 80,
        "individual_bottom20": lambda r: r["p_individual"] <= 20,
    }
    res = {"from": rr[0]["date"], "to": rr[-1]["date"], "days": len(rr), "baseline20": round(base, 2), "signals": {}}
    for name, f in defs.items():
        fl = [f(r) for r in rr]
        sel = [x for x, b in zip(fwd, fl) if b]
        if not sel:
            continue
        excess = round(sum(sel) / len(sel) - base, 2)
        ci = collect._excess_ci(fwd, fl)
        res["signals"][name] = {"n20": len(sel), "excess": excess, "ci90": list(ci) if ci else None,
                                "significant": collect._significant(ci)}
    return res


def yearly(rows: list[dict]) -> list[dict]:
    """사이트 yearly 와 같은 방식(해마다 임계값, 20일 뒤 수익률은 전체 시계열에서)을 네 신호 모두에."""
    rows = [r for r in rows if r["date"] in PX]
    rows, basis = collect.flow_basis(rows)
    n = len(rows)
    C = [PX[r["date"]] for r in rows]
    V = {k: collect.flow_scale(rows, basis, k) for k in ACTORS}
    fwd = [(C[i + 20] / C[i] - 1) * 100 for i in range(n - 20)]
    out = []
    for y in sorted({r["date"][:4] for r in rows}):
        yi = [i for i, r in enumerate(rows) if r["date"].startswith(y)]
        y_fwd = [i for i in yi if i + 20 < n]
        if len(yi) < 60 or len(y_fwd) < 60:
            continue
        yb = sum(fwd[i] for i in y_fwd) / len(y_fwd)
        rec = {"year": y, "days": len(yi), "baseline20": round(yb, 2)}
        for name in ("individual_top20", "foreign_top20", "institution_top20", "individual_bottom20"):
            k = name.split("_")[0]
            yv = [V[k][i] for i in yi]
            if name.endswith("bottom20"):
                c = sorted(yv)[int(len(yv) * 0.2)]
                fl = [V[k][i] <= c for i in y_fwd]
            else:
                c = sorted(yv)[int(len(yv) * 0.8)]
                fl = [V[k][i] >= c for i in y_fwd]
            hv = [fwd[i] for i, b in zip(y_fwd, fl) if b]
            if len(hv) < 8:
                continue
            ex = sum(hv) / len(hv) - yb
            ci = collect._excess_ci([fwd[i] for i in y_fwd], fl)
            rec[name] = {"n": len(hv), "excess": round(ex, 2), "ci90": list(ci) if ci else None,
                         "significant": collect._significant(ci)}
        out.append(rec)
    return out


def strip(w):
    return {k: v for k, v in w.items() if not k.startswith("_")}


def main():
    pre = parse_pre2009()
    rows_all = ROWS
    n_all = len(rows_all)
    IS = rows_all[-750:]
    report: dict = {"data": {
        "flows_first": rows_all[0]["date"], "flows_last": rows_all[-1]["date"], "flows_rows": n_all,
        "pre2009_supplement": {"first": pre[0]["date"], "last": pre[-1]["date"], "rows": len(pre),
                                "with_tradingValue": sum(1 for r in pre if r.get("tradingValue"))},
        "index_first": next(iter(PX)), "index_rows": len(PX),
    }}

    # 7000 코드 크기: 2009 전후 비교 (기타법인 포함 여부 추정)
    def med_abs(rs, key):
        return round(statistics.median(abs(r[key]) / r["tradingValue"] * 100 for r in rs if r.get("tradingValue")), 3)
    post09 = [r for r in rows_all if r["date"] < "2010-03-23"]
    report["data"]["code7000_median_abs_intensity_pre2009"] = med_abs(pre, "_code7000")
    report["data"]["other_corp_median_abs_intensity_2009_2010"] = med_abs(post09, "other_corp")

    windows = {
        "IS_2023-08-31~2026-10-02": IS,
        "OOS_A_2009-03~2014": [r for r in rows_all if r["date"] <= "2014-12-31"],
        "OOS_B_2015~2019": [r for r in rows_all if "2015-01-01" <= r["date"] <= "2019-12-31"],
        "OOS_C_2020~2023-08-30": [r for r in rows_all if "2020-01-01" <= r["date"] < IS[0]["date"]],
        "OOS_pooled_2009-03~2023-08-30": [r for r in rows_all if r["date"] < IS[0]["date"]],
        "FULL_2009-03~2026-10-02": rows_all,
        "SUPP_2005~2009-03-20(기관 정의 다름)": pre,
    }
    W = {}
    for name, rs in windows.items():
        W[name] = window_signals(rs)
    report["windows"] = {k: strip(v) for k, v in W.items()}

    # 다중비교: 순환 이동 귀무분포
    report["shift_null"] = {k: shift_null(W[k]) for k in
                            ("IS_2023-08-31~2026-10-02", "OOS_A_2009-03~2014", "OOS_B_2015~2019",
                             "OOS_C_2020~2023-08-30", "OOS_pooled_2009-03~2023-08-30")}

    # 시드 민감도 + 넓은 분위수 (사이트 표본, 패닉 매도)
    s = W["IS_2023-08-31~2026-10-02"]["_series"]
    iv, n, fwd = s["V"]["individual"], len(s["V"]["individual"]), s["fwd"]
    cut = sorted(iv)[int(n * 0.2)]
    fl = [iv[i] <= cut for i in range(n - 20)]
    seeds = []
    for sd in range(1, 51):
        ci = collect._excess_ci(fwd, fl, seed=sd)
        seeds.append(ci)
    big = ci_stats(fwd, fl, reps=5000, seed=7)
    report["IS_panic_sell_robustness"] = {
        "seed7_site": list(collect._excess_ci(fwd, fl)),
        "seeds_1_50_lower_gt0": sum(1 for c in seeds if c and c[0] > 0),
        "seeds_1_50_lower_range": [min(c[0] for c in seeds), max(c[0] for c in seeds)],
        "reps5000_ci90": [q(big, 0.05), q(big, 0.95)],
        "reps5000_ci95": [q(big, 0.025), q(big, 0.975)],
        "reps5000_ci97.5(bonferroni4)": [q(big, 0.0125), q(big, 0.9875)],
        "block40_ci90": list(collect._excess_ci(fwd, fl, block=40)),
        "block60_ci90": list(collect._excess_ci(fwd, fl, block=60)),
    }

    # 실시간(직전 750일 백분위) 판정
    recs = rolling_realtime(pre + rows_all)   # 2005년부터 이어 붙여 2008년부터 판정 가능 (기관만 정의 차이)
    report["rolling_realtime"] = {
        "first_judged": recs[0]["date"],
        "2008-01~2014": eval_rolling(recs, "2008-01-01", "2014-12-31"),
        "2015~2019": eval_rolling(recs, "2015-01-01", "2019-12-31"),
        "2020~2023-08-30": eval_rolling(recs, "2020-01-01", "2023-08-30"),
        "IS_2023-08-31~": eval_rolling(recs, "2023-08-31", "2026-12-31"),
        "OOS_2008~2023-08-30": eval_rolling(recs, "2008-01-01", "2023-08-30"),
    }

    # 연도별
    report["yearly"] = yearly(pre + rows_all)

    (HERE / "results" / "oos_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def show(rep):
    print("DATA", json.dumps(rep["data"], ensure_ascii=False))
    for name, w in rep["windows"].items():
        print(f"\n== {name}  {w['from']}~{w['to']}  n={w['days']} basis={w['basis']} base20={w['baseline20']}")
        for sname, sg in w["signals"].items():
            print(f"   {sname:22s} n20={sg['n20']:4d} r20={sg['r20']:+6.2f} excess={sg['excess']:+6.2f} ci90={sg['ci90']} sig={sg['significant']}")
    print("\nSHIFT NULL")
    for name, r in rep["shift_null"].items():
        print(" ", name, {k: v for k, v in r.items()})
    print("\nIS panic robustness", json.dumps(rep["IS_panic_sell_robustness"], ensure_ascii=False))
    print("\nROLLING (trailing 750d percentile)  first judged", rep["rolling_realtime"]["first_judged"])
    for name, w in rep["rolling_realtime"].items():
        if name == "first_judged":
            continue
        print(f" == {name} {w['from']}~{w['to']} days={w['days']} base20={w['baseline20']}")
        for sname, sg in w["signals"].items():
            print(f"     {sname:22s} n20={sg['n20']:4d} excess={sg['excess']:+6.2f} ci90={sg['ci90']} sig={sg['significant']}")
    print("\nYEARLY")
    for y in rep["yearly"]:
        cells = []
        for sname in ("individual_top20", "foreign_top20", "institution_top20", "individual_bottom20"):
            sg = y.get(sname)
            cells.append(f"{sname.split('_')[0][:5]}{'B' if 'bottom' in sname else 'T'}={sg['excess']:+6.2f}{'*' if sg['significant'] else ' '}" if sg else f"{sname}=NA")
        print(f" {y['year']} base={y['baseline20']:+6.2f} " + "  ".join(cells))


if __name__ == "__main__":
    show(main())
