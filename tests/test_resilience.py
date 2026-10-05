"""
소스가 죽거나 멈췄을 때의 동작 — 2026-09 에 수급 소스 폐지와 FDR 정지가 겹쳐 사이트가 17일간 빈 채로 나갔다.
"""
import json
from pathlib import Path
from datetime import date, timedelta

import pytest

import collect as c
from conftest import make_market, market_payload


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
             kosdaq_stuck=False, kosdaq_naver=True, og_raises=False):
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
