"""
수집기 테스트 공통 준비물. 네트워크는 쓰지 않는다 — 외부 소스는 전부 가짜로 바꿔 끼운다.
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import collect as c  # noqa: E402

FLOW_KEYS = list(c.INVESTOR_GROUPS)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """모듈 전역 상태(경고·지난 데이터 표시·시크릿)를 테스트마다 비우고, 출력 위치를 임시 폴더로."""
    for xs in (c.WARNINGS, c.STALE):
        xs.clear()
    c.FRESH_AT.clear()
    c.SECRETS.clear()
    (tmp_path / "docs" / "data").mkdir(parents=True)
    monkeypatch.setattr(c, "ROOT", tmp_path)
    monkeypatch.setattr(c, "OUT", tmp_path / "docs" / "data")
    # 네트워크로 새는 호출은 바로 실패하게
    monkeypatch.setattr(c, "get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("테스트: 네트워크 금지")))
    blocked = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("테스트: 네트워크 금지"))
    monkeypatch.setattr(c.session, "get", blocked)
    monkeypatch.setattr(c.session, "post", blocked)
    # 장 일정(휴장일·금통위)은 실행 중에 바뀔 수 있어 테스트마다 저장소 seed 상태로
    monkeypatch.setattr(c, "HOLIDAYS", dict(c.HOLIDAYS))
    monkeypatch.setattr(c, "MPC", {y: list(v) for y, v in c.MPC.items()})
    monkeypatch.setattr(c, "SPECIAL_SESSIONS", dict(c.SPECIAL_SESSIONS))
    monkeypatch.setitem(c.REQUESTS, "n", 0)
    yield


def freeze_now(monkeypatch, when):
    """collect 안의 datetime.now() 를 when(KST aware)으로 고정 — 실행 날짜에 따라 결과가 바뀌는 테스트를 막는다."""
    real = c.datetime

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)
    monkeypatch.setattr(c, "datetime", Frozen)


def trading_days(n: int, end: date) -> list[str]:
    """end 이전(포함) 평일 n 개, 오름차순."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def make_market(n: int = 750, end: date = date(2026, 10, 2), seed: int = 1, tv: bool = True):
    """
    가짜 시장: 개인·외국인이 반대로 움직이는 수급(억원)과 지수 종가.
    거래대금은 시간이 갈수록 커지게 만든다(실제처럼 3년 새 규모가 몇 배로).
    반환: (수급 행, {날짜: 종가})
    """
    rng = random.Random(seed)
    rows, closes, px = [], {}, 2500.0
    days = trading_days(n, end)
    for k, d in enumerate(days):
        value = 80000 + 220000 * k / n + rng.random() * 30000
        ind = rng.gauss(0, 0.04) * value
        frn = -0.8 * ind + rng.gauss(0, 0.02) * value
        ins = rng.gauss(0, 0.015) * value
        row = {key: 0.0 for key in FLOW_KEYS}
        row.update({"date": d, "individual": float(round(ind)), "foreign": float(round(frn)),
                    "institution": float(round(ins)), "other_corp": float(round(-(ind + frn + ins)))})
        if tv:
            row["tradingValue"] = float(round(value))
        rows.append(row)
        px *= 1 + rng.gauss(0.0006, 0.012)
        closes[d] = round(px, 2)
    return rows, closes


def market_payload(closes: dict, traded: str | None = None, price: float | None = None, status="CLOSE"):
    """build_market 이 돌려주는 모양의 (market, closes)."""
    days = sorted(closes)
    bars = [{"date": d, "close": closes[d], "open": closes[d], "high": closes[d], "low": closes[d],
             "volume": None, "value": None} for d in days]
    traded = traded or days[-1]
    entry = lambda code, name: {
        "code": code, "name": name, "price": price if price is not None else closes[days[-1]],
        "change": 1.0, "changeRate": 0.1, "marketStatus": status,
        "tradedAt": f"{traded}T20:15:00+09:00", "history": bars[-c.CHART_DAYS:],
    }
    return ({"indices": {"KOSPI": entry("KOSPI", "코스피"), "KOSDAQ": entry("KOSDAQ", "코스닥")}},
            {"KOSPI": dict(closes), "KOSDAQ": dict(closes)})
