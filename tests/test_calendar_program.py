"""
국내 장 일정(휴장일·만기·금통위), 프로그램 매매, 공매도 — 네트워크 없이 파싱·계산·실패 경로를 본다.
"""
import json
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
