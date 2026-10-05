# -*- coding: utf-8 -*-
"""
트레이더용 '참고 통계' 후보 6개(H1~H6)의 사전 등록 검정. 정의·판정 기준은 prereg.json (결과 전에 작성).

정의는 eval/oos/analyze_oos.py 와 collector/collect.py 를 따른다.
  - 강도 = 순매수 / 거래대금 × 100 (collect.flow_basis / flow_scale)
  - 초과수익 = 신호일 fwd 평균 − 같은 구간 모든 날 fwd 평균, fwd 는 구간 안의 행만
  - 90% 범위 = collect._excess_ci (이동 블록 20일, 600회, seed 7)
  - 임계값 = 구간 안 전체 날(사이트 관례). 보조로 직전 250일 실시간 백분위
  - 다중비교 = 순환 이동 귀무분포 p → Holm(가족 오류율 10%), BH q

저장소 파일은 읽기만 한다. collect 는 import 만 하고 ROOT/OUT 을 임시 폴더로 돌린다. 외부 요청 없음.
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
EVAL = HERE / "data"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import collect  # noqa: E402

collect.ROOT = HERE / "data" / "_tmp"
collect.OUT = HERE / "data" / "_tmp" / "out"

# ------------------------------------------------------------------ 데이터
FLOWS = json.loads((EVAL / "kospi_flows_full.json").read_text(encoding="utf-8"))
PX: dict[str, float] = json.loads((EVAL / "kospi_close_full.json").read_text(encoding="utf-8"))
RAW = json.loads((EVAL / "raw" / "028_startDateTime-199901010000.json").read_text(encoding="utf-8"))
OHLC = {f"{x['localDate'][:4]}-{x['localDate'][4:6]}-{x['localDate'][6:]}": x for x in RAW}
CAL = sorted(PX)
CAL_SET = set(CAL)
CAL_POS = {d: i for i, d in enumerate(CAL)}

ROWS, BASIS = collect.flow_basis([r for r in FLOWS if r["date"] in PX])
assert BASIS == "intensity" and len(ROWS) == 4322, (BASIS, len(ROWS))
DATES = [r["date"] for r in ROWS]
N = len(ROWS)
for i in range(1, N):   # 흐름 행이 거래일을 빠짐없이 잇는지
    assert CAL_POS[DATES[i]] == CAL_POS[DATES[i - 1]] + 1

C = np.array([PX[d] for d in DATES])
O = np.array([float(OHLC[d]["openPrice"]) for d in DATES])
Hh = np.array([float(OHLC[d]["highPrice"]) for d in DATES])
Ll = np.array([float(OHLC[d]["lowPrice"]) for d in DATES])
assert np.allclose(C, [float(OHLC[d]["closePrice"]) for d in DATES], atol=0.011)
CPREV = np.array([PX[CAL[CAL_POS[d] - 1]] for d in DATES])
RET = (C / CPREV - 1) * 100                       # 당일 수익률
ABSR = np.abs(RET)                                # H5 주 지표
RANGE = (Hh - Ll) / CPREV * 100                   # H5 보조 지표

F = np.array(collect.flow_scale(ROWS, BASIS, "foreign"))
INST = np.array(collect.flow_scale(ROWS, BASIS, "institution"))
IND = np.array(collect.flow_scale(ROWS, BASIS, "individual"))
FAMT = np.array([r["foreign"] for r in ROWS])
TV = np.array([r["tradingValue"] for r in ROWS])

# H1 연속 일수
BUY_S = np.zeros(N, int)
SELL_S = np.zeros(N, int)
for t in range(N):
    if FAMT[t] > 0:
        BUY_S[t] = (BUY_S[t - 1] if t else 0) + 1
    elif FAMT[t] < 0:
        SELL_S[t] = (SELL_S[t - 1] if t else 0) + 1

# H6 20일 누적 강도, 대조군 지난 20일 지수 수익률
CUM20 = np.full(N, np.nan)
for t in range(19, N):
    CUM20[t] = FAMT[t - 19:t + 1].sum() / TV[t - 19:t + 1].sum() * 100
PAST20 = np.array([(PX[d] / PX[CAL[CAL_POS[d] - 20]] - 1) * 100 for d in DATES])
H6_VALID = ~np.isnan(CUM20)


# H5 만기일
def expiry_days() -> dict[str, bool]:
    out = {}
    y, m = 2009, 1
    while (y, m) <= (2026, 12):
        first = date(y, m, 1)
        d = first + timedelta(days=(3 - first.weekday()) % 7 + 7)   # 둘째 목요일 (collect.derivative_expiries 와 같음)
        if d.isoformat() > CAL[-1]:      # 데이터 끝(2026-10-02) 뒤의 달은 거래일을 모르므로 넣지 않는다
            break
        while d.isoformat() not in CAL_SET and d > first:
            d -= timedelta(days=1)
        if d.isoformat() in CAL_SET:
            out[d.isoformat()] = (m % 3 == 0)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


EXP = expiry_days()
EXP_FLAG = np.array([d in EXP for d in DATES])
EXP_Q = np.array([EXP.get(d, False) for d in DATES])
NEXT_FLAG = np.r_[False, EXP_FLAG[:-1]]
WEEKDAY = np.array([date.fromisoformat(d).weekday() for d in DATES])
THU = WEEKDAY == 3
AFTER_THU = np.r_[False, THU[:-1]]


# 실시간 백분위 (직전 250거래일, 오늘 포함, collect._percentile 방식)
def trailing_pct(v: np.ndarray, w: int = 250) -> np.ndarray:
    out = np.full(N, np.nan)
    valid_idx = np.where(~np.isnan(v))[0]
    for j in range(w - 1, len(valid_idx)):
        t = valid_idx[j]
        win = v[valid_idx[j - w + 1:j + 1]]
        out[t] = ((win < v[t]).sum() + (win == v[t]).sum() / 2) / w * 100
    return out


P_F, P_I, P_IND, P_CUM = trailing_pct(F), trailing_pct(INST), trailing_pct(IND), trailing_pct(CUM20)

PERIODS = {
    "OOS": ("2009-03-23", "2023-08-30"),
    "IS": ("2023-08-31", "2026-10-02"),
    "A": ("2009-03-23", "2014-12-31"),
    "B": ("2015-01-01", "2019-12-31"),
    "C": ("2020-01-01", "2023-08-30"),
}


def span(lo: str, hi: str) -> tuple[int, int]:
    a = next(i for i, d in enumerate(DATES) if d >= lo)
    b = max(i for i, d in enumerate(DATES) if d <= hi) + 1
    return a, b


# ------------------------------------------------------------------ 통계 도구
def boot_indices(m: int, block: int = 20, reps: int = 600, seed: int = 7):
    """collect._excess_ci 와 똑같은 난수 순서로 인덱스를 만든다(비율·순위상관 범위용)."""
    rng = random.Random(seed)
    for _ in range(reps):
        idx: list[int] = []
        while len(idx) < m:
            st = rng.randrange(0, m - block + 1)
            idx.extend(range(st, st + block))
        yield np.array(idx[:m])


def pct_ci(stats: list[float]) -> list[float] | None:
    if not stats:
        return None
    s = sorted(stats)
    return [round(s[int(len(s) * 0.05)], 3), round(s[int(len(s) * 0.95) - 1], 3)]


def shift_p(vals: np.ndarray, fl: np.ndarray, min_gap: int = 40) -> float:
    m = len(vals)
    base = vals.mean()
    obs = vals[fl].mean() - base
    ks = range(min_gap, m - min_gap)
    cnt = 0
    for k in ks:
        e = vals[np.roll(fl, k)].mean() - base
        if abs(e) >= abs(obs) - 1e-12:
            cnt += 1
    return (1 + cnt) / (1 + len(ks))


def rank(x: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(x, kind="mergesort"), kind="mergesort").astype(float)


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.corrcoef(rank(x), rank(y))[0, 1])


def shift_p_corr(x: np.ndarray, y: np.ndarray, min_gap: int = 40) -> float:
    rx, ry = rank(x), rank(y)
    rx = (rx - rx.mean()) / rx.std()
    ry = (ry - ry.mean()) / ry.std()
    m = len(rx)
    obs = float((rx * ry).mean())
    ks = range(min_gap, m - min_gap)
    cnt = sum(1 for k in ks if abs(float((np.roll(rx, k) * ry).mean())) >= abs(obs) - 1e-12)
    return (1 + cnt) / (1 + len(ks))


def runs(fl: np.ndarray) -> int:
    if not len(fl):
        return 0
    return int(fl[0]) + int((fl[1:] & ~fl[:-1]).sum())


def fwd_series(a: int, b: int, h: int, universe: np.ndarray | None, open_entry: bool = False):
    idx = np.arange(a, b - h)
    if universe is not None:
        idx = idx[universe[idx]]
    if open_entry:
        fwd = (C[idx + h] / O[idx + 1] - 1) * 100
    else:
        fwd = (C[idx + h] / C[idx] - 1) * 100
    return idx, fwd


def eval_dir(flags: np.ndarray, universe, h: int, pred: int, a: int, b: int, *,
             open_entry=False, with_p=False, block=20) -> dict:
    idx, fwd = fwd_series(a, b, h, universe, open_entry)
    fl = flags[idx].astype(bool)
    n = int(fl.sum())
    if n == 0:
        return {"n": 0}
    base = float(fwd.mean())
    sel = fwd[fl]
    excess = float(sel.mean()) - base
    ci = collect._excess_ci(fwd.tolist(), fl.tolist(), block=block)
    out = {
        "n": n, "runs": runs(fl), "days": int(len(fwd)),
        "mean_sel": round(float(sel.mean()), 3), "base": round(base, 3), "excess": round(excess, 3),
        "ci90": list(ci) if ci else None, "sig": collect._significant(ci),
        "sd": round(float(fwd.std(ddof=1)), 3), "excess_sd": round(excess / float(fwd.std(ddof=1)), 3),
        "hit_pred": round(float((np.sign(sel) == pred).mean()), 3),
        "base_pred": round(float((np.sign(fwd) == pred).mean()), 3),
        "hit_up": round(float((sel > 0).mean()), 3), "base_up": round(float((fwd > 0).mean()), 3),
    }
    if with_p:
        out["p_shift"] = round(shift_p(fwd, fl), 4)
    return out


def eval_corr(x_full: np.ndarray, universe: np.ndarray, h: int, a: int, b: int, *,
              open_entry=False, with_p=False) -> dict:
    idx, fwd = fwd_series(a, b, h, universe, open_entry)
    x = x_full[idx]
    rho = spearman(x, fwd)
    out = {"n": int(len(x)), "rho": round(rho, 3)}
    for blk in (20, 60):
        st = [spearman(x[ii], fwd[ii]) for ii in boot_indices(len(x), block=blk)]
        out[f"ci90_b{blk}"] = pct_ci(st)
    out["ci90"] = out["ci90_b20"]
    out["sig"] = collect._significant(out["ci90"])
    agree = float(((np.sign(x) == np.sign(fwd)) & (x != 0)).mean())
    pp, pu = float((x > 0).mean()), float((fwd > 0).mean())
    out["hit_pred"] = round(agree, 3)
    out["base_pred"] = round(pp * pu + (1 - pp) * (1 - pu), 3)
    out["base_up"] = round(pu, 3)
    out["excess"] = out["rho"]   # 부호 비교용
    if with_p:
        out["p_shift"] = round(shift_p_corr(x, fwd), 4)
    return out


def eval_vol(flags: np.ndarray, measure: np.ndarray, a: int, b: int, *, control: np.ndarray | None = None,
             with_p=False) -> dict:
    idx = np.arange(a, b)
    if control is not None:
        idx = idx[control[idx] | flags[idx]]
    vals = measure[idx]
    fl = flags[idx].astype(bool)
    n = int(fl.sum())
    if n == 0:
        return {"n": 0}
    me, mn = float(vals[fl].mean()), float(vals[~fl].mean())
    ci = collect._excess_ci(vals.tolist(), fl.tolist())
    rat = []
    for ii in boot_indices(len(vals)):
        f2 = fl[ii]
        if f2.any() and (~f2).any():
            rat.append(float(vals[ii][f2].mean() / vals[ii][~f2].mean()))
    out = {"n": n, "days": int(len(vals)), "mean_flag": round(me, 3), "mean_other": round(mn, 3),
           "ratio": round(me / mn, 3), "ratio_ci90": pct_ci(rat),
           "excess": round(me - float(vals.mean()), 3), "ci90": list(ci) if ci else None,
           "sig": collect._significant(ci),
           "hit_above_median": round(float((vals[fl] > np.median(vals[~fl])).mean()), 3), "base_hit": 0.5,
           "share_big_flag": round(float((vals[fl] >= 2.0).mean()), 3),
           "share_big_other": round(float((vals[~fl] >= 2.0).mean()), 3)}
    if with_p:
        out["p_shift"] = round(shift_p(vals, fl), 4)
    return out


# ------------------------------------------------------------------ 칸 정의
def thr(v: np.ndarray, a: int, b: int, q: float) -> float:
    vals = v[a:b]
    vals = vals[~np.isnan(vals)]
    s = np.sort(vals)
    return float(s[int(len(s) * q)])


def direction_cells(a: int, b: int) -> list[dict]:
    """구간 [a, b) 안에서 임계값을 정한 방향 칸 20개 + 보조 칸."""
    tF70, tI70 = thr(F, a, b, 0.7), thr(INST, a, b, 0.7)
    tF90, tF10 = thr(F, a, b, 0.9), thr(F, a, b, 0.1)
    tN10, tN90 = thr(IND, a, b, 0.1), thr(IND, a, b, 0.9)
    tC80, tC20 = thr(CUM20, a, b, 0.8), thr(CUM20, a, b, 0.2)
    with np.errstate(invalid="ignore"):
        rt_ok_FI = ~np.isnan(P_F) & ~np.isnan(P_I)
        rt_ok_F = ~np.isnan(P_F)
        rt_ok_N = ~np.isnan(P_IND)
        rt_ok_C = ~np.isnan(P_CUM)
        cells = []
        for h in (1, 5, 20):
            cells.append(dict(hyp="H1", cell="외국인 연속 순매수 5일 이상", key="H1_buy", h=h, pred=+1, flags=BUY_S >= 5,
                              universe=None, alt_flags=BUY_S == 5, alt_name="연속 정확히 5일째"))
        for h in (1, 5, 20):
            cells.append(dict(hyp="H1", cell="외국인 연속 순매도 5일 이상", key="H1_sell", h=h, pred=-1, flags=SELL_S >= 5,
                              universe=None, alt_flags=SELL_S == 5, alt_name="연속 정확히 5일째"))
        for h in (1, 5, 20):
            cells.append(dict(hyp="H2", cell="외국인·기관 동반 강매수(둘 다 상위 30%)", key="H2_joint", h=h, pred=+1,
                              flags=(F >= tF70) & (INST >= tI70), universe=None,
                              rt_flags=(P_F >= 70) & (P_I >= 70), rt_universe=rt_ok_FI))
        cells.append(dict(hyp="H3", cell="외국인 강도 상위 10% → 다음 날", key="H3_top", h=1, pred=+1, flags=F >= tF90,
                          universe=None, rt_flags=P_F >= 90, rt_universe=rt_ok_F))
        cells.append(dict(hyp="H3", cell="외국인 강도 하위 10% → 다음 날", key="H3_bot", h=1, pred=-1, flags=F <= tF10,
                          universe=None, rt_flags=P_F <= 10, rt_universe=rt_ok_F))
        for h in (5, 20):
            cells.append(dict(hyp="H4", cell="개인 강매도(하위 10%) & 지수 상승", key="H4_sellup", h=h, pred=+1,
                              flags=(IND <= tN10) & (RET > 0), universe=None,
                              rt_flags=(P_IND <= 10) & (RET > 0), rt_universe=rt_ok_N))
        for h in (5, 20):
            cells.append(dict(hyp="H4", cell="개인 강매수(상위 10%) & 지수 하락", key="H4_buydown", h=h, pred=-1,
                              flags=(IND >= tN90) & (RET < 0), universe=None,
                              rt_flags=(P_IND >= 90) & (RET < 0), rt_universe=rt_ok_N))
        cells.append(dict(hyp="H6", cell="20일 누적 외국인 강도 > 0", key="H6_pos", h=20, pred=+1,
                          flags=H6_VALID & (CUM20 > 0), universe=H6_VALID))
        cells.append(dict(hyp="H6", cell="20일 누적 외국인 강도 < 0", key="H6_neg", h=20, pred=-1,
                          flags=H6_VALID & (CUM20 < 0), universe=H6_VALID))
        cells.append(dict(hyp="H6", cell="20일 누적 외국인 강도 상위 20%", key="H6_top20", h=20, pred=+1,
                          flags=H6_VALID & (CUM20 >= tC80), universe=H6_VALID,
                          rt_flags=P_CUM >= 80, rt_universe=rt_ok_C))
        cells.append(dict(hyp="H6", cell="20일 누적 외국인 강도 하위 20%", key="H6_bot20", h=20, pred=-1,
                          flags=H6_VALID & (CUM20 <= tC20), universe=H6_VALID,
                          rt_flags=P_CUM <= 20, rt_universe=rt_ok_C))
        cells.append(dict(hyp="H6", cell="20일 누적 외국인 강도 ↔ 20일 뒤 수익 순위상관", key="H6_rho", h=20, pred=+1,
                          corr=CUM20, universe=H6_VALID))
    return cells


def control_cells(a: int, b: int) -> list[dict]:
    """H6 대조군: 같은 칸을 지난 20일 지수 수익률로(판정에는 안 씀)."""
    t80, t20 = thr(np.where(H6_VALID, PAST20, np.nan), a, b, 0.8), thr(np.where(H6_VALID, PAST20, np.nan), a, b, 0.2)
    return [
        dict(hyp="H6c", cell="지난 20일 지수 수익 > 0", key="H6c_pos", h=20, pred=+1, flags=H6_VALID & (PAST20 > 0), universe=H6_VALID),
        dict(hyp="H6c", cell="지난 20일 지수 수익 < 0", key="H6c_neg", h=20, pred=-1, flags=H6_VALID & (PAST20 < 0), universe=H6_VALID),
        dict(hyp="H6c", cell="지난 20일 지수 수익 상위 20%", key="H6c_top20", h=20, pred=+1, flags=H6_VALID & (PAST20 >= t80), universe=H6_VALID),
        dict(hyp="H6c", cell="지난 20일 지수 수익 하위 20%", key="H6c_bot20", h=20, pred=-1, flags=H6_VALID & (PAST20 <= t20), universe=H6_VALID),
        dict(hyp="H6c", cell="지난 20일 지수 수익 ↔ 20일 뒤 수익 순위상관", key="H6c_rho", h=20, pred=+1, corr=PAST20, universe=H6_VALID),
    ]


def run_cell(c: dict, a: int, b: int, *, full: bool) -> dict:
    if "corr" in c:
        res = eval_corr(c["corr"], c["universe"], c["h"], a, b, with_p=full)
        if full:
            res["open_entry"] = eval_corr(c["corr"], c["universe"], c["h"], a, b, open_entry=True)
        return res
    res = eval_dir(c["flags"], c["universe"], c["h"], c["pred"], a, b, with_p=full)
    if full:
        o = eval_dir(c["flags"], c["universe"], c["h"], c["pred"], a, b, open_entry=True)
        res["open_entry"] = {k: o.get(k) for k in ("n", "excess", "ci90", "sig", "hit_pred", "base_pred")}
        if "rt_flags" in c:
            rt = eval_dir(c["rt_flags"], c["rt_universe"], c["h"], c["pred"], a, b)
            res["realtime"] = {k: rt.get(k) for k in ("n", "days", "excess", "ci90", "sig", "hit_pred", "base_pred")}
        if "alt_flags" in c:
            al = eval_dir(c["alt_flags"], c["universe"], c["h"], c["pred"], a, b)
            res["alt"] = {"name": c["alt_name"], **{k: al.get(k) for k in ("n", "excess", "ci90", "sig", "hit_pred", "base_pred")}}
        if c["hyp"] == "H6":
            b60 = eval_dir(c["flags"], c["universe"], c["h"], c["pred"], a, b, block=60)
            res["ci90_b60"] = b60.get("ci90")
    return res


def holm(ps: list[float]) -> list[float]:
    k = len(ps)
    order = sorted(range(k), key=lambda i: ps[i])
    adj = [0.0] * k
    run = 0.0
    for rnk, i in enumerate(order):
        run = max(run, min(1.0, (k - rnk) * ps[i]))
        adj[i] = run
    return adj


def bh(ps: list[float]) -> list[float]:
    k = len(ps)
    order = sorted(range(k), key=lambda i: ps[i], reverse=True)
    q = [0.0] * k
    run = 1.0
    for j, i in enumerate(order):
        rnk = k - j
        run = min(run, ps[i] * k / rnk)
        q[i] = run
    return q


# ------------------------------------------------------------------ 실행
def main() -> dict:
    # 검산: 내 부트스트랩 = collect._excess_ci
    a, b = span(*PERIODS["IS"])
    idx, fwd = fwd_series(a, b, 20, None)
    flt = (IND <= thr(IND, a, b, 0.2))[idx]
    mine = []
    for ii in boot_indices(len(fwd)):
        f2 = flt[ii]
        if f2.any():
            mine.append(float(fwd[ii][f2].mean() - fwd[ii].mean()))
    s = sorted(mine)
    check = {"site_panic_sell_IS": list(collect._excess_ci(fwd.tolist(), flt.tolist())),
             "my_bootstrap": [round(s[int(len(s) * 0.05)], 2), round(s[int(len(s) * 0.95) - 1], 2)],
             "excess": round(float(fwd[flt].mean() - fwd.mean()), 2), "n20": int(flt.sum())}

    out: dict = {"check_reproduce_site": check, "data": {
        "flows": [DATES[0], DATES[-1], N], "expiries_in_flows": int(EXP_FLAG.sum()),
        "expiries_moved_off_thursday": sorted(d for d in EXP if d >= DATES[0] and date.fromisoformat(d).weekday() != 3),
        "corr_cum20_past20_OOS": None}, "periods": {}}
    ao, bo = span(*PERIODS["OOS"])
    m = H6_VALID[ao:bo]
    out["data"]["corr_cum20_past20_OOS"] = round(spearman(CUM20[ao:bo][m], PAST20[ao:bo][m]), 3)
    ai, bi = span(*PERIODS["IS"])
    m = H6_VALID[ai:bi]
    out["data"]["corr_cum20_past20_IS"] = round(spearman(CUM20[ai:bi][m], PAST20[ai:bi][m]), 3)

    for pname, (lo, hi) in PERIODS.items():
        a, b = span(lo, hi)
        full = pname in ("OOS", "IS")
        rec = {"from": DATES[a], "to": DATES[b - 1], "days": b - a, "direction": [], "control": [], "volatility": []}
        for c in direction_cells(a, b):
            rec["direction"].append({"hyp": c["hyp"], "key": c["key"], "cell": c["cell"], "h": c["h"], "pred": c["pred"],
                                     **run_cell(c, a, b, full=full)})
        for c in control_cells(a, b):
            rec["control"].append({"hyp": c["hyp"], "key": c["key"], "cell": c["cell"], "h": c["h"], "pred": c["pred"],
                                   **run_cell(c, a, b, full=False)})
        # H5
        for nm, fl, ctl in (("만기일 당일", EXP_FLAG, THU), ("만기 다음 거래일", NEXT_FLAG, AFTER_THU)):
            for meas_name, meas in (("abs_return", ABSR), ("intraday_range", RANGE)):
                v = {"hyp": "H5", "cell": nm, "measure": meas_name,
                     "vs_all": eval_vol(fl, meas, a, b, with_p=full and meas_name == "abs_return")}
                if full or meas_name == "abs_return":
                    v["vs_other_thursday_or_friday"] = eval_vol(fl, meas, a, b, control=ctl)
                if full:
                    v["quarterly_only"] = eval_vol(fl & (EXP_Q if nm == "만기일 당일" else np.r_[False, EXP_Q[:-1]]), meas, a, b)
                    v["monthly_only"] = eval_vol(fl & ~(EXP_Q if nm == "만기일 당일" else np.r_[False, EXP_Q[:-1]]), meas, a, b)
                rec["volatility"].append(v)
        out["periods"][pname] = rec

    # 다중비교 (OOS 방향 칸 20개, 변동성 칸 2개)
    oos = out["periods"]["OOS"]
    ps = [c["p_shift"] for c in oos["direction"]]
    for c, ph, q in zip(oos["direction"], holm(ps), bh(ps)):
        c["p_holm"], c["q_bh"] = round(ph, 4), round(q, 4)
    vcells = [v for v in oos["volatility"] if v["measure"] == "abs_return"]
    vps = [v["vs_all"]["p_shift"] for v in vcells]
    for v, ph in zip(vcells, holm(vps)):
        v["vs_all"]["p_holm"] = round(ph, 4)
    out["multiplicity"] = {
        "direction_cells_OOS": len(ps),
        "expected_false_sig_at_90pct": round(len(ps) * 0.10, 1),
        "observed_sig_OOS": sum(1 for c in oos["direction"] if c.get("sig")),
        "observed_sig_IS": sum(1 for c in out["periods"]["IS"]["direction"] if c.get("sig")),
        "holm_pass_OOS": [c["key"] + f"_h{c['h']}" for c in oos["direction"] if c["p_holm"] <= 0.10],
        "bh_pass_OOS": [c["key"] + f"_h{c['h']}" for c in oos["direction"] if c["q_bh"] <= 0.10],
    }
    # H6 대조군과 신호일 겹침(서술용): 외국인 20일 누적 하위 20% 와 지난 20일 지수 하위 20%
    for pname in ("OOS", "IS"):
        a, b = span(*PERIODS[pname])
        m = np.zeros(N, bool); m[a:b] = True
        f1 = H6_VALID & (CUM20 <= thr(CUM20, a, b, 0.2)) & m
        f2 = H6_VALID & (PAST20 <= thr(np.where(H6_VALID, PAST20, np.nan), a, b, 0.2)) & m
        out["data"][f"H6_bot20_overlap_with_price_bot20_{pname}"] = {
            "both": int((f1 & f2).sum()), "flow_only": int((f1 & ~f2).sum()), "price_only": int((~f1 & f2).sum()),
            "share_of_flow_days_also_price": round(float((f1 & f2).sum() / f1.sum()), 3)}
        # 겹치지 않는 날만(외국인만 크게 판 날) 20일 초과수익 — 서술용
        r = eval_dir(f1 & ~f2, H6_VALID, 20, -1, a, b)
        out["data"][f"H6_bot20_flow_only_excess_{pname}"] = {k: r.get(k) for k in ("n", "excess", "ci90", "sig")}
    out["verdicts"] = judge(out)
    return out


def sgn(x) -> int:
    return 0 if x is None or x == 0 else (1 if x > 0 else -1)


def judge(out: dict) -> dict:
    P = out["periods"]
    res = {"cells": [], "hypotheses": {}}
    for i, c in enumerate(P["OOS"]["direction"]):
        s = sgn(c["excess"])
        sub = [P[k]["direction"][i] for k in ("A", "B", "C")]
        is_ = P["IS"]["direction"][i]
        assert all(x["key"] == c["key"] and x["h"] == c["h"] for x in sub + [is_])
        k1 = bool(c.get("sig"))
        k2 = c["p_holm"] <= 0.10
        k3 = all(sgn(x.get("excess")) == s for x in sub)
        k4 = sgn(is_.get("excess")) == s
        if "rho" in c:
            size_ok = abs(c["rho"]) >= 0.05
            exe_ok = sgn(c["open_entry"]["rho"]) == s
        else:
            # 관측된 방향 기준 적중률 차이
            hit_dir = c["hit_pred"] if s == c["pred"] else None
            if hit_dir is None:   # 가설과 반대 방향으로 나온 칸: 반대 방향 적중률 = 1 - hit_pred(0 수익 무시)
                uplift = (1 - c["hit_pred"]) - (1 - c["base_pred"])
            else:
                uplift = c["hit_pred"] - c["base_pred"]
            size_ok = uplift >= 0.03
            exe_ok = sgn(c["open_entry"]["excess"]) == s and (
                "realtime" not in c or sgn(c["realtime"]["excess"]) == s)
        k5 = size_ok and exe_ok
        res["cells"].append({"cell": f"{c['key']}_h{c['h']}", "c1_oos_ci_excl0": k1, "c2_holm": k2,
                             "c3_subperiod_sign": k3, "c4_is_sign": k4, "c5_size_exec": k5,
                             "pass_all": all((k1, k2, k3, k4, k5)), "ref_only": k1 and k4})
    for hyp in ("H1", "H2", "H3", "H4", "H6"):
        cs = [r for r, c in zip(res["cells"], P["OOS"]["direction"]) if c["hyp"] == hyp]
        if any(r["pass_all"] for r in cs):
            v = "화면에 보여 줄 가치 있음"
        elif any(r["ref_only"] for r in cs):
            v = "참고만"
        else:
            v = "보여 주지 말 것"
        res["hypotheses"][hyp] = v
    # H5
    vo = [v for v in P["OOS"]["volatility"] if v["measure"] == "abs_return"]
    best = "보여 주지 말 것"
    detail = []
    for j, v in enumerate(vo):
        k1 = bool(v["vs_all"]["sig"])
        k2 = v["vs_all"]["p_holm"] <= 0.10
        side = sgn(v["vs_all"]["ratio"] - 1)
        subs = []
        for k in ("A", "B", "C", "IS"):
            vv = [x for x in P[k]["volatility"] if x["measure"] == "abs_return" and x["cell"] == v["cell"]][0]
            subs.append(sgn(vv["vs_all"]["ratio"] - 1) == side)
        k3 = all(subs)
        k4 = sgn(v["vs_other_thursday_or_friday"]["ratio"] - 1) == side
        detail.append({"cell": v["cell"], "c1": k1, "c2": k2, "c3_sub_is": k3, "c4_vs_thu": k4})
        if k1 and k2 and k3 and k4:
            best = "화면에 보여 줄 가치 있음"
        elif k1 and best == "보여 주지 말 것":
            best = "참고만"
    res["hypotheses"]["H5"] = best
    res["H5_detail"] = detail
    return res


def fmt_ci(ci):
    return "[" + ", ".join(f"{x:+.2f}" for x in ci) + "]" if ci else "[ - ]"


def show(out: dict) -> str:
    L = []
    L.append(f"검산(사이트 패닉 매도 IS): {out['check_reproduce_site']}")
    L.append(f"데이터: {out['data']}")
    for pname in ("OOS", "IS", "A", "B", "C"):
        p = out["periods"][pname]
        L.append(f"\n==== {pname} {p['from']}~{p['to']} ({p['days']}일)")
        for c in p["direction"]:
            if "rho" in c:
                L.append(f" {c['key']:11s} h{c['h']:<2d} n={c['n']:4d} rho={c['rho']:+.3f} ci90(b20)={fmt_ci(c['ci90'])} "
                         f"ci90(b60)={fmt_ci(c['ci90_b60'])} 부호일치={c['hit_pred']:.3f} 기준={c['base_pred']:.3f}"
                         + (f" p={c['p_shift']:.3f}" if 'p_shift' in c else "")
                         + (f" holm={c['p_holm']:.3f}" if 'p_holm' in c else "")
                         + (f" | 시가진입 rho={c['open_entry']['rho']:+.3f}" if 'open_entry' in c else ""))
                continue
            line = (f" {c['key']:11s} h{c['h']:<2d} n={c['n']:4d}(묶음 {c['runs']:3d}) 신호평균={c['mean_sel']:+6.2f} 기준={c['base']:+6.2f} "
                    f"초과={c['excess']:+6.2f} ci90={fmt_ci(c['ci90'])}{'*' if c['sig'] else ' '} "
                    f"적중={c['hit_pred']:.3f} 기준={c['base_pred']:.3f} ({c['excess_sd']:+.3f}σ)")
            if "p_shift" in c:
                line += f" p={c['p_shift']:.3f}"
            if "p_holm" in c:
                line += f" holm={c['p_holm']:.3f} bh={c['q_bh']:.3f}"
            if "open_entry" in c:
                o = c["open_entry"]
                line += f" | 시가진입 초과={o['excess']:+.2f} {fmt_ci(o['ci90'])}{'*' if o['sig'] else ''}"
            if "realtime" in c:
                r = c["realtime"]
                line += f" | 실시간 n={r['n']} 초과={r['excess']:+.2f} {fmt_ci(r['ci90'])}{'*' if r['sig'] else ''}"
            if "alt" in c:
                r = c["alt"]
                line += f" | 5일째만 n={r['n']} 초과={r['excess']:+.2f} {fmt_ci(r['ci90'])}{'*' if r['sig'] else ''}"
            if "ci90_b60" in c:
                line += f" | b60 {fmt_ci(c['ci90_b60'])}"
            L.append(line)
        L.append(" -- 대조군(지난 20일 지수 수익)")
        for c in p["control"]:
            if "rho" in c:
                L.append(f"   {c['key']:11s} rho={c['rho']:+.3f} ci90={fmt_ci(c['ci90'])} 부호일치={c['hit_pred']:.3f} 기준={c['base_pred']:.3f}")
            else:
                L.append(f"   {c['key']:11s} n={c['n']:4d} 초과={c['excess']:+6.2f} ci90={fmt_ci(c['ci90'])}{'*' if c['sig'] else ' '} "
                         f"적중={c['hit_pred']:.3f} 기준={c['base_pred']:.3f}")
        L.append(" -- H5 변동성")
        for v in p["volatility"]:
            a = v["vs_all"]
            line = (f"   {v['cell']:10s} {v['measure']:15s} n={a['n']:3d} 만기={a['mean_flag']:.3f} 나머지={a['mean_other']:.3f} "
                    f"비율={a['ratio']:.3f} {a['ratio_ci90']} 차이ci90={fmt_ci(a['ci90'])}{'*' if a['sig'] else ''} "
                    f"중앙값초과={a['hit_above_median']:.3f} |r|>=2% {a['share_big_flag']:.3f} vs {a['share_big_other']:.3f}")
            if "p_shift" in a:
                line += f" p={a['p_shift']:.3f}" + (f" holm={a['p_holm']:.3f}" if "p_holm" in a else "")
            if "vs_other_thursday_or_friday" in v:
                t = v["vs_other_thursday_or_friday"]
                line += f" | 다른목·금 대비 비율={t['ratio']:.3f} {t['ratio_ci90']} 차이={fmt_ci(t['ci90'])}{'*' if t['sig'] else ''}"
            if "quarterly_only" in v:
                qd, mo = v["quarterly_only"], v["monthly_only"]
                line += f" | 동시만기 n={qd['n']} 비율={qd['ratio']:.3f} {qd['ratio_ci90']} · 월만기 n={mo['n']} 비율={mo['ratio']:.3f} {mo['ratio_ci90']}"
            L.append(line)
    L.append(f"\n다중비교: {out['multiplicity']}")
    L.append("\n판정(칸):")
    for r in out["verdicts"]["cells"]:
        L.append(f"  {r}")
    L.append(f"H5 세부: {out['verdicts']['H5_detail']}")
    L.append(f"\n판정(가설): {out['verdicts']['hypotheses']}")
    return "\n".join(L)


if __name__ == "__main__":
    res = main()
    (HERE / "results" / "prereg_results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    txt = show(res)
    (HERE / "results" / "prereg_results.txt").write_text(txt, encoding="utf-8")
    print(txt)
