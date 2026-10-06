"""
소스가 죽거나 멈췄을 때의 동작 — 2026-09 에 수급 소스 폐지와 FDR 정지가 겹쳐 사이트가 17일간 빈 채로 나갔다.
"""
import json
from pathlib import Path
from datetime import date, timedelta

import pytest

import collect as c
from datetime import datetime

from conftest import freeze_now, make_market, market_payload


# ── 멈춤 감지 ────────────────────────────────────────────────

def _fresh_case(last, flow_last, traded, today, status="CLOSE", price=1.0):
    days = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-30", "2026-10-01", "2026-10-02"]
    px = {d: 1.0 for d in days if d <= last}
    m = {"indices": {"KOSPI": {"name": "코스피", "tradedAt": f"{traded}T08:05:00+09:00",
                                "marketStatus": status, "price": price}}}
    flows = {"markets": {"KOSPI": {"daily": [{"date": d} for d in days if d <= flow_last]}}}
    stale = c.check_freshness(flows, m, {"KOSPI": px}, today=today)
    return stale, px


def test_holiday_next_morning_is_not_stale():
    # 9/30 다음 10/1·10/2 휴장(가정) 뒤 10/5 아침: 일봉·수급 모두 9/30 에 머문 것이 정상
    stale, _ = _fresh_case("2026-09-30", "2026-09-30", "2026-10-05", today="2026-10-05", status="PREOPEN")
    assert stale == set()


def test_both_sources_stuck_long_is_stale():
    stale, _ = _fresh_case("2026-09-17", "2026-09-17", "2026-10-05", today="2026-10-05")
    assert {"market.KOSPI.history", "flows.KOSPI"} <= stale


def test_weekend_run_missing_friday_bar_is_stale():
    stale, _ = _fresh_case("2026-10-01", "2026-10-01", "2026-10-02", today="2026-10-04")
    assert "market.KOSPI.history" in stale


def test_intraday_missing_today_bar_is_patched_from_price():
    stale, px = _fresh_case("2026-10-01", "2026-10-02", "2026-10-02", today="2026-10-02",
                            status="OPEN", price=7.0)
    assert stale == set() and px["2026-10-02"] == 7.0      # 성적표의 '오늘'이 수급 표의 '오늘'과 같아진다


def test_flows_stuck_while_index_fresh_is_stale():
    stale, _ = _fresh_case("2026-10-02", "2026-09-17", "2026-10-02", today="2026-10-02")
    assert stale == {"flows.KOSPI"}


def test_market_phase_holiday_and_hours():
    K = c.KST
    from datetime import datetime
    assert c.market_phase(datetime(2026, 10, 5, 10, 0, tzinfo=K), {"tradedAt": "2026-10-02T20:15"}) == "휴장일"
    assert c.market_phase(datetime(2026, 10, 6, 8, 10, tzinfo=K), {"tradedAt": "2026-10-02T20:15"}) == "장전"
    assert c.market_phase(datetime(2026, 10, 6, 10, 0, tzinfo=K), {"tradedAt": "2026-10-06T10:00"}) == "장중"
    assert c.market_phase(datetime(2026, 10, 6, 10, 0, tzinfo=K), None) == "장중"
    assert c.market_phase(datetime(2026, 10, 4, 10, 0, tzinfo=K), None) == "주말"


# ── 직전 정상본 유지 ─────────────────────────────────────────

NOW = "2026-10-05T12:00:00+09:00"
LAST = c._last_date(lambda xs: xs[-1]["date"])


def test_keep_good_paths():
    prev = [{"date": "2026-10-02"}]
    assert c.keep_good("x", "X", [{"date": "2026-10-03"}], prev, bool, NOW, last=LAST)[0]["date"] == "2026-10-03"
    assert c.FRESH_AT["x"] == NOW
    # 비었으면 직전본
    assert c.keep_good("y", "Y", [], prev, bool, NOW, last=LAST) is prev
    assert c.STALE[-1]["key"] == "y" and c.STALE[-1]["asOf"] == "2026-10-02"
    # 멈춘 데이터인데 직전본이 더 최신이면 직전본
    old = [{"date": "2026-09-17"}]
    assert c.keep_good("z", "Z", old, prev, bool, NOW, stale_keys={"z"}, last=LAST) is prev
    assert "z" not in c.FRESH_AT
    # 계산 기준이 나빠졌으면(금액 기준) 직전본
    worse = lambda n, p: n["basis"] == "amount" and p["basis"] == "intensity"
    p2 = {"basis": "intensity", "sample": {"to": "2026-10-02"}}
    n2 = {"basis": "amount", "sample": {"to": "2026-10-05"}}
    assert c.keep_good("ant", "개미 성적표", n2, p2, bool, NOW, worse=worse) is p2
    # 같은 키는 배너에 한 번만
    keys = [s["key"] for s in c.STALE]
    assert len(keys) == len(set(keys))


def test_fetch_flows_fallback_prefers_previous_values(monkeypatch):
    rows, _ = make_market(120)
    daum = [dict(r, individual=r["individual"] + 999) for r in rows]          # 최근 값이 다른 예비 소스
    newest = dict(rows[-1], date="2026-10-05", individual=1.0)
    daum.append(newest)

    def boom(*a, **k):
        raise RuntimeError("HTTP 410")

    monkeypatch.setattr(c, "fetch_naver_trend", boom)
    monkeypatch.setattr(c, "fetch_daum_flows", lambda m, d: daum)
    out = c.fetch_flows("KOSPI", 121, prev_rows=[dict(r, cum={}) for r in rows])
    by = {r["date"]: r for r in out}
    assert all(by[r["date"]]["individual"] == r["individual"] for r in rows)   # 직전본 날짜는 직전 값
    assert by["2026-10-05"]["individual"] == 1.0                               # 새 날짜만 예비 소스
    assert "cum" not in by[rows[0]["date"]]


def test_fetch_flows_partial_naver_fills_older_from_daum(monkeypatch):
    rows, _ = make_market(300)
    monkeypatch.setattr(c, "fetch_naver_trend", lambda m, d: rows[-200:])      # 과거 페이지 실패
    monkeypatch.setattr(c, "fetch_daum_flows", lambda m, d: [dict(r, individual=-1.0) for r in rows])
    out = c.fetch_flows("KOSPI", 300)
    assert len(out) == 300
    assert out[-1] == rows[-1]                                                  # 최근은 네이버 그대로
    assert out[0]["individual"] == -1.0                                         # 오래된 쪽만 보충


# ── 전체 실행 (외부 소스는 가짜) ─────────────────────────────

def _offline(monkeypatch, *, naver=True, daum=False, fred=True, end=date(2026, 10, 2),
             kosdaq_stuck=False, kosdaq_naver=True, og_raises=False, program_lag=0, krx_blocked=False):
    freeze_now(monkeypatch, datetime(2026, 10, 5, 7, 37, tzinfo=c.KST))   # 10/5 개천절 대체 휴장일 아침
    rows, closes = make_market(750, end=end)
    kosdaq, _ = make_market(750, end=end, seed=2)
    fut = [{**{k: 0.0 for k in c.INVESTOR_GROUPS}, "date": r["date"], "foreign": -r["foreign"] / 20,
            "individual": r["foreign"] / 40, "institution": r["foreign"] / 40} for r in rows]
    series = {"KOSPI": rows, "KOSDAQ": kosdaq, "FUT": fut}

    def naver_trend(market, days):
        if not naver or (market == "KOSDAQ" and not kosdaq_naver):
            raise RuntimeError("HTTP 410")
        rows_ = series[market][:-15] if (market == "KOSDAQ" and kosdaq_stuck) else series[market]
        return [dict(r) for r in rows_[-days:]]

    def daum_flows(market, days):
        if not daum:
            raise RuntimeError("HTTP 403")
        return [{k: v for k, v in r.items() if k != "tradingValue"} for r in series[market][-days:]]

    monkeypatch.setattr(c, "fetch_naver_trend", naver_trend)
    monkeypatch.setattr(c, "fetch_daum_flows", daum_flows)
    monkeypatch.setattr(c, "fetch_intraday", lambda m: {"date": rows[-1]["date"], "final": None, "points": [
        {"t": "09:05", "individual": 1.0, "foreign": -1.0, "institution": 0.0, "other_corp": 0.0},
        {"t": "09:10", "individual": 2.0, "foreign": -2.0, "institution": 0.0, "other_corp": 0.0}]})
    monkeypatch.setattr(c, "build_market", lambda: market_payload(closes))
    monkeypatch.setattr(c, "build_credit", lambda: {"unit": "억원", "loans": [], "money": [], "latest": {}})
    monkeypatch.setattr(c, "build_stocks", lambda **kw: {"top": [], "industries": []})
    monkeypatch.setattr(c, "build_global", lambda: {"items": [], "sparkDays": 30})
    monkeypatch.setattr(c, "build_events", lambda: (
        {"upcoming": [{"type": "FOMC", "date": "2099-01-01", "title": "t", "dday": 1}], "recent": [], "today": "x"},
        [{"id": "UNRATE", "name": "실업률", "value": 4.2}] if fred else None))
    def og(*a, **k):
        if og_raises:
            raise OSError("폰트 파일이 깨짐")
    monkeypatch.setattr(c, "build_og_card", og)

    # 프로그램 매매: 시장마다 총매수를 달리 해 '두 시장이 같음' 검사에 걸리지 않게
    def program_daily(market, days, now=None):
        src = (rows if market == "KOSPI" else kosdaq)[:len(rows) - program_lag]
        out = [{"date": r["date"], "arb_net": 10.0, "nonarb_net": r["foreign"] / 10, "total_net": 10 + r["foreign"] / 10,
                "total_buy": 30000.0 if market == "KOSPI" else 9000.0} for r in src[-days:]]
        out[-1].update(total_net=-90000.0, nonarb_net=-90010.0)      # 마지막 날은 1년 중 가장 큰 순매도 → 브리핑 한 줄이 나온다
        return out
    monkeypatch.setattr(c, "fetch_program_daily", program_daily)

    monkeypatch.setattr(c.time, "sleep", lambda s: None)              # 소스 사이 쉬는 시간은 테스트에선 필요 없다
    # 공매도(KRX): 장중 건너뛰기와 상관없이 늘 받는 것으로
    monkeypatch.setattr(c, "krx_short_due", lambda now: True)
    def krx_rows(bld, market, start, end_, key):
        if krx_blocked:
            raise c.KrxBlocked("KRX MDCSTAT30201_OUT: HTTP 403")
        out = {}
        for r in rows:
            if start.isoformat() <= r["date"] <= end_.isoformat():
                d = r["date"].replace("-", "/")
                out[r["date"]] = ({"TRD_DD": d, "CVSRTSELL_TRDVAL": "500,000,000,000", "ACC_TRDVAL": "10,000,000,000,000",
                                   "TRDVAL_WT": "5.00"} if key == "TRD_DD" else
                                  {"RPT_DUTY_OCCR_DD": d, "BAL_AMT": "1,900,000,000,000", "MKTCAP": "560,000,000,000,000",
                                   "BAL_RTO2": "0.34"})
        return out
    monkeypatch.setattr(c, "_krx_market_rows", krx_rows)

    # 외국인·기관 종목 순위: 마감 뒤 확정치
    def rank(inv, mkt, per):
        base = 1 if inv == "FOREIGNER" else 2
        mk = lambda i, a: {"code": f"{base}{i:05d}", "name": f"{mkt}{inv[:1]}{i}", "amt": a, "qty": a * 100,
                           "volPct": 3.0, "chg": 1.0}
        return {"from": "2026-09-28" if per == "WEEK" else "2026-10-02", "to": "2026-10-02", "estimated": False,
                "buy": [mk(i, 500 - i) for i in range(20)], "sell": [mk(i + 50, -(500 - i)) for i in range(20)]}
    monkeypatch.setattr(c, "fetch_rank", rank)

    # 장 일정: 공식 소스가 저장소 seed 와 같은 값을 주는 것으로
    seed_by_year = {}
    for d, nm in c.CAL_SEED["holidays"].items():
        seed_by_year.setdefault(int(d[:4]), {})[d] = nm
    monkeypatch.setattr(c, "fetch_krx_holidays", lambda years: {y: dict(seed_by_year.get(y, {})) for y in years})
    monkeypatch.setattr(c, "fetch_bok_mpc", lambda y: list(c.CAL_SEED["mpc"].get(str(y), [])))


def _read(name):
    return json.loads((c.OUT / name).read_text(encoding="utf-8"))


def test_main_all_good(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    assert _read("ant.json")["basis"] == "intensity"
    assert _read("flows.json")["markets"]["KOSPI"]["daily"][-1]["date"] == "2026-10-02"
    assert len(_read("futures.json")["daily"]) == c.FUT_CHART_DAYS
    meta = _read("meta.json")
    assert meta["stale"] == [] and "flows.KOSPI" in meta["freshAt"]


def test_main_sources_dead_keeps_last_good_and_exits_3(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0                                       # 정상 실행으로 직전본을 만든다
    good = _read("flows.json")
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch, naver=False, daum=False)
    assert c.main() == 3                                       # 핵심 갱신 실패 → 워크플로가 실패로 알린다
    assert _read("flows.json")["markets"]["KOSPI"] == good["markets"]["KOSPI"]
    assert any(s["key"] == "flows.KOSPI" for s in _read("meta.json")["stale"])
    texts = " ".join(t["text"] for t in _read("insights.json")["items"])
    assert "오늘" not in texts                                 # 지난 데이터로 '오늘'을 말하지 않는다


def test_main_daum_fallback_keeps_intensity_scorecard(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch, naver=False, daum=True)              # 예비 소스엔 거래대금이 없다
    assert c.main() == 0
    assert _read("ant.json")["basis"] == "intensity"           # 금액 기준으로 떨어지지 않고 직전본 유지
    assert any(s["key"] == "ant" for s in _read("meta.json")["stale"])


def test_main_without_fred_key_leaves_macro_empty(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch, fred=False)
    assert c.main() == 0
    assert _read("global.json")["macro"] == []
    assert not any(s["key"] == "global.macro" for s in _read("meta.json")["stale"])


def test_insights_name_who_took_the_selling(monkeypatch):
    base = {"streaks": {k: {"days": 0, "side": "flat", "total": 0} for k in ("foreign", "institution", "individual")}}
    day = {"individual": -100, "foreign": -50, "institution": 30, "other_corp": 120,
           "inst_pension": 0, "inst_trust": 0, "inst_fin_inv": 0}
    flows = {"markets": {"KOSPI": {**base, "latest": day, "daily": [day] * 3}}}
    text = " ".join(t["text"] for t in c.build_insights(flows, {"indices": {}}, {"items": []}, {}))
    assert "기타법인과 기관이 받았습니다" in text and "최근 3거래일 중 3일" in text
    day2 = dict(day, other_corp=-10)
    flows["markets"]["KOSPI"].update(latest=day2, daily=[day2])
    text = " ".join(t["text"] for t in c.build_insights(flows, {"indices": {}}, {"items": []}, {}))
    assert "기관 홀로 받았습니다" in text


def test_credit_alert_needs_matching_sign_and_tail():
    def tips(d5, q):
        credit = {"latest": {"loans": {"d5": d5, "d5Pctl": q, "total": 300000, "days": 140}}}
        return [t["text"] for t in c.build_insights({"markets": {}}, {"indices": {}}, {"items": []}, {}, None, credit)]
    assert tips(6000, 95)                 # 빠르게 늘었다
    assert not tips(200, 2)               # 늘긴 했지만 가장 느린 쪽 → 경보 아님
    assert tips(-6000, 3)                 # 빠르게 줄었다
    assert not tips(6000, 70)             # 평범


def test_main_adds_kosdaq_scorecard_and_intraday(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    ant = _read("ant.json")
    kq = ant["byMarket"]["KOSDAQ"]
    assert kq["market"] == "KOSDAQ" and kq["basis"] == "intensity" and set(kq["actors"]) == set(c.ACTOR_KEYS)
    assert _read("flows.json")["markets"]["KOSPI"]["intraday"]["points"][-1]["t"] == "09:10"


CODES = ["1000", "2000", "3000", "3100", "4000", "5000", "6000", "7000", "7100", "8000", "9000", "9001"]


def _minute(day, t, indiv_won, foreign_won=0):
    amt = {k: 0 for k in CODES}
    amt["8000"], amt["9000"] = indiv_won, foreign_won
    return {"bizdate": day, "time": t, "netAmounts": [{"investorGubun": k, "diffValue": str(v)} for k, v in amt.items()]}


def _serve(monkeypatch, rows, page_size=3):
    """최신순 rows 를 page_size 씩 나눠 주는 가짜 API (last 플래그 포함)."""
    pages = [rows[i:i + page_size] for i in range(0, len(rows), page_size)]
    calls = []

    def fake(url, tries=3, **kw):
        n = kw["params"]["startIdx"]
        calls.append(n)
        return {"content": pages[n] if n < len(pages) else [], "last": n >= len(pages) - 1}

    monkeypatch.setattr(c, "get_json", fake)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    return calls


def test_fetch_intraday_values_dates_pages_and_close(monkeypatch):
    rows = [  # 최신순
        _minute("20261002", "200400", -1_789_715_000_000, -108_000_000_000),   # 확정치
        _minute("20261002", "153100", -1_720_600_000_000),                     # 종가 단일가 체결분
        _minute("20261002", "153000", -1_603_400_000_000),                     # 동시호가 중(체결 전)
        _minute("20261002", "152800", -1_603_400_000_000),
        _minute("20261002", "091200", 123_456_789_000),                         # 1,235억
        _minute("20261002", "090300", 10_000_000_000),
        _minute("20261001", "153000", 9_900_000_000_000),                       # 전날 — 버려야 함
        _minute("20261001", "091200", 9_900_000_000_000),                       # 전날, 보이는 시각에도
    ]
    calls = _serve(monkeypatch, rows)
    it = c.fetch_intraday("KOSPI")
    assert calls == [0, 1, 2]                                   # 마지막 페이지까지 넘긴다
    assert it["date"] == "2026-10-02" and it["open"] == "09:00" and it["close"] == "15:30"
    by = {p["t"]: p for p in it["points"]}
    assert by["09:12"]["individual"] == 1235.0                  # 원 → 억원 반올림
    assert by["15:30"]["individual"] == -17206.0                # 마감 점은 종가 체결 뒤 값(전날 값도, 체결 전 값도 아님)
    assert "20:04" not in by
    assert it["final"]["t"] == "20:04" and it["final"]["individual"] == -17897.0 and it["final"]["foreign"] == -1080.0
    assert it["after"] is None
    ts = [p["t"] for p in it["points"]]
    assert ts == sorted(ts)
    assert len(ts) == len(set((int(t[:2]) * 60 + int(t[3:]) - 1) // c.INTRADAY_STEP for t in ts))  # 5분 구간당 하나


def test_fetch_intraday_after_close_is_not_final_before_20(monkeypatch):
    _serve(monkeypatch, [_minute("20261002", "181600", -1_749_300_000_000),
                         _minute("20261002", "100000", 1_000_000_000)])
    it = c.fetch_intraday("KOSPI")
    assert it["final"] is None and it["after"]["t"] == "18:16"   # 20시 전 값은 '확정'이라 부르지 않는다


def test_fetch_intraday_special_session(monkeypatch):
    # 수능일: 10:00~16:30 — 16:00 대 값도 정규장 점으로 들어가고, 16:30 이후는 마감 뒤 값
    _serve(monkeypatch, [_minute("20261119", "163500", 30_000_000_000),
                         _minute("20261119", "160500", 20_000_000_000),
                         _minute("20261119", "101000", 10_000_000_000)])
    it = c.fetch_intraday("KOSPI")
    assert it["open"] == "10:00" and it["close"] == "16:30"
    assert [p["t"] for p in it["points"]] == ["10:10", "16:05", "16:30"]
    assert it["points"][-1]["individual"] == 300.0              # 16:31~16:39 사이 첫 행 = 종가 체결분
    assert it["after"]["t"] == "16:35"


def test_main_skips_kosdaq_scorecard_when_kosdaq_stuck(monkeypatch):
    _offline(monkeypatch, kosdaq_stuck=True)
    assert c.main() == 0                                        # 코스피는 정상
    assert "byMarket" not in _read("ant.json")                  # 멈춘 코스닥 성적표는 싣지 않는다


def test_main_keeps_previous_kosdaq_scorecard_when_it_degrades(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch, kosdaq_naver=False, daum=True)        # 코스닥만 예비 소스(거래대금 없음)
    assert c.main() == 0
    ant = _read("ant.json")
    assert ant["basis"] == "intensity" and ant["byMarket"]["KOSDAQ"]["basis"] == "intensity"
    assert any(s["key"] == "ant.KOSDAQ" for s in _read("meta.json")["stale"])


def test_main_survives_og_card_failure(monkeypatch):
    _offline(monkeypatch, og_raises=True)
    assert c.main() == 0
    assert any("공유 카드" in w for w in _read("meta.json")["warnings"])


def test_market_phase_evening_and_special_session():
    from datetime import datetime
    K = c.KST
    assert c.market_phase(datetime(2026, 10, 6, 19, 30, tzinfo=K), None) == "장마감(수급 확정 반영중)"
    assert c.market_phase(datetime(2026, 10, 6, 20, 30, tzinfo=K), None) == "장마감"
    assert c.market_phase(datetime(2026, 11, 19, 16, 10, tzinfo=K), {"tradedAt": "2026-11-19T16:10"}) == "장중"


def test_find_korean_font_prefers_og_font_dir(tmp_path, monkeypatch):
    for f in ("NanumGothic-Bold.ttf", "NanumGothic-Regular.ttf"):
        (tmp_path / f).write_bytes(b"")
    monkeypatch.setenv("OG_FONT_DIR", str(tmp_path))
    bold, regular = c.find_korean_font()
    assert Path(bold) == tmp_path / "NanumGothic-Bold.ttf" and Path(regular) == tmp_path / "NanumGothic-Regular.ttf"


def test_feed_entry_provisional_then_final_and_valid_xml():
    import xml.etree.ElementTree as ET
    from datetime import datetime
    day = {"date": "2026-10-06", "individual": -17897.0, "foreign": -1080.0, "institution": 4155.0}
    flows = {"markets": {"KOSPI": {"latest": day}}}
    market = {"indices": {"KOSPI": {"price": 7003.74, "changeRate": 0.46}}}
    tips = [{"text": "외국인이 <6>거래일 & 연속"}]
    f1 = c.build_feed({}, flows, market, tips, datetime(2026, 10, 6, 15, 37, tzinfo=c.KST))
    assert f1["entries"][0]["final"] is False and f1["entries"][0]["title"].endswith("(잠정)")
    f2 = c.build_feed(f1, flows, market, tips, datetime(2026, 10, 6, 20, 23, tzinfo=c.KST))
    e = f2["entries"][0]
    assert len(f2["entries"]) == 1 and e["final"] is True and "-1.79조" in e["title"] and "+4,155억" in e["title"]
    root = ET.fromstring(c.feed_xml(f2))                     # 특수문자가 있어도 올바른 XML
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert root.find("a:entry/a:content", ns).text.startswith("외국인이 <6>거래일 & 연속")
    # 수집 실패(flows 없음)면 직전 피드 그대로
    assert c.build_feed(f2, None, market, [], datetime(2026, 10, 7, 9, 7, tzinfo=c.KST))["entries"] == f2["entries"]


def test_telegram_sends_final_once(monkeypatch):
    sent = []

    class R:
        status_code = 200
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(c.session, "post", lambda url, **kw: sent.append((url, kw)) or R())
    feed = {"entries": [{"id": "2026-10-06", "title": "t", "summary": "s", "final": True, "sent": False}]}
    c.notify_telegram(feed)                                   # 토큰 없음 → 안 보냄
    assert not sent
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    c.notify_telegram(feed)
    c.notify_telegram(feed)                                   # 두 번째는 보내지 않는다
    assert len(sent) == 1 and feed["entries"][0]["sent"] is True and sent[0][1]["json"]["chat_id"] == "42"
    feed["entries"][0].update(final=False, sent=False)
    c.notify_telegram(feed)                                   # 잠정본은 보내지 않는다
    assert len(sent) == 1
    c.warn("x 123:SECRET y")
    assert "SECRET" not in c.WARNINGS[-1]                    # 토큰은 경고에 섞이지 않는다


def test_main_writes_feed(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    assert _read("feed.json")["entries"][0]["id"] == "2026-10-02-post"
    assert (c.OUT / "feed.xml").read_text(encoding="utf-8").startswith("<?xml")
    assert _read("stockflows.json")["unit"] == "주"


def test_main_writes_program_short_calendar(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    prog = _read("program.json")["markets"]
    assert len(prog["KOSPI"]["daily"]) == c.PROGRAM_CHART_DAYS and prog["KOSPI"]["latest"]["date"] == "2026-10-02"
    assert prog["KOSDAQ"]["latest"]["pctl"] is not None
    sh = _read("short.json")
    k = sh["markets"]["KOSPI"]
    assert cal["source"]["holidays.2027"] == "KRX" if (cal := _read("calendar.json")) else False
    assert k["daily"][0]["date"] >= c.SHORT_RESUMED and k["daily"][-1] == {"date": "2026-10-02", "value": 5000,
                                                                          "total": 100000, "pct": 5.0}
    assert k["latest"]["balance"]["pct"] == 0.34 and k["latest"]["daily"]["pctAvg20"] == 5.0
    cal = _read("calendar.json")
    assert cal["source"]["holidays.2026"] == "KRX" and "2026-10-05" in cal["holidays"]["2026"]
    meta = _read("meta.json")
    assert meta["stale"] == [] and "program.KOSPI" in meta["freshAt"] and "short.KOSPI" in meta["freshAt"]


def test_main_flags_stuck_program_and_blocked_krx(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    good_short = _read("short.json")["markets"]["KOSPI"]["daily"]
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch, program_lag=3, krx_blocked=True)
    assert c.main() == 0                                        # 부가 섹션이라 실행 실패로 만들지 않는다
    stale = {s_["key"]: s_ for s_ in _read("meta.json")["stale"]}
    assert "program.KOSPI" in stale and stale["program.KOSPI"]["asOf"] == "2026-10-02"   # 더 최신인 직전본을 유지
    assert _read("program.json")["markets"]["KOSPI"]["latest"]["date"] == "2026-10-02"
    assert "short.KOSPI" in stale and "short.KOSDAQ" in stale
    assert _read("short.json")["markets"]["KOSPI"]["daily"] == good_short   # 막혀도 직전 행은 그대로
    assert sum("공매도" in w for w in c.WARNINGS) == 1                       # 한 번 막히면 나머지 요청은 건너뛴다
    # 브리핑은 멈춘 프로그램 매매로 '오늘'을 말하지 않는다
    assert not any("프로그램" in t["text"] for t in _read("insights.json")["items"])


def test_program_briefing_only_for_the_flows_day(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    assert any("코스피 프로그램 매매가 9.00조원 순매도" in t["text"] for t in _read("insights.json")["items"])
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    # 하루 늦은 프로그램 매매(장중에 흔함)는 멈춤으로 치지 않지만, 날짜 없는 브리핑 문장에는 쓰지 않는다
    _offline(monkeypatch, program_lag=1)
    for f in ("program.json",):
        (c.OUT / f).unlink()
    assert c.main() == 0
    assert not any("프로그램" in t["text"] for t in _read("insights.json")["items"])
    assert "program.KOSPI" not in {s_["key"] for s_ in _read("meta.json")["stale"]}


def test_short_balance_freeze_is_flagged(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    sh = _read("short.json")
    for m in sh["markets"].values():                     # 잔고만 9월 초에서 멈춘 직전 파일
        m["balance"] = [r for r in m["balance"] if r["date"] <= "2026-09-03"]
    c.write("short.json", sh, compact=True)
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch)
    real = c._krx_market_rows
    def no_balance(bld, market, start, end_, key):
        if key == "RPT_DUTY_OCCR_DD":
            return {d: {**x, "BAL_AMT": "-"} for d, x in real(bld, market, start, end_, key).items()}   # 필드가 비어 옴
        return real(bld, market, start, end_, key)
    monkeypatch.setattr(c, "_krx_market_rows", no_balance)
    assert c.main() == 0
    stale = {s_["key"] for s_ in _read("meta.json")["stale"]}
    assert {"short.KOSPI.balance", "short.KOSDAQ.balance"} <= stale and "short.KOSPI" not in stale
    assert any("값이 읽힌 행 0개" in w for w in c.WARNINGS)


def test_short_balance_silently_frozen_at_source_is_flagged(monkeypatch):
    # KRX 가 잔고를 정상 형식으로 주지만 9/3 이후 행이 더 생기지 않는 경우 — 실패 기록 없이 나이로 잡는다
    _offline(monkeypatch)
    real = c._krx_market_rows
    monkeypatch.setattr(c, "_krx_market_rows", lambda bld, market, start, end_, key: {
        d: x for d, x in real(bld, market, start, end_, key).items() if key == "TRD_DD" or d <= "2026-09-03"})
    assert c.main() == 0
    stale = {s_["key"]: s_ for s_ in _read("meta.json")["stale"]}
    assert stale["short.KOSPI.balance"]["asOf"] == "2026-09-03" and "short.KOSPI" not in stale


def test_joint_stall_across_holidays_is_still_caught():
    # 지수 일봉과 수급이 9/22 에 함께 멈춘 채 10/1 장중 — 추석(9/24·25)을 빼도 4거래일이 빠졌다
    m = {"indices": {"KOSPI": {"name": "코스피", "tradedAt": "2026-10-01T10:05:00+09:00", "marketStatus": "OPEN",
                                "price": 1.0}}}
    flows = {"markets": {"KOSPI": {"daily": [{"date": "2026-09-21"}, {"date": "2026-09-22"}]}}}
    stale = c.check_freshness(flows, m, {"KOSPI": {"2026-09-21": 1.0, "2026-09-22": 1.0}}, today="2026-10-01")
    assert stale == {"market.KOSPI.history", "flows.KOSPI"}
    # 휴장 다음 날 아침(하루만 빠짐)은 여전히 정상
    m["indices"]["KOSPI"]["tradedAt"] = "2026-10-06T09:05:00+09:00"
    flows = {"markets": {"KOSPI": {"daily": [{"date": "2026-10-02"}]}}}
    assert c.check_freshness(flows, m, {"KOSPI": {"2026-10-02": 1.0}}, today="2026-10-06") == set()


def test_market_phase_trusts_live_quote_over_holiday_list():
    c.HOLIDAYS["2026-10-08"] = "잘못 적힌 날"
    K = c.KST
    assert c.market_phase(datetime(2026, 10, 8, 8, 0, tzinfo=K)) == "휴장일"
    assert c.market_phase(datetime(2026, 10, 8, 11, 0, tzinfo=K), {"tradedAt": "2026-10-08T11:00"}) == "장중"
    assert "2026-10-08" not in c.HOLIDAYS and any("잘못 적힌 날" in w for w in c.WARNINGS)


def test_first_trading_day_opens_at_ten():
    assert c.session_hours("2026-01-02") == ("1000", "1530")
    assert c.session_hours("2027-01-04") == ("1000", "1530")     # 1/1 신정 뒤 첫 평일
    assert c.session_hours("2027-01-05") == ("0900", "1530")
    assert c.session_hours("2026-11-19") == ("1000", "1630")     # 수능일
    assert c.market_phase(datetime(2027, 1, 4, 9, 30, tzinfo=c.KST), {"tradedAt": "2026-12-30T15:30"}) == "동시호가"


def test_feed_final_waits_for_program_trading_to_settle():
    day = {"date": "2026-10-06", "individual": -1.0, "foreign": 1.0, "institution": 0.0}
    flows = {"markets": {"KOSPI": {"latest": day}}}
    f = c.build_feed({}, flows, {"indices": {}}, [], datetime(2026, 10, 6, 20, 3, tzinfo=c.KST))
    assert f["entries"][0]["final"] is False
    f = c.build_feed(f, flows, {"indices": {}}, [], datetime(2026, 10, 6, 20, 6, tzinfo=c.KST))
    assert f["entries"][0]["final"] is True


def test_main_meta_dates_and_date_aware_briefing(monkeypatch):
    _offline(monkeypatch)                                   # 시계: 10/5(휴장일) 07:37, 데이터: 10/2
    assert c.main() == 0
    meta = _read("meta.json")
    assert meta["dataDate"] == "2026-10-02" and meta["dataFinal"] is True and meta["nextSession"] == "2026-10-06"
    texts = [t["text"] for t in _read("insights.json")["items"]]
    assert not any(t.startswith("오늘") for t in texts)      # 휴장일 아침에 10/2 를 '오늘'이라 부르지 않는다
    assert not any("20거래일 뒤 지수는 평균" in t for t in texts)   # 표본 밖에서 유지 안 된 기록은 브리핑에서 뺀다
    flows = _read("flows.json")["markets"]["KOSPI"]
    assert set(flows["streaks"]) == {"individual", "foreign", "institution", "other_corp"}
    ant = _read("ant.json")
    assert all("dayChange" in h for h in ant["hallOfFame"])
    cr = ant.get("contrarianRead")
    if cr:
        assert cr["longRun"]["period"] == "2009-03~2023-08" and cr["longRun"]["significant"] is False
    assert "2009-03~2023-08" in ant["caveats"][0]


def test_next_session_and_briefing_words():
    K = c.KST
    assert c.next_session(datetime(2026, 10, 6, 10, 0, tzinfo=K)).isoformat() == "2026-10-06"   # 장중
    assert c.next_session(datetime(2026, 10, 6, 16, 0, tzinfo=K)).isoformat() == "2026-10-07"   # 마감 뒤
    assert c.next_session(datetime(2026, 10, 8, 18, 0, tzinfo=K)).isoformat() == "2026-10-12"   # 10/9 한글날·주말
    latest = {"date": "2026-10-02", "individual": 5.0, "foreign": -9.0, "institution": 1.0, "other_corp": 3.0}
    flows = {"markets": {"KOSPI": {"latest": latest, "daily": [latest], "streaks": {
        k: {"days": 1, "side": "buy", "total": 1.0} for k in ("individual", "foreign", "institution")}}}}
    sox = {"name": "필라델피아 반도체", "changeRate": 3.0, "asOf": "2026-10-02"}
    tips = [t["text"] for t in c.build_insights(flows, {"indices": {}}, {"items": [sox]}, {}, today="2026-10-05")]
    assert any(t.startswith("10/2(금)에는 외국인이 판 물량") for t in tips)
    assert any(t.startswith("10/2(금) 미국장에서 필라델피아") for t in tips)
    tips = [t["text"] for t in c.build_insights(flows, {"indices": {}}, {"items": [{**sox, "asOf": "2026-10-04"}]}, {},
                                                today="2026-10-02")]
    assert any(t.startswith("오늘은 외국인이") for t in tips)


def test_main_writes_ranks_today_and_counts_requests(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    ranks = _read("ranks.json")
    assert ranks["asOf"] == "2026-10-02" and ranks["final"] is True
    assert len(ranks["markets"]["KOSPI"]["foreign"]["day"]["buy"]) == 20
    today = _read("today.json")
    assert today["footer"] and today["date"] == "2026-10-02"
    meta = _read("meta.json")
    assert meta["mode"] == "full" and meta["requests"]["date"] == "2026-10-05"
    flows = _read("flows.json")
    assert flows["streakBaseline"] and "rarity" in flows["markets"]["KOSPI"]["streaks"]["foreign"]
    assert not any("순위" in w for w in c.WARNINGS)


def test_run_intraday_patches_on_top_of_last_full_run(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0                                         # 전체 수집이 직전 정상본을 만든다
    full_meta = _read("meta.json")
    snap = {p.relative_to(c.OUT).as_posix(): p.read_bytes() for p in c.OUT.rglob("*") if p.is_file()}
    full_prog = _read("program.json")["markets"]["KOSPI"]
    c.WARNINGS.clear()
    freeze_now(monkeypatch, datetime(2026, 10, 6, 10, 7, tzinfo=c.KST))   # 다음 거래일 장중
    basic = {"closePrice": "7,050.00", "compareToPreviousClosePrice": "46.26", "fluctuationsRatio": "0.66",
             "marketStatus": "OPEN", "localTradedAt": "2026-10-06T10:07:00+09:00"}
    monkeypatch.setattr(c, "get_json", lambda url, *a, **k: basic)
    today = {"date": "2026-10-06", "individual": -100.0, "foreign": 80.0, "institution": 20.0, "other_corp": 0.0}
    asked = {}
    def trend(market, days, page_size=200):
        asked["trend"] = (days, page_size)
        return [dict(today)]
    monkeypatch.setattr(c, "fetch_naver_trend", trend)
    monkeypatch.setattr(c, "fetch_intraday", lambda m: {"date": "2026-10-06", "points": [], "final": None, "after": None})
    monkeypatch.setattr(c, "fetch_program_daily", lambda m, d, now=None, page_size=200: [
        {"date": "2026-10-06", "arb_net": 1.0, "nonarb_net": 2.0, "total_net": 3.0, "total_buy": 9.0, "provisional": True}])
    assert c.run_intraday() == 0
    assert asked["trend"] == (5, 10)                             # 오늘 행만 작게 받는다
    flows = _read("flows.json")["markets"]["KOSPI"]
    assert flows["latest"]["date"] == "2026-10-06" and flows["daily"][-2]["date"] == "2026-10-02"
    assert flows["latest"]["cum"]["individual"] == round(flows["daily"][-2]["cum"]["individual"] - 100.0, 1)
    assert flows["intraday"]["date"] == "2026-10-06"
    prog = _read("program.json")["markets"]["KOSPI"]["latest"]
    assert prog["date"] == "2026-10-06" and prog["provisional"] is True
    assert prog["pctl"] == round(c._percentile(full_prog["settled"], 3.0), 1)     # 확정 1년 분포에서 다시 잰 위치
    prev_st = full_prog["latest"]["streak"]
    assert prog["streak"]["days"] == (prev_st["days"] + 1 if prev_st["side"] == "buy" else 1)   # 5일에 묶이지 않는다
    meta = _read("meta.json")
    assert meta["mode"] == "intraday" and meta["phase"] == "장중" and meta["fullAt"] == full_meta["fullAt"]
    assert meta["dataDate"] == "2026-10-06" and meta["dataFinal"] is False
    assert _read("market.json")["indices"]["KOSPI"]["price"] == 7050.0
    after = {p.relative_to(c.OUT).as_posix(): p.read_bytes() for p in c.OUT.rglob("*") if p.is_file()}
    touched = {"flows.json", "program.json", "market.json", "meta.json", "today.json", "insights.json"}
    assert set(after) == set(snap)                                # 새 파일도, 지운 파일도 없다
    assert {k: v for k, v in after.items() if k not in touched} == {k: v for k, v in snap.items() if k not in touched}
    assert _read("today.json")["provisional"] is True             # 장중 숫자로 다시 쓴 '평소와 다른 것'
    assert not any("20거래일 뒤" in t["text"] for t in _read("insights.json")["items"])


def test_run_intraday_on_holiday_skips_flows(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    before = _read("flows.json")
    basic = {"closePrice": "7,003.74", "marketStatus": "CLOSE", "localTradedAt": "2026-10-02T20:15:00+09:00"}
    monkeypatch.setattr(c, "get_json", lambda url, *a, **k: basic)
    called = []
    rec = lambda name: (lambda *a, **k: called.append(name) or [])
    monkeypatch.setattr(c, "fetch_naver_trend", rec("trend"))
    monkeypatch.setattr(c, "fetch_intraday", rec("intra"))
    monkeypatch.setattr(c, "fetch_program_daily", rec("prog"))
    prog_before = _read("program.json")
    assert c.run_intraday() == 0                                 # 10/5 휴장일 07:37
    assert called == []
    assert _read("program.json") == prog_before
    assert _read("flows.json")["markets"]["KOSPI"]["latest"] == before["markets"]["KOSPI"]["latest"]
    assert _read("meta.json")["phase"] == "휴장일"


def test_request_cap_skips_heavy_sections(monkeypatch):
    _offline(monkeypatch)
    c.write("meta.json", {"requests": {"date": "2026-10-05", "count": c.REQUEST_CAP}})
    called = []
    monkeypatch.setattr(c, "build_universe", lambda now: called.append("universe"))
    monkeypatch.setattr(c, "build_ranks", lambda *a, **k: called.append("ranks"))
    assert c.main() == 0
    assert called == []
    assert any("상한" in w for w in c.WARNINGS)
    assert _read("meta.json")["requests"]["count"] >= c.REQUEST_CAP


def test_run_intraday_extends_long_program_streak(monkeypatch):
    _offline(monkeypatch)
    assert c.main() == 0
    prog = _read("program.json")
    days = [r["date"] for r in _read("flows.json")["markets"]["KOSPI"]["daily"]][-12:]
    for code in ("KOSPI", "KOSDAQ"):                              # 12거래일 연속 순매수였던 것으로
        prog["markets"][code]["daily"] = [{"date": d, "arb": 1.0, "nonarb": 9.0, "total": 10.0} for d in days]
    c.write("program.json", prog)
    freeze_now(monkeypatch, datetime(2026, 10, 6, 10, 7, tzinfo=c.KST))
    basic = {"closePrice": "7,050.00", "marketStatus": "OPEN", "localTradedAt": "2026-10-06T10:07:00+09:00"}
    monkeypatch.setattr(c, "get_json", lambda url, *a, **k: basic)
    monkeypatch.setattr(c, "fetch_naver_trend", lambda m, d, page_size=200: [])
    monkeypatch.setattr(c, "fetch_intraday", lambda m: None)
    rows = [{"date": d, "arb_net": 1.0, "nonarb_net": 9.0, "total_net": 10.0, "total_buy": 9.0} for d in days[-4:]]
    rows.append({"date": "2026-10-06", "arb_net": 1.0, "nonarb_net": 4.0, "total_net": 5.0, "total_buy": 9.0,
                 "provisional": True})
    monkeypatch.setattr(c, "fetch_program_daily", lambda m, d, now=None, page_size=200: [dict(r) for r in rows])
    assert c.run_intraday() == 0
    st = _read("program.json")["markets"]["KOSPI"]["latest"]["streak"]
    assert st["days"] == 13 and st["side"] == "buy"              # 받은 5행이 아니라 이어 붙인 전체에서 센다


def test_main_waits_for_stock_rows_and_flags_pending_final(monkeypatch):
    _offline(monkeypatch)
    freeze_now(monkeypatch, datetime(2026, 10, 2, 20, 30, tzinfo=c.KST))    # 그날 확정 수집 시각
    c.write("stocks.json", {"top": [], "industries": [], "universe": [{"code": "005930", "name": "삼성전자", "asOf": "2026-10-01"}],
                            "universeAt": "2026-10-01T20:20:00+09:00", "universePending": 0})
    called = []
    monkeypatch.setattr(c, "build_universe", lambda now, backfill_only=False: called.append(backfill_only))
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: [["20261001", 1, 1, 1, 1.0, 1.0, 1]])
    assert c.main() == 0
    assert called == []                                                      # 종목별 10/2 수급이 아직 없음 → 350종목 안 받음
    assert _read("meta.json")["pendingFinal"] is True                       # 23:30 까지 다시 시도하라는 표시
    c.WARNINGS.clear(); c.STALE.clear(); c.FRESH_AT.clear()
    _offline(monkeypatch)
    freeze_now(monkeypatch, datetime(2026, 10, 2, 21, 0, tzinfo=c.KST))
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: [["20261002", 1, 1, 1, 1.0, 1.0, 1]])
    uni = [{"code": f"{i:06d}", "name": "x", "market": "KOSPI", "asOf": "2026-10-02",
            "foreign": {"v60": 1.0}, "institution": {"v60": 1.0}} for i in range(30)]
    monkeypatch.setattr(c, "build_universe", lambda now, backfill_only=False: (called.append(backfill_only) or (uni, {}, 0)))
    assert c.main() == 0
    assert called == [False]                                                 # 올라왔으니 받는다(채우는 중 아님 → 전체)
    assert _read("meta.json")["pendingFinal"] is False
