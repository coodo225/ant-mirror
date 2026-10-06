"""
국내 장 일정(휴장일·만기·금통위), 프로그램 매매, 공매도 — 네트워크 없이 파싱·계산·실패 경로를 본다.
"""
import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pytest

import collect as c
from conftest import freeze_now

K = timezone(timedelta(hours=9))


# ── 만기·휴장 ────────────────────────────────────────────────

def test_expiries_second_thursday_moved_earlier_on_holidays():
    got = c.derivative_expiries(date(2026, 1, 1), date(2027, 12, 31))
    assert [x["date"] for x in got] == [
        "2026-01-08", "2026-02-12", "2026-03-12", "2026-04-09", "2026-05-14", "2026-06-11",
        "2026-07-09", "2026-08-13", "2026-09-10", "2026-10-08", "2026-11-12", "2026-12-10",
        "2027-01-14", "2027-02-11", "2027-03-11", "2027-04-08",
        "2027-05-12",                                   # 둘째 목요일 5/13 석가탄신일 → 하루 앞당김
        "2027-06-10", "2027-07-08", "2027-08-12", "2027-09-09", "2027-10-14", "2027-11-11", "2027-12-09"]
    assert [x["date"][5:7] for x in got if x["quarterly"]] == ["03", "06", "09", "12"] * 2


def test_new_holiday_moves_expiry():
    c.HOLIDAYS["2026-10-08"] = "임시공휴일"
    assert c.derivative_expiries(date(2026, 10, 1), date(2026, 10, 31))[0]["date"] == "2026-10-07"


def test_korean_events_group_long_holidays():
    ev = c.korean_events(date(2026, 9, 20))
    chuseok = [e for e in ev if e["type"] == "휴장" and "추석" in e["title"]]
    assert len(chuseok) == 1 and chuseok[0]["date"] == "2026-09-24"
    assert "9/24(목)~9/25(금)" in chuseok[0]["note"] and "다음 거래일 9/28(월)" in chuseok[0]["note"]
    # 연휴 중간에는 오늘 날짜로 띄운다 (지난 날짜면 '다가오는 이벤트'에서 빠진다)
    assert [e["date"] for e in c.korean_events(date(2026, 9, 25)) if "추석" in e["title"]] == ["2026-09-25"]
    # 연말 휴장(12/31 목)과 신정(1/1 금)은 이어진 연휴
    end = [e for e in c.korean_events(date(2026, 12, 20)) if e["type"] == "휴장" and e["date"] == "2026-12-31"]
    assert end and "신정" in end[0]["title"] and "다음 거래일 1/4(월)" in end[0]["note"]
    kinds = {e["type"] for e in ev}
    assert {"금통위", "만기", "휴장"} <= kinds
    assert any(e["type"] == "금통위" and e["date"] == "2026-10-22" for e in ev)


def test_market_phase_knows_holidays_before_open():
    # 예전엔 휴장일 아침 9시 5분 전까지 '장전'으로 떴다
    assert c.market_phase(datetime(2026, 10, 9, 8, 0, tzinfo=K)) == "휴장일"
    assert c.market_phase(datetime(2026, 10, 8, 8, 0, tzinfo=K)) == "장전"


def test_build_events_keeps_next_fomc_and_mpc(monkeypatch):
    freeze_now(monkeypatch, datetime(2026, 10, 5, 7, 37, tzinfo=K))
    monkeypatch.setattr(c, "scrape_fomc", lambda: [{"type": "FOMC", "date": "2027-06-16", "title": "FOMC", "impact": "high"}])
    monkeypatch.setattr(c, "fetch_fred_events", lambda key: [])
    monkeypatch.setattr(c, "fetch_fred_series", lambda key: [])
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(c, "SEED", c.ROOT / "없음.json")
    monkeypatch.setattr(c, "MPC", {"2027": ["2027-02-25"]})      # 휴장·만기 10건 뒤에 오는 금통위
    events, _ = c.build_events()
    up = events["upcoming"]
    assert len(up) == c.UPCOMING_MAX
    assert any(e["type"] == "FOMC" and e["date"] == "2027-06-16" for e in up)
    assert any(e["type"] == "금통위" and e["date"] == "2027-02-25" for e in up)
    assert [e["date"] for e in up] == sorted(e["date"] for e in up)
    assert up[0]["type"] == "휴장" and up[0]["dday"] == 0          # 오늘(10/5) 휴장


# ── 한국은행 금통위 ─────────────────────────────────────────

def _bok_html(year, rows):
    trs = "".join(f'<tr><th scope="row">{r}</th><td>통화정책방향</td></tr>' for r in rows)
    return (f'<div class="h-group"><h3>{year}년</h3></div><table id="tableId"><thead></thead>'
            f'<tbody>{trs}</tbody></table>')


def test_parse_bok_mpc():
    assert c.parse_bok_mpc(_bok_html(2026, ["01월 15일(목)", "02월 26일(목)", "04월 10일(금)", "05월 28일(목)"]), 2026) == \
        ["2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28"]
    assert c.parse_bok_mpc(_bok_html(2027, []), 2027) == []                  # 아직 공표 전
    with pytest.raises(RuntimeError, match="요일"):
        c.parse_bok_mpc(_bok_html(2026, ["01월 15일(금)"] * 4), 2026)
    with pytest.raises(RuntimeError, match="연도"):
        c.parse_bok_mpc(_bok_html(0, []), 2026)
    with pytest.raises(RuntimeError, match="형식"):
        c.parse_bok_mpc('<div class="h-group"><h3>2026년</h3></div><table id="tableId"><tbody>'
                        '<tr><td>1월 15일</td></tr></tbody></table>', 2026)


# ── 장 일정 갱신 ────────────────────────────────────────────

def test_refresh_calendar_official_first_then_throttled(monkeypatch):
    calls = {"krx": 0, "bok": 0}
    krx_2026 = {d: n for d, n in c.CAL_SEED["holidays"].items() if d.startswith("2026")}
    krx_2026["2026-10-07"] = "임시공휴일"

    def krx(years):
        calls["krx"] += 1
        return {2026: dict(krx_2026), 2027: {"2027-01-01": "신정"}}        # 2027 은 너무 적다 → 쓰지 않음

    def bok(y):
        calls["bok"] += 1
        return ["2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28", "2026-07-16",
                "2026-08-27", "2026-10-22", "2026-11-26"] if y == 2026 else []
    monkeypatch.setattr(c, "fetch_krx_holidays", krx)
    monkeypatch.setattr(c, "fetch_bok_mpc", bok)
    now = datetime(2026, 10, 5, 7, 37, tzinfo=K)
    cal = c.refresh_calendar(now)
    assert cal["source"]["holidays.2026"] == "KRX" and cal["source"]["holidays.2027"] == "seed"
    assert "2026-10-07" in c.HOLIDAYS and "2027-02-08" in c.HOLIDAYS        # 2027 은 seed 유지
    assert cal["source"]["mpc.2026"] == "한국은행" and "2027" not in cal["mpc"]
    c.write("calendar.json", cal)

    # 1시간 뒤: 다시 받지 않는다(다음 해가 비어 있어도 하루에 한 번)
    def boom(*a):
        raise AssertionError("받으면 안 됨")
    monkeypatch.setattr(c, "fetch_krx_holidays", boom)
    monkeypatch.setattr(c, "fetch_bok_mpc", boom)
    c.HOLIDAYS.clear()
    c.refresh_calendar(now + timedelta(hours=1))
    assert "2026-10-07" in c.HOLIDAYS                                       # 직전에 받은 공식 목록이 seed 보다 우선

    # 이틀 뒤 KRX 가 막히면 네이버 캘린더에서 찾은 휴장일을 더한다
    def krx_down(years):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(c, "fetch_krx_holidays", krx_down)
    monkeypatch.setattr(c, "fetch_bok_mpc", bok)
    monkeypatch.setattr(c, "fetch_naver_calendar",
                        lambda cat, a, b: [("2027-01-29", "한국 휴장일", "임시공휴일")] if cat == "holiday" else [])
    cal = c.refresh_calendar(now + timedelta(days=2))
    assert c.HOLIDAYS.get("2027-01-29") == "임시공휴일" and "2026-10-07" in c.HOLIDAYS
    assert cal["extraHolidays"] == {"2027-01-29": "임시공휴일"}
    assert any("KRX 휴장일 갱신 실패" in w for w in c.WARNINGS)


def _prev_krx_calendar(monkeypatch):
    """KRX 목록을 받아 둔 직전 calendar.json 을 만든다."""
    by_year = {}
    for d, n in c.CAL_SEED["holidays"].items():
        by_year.setdefault(int(d[:4]), {})[d] = n
    monkeypatch.setattr(c, "fetch_krx_holidays", lambda years: {y: dict(by_year.get(y, {})) for y in years})
    monkeypatch.setattr(c, "fetch_bok_mpc", lambda y: list(c.CAL_SEED["mpc"].get(str(y), [])))
    c.write("calendar.json", c.refresh_calendar(datetime(2026, 10, 5, 7, 37, tzinfo=K)))


def test_naver_fallback_and_seed_additions_survive_stored_krx_list(monkeypatch):
    _prev_krx_calendar(monkeypatch)
    monkeypatch.setattr(c, "fetch_krx_holidays", lambda years: (_ for _ in ()).throw(RuntimeError("HTTP 403")))
    monkeypatch.setattr(c, "fetch_naver_calendar",
                        lambda cat, a, b: [("2026-12-28", "한국 휴장일", "임시공휴일")] if cat == "holiday" else [])
    monkeypatch.setitem(c.CAL_SEED, "holidays", {**c.CAL_SEED["holidays"], "2026-11-20": "손으로 더한 날"})
    cal = c.refresh_calendar(datetime(2026, 10, 13, 7, 37, tzinfo=K))      # 일주일 뒤라 다시 받을 차례
    assert cal["source"]["holidays.2026"] == "KRX"                           # 저장된 KRX 목록은 그대로
    assert c.HOLIDAYS.get("2026-12-28") == "임시공휴일"                       # KRX 가 막혀 네이버로 보충
    assert c.HOLIDAYS.get("2026-11-20") == "손으로 더한 날"                   # seed 에 더한 날짜도 합친다
    assert c.derivative_expiries(date(2026, 11, 1), date(2026, 11, 30))[0]["date"] == "2026-11-12"


def test_empty_official_lists_for_this_year_count_as_failure(monkeypatch):
    called = []
    monkeypatch.setattr(c, "fetch_krx_holidays", lambda years: {y: {} for y in years})   # 200 + 빈 목록
    monkeypatch.setattr(c, "fetch_bok_mpc", lambda y: [])
    monkeypatch.setattr(c, "fetch_naver_calendar", lambda cat, a, b: called.append(cat) or [])
    cal = c.refresh_calendar(datetime(2026, 10, 5, 7, 37, tzinfo=K))
    assert cal["attempt"]["holidays"]["ok"] is False and cal["attempt"]["mpc"]["ok"] is False
    assert called == ["holiday", "economicIndicators"]
    assert sum("갱신 실패" in w for w in c.WARNINGS) == 2
    assert "2026-10-09" in c.HOLIDAYS and c.MPC["2026"]                      # 갖고 있던 목록은 그대로


def test_refresh_calendar_survives_everything_down(monkeypatch):
    # 네트워크가 전부 막혀도(테스트 기본값) 저장소 목록으로 돈다
    cal = c.refresh_calendar(datetime(2026, 10, 5, 7, 37, tzinfo=K))
    assert "2026-10-09" in c.HOLIDAYS and c.MPC["2026"][-1] == "2026-11-26"
    assert cal["attempt"]["holidays"]["ok"] is False and cal["attempt"]["mpc"]["ok"] is False


# ── 프로그램 매매 ──────────────────────────────────────────

def _prog_row(day, arb=1_000_000_000, nonarb=-3_000_000_000, buy=2_000_000_000_000):
    """원 단위 한 행. 매수-매도=순매수, 차익+비차익=전체가 맞게."""
    r = {}
    for pre, net, b in (("diff", arb, 10_000_000_000), ("biDiff", nonarb, buy)):
        r[f"{pre}BuyAmt"], r[f"{pre}SellAmt"], r[f"{pre}PureBuyAmt"] = str(b), str(b - net), str(net)
    for suf in ("BuyAmt", "SellAmt", "PureBuyAmt"):
        r[f"totalDiff{suf}"] = str(int(r[f"diff{suf}"]) + int(r[f"biDiff{suf}"]))
    return {"bizdate": day.replace("-", ""), **r}


def test_fetch_program_daily_parses_checks_and_flags_provisional(monkeypatch):
    days = ["2026-10-06", "2026-10-02", "2026-10-01"]
    broken = _prog_row("2026-09-30")
    broken["totalDiffPureBuyAmt"] = "1"                                   # 항등식이 깨진 행
    monkeypatch.setattr(c, "get_json", lambda *a, **k: {"content": [_prog_row(d) for d in days] + [broken],
                                                        "last": True})
    rows = c.fetch_program_daily("KOSPI", 250, datetime(2026, 10, 6, 15, 0, tzinfo=K))
    assert [r["date"] for r in rows] == sorted(days)
    assert rows[-1]["arb_net"] == 10 and rows[-1]["nonarb_net"] == -30 and rows[-1]["total_net"] == -20
    assert rows[-1]["provisional"] and "provisional" not in rows[0]
    assert any("1행을 건너뜀" in w for w in c.WARNINGS)
    rows = c.fetch_program_daily("KOSPI", 250, datetime(2026, 10, 6, 20, 30, tzinfo=K))
    assert "provisional" not in rows[-1]
    with pytest.raises(ValueError):
        c.fetch_program_daily("kospi", 10)


def test_fetch_program_daily_all_rows_broken_is_error(monkeypatch):
    bad = _prog_row("2026-10-02")
    del bad["biDiffBuyAmt"]
    monkeypatch.setattr(c, "get_json", lambda *a, **k: {"content": [bad], "last": True})
    with pytest.raises(RuntimeError, match="필드"):
        c.fetch_program_daily("KOSDAQ", 10)


def test_build_program_rejects_identical_markets(monkeypatch):
    same = [{"date": f"2026-09-{d:02d}", "arb_net": 1.0, "nonarb_net": 2.0, "total_net": 3.0, "total_buy": 9.0}
            for d in range(1, 29)]
    monkeypatch.setattr(c, "fetch_program_daily", lambda m, n, now=None: [dict(r) for r in same])
    assert c.build_program()["markets"] == {}
    assert any("똑같이" in w for w in c.WARNINGS)


def test_program_insight_only_when_extreme():
    flows = {"markets": {}}
    mk = lambda pctl, total: {"markets": {"KOSPI": {"latest": {
        "total": total, "nonarb": total, "arb": 0, "pctl": pctl, "days": 249, "provisional": False}}}}
    say = lambda prog: [t["text"] for t in c.build_insights(flows, {"indices": {}}, {"items": []}, {}, None, None, prog)
                        if "프로그램" in t["text"]]
    assert say(mk(50.0, 3000.0)) == []
    assert say(mk(99.0, 30000.0)) and "순매수 쪽 상위 1%" in say(mk(99.0, 30000.0))[0]
    assert "순매도 쪽 상위 2%" in say(mk(2.0, -15000.0))[0]
    assert say(mk(2.0, 15000.0)) == [] and say(mk(98.0, -15000.0)) == []     # 극단 쪽과 부호가 어긋나면 말하지 않는다


# ── 공매도 ──────────────────────────────────────────────────

def _short_raw(days, sv="500,000,000,000", tv="10,000,000,000,000", wt="5.00"):
    return {d: {"TRD_DD": d.replace("-", "/"), "CVSRTSELL_TRDVAL": sv, "ACC_TRDVAL": tv, "TRDVAL_WT": wt} for d in days}


def test_parse_short_daily_checks_units_and_ratio():
    now = datetime(2026, 10, 6, 18, 0, tzinfo=K)
    rows = c.parse_short_daily(_short_raw(["2025-03-28", "2026-10-02", "2026-10-06"], wt="9.99"), now)
    assert [r["date"] for r in rows] == ["2026-10-02", "2026-10-06"]        # 재개 전 날짜는 버린다
    assert rows[0] == {"date": "2026-10-02", "value": 5000, "total": 100000, "pct": 5.0}   # 어긋난 비중은 다시 계산
    assert rows[-1]["provisional"]                                          # 20:10 전 오늘 값
    with pytest.raises(RuntimeError, match="단위"):
        c.parse_short_daily(_short_raw(["2026-10-02"], sv="5,000", tv="100,000", wt="5.00"), now)
    with pytest.raises(RuntimeError, match="큼"):
        c.parse_short_daily(_short_raw(["2026-10-02"], sv="9,000,000,000,000", tv="1,000,000,000,000"), now)


def test_build_short_backfills_then_refreshes_recent(monkeypatch):
    seen = []
    days = [d.isoformat() for d in (date(2025, 3, 31) + timedelta(days=k) for k in range(560)) if d.weekday() < 5]

    def rows(bld, market, start, end, key):
        seen.append((bld, start))
        got = [d for d in days if start.isoformat() <= d <= end.isoformat()]
        if key == "TRD_DD":
            return _short_raw(got)
        return {d: {"RPT_DUTY_OCCR_DD": d.replace("-", "/"), "BAL_AMT": "2,000,000,000,000",
                    "MKTCAP": "500,000,000,000,000", "BAL_RTO2": "0.40"} for d in got}
    monkeypatch.setattr(c, "_krx_market_rows", rows)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    now = datetime(2026, 10, 6, 21, 0, tzinfo=K)
    out = c.build_short(now)
    assert {s for _, s in seen} == {date(2025, 3, 31)}                     # 처음엔 재개일부터
    k = out["markets"]["KOSPI"]
    assert k["daily"][0]["date"] == "2025-03-31" and k["latest"]["daily"]["pctPctl"] == 50.0
    assert k["latest"]["balance"]["d5"] == 0 and out["failed"] == []
    c.write("short.json", out, compact=True)
    seen.clear()
    out2 = c.build_short(now + timedelta(days=1))
    assert {s for _, s in seen} == {(now + timedelta(days=1)).date() - timedelta(days=c.KRX_REFRESH_DAYS)}
    assert out2["markets"]["KOSPI"]["daily"][0]["date"] == "2025-03-31"    # 앞쪽 이력은 직전 파일에서
    # 장중(평일 09:00~15:45)엔 KRX 를 부르지 않고 직전 값을 그대로
    seen.clear()
    out3 = c.build_short(datetime(2026, 10, 7, 10, 0, tzinfo=K))
    assert seen == [] and out3["markets"]["KOSPI"]["daily"] == out["markets"]["KOSPI"]["daily"]


def test_short_20day_average_uses_settled_days_before_shown_row(monkeypatch):
    days = [d.isoformat() for d in (date(2026, 8, 1) + timedelta(days=k) for k in range(70)) if d.weekday() < 5
            and d.isoformat() not in c.HOLIDAYS]
    days = [d for d in days if d <= "2026-10-06"]
    def rows(bld, market, start, end, key):
        if key != "TRD_DD":
            return {}
        raw = _short_raw([d for d in days if d != "2026-10-02"])
        raw.update(_short_raw(["2026-10-02"], sv="900,000,000,000"))          # 가장 최근 확정일만 9%
        return raw
    monkeypatch.setattr(c, "_krx_market_rows", rows)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "SHORT_RESUMED", "2026-08-01")
    out = c.build_short(datetime(2026, 10, 6, 18, 17, tzinfo=K))               # 오늘(10/6) 값은 잠정
    lt = out["markets"]["KOSPI"]["latest"]["daily"]
    assert lt["date"] == "2026-10-06" and lt["provisional"]
    assert lt["pctAvg20"] == round((5.0 * 19 + 9.0) / 20, 2)                  # 어제(10/2)까지의 확정 20일


def test_krx_srt_post_marks_blocking_errors(monkeypatch):
    class R:
        def __init__(self, code, text):
            self.status_code, self.text = code, text
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c.session, "post", lambda *a, **k: R(403, "<html>"))
    with pytest.raises(c.KrxBlocked):
        c.krx_srt_post("MDCSTAT30201_OUT")
    monkeypatch.setattr(c.session, "post", lambda *a, **k: R(400, "INVALIDPERIOD2"))
    with pytest.raises(RuntimeError) as ei:
        c.krx_srt_post("MDCSTAT30201_OUT")
    assert not isinstance(ei.value, c.KrxBlocked) and "INVALIDPERIOD2" in str(ei.value)
    monkeypatch.setattr(c.session, "post", lambda *a, **k: (_ for _ in ()).throw(c.requests.ConnectionError("x")))
    with pytest.raises(c.KrxBlocked):                                       # 연결 자체가 안 됨
        c.krx_srt_post("MDCSTAT30201_OUT")


# ── 종목 순위 · 평소와 다른 것 · 장중 기록 · 문구 ─────────────

def _rank(to="2026-10-02", estimated=False, amt=100):
    row = lambda code, a: {"code": code, "name": code, "amt": a, "qty": a, "volPct": 1.0, "chg": 0.0}
    return {"from": to, "to": to, "estimated": estimated, "buy": [row("000660", amt)], "sell": [row("005930", -amt)]}


def test_build_ranks_keeps_previous_until_final(monkeypatch):
    calls = []
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "fetch_rank", lambda inv, mkt, per: calls.append(1) or _rank())
    r = c.build_ranks(None, datetime(2026, 10, 2, 20, 30, tzinfo=K), "장마감",
                      {"000660": {"foreign": {"days": 3, "side": "buy"}, "institution": None}})
    assert r["final"] and r["asOf"] == "2026-10-02" and len(calls) == 8
    assert r["markets"]["KOSPI"]["foreign"]["day"]["buy"][0]["streak"] == {"days": 3, "side": "buy"}
    # 장중엔 받지 않고 직전 확정본(전 거래일)을 쓴다
    calls.clear()
    r2 = c.build_ranks(r, datetime(2026, 10, 6, 10, 0, tzinfo=K), "장중")
    assert calls == [] and r2["final"] is False and r2["asOf"] == "2026-10-02"
    # 마감 뒤라도 18시 전 오늘 값·장중 추정 값은 받아들이지 않는다
    monkeypatch.setattr(c, "fetch_rank", lambda inv, mkt, per: _rank("2026-10-06", estimated=(inv == "FOREIGNER")))
    r3 = c.build_ranks(r, datetime(2026, 10, 6, 16, 0, tzinfo=K), "장마감(수급 확정 반영중)")
    assert r3["asOf"] == "2026-10-02" and r3["final"] is False


def test_fetch_rank_parses_and_rejects_sign_flip(monkeypatch):
    def item(code, q, a, typ="ST"):
        return {"itemcode": code, "itemname": code, "type": typ, "bizdateFrom": "20261002",
                "bizdateTo": "20261002", "accTradeVolume": str(q), "accTradeAmount": str(a),
                "dailyTradeVolume": "1000", "prevChangeRate": "1.5", "estimated": False}
    j = {"sections": {"buyRankList": [item("A", 100, 5e9), item("ETF", 10, 9e9, "EF")],
                      "sellRankList": [item("B", -300, -2e10)]}}
    monkeypatch.setattr(c, "get_json", lambda *a, **k: j)
    r = c.fetch_rank("FOREIGNER", "KOSPI", "DAY")
    assert [x["code"] for x in r["buy"]] == ["A"] and r["buy"][0]["amt"] == 50 and r["buy"][0]["volPct"] == 10.0
    assert r["sell"][0]["amt"] == -200 and r["to"] == "2026-10-02" and r["estimated"] is False
    j["sections"]["buyRankList"].append(item("C", -5, -1e9))
    with pytest.raises(RuntimeError, match="부호"):
        c.fetch_rank("FOREIGNER", "KOSPI", "DAY")


def test_streak_rarity_from_reference():
    r = c.streak_rarity("KOSPI", "other_corp", {"days": 19, "side": "buy"})
    assert 2 < r["pctRuns"] < 4 and r["longest"]["buy"]["days"] >= 58
    assert c.streak_rarity("KOSPI", "foreign", {"days": 999, "side": "sell"})["pctRuns"] == 0.0   # 사상 최장
    assert c.streak_rarity("KOSDAQ", "foreign", {"days": 3, "side": "buy"})["pctRuns"] is None


def test_build_today_picks_rare_facts_only():
    flows = {"markets": {"KOSPI": {"latest": {"date": "2026-10-02"}, "streaks": {
        "other_corp": {"days": 19, "side": "buy", "rarity": {"since": "2009-03", "pctRuns": 2.9}},
        "foreign": {"days": 6, "side": "sell", "rarity": {"since": "2009-03", "pctRuns": 12.2}}}}}}
    uni = [{"code": f"{i:06d}", "name": f"S{i}", "foreign": {"v60": -5.0}, "institution": {"v60": 1.0}}
           for i in range(30)]
    uni[0]["foreign"]["v60"], uni[1]["foreign"]["v60"] = -218700.0, -83000.0
    stocks = {"concentration": c.concentration(uni)}
    t = c.build_today(flows, stocks, {}, {}, {"items": []}, {})
    kinds = [i["kind"] for i in t["items"]]
    assert kinds == ["streak", "concentration"]                     # 외국인 6일(12.2%)은 드물지 않아 빠짐
    assert t["items"][0]["text"] == "기타법인 19거래일 연속 순매수" and "2.9%" in t["items"][0]["detail"]
    assert t["items"][1]["text"].startswith("외국인 60일 순매도의 100%")
    assert not c.check_copy([f"{i['text']} {i['detail']}" for i in t["items"]], "x")
    empty = c.build_today({"markets": {}}, {}, {}, {}, {}, {})
    assert empty["items"] == [] and empty["none"]


def test_record_intraday_hist_once_after_final(monkeypatch):
    pts = [{"t": t, "individual": 1.0, "foreign": -1.0, "institution": 0.0, "other_corp": 0.0}
           for t in ("09:05", "10:00", "11:00", "13:00", "14:30", "15:30")]

    def mk(final):
        return {"markets": {code: {"latest": {"date": "2026-10-02", "individual": 5.0, "foreign": -5.0,
                                              "institution": 0.0, "other_corp": 0.0},
                                   "intraday": {"date": "2026-10-02", "points": pts,
                                                "final": {"t": "20:04"} if final else None}}
                            for code in ("KOSPI", "KOSDAQ")}}
    monkeypatch.setattr(c, "fetch_program_intraday", lambda m: {"_date": "2026-10-02", "100000": {"total": 7.0},
                                                               "153000": {"total": 9.0}})
    assert c.record_intraday_hist(None, mk(False), {}, datetime(2026, 10, 2, 19, 0, tzinfo=K)) is None
    h = c.record_intraday_hist(None, mk(True), {}, datetime(2026, 10, 2, 20, 30, tzinfo=K))
    snap = h["days"][0]["times"]["KOSPI"]
    assert list(snap) == list(c.HIST_TIMES) and snap["10:00"]["program"] == 7.0 and snap["15:30"]["program"] == 9.0
    assert h["days"][0]["final"]["KOSPI"]["individual"] == 5.0
    assert c.record_intraday_hist(h, mk(True), {}, datetime(2026, 10, 2, 21, 0, tzinfo=K)) is None   # 하루 한 번


def test_pre_entry_and_feed_kinds():
    flows = {"markets": {"KOSPI": {"latest": {"date": "2026-10-02", "individual": -17897.0, "foreign": -1080.0,
                                              "institution": 4155.0, "other_corp": 14822.0}}}}
    glob = {"items": [{"symbol": "^SOX", "name": "필라델피아 반도체", "changeRate": 2.4},
                      {"symbol": "^GSPC", "name": "S&P 500", "changeRate": 0.73}], "preopen": ["^SOX", "^GSPC"]}
    ev = {"upcoming": [{"title": "옵션 만기", "dday": 0, "note": "코스피200 옵션 최종거래일"}]}
    pre = c.build_pre_entry(datetime(2026, 10, 8, 7, 37, tzinfo=K), flows, glob, ev, {"items": []})
    assert pre["id"] == "2026-10-08-pre" and "필라델피아 반도체 +2.40%" in pre["title"]
    assert "오늘 옵션 만기" in pre["summary"]
    assert c.build_pre_entry(datetime(2026, 10, 9, 7, 37, tzinfo=K), flows, glob, ev, {}) is None   # 휴장일
    assert c.build_pre_entry(datetime(2026, 10, 8, 9, 5, tzinfo=K), flows, glob, ev, {}) is None    # 장 시작 뒤
    old = {"entries": [{"id": "2026-10-01", "title": "t", "summary": "", "updated": "2026-10-01T20:30:00+09:00",
                        "final": True}]}
    f = c.build_feed(old, flows, {"indices": {}}, [], datetime(2026, 10, 8, 7, 37, tzinfo=K), pre)
    ids = [e["id"] for e in f["entries"]]
    assert ids[0] == "2026-10-08-pre" and "2026-10-01-post" in ids


def test_korean_indicator_events(monkeypatch):
    monkeypatch.setattr(c, "fetch_naver_calendar", lambda cat, a, b: [
        ("2026-10-06", "외환보유고 (미국달러)", "06:00 예정"),
        ("2026-10-15", "수출 전년대비(확정치)", "09:00 예정 · 시장 영향력 매우 높음"),
        ("2026-10-15", "수입 전년대비(확정치)", "09:00 예정"),
        ("2026-10-22", "중앙은행 기준금리", "10:00 예정")])
    ev = c.korean_indicator_events(date(2026, 10, 5))
    assert ev == [{"type": "지표", "date": "2026-10-15", "title": "수출 전년대비(확정치) · 수입 전년대비(확정치)",
                   "note": "09:00 발표 예정", "impact": "mid"}]


def test_check_copy_flags_banned_words():
    assert c.check_copy(["외국인 매수 신호"], "x") == ["신호"]
    assert any("신호" in w for w in c.WARNINGS)


# ── 350종목 범위 · 장중 가벼운 수집 ─────────────────────────

def _trend_rows(n, start=date(2025, 1, 2), f=100, o=-50, i=-50, hold0=40.0):
    out, d, k = [], start, 0
    while len(out) < n:
        if d.weekday() < 5:
            out.append([d.strftime("%Y%m%d"), f if k % 7 else -f, o, i, round(hold0 + k * 0.01, 2), 1000.0 + k, 10000])
            k += 1
        d += timedelta(days=1)
    return out


def test_universe_name_filter():
    pat = c._EXCLUDE_NAME
    for name in ("삼성전자우", "현대차2우B", "미래에셋비전스팩5호", "ESR켄달스퀘어리츠", "LG화학우(전환)"):
        assert pat.search(name), name
    for name in ("우리금융지주", "삼성전자", "LG에너지솔루션", "HD현대중공업"):
        assert not pat.search(name), name


def test_stock_summary_values():
    rows = _trend_rows(30)
    u = {"code": "000001", "name": "가", "market": "KOSPI", "price": 1.0, "chg": 0.5, "marketCap": 10}
    s = c.stock_summary(u, rows)
    assert s["foreign"]["d5"] == sum(r[1] for r in rows[-5:]) and s["institution"]["streak"] == {"days": 30, "side": "sell"}
    assert s["institution"]["longest"]["sell"] == 30 and s["foreign"]["longest"]["buy"] == 6
    assert s["foreign"]["v20"] == round(sum(r[1] * r[5] for r in rows[-20:]) / 1e8, 1)
    assert s["holdChg20"] == round(rows[-1][4] - rows[-21][4], 2) and s["asOf"] == f"{rows[-1][0][:4]}-{rows[-1][0][4:6]}-{rows[-1][0][6:]}"


def test_build_universe_backfills_in_batches_and_writes_series(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_STORE", str(tmp_path / "store.json.gz"))
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "STOCK_BACKFILL_PER_RUN", 150)
    uni = lambda m, n: [{"code": f"{1 if m == 'KOSPI' else 2}{k:05d}", "name": f"{m}{k}", "market": m, "price": 1.0,
                         "chg": 0.0, "marketCap": 1, "holdRatio": 10.0} for k in range(n)]
    monkeypatch.setattr(c, "fetch_universe", uni)
    asked = []
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: asked.append(days) or _trend_rows(min(days, 80)))
    now = datetime(2026, 10, 5, 7, 37, tzinfo=K)
    summaries, rows_by, pending = c.build_universe(now)
    assert len(summaries) == 350 and asked.count(c.STOCK_BACKFILL_DAYS) == 150 and asked.count(60) == 200
    assert pending == 200
    store = c.load_stock_store()
    assert sum(1 for r in store["codes"].values() if r.get("partial")) == 200
    # 다음 실행: 남은 종목을 이어서 처음부터, 끝난 종목은 최근 며칠만
    asked.clear()
    c.build_universe(now + timedelta(days=1))
    assert asked.count(c.STOCK_BACKFILL_DAYS) == 150 and all(d == c.STOCK_BACKFILL_DAYS or d < 2000 for d in asked)
    n = c.write_stock_series(rows_by, {"100000"})
    files = list((c.OUT / "stockseries").glob("*.json"))
    assert n == 350 and [f.stem for f in files] == ["100000"]          # 범위 밖 파일은 지운다
    x = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(x) == {"code", "unit", "d", "f", "o", "i", "h", "c"} and len(x["d"]) <= c.STOCK_SERIES_DAYS


def test_build_universe_gives_up_when_too_many_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_STORE", str(tmp_path / "store.json.gz"))
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "fetch_universe", lambda m, n: [{"code": f"{m[:1]}{k}", "name": "x", "market": m}
                                                          for k in range(n)])
    calls = []
    monkeypatch.setattr(c, "fetch_stock_trend",
                        lambda code, days: calls.append(code) or (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    assert c.build_universe(datetime(2026, 10, 5, 7, 37, tzinfo=K)) is None
    assert len(calls) == c.STOCK_MAX_CONSECUTIVE_FAIL                 # 연달아 실패하면 나머지는 부르지 않는다
    assert any("직전 요약" in w for w in c.WARNINGS)


def test_patch_daily_keeps_cumulative_chain():
    daily = [{"date": f"2026-09-{d:02d}", "individual": 1.0, "foreign": 2.0, "institution": 3.0,
              "cum": {"individual": float(k + 1), "foreign": 2.0 * (k + 1), "institution": 3.0 * (k + 1)}}
             for k, d in enumerate((28, 29, 30))]
    new = [{"date": "2026-09-30", "individual": 10.0, "foreign": 0.0, "institution": 0.0},
           {"date": "2026-10-01", "individual": 1.0, "foreign": 1.0, "institution": 1.0}]
    out = c._patch_daily(daily, new)
    assert [r["date"] for r in out] == ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]
    assert out[2]["cum"] == {"individual": 12.0, "foreign": 4.0, "institution": 6.0}
    assert out[3]["cum"] == {"individual": 13.0, "foreign": 5.0, "institution": 7.0}


def test_universe_due_rules():
    now = datetime(2026, 10, 6, 16, 40, tzinfo=K)
    done = {"universe": [{"asOf": "2026-10-06"}], "universePending": 0, "universeAt": "2026-10-06T16:40:00+09:00"}
    assert c.universe_due({}, "2026-10-06", now)                                       # 처음
    assert c.universe_due({**done, "universePending": 30}, "2026-10-06", now)           # 채우는 중
    assert c.universe_due({**done, "universe": [{"asOf": "2026-10-02"}]}, "2026-10-06", now)   # 새 거래일
    assert not c.universe_due(done, "2026-10-06", now)                                 # 오늘 이미 돎
    assert not c.universe_due(done, "2026-10-06", datetime(2026, 10, 6, 18, 40, tzinfo=K))   # 확정(20:15) 전
    assert not c.universe_due(done, "2026-10-06", datetime(2026, 10, 6, 20, 10, tzinfo=K))
    assert c.universe_due(done, "2026-10-06", datetime(2026, 10, 6, 20, 20, tzinfo=K))      # 확정 뒤 한 번
    assert not c.universe_due({**done, "universeAt": "2026-10-06T20:20:00+09:00"}, "2026-10-06",
                              datetime(2026, 10, 6, 20, 50, tzinfo=K))
    fri = {**done, "universe": [{"asOf": "2026-10-02"}], "universeAt": "2026-10-02T20:20:00+09:00"}
    assert not c.universe_due(fri, "2026-10-02", datetime(2026, 10, 3, 19, 0, tzinfo=K))    # 주말엔 다시 안 돎
    assert c.universe_due({**fri, "universeAt": "2026-10-02T18:30:00+09:00"}, "2026-10-02",
                          datetime(2026, 10, 3, 9, 0, tzinfo=K))                               # 금요일 확정을 놓쳤으면 한 번


def test_mode_decide_every_five_minutes_all_day():
    import mode as md
    hols = {"2026-10-09"}
    at = lambda h, m, d=6: datetime(2026, 10, d, h, m, tzinfo=K)
    meta = lambda gen, full=None: {"generatedAt": gen.isoformat(), "fullAt": (full or gen).isoformat()}
    assert md.decide(meta(at(10, 3)), hols, at(10, 4)) == "skip"                       # 2.5분 안 중복
    assert md.decide(meta(at(10, 0), at(9, 30)), hols, at(10, 5)) == "intraday"
    assert md.decide(meta(at(10, 25), at(9, 30)), hols, at(10, 30)) == "full"          # 1시간 지난 전체
    assert md.decide(meta(at(20, 30, 5)), hols, at(2, 0)) == "skip"                    # 밤
    assert md.decide(meta(at(20, 30, 5)), hols, at(7, 35)) == "full"                   # 장 전 한 번
    assert md.decide(meta(at(7, 35)), hols, at(7, 45)) == "skip"
    assert md.decide(meta(at(16, 40)), hols, at(17, 0)) == "skip"                      # 마감 뒤는 1시간마다
    assert md.decide(meta(at(16, 40)), hols, at(17, 45)) == "full"
    assert md.decide(meta(at(19, 50)), hols, at(20, 10)) == "skip"                     # 마감 뒤는 1시간마다
    assert md.decide(meta(at(19, 50)), hols, at(20, 25)) == "full"                     # 20:15 확정 한 번
    assert md.decide(meta(at(20, 10)), hols, at(20, 40)) == "full"                     # 20:10 은 확정 전 — 한 번 더
    assert md.decide(meta(at(20, 20)), hols, at(20, 55)) == "skip"
    assert md.decide(meta(at(20, 20)), hols, at(22, 0)) == "skip"
    pend = {**meta(at(20, 20)), "pendingFinal": True}                                    # 순위·종목 수급이 덜 들어옴
    assert md.decide(pend, hols, at(20, 40)) == "skip" and md.decide(pend, hols, at(20, 55)) == "full"
    assert md.decide(pend, hols, at(23, 40)) == "skip"                                 # 밤엔 쉬고 07:30 에 채운다
    assert md.decide(meta(at(10, 0, 9)), hols, at(12, 0, 9)) == "skip"                 # 휴장일은 6시간마다
    assert md.decide(meta(at(5, 0, 9)), hols, at(12, 0, 9)) == "full"
    assert md.decide({}, hols, at(3, 0)) == "skip" and md.decide({}, hols, at(10, 0)) == "full"
    assert md.decide(meta(at(2, 0)), hols, at(2, 1), force=True) == "full"



def test_mode_backs_off_after_failed_attempts_and_follows_session_hours(monkeypatch, tmp_path):
    import mode as md
    hols = {"2026-10-09"}
    at = lambda h, m, d=6, mo=10, y=2026: datetime(y, mo, d, h, m, tzinfo=K)
    ok = lambda t: {"generatedAt": t.isoformat(), "fullAt": t.isoformat()}
    fail = lambda m, t, mode="full": {**m, "lastAttempt": {"at": t.isoformat(), "mode": mode, "ok": False}}
    m = fail(ok(at(9, 50)), at(10, 55))
    assert md.decide(m, hols, at(10, 56)) == "skip"                                    # 실패 직후 중복
    assert md.decide(m, hols, at(11, 0)) == "intraday"                                 # 실패해도 장중엔 가벼운 수집
    assert md.decide(m, hols, at(11, 55)) == "full"                                    # 한 시간 뒤 다시
    h = fail(ok(at(6, 0, 9)), at(12, 0, 9))
    assert md.decide(h, hols, at(12, 30, 9)) == "skip" and md.decide(h, hols, at(18, 5, 9)) == "full"
    pm = fail(ok(at(19, 0)), at(20, 10))
    assert md.decide(pm, hols, at(20, 20)) == "skip" and md.decide(pm, hols, at(20, 45)) == "full"
    # 수능일(16:30 마감)엔 16:00 에도 장중, 새해 첫 거래일(10시 개장)엔 09:40 은 장 전
    special = {"2026-11-19": ("1000", "1630")}
    assert md.decide(ok(at(15, 30, 19, 11)), hols, at(16, 0, 19, 11), special=special) == "intraday"
    assert md.decide(ok(at(15, 30, 19, 11)), hols, at(16, 0, 19, 11)) == "skip"         # 평일 규칙이면 마감 뒤(1시간마다)
    assert md._session(date(2027, 1, 4), {"2027-01-01"}, {}) == (600, 930)
    # 실패 기록: 마지막 시도와 요청 수
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "attempt.json").write_text('{"requests": 40}', encoding="utf-8")
    f = tmp_path / "meta.json"
    f.write_text(json.dumps({"requests": {"date": "2026-10-06", "count": 100}}), encoding="utf-8")
    got = md.record_failure(f, "full", root=tmp_path, now=at(11, 0))
    assert got["lastAttempt"] == {"at": at(11, 0).isoformat(timespec="seconds"), "mode": "full", "ok": False}
    assert got["requests"] == {"date": "2026-10-06", "count": 140}


def test_stock_store_marker_matches_workflow(monkeypatch, tmp_path):
    import re as _re
    monkeypatch.delenv("STOCK_STORE", raising=False)
    c.save_stock_store({"codes": {}})
    marker = c.stock_store_marker()
    assert marker.exists()
    yml = (Path(c.__file__).resolve().parent.parent / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
    lit = _re.search(r"hashFiles\('([^']+)'\)", yml).group(1)
    assert marker.relative_to(c.ROOT).as_posix() == lit


def test_estimated_ranks_never_used_even_after_cutoff(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "fetch_rank", lambda inv, mkt, per: _rank())
    prev = c.build_ranks(None, datetime(2026, 10, 2, 20, 30, tzinfo=K), "장마감")
    for now, phase in ((datetime(2026, 10, 6, 20, 30, tzinfo=K), "장마감"), (datetime(2026, 10, 7, 7, 35, tzinfo=K), "장전")):
        monkeypatch.setattr(c, "fetch_rank", lambda inv, mkt, per: _rank("2026-10-06", estimated=True))
        r = c.build_ranks(prev, now, phase)
        assert r["asOf"] == "2026-10-02" and r["final"] is False
        assert r["markets"]["KOSPI"]["foreign"]["day"]["to"] == "2026-10-02"


def test_ranks_mark_lists_left_from_an_older_day(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "fetch_rank", lambda inv, mkt, per: _rank())
    prev = c.build_ranks(None, datetime(2026, 10, 2, 20, 30, tzinfo=K), "장마감")
    def flaky(inv, mkt, per):
        if mkt == "KOSDAQ" and inv == "ORGANIZATION":
            raise RuntimeError("HTTP 500")
        return _rank("2026-10-06")
    monkeypatch.setattr(c, "fetch_rank", flaky)
    r = c.build_ranks(prev, datetime(2026, 10, 6, 20, 30, tzinfo=K), "장마감")
    assert r["asOf"] == "2026-10-06" and r["mixed"] == ["코스닥 기관 하루"]
    assert r["markets"]["KOSDAQ"]["institution"]["day"]["stale"] is True
    assert any(s_["key"] == "ranks" for s_ in c.STALE)


def test_rank_streak_counts_as_of_list_date():
    rows_by = {"000660": [["20260929", -5, 1, 0, 1.0, 1.0, 1], ["20260930", -5, 1, 0, 1.0, 1.0, 1],
                          ["20261001", -5, 1, 0, 1.0, 1.0, 1], ["20261002", -5, -1, 0, 1.0, 1.0, 1],
                          ["20261006", 9, 1, 0, 1.0, 1.0, 1]]}
    fn = c.rank_streak_fn({}, rows_by)
    st = fn("000660", "foreign", "2026-10-02")
    assert st["days"] == 4 and st["side"] == "sell" and st.get("atLeast")      # 맨 앞 행까지 이어짐 → 그 이상일 수 있음
    assert fn("000660", "foreign", "2026-10-06")["days"] == 1
    assert fn("000660", "institution", "2026-10-02") == {"days": 1, "side": "sell", "total": -1.0}
    assert fn("999999", "foreign", "2026-10-02") is None


@pytest.mark.parametrize("kind,data,expect", [
    ("streak", {"days": 2, "pct": 2.0}, False), ("streak", {"days": 3, "pct": 2.0}, True),
    ("streak", {"days": 3, "pct": 5.1}, False), ("streak", {"days": 3, "pct": 5.0}, True),
    ("program", {"pctl": 97.4, "total": 5000}, False), ("program", {"pctl": 97.5, "total": 5000}, True),
    ("program", {"pctl": 99.0, "total": 999}, False), ("program", {"pctl": 98.0, "total": -5000}, False),
    ("program", {"pctl": 2.0, "total": 5000}, False), ("program", {"pctl": 2.5, "total": -5000}, True),
    ("credit", {"q": 97.4, "d5": 9000}, False), ("credit", {"q": 97.5, "d5": 9000}, True),
    ("credit", {"q": 1.2, "d5": 120}, False), ("credit", {"q": 2.5, "d5": -9000}, True),
    ("short", {"q": 97.4}, False), ("short", {"q": 97.5}, True),
    ("overnight", {"q": 97.4}, False), ("overnight", {"q": 97.5}, True),
])
def test_build_today_thresholds(kind, data, expect):
    day = "2026-10-02"
    flows, stocks, credit, program, glob, short = {"markets": {"KOSPI": {"latest": {"date": day}}}}, {}, {}, {}, {}, {}
    if kind == "streak":
        flows["markets"]["KOSPI"]["streaks"] = {"foreign": {"days": data["days"], "side": "sell",
                                                            "rarity": {"since": "2009-03", "pctRuns": data["pct"]}}}
    if kind == "program":
        program = {"markets": {"KOSPI": {"latest": {"date": day, "pctl": data["pctl"], "total": data["total"], "days": 250}}}}
    if kind == "credit":
        credit = {"latest": {"loans": {"d5": data["d5"], "d5Pctl": data["q"], "days": 146}}}
    if kind == "short":
        short = {"markets": {"KOSPI": {"latest": {"daily": {"pct": 9.0, "pctPctl": data["q"], "days": 369}}}}}
    if kind == "overnight":
        glob = {"items": [{"symbol": "EWY", "name": "한국 ETF(EWY)", "changeRate": -5.0, "movePctl": data["q"],
                           "asOf": "2026-10-02"}], "preopen": ["EWY"]}
    t = c.build_today(flows, stocks, credit, program, glob, short, datetime(2026, 10, 5, 7, 37, tzinfo=K))
    assert (len(t["items"]) == 1) is expect, t["items"]


def test_today_concentration_wording_never_rounds_to_100():
    uni = [{"code": f"{i:06d}", "name": f"S{i}", "foreign": {"v60": -5.0}, "institution": {"v60": 1.0}} for i in range(30)]
    uni[0]["foreign"]["v60"], uni[1]["foreign"]["v60"] = -218700.0, -83000.0
    conc = c.concentration(uni)
    conc["foreign"]["share"] = 99.7
    flows = {"markets": {"KOSPI": {"latest": {"date": "2026-10-02"}}}}
    t = c.build_today(flows, {"concentration": conc}, {}, {}, {}, {}, datetime(2026, 10, 5, 7, 37, tzinfo=K))
    assert "99.7%" in t["items"][0]["text"] and "수집 30종목" in t["items"][0]["detail"]
    conc["foreign"]["share"] = 140.0
    t = c.build_today(flows, {"concentration": conc}, {}, {}, {}, {}, datetime(2026, 10, 5, 7, 37, tzinfo=K))
    assert "두 종목 합계" in t["items"][0]["text"] and "반대 방향" in t["items"][0]["detail"]
    t = c.build_today({"markets": {"KOSPI": {"latest": {"date": "2026-10-06"}, "streaks": {
        "foreign": {"days": 9, "side": "sell", "rarity": {"since": "2009-03", "pctRuns": 1.0}}}}}},
        {}, {}, {}, {}, {}, datetime(2026, 10, 6, 11, 0, tzinfo=K))
    assert t["provisional"] is True and t["items"][0]["text"].endswith("(잠정)")


def test_feed_orders_by_day_and_keeps_timestamp_when_unchanged():
    flows = {"markets": {"KOSPI": {"latest": {"date": "2026-10-06", "individual": 1.0, "foreign": 2.0, "institution": 3.0}}}}
    f1 = c.build_feed({}, flows, {"indices": {}}, [], datetime(2026, 10, 6, 20, 30, tzinfo=K))
    f2 = c.build_feed(f1, flows, {"indices": {}}, [], datetime(2026, 10, 7, 7, 37, tzinfo=K),
                      {"id": "2026-10-07-pre", "kind": "pre", "date": "2026-10-07", "title": "t", "summary": "",
                       "final": True, "updated": "2026-10-07T07:37:00+09:00"})
    assert [e["id"] for e in f2["entries"]] == ["2026-10-07-pre", "2026-10-06-post"]
    assert f2["entries"][1]["updated"] == f1["entries"][0]["updated"]               # 내용이 같으면 시각 그대로


def test_intraday_hist_waits_for_program_final(monkeypatch):
    pts = [{"t": "15:30", "individual": 1.0, "foreign": -1.0, "institution": 0.0, "other_corp": 0.0}]
    flows = {"markets": {c_: {"latest": {"date": "2026-10-06", "individual": 1.0}, "intraday": {
        "date": "2026-10-06", "points": pts, "final": {"t": "20:01"}}} for c_ in ("KOSPI", "KOSDAQ")}}
    monkeypatch.setattr(c, "fetch_program_intraday", lambda m: {"_date": "2026-10-06"})
    assert c.record_intraday_hist(None, flows, {}, datetime(2026, 10, 6, 20, 2, tzinfo=K)) is None
    assert c.record_intraday_hist(None, flows, {}, datetime(2026, 10, 6, 20, 6, tzinfo=K)) is not None


def test_universe_backfill_completes_over_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_STORE", str(tmp_path / "store.json.gz"))
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "STOCK_BACKFILL_PER_RUN", 150)
    uni = lambda m, n: [{"code": f"{1 if m == 'KOSPI' else 2}{k:05d}", "name": f"{m}{k}", "market": m} for k in range(n)]
    monkeypatch.setattr(c, "fetch_universe", uni)
    asked = []
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: asked.append(days) or _trend_rows(min(days, 80)))
    now = datetime(2026, 10, 5, 7, 37, tzinfo=K)
    assert c.build_universe(now)[2] == 200
    asked.clear()
    _, _, pending2 = c.build_universe(now + timedelta(days=1))
    assert pending2 == 50 and asked.count(c.STOCK_BACKFILL_DAYS) == 150 and asked.count(60) == 50
    assert sum(1 for d in asked if d not in (c.STOCK_BACKFILL_DAYS, 60)) == 150     # 다 채운 종목은 최근 며칠만
    asked.clear()
    _, _, pending3 = c.build_universe(now + timedelta(days=2))
    assert pending3 == 0 and asked.count(c.STOCK_BACKFILL_DAYS) == 50 and asked.count(60) == 0
    asked.clear()
    _, _, pending4 = c.build_universe(now + timedelta(days=3))
    assert pending4 == 0 and c.STOCK_BACKFILL_DAYS not in asked


def test_universe_time_budget_carries_rest_to_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_STORE", str(tmp_path / "store.json.gz"))
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "fetch_universe", lambda m, n: [{"code": f"{m}{k}", "name": "x", "market": m}
                                                          for k in range(n)])
    clock = iter(range(0, 10 ** 6, 10))                       # 종목마다 10초씩 흐르는 가짜 시계
    monkeypatch.setattr(c.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: _trend_rows(30))
    summaries, rows_by, pending = c.build_universe(datetime(2026, 10, 5, 7, 37, tzinfo=K))
    assert 0 < len(summaries) < 350 and pending >= 350 - len(summaries)   # 못 받은 종목은 다음 실행으로
    assert any("시간 예산" in w for w in c.WARNINGS)


def test_request_counter_wraps_session(monkeypatch):
    monkeypatch.setattr(c, "_session_request", lambda method, url, **kw: "ok")
    before = c.REQUESTS["n"]
    assert c._counted_request("GET", "x") == "ok" and c._counted_request("POST", "y") == "ok"
    assert c.REQUESTS["n"] == before + 2



def test_empty_backfill_response_keeps_stock_partial(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_STORE", str(tmp_path / "store.json.gz"))
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "STOCK_BACKFILL_PER_RUN", 0)
    uni = lambda m, n: [{"code": f"{m}{k}", "name": "x", "market": m} for k in range(n)]
    monkeypatch.setattr(c, "fetch_universe", uni)
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: _trend_rows(60))
    now = datetime(2026, 10, 5, 7, 37, tzinfo=K)
    assert c.build_universe(now)[2] == 350                       # 처음엔 모두 60일만(채우는 중)
    monkeypatch.setattr(c, "STOCK_BACKFILL_PER_RUN", 1000)
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: [])   # 처음부터 받기가 빈 응답
    assert c.build_universe(now + timedelta(days=1))[2] == 350   # 빈 응답이면 '다 채움'으로 바꾸지 않는다



def test_universe_backfill_only_fetches_just_the_next_batch(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_STORE", str(tmp_path / "store.json.gz"))
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    monkeypatch.setattr(c, "STOCK_BACKFILL_PER_RUN", 70)
    monkeypatch.setattr(c, "fetch_universe", lambda m, n: [{"code": f"{m}{k}", "name": "x", "market": m} for k in range(n)])
    asked = []
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: asked.append(days) or _trend_rows(min(days, 80)))
    now = datetime(2026, 10, 6, 7, 30, tzinfo=K)
    assert c.build_universe(now)[2] == 280
    asked.clear()
    summaries, rows_by, pending = c.build_universe(now + timedelta(hours=9), backfill_only=True)
    assert asked == [c.STOCK_BACKFILL_DAYS] * 70                 # 이미 최신인 종목은 건너뛰고 다음 70종목만
    assert pending == 210 and len(summaries) == 350 and len(rows_by) == 350


def test_build_stocks_reuses_previous_flows_without_fetching(monkeypatch):
    listing = {"stocks": [{"itemCode": "005930", "stockName": "삼성전자", "closePrice": "1", "marketValue": "1",
                           "fluctuationsRatio": "0", "stockEndType": "stock"}]}
    asked = []
    def fake_get(url, *a, **k):
        asked.append(url)
        if "marketValue" in url:
            return listing if "page=1" in url else {"stocks": []}
        if "industry" in url:
            return {"groups": []}
        raise AssertionError(f"종목 수급을 다시 받으면 안 됨: {url}")
    monkeypatch.setattr(c, "get_json", fake_get)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    prev = {"005930": {"code": "005930", "flow": [{"date": "20261002", "foreign": -1}], "stat60": {"days": 60},
                       "foreignHoldRatio": 46.4}}
    out = c.build_stocks(1, 0, reuse={"top": prev, "series": {"005930": {"d": ["20261002"]}}})
    s = out["top"][0]
    assert s["flow"] == prev["005930"]["flow"] and s["stat60"] == {"days": 60} and out["series"]["005930"]["d"] == ["20261002"]
    assert not any("/trend" in u_ for u_ in asked)



def test_universe_waits_until_stock_rows_are_published(monkeypatch):
    prev = {"universe": [{"code": "005930", "asOf": "2026-10-02"}], "universePending": 0}
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: _trend_rows(3, start=date(2026, 9, 30)))
    assert not c.stock_rows_published(prev, "2026-10-06")                  # 9/30~10/2 만 있음
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: [["20261006", 1, 1, 1, 1.0, 1.0, 1]])
    assert c.stock_rows_published(prev, "2026-10-06")
    monkeypatch.setattr(c, "fetch_stock_trend", lambda code, days: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    assert not c.stock_rows_published(prev, "2026-10-06")
    assert c.stock_rows_published({}, "2026-10-06")                        # 처음이면 확인 없이 받는다
