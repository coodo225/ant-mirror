"""통계·표기 함수 — 화면 숫자가 여기서 나온다."""
from datetime import date

import pytest

import collect as c
from conftest import make_market, trading_days


def test_scrub_hides_api_keys():
    c.SECRETS.add("SECRETKEY123")
    msg = c.scrub("https://api.stlouisfed.org/fred/releases?api_key=SECRETKEY123&file_type=json -> HTTP 503")
    assert "SECRETKEY123" not in msg and "api_key=***" in msg
    c.warn("raw SECRETKEY123 in text")
    assert c.WARNINGS[-1] == "raw *** in text"


def test_percentile_and_rank_text():
    assert c._percentile([1, 2, 3, 4], 4) == 87.5
    assert c.rank_text(0.3) == "1% 미만"
    assert c.rank_text(3.9) == "4%"


@pytest.mark.parametrize("excess,ci,grade", [
    (-5.0, None, "C"),              # 범위를 못 구했으면 크기로 채점하지 않는다
    (-5.0, (-9.0, 0.1), "C"),       # 0 을 포함하면 C
    (3.5, (0.5, 6.0), "A"),
    (1.5, (0.2, 3.0), "B"),
    (0.6, (0.1, 1.1), "C"),         # 구분되지만 1%p 안쪽 → C
    (-2.0, (-3.5, -0.4), "D"),
    (-4.0, (-6.0, -2.0), "F"),
    (None, None, "?"),
])
def test_grade_rules(excess, ci, grade):
    assert c._grade(excess, ci) == grade


def test_excess_ci_is_deterministic_and_covers_estimate():
    rows, closes = make_market(400)
    C = [closes[r["date"]] for r in rows]
    fwd = [(C[i + 20] / C[i] - 1) * 100 for i in range(len(C) - 20)]
    heavy = [i % 5 == 0 for i in range(len(fwd))]
    a, b = c._excess_ci(fwd, heavy), c._excess_ci(fwd, heavy)
    assert a == b                                           # 같은 입력이면 같은 범위 (화면 숫자가 흔들리지 않게)
    est = sum(x for x, h in zip(fwd, heavy) if h) / sum(heavy) - sum(fwd) / len(fwd)
    assert a[0] <= est <= a[1]
    assert c._excess_ci(fwd[:50], heavy[:50]) is None       # 너무 짧으면 범위를 내지 않는다
    lo, hi = c._excess_ci(fwd, [True] * len(fwd))           # 전부 고르면 차이는 0
    assert abs(lo) < 1e-9 and abs(hi) < 1e-9


def test_pick_apart_keeps_episodes_apart():
    picked = c._pick_apart([10, 12, 40, 41, 80, 15, 100], 20, 5)
    assert picked == [10, 40, 80, 100]
    assert all(abs(a - b) >= 20 for a in picked for b in picked if a != b)


def test_flow_basis_variants():
    rows, _ = make_market(750)
    kept, basis = c.flow_basis(rows)
    assert basis == "intensity" and len(kept) == 750
    # 과거 페이지만 실패해 앞쪽이 거래대금 없는 행이면, 충분히 긴 최근 구간만 써서 강도 기준 유지
    cut = [dict(r) for r in rows]
    for r in cut[:300]:
        r.pop("tradingValue")
    kept, basis = c.flow_basis(cut)
    assert basis == "intensity" and len(kept) == 450 and kept[0]["date"] == rows[300]["date"]
    # 최근 구간이 짧으면 금액 기준
    for r in cut[:600]:
        r.pop("tradingValue", None)
    kept, basis = c.flow_basis(cut)
    assert basis == "amount" and len(kept) == 750


def test_weekdays_and_fomc_hour():
    assert c._weekdays_between("2026-10-02", "2026-10-06") == 2      # 월·화
    assert c.fomc_kst_hour(2026, 10, 28) == 3                         # 미국 서머타임
    assert c.fomc_kst_hour(2026, 12, 9) == 4


def test_build_ant_uses_intensity_and_ci():
    rows, closes = make_market(750)
    ant = c.build_ant({"KOSPI": rows}, {"KOSPI": closes})
    assert ant["basis"] == "intensity" and ant["sample"]["days"] == 750
    for a in ant["actors"].values():
        assert a["grade"] in "ABCDF"
        assert a["excessCI"] is not None and a["excessCI"][0] <= a["excessCI"][1]
        assert a["significant"] == (not a["excessCI"][0] <= 0 <= a["excessCI"][1])
        assert a["heavyBuy"]["n20"] <= a["heavyBuy"]["n"]
        if not a["significant"]:
            assert a["grade"] == "C"
    dates = [h["date"] for h in ant["hallOfFame"]]
    idx = {d: k for k, d in enumerate(r["date"] for r in rows)}
    assert all(abs(idx[x] - idx[y]) >= 20 for x in dates for y in dates if x != y)   # 서로 다른 국면
    for y in ant["yearly"]:
        assert y["excessCI"] is not None                     # 범위를 못 구하는 연도는 표에 넣지 않는다


def test_build_analog_spacing_and_verdict():
    rows, closes = make_market(750)
    an = c.build_analog({"KOSPI": rows}, {"KOSPI": closes})
    idx = {r["date"]: k for k, r in enumerate(rows)}
    ks = [idx[m["date"]] for m in an["matches"]]
    assert len(ks) == 5 and all(abs(a - b) >= c.ANALOG_GAP for a in ks for b in ks if a != b)
    assert max(ks) <= len(rows) - 21                        # 20일 뒤 결과를 아는 날만
    assert an["verdict"] in ("better", "worse", "mixed")
    assert an["above"] == sum(1 for m in an["matches"] if m["ret20"] > an["baseline20"])
    assert set(an["similarity"]) == {"nearest", "typical", "p90", "rare"}


def test_futures_divergence_uses_trailing_typical_and_no_lookahead():
    rows, closes = make_market(400)
    fut = [{**{k: 0.0 for k in c.INVESTOR_GROUPS}, "date": r["date"], "foreign": -r["foreign"] / 10}
           for r in rows]                                   # 현물과 늘 반대 방향
    for r in rows[-5:]:
        r["foreign"] = -500000.0                             # 마지막 주: 현물 대량 매도
    for f in fut[-5:]:
        f["foreign"] = 60000.0                               # 같은 주 선물 대량 매수
    futures = {"daily": fut[-60:]}
    c.attach_futures_divergence(futures, {"KOSPI": rows}, fut, {"KOSPI": closes})
    div = futures["divergence"]
    assert div["state"] == "split" and div["aligned"] is False
    assert div["typicalDays"] == c.DIV_TYPICAL_DAYS
    assert div["history"] is None or div["history"]["n"] >= 1


def test_since_buy_and_baskets(monkeypatch):
    days = trading_days(60, date(2026, 10, 2))
    closes = {d: 100.0 + k for k, d in enumerate(days)}      # 지수: 꾸준히 오름

    def fake_get_json(url, tries=3, **kw):
        if "marketValue/KOSPI" in url:
            page = int(url.split("page=")[1].split("&")[0])
            if page > 1:
                return {"stocks": []}
            stocks = [{"itemCode": f"{k:06d}", "stockName": f"종목{k}", "closePrice": "100",
                       "stockEndType": "etf" if k == 0 else "stock"} for k in range(12)]
            return {"stocks": stocks}
        if "marketValue/KOSDAQ" in url:
            return {"stocks": []}
        if "/trend" in url:
            code = int(url.split("/stock/")[1].split("/")[0])
            # 종목 k: 앞 30일 개인이 사고, 가격은 k 가 짝수면 오르고 홀수면 내림
            out = []
            for i, d in enumerate(days):
                price = 100 + (i if code % 2 == 0 else -i * 0.5)
                out.append({"bizdate": d.replace("-", ""), "individualPureBuyQuant": "1000000" if i < 30 else "0",
                            "foreignerPureBuyQuant": "500000", "organPureBuyQuant": "0",
                            "foreignerHoldRatio": "10", "closePrice": str(price)})
            return out
        if "industry" in url:
            return {"groups": []}
        raise AssertionError(url)

    monkeypatch.setattr(c, "get_json", fake_get_json)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    stocks = c.build_stocks(n_kospi=12, n_kosdaq=0, closes={"KOSPI": closes})
    b = c.build_ant_stocks(stocks, {"KOSPI": closes})
    names = [x["name"] for x in b["antBasket"]]
    assert len(names) == 10 and "종목0" not in names        # ETF 제외
    assert b["wins"] and b["tears"]
    assert all(x["sinceBuy"] > 0 for x in b["wins"]) and all(x["sinceBuy"] < 0 for x in b["tears"])
    assert b["antAvgMarketSinceBuy"] is not None and b["antAvgMarketSinceBuy"] > 0
