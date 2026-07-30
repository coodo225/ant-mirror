# -*- coding: utf-8 -*-
"""
한국 증시 수급 대시보드 - 데이터 수집기

공개 데이터만 사용해서 docs/data/*.json 을 생성한다.
브라우저는 CORS 때문에 이 소스들을 직접 못 부르므로, 수집은 여기(서버/Actions)서 하고
프론트엔드는 같은 오리진의 정적 JSON만 읽는다.

소스
  - 네이버 금융      : 개인/외국인/기관 수급, 지수 시세, 시총상위, 업종별 등락
  - FinanceDataReader: 코스피/코스닥 일봉 히스토리
  - yfinance         : 미국 지수/선물/금리/환율/원자재
  - federalreserve.gov / bls.gov : FOMC·CPI 발표 일정 (실패 시 events_seed.json 사용)
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
SEED = Path(__file__).resolve().parent / "events_seed.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

session = requests.Session()
session.headers.update({"User-Agent": UA, "Referer": "https://finance.naver.com/"})

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  [warn] {msg}")


def num(s) -> float | None:
    """'-19,701' / '+3.5%' / '1,234.56' -> float"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace(",", "").replace("%", "").replace("+", "").strip()
    if t in ("", "-", "N/A", "null"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def get_json(url: str, tries: int = 3, **kw):
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=20, **kw)
            if r.status_code == 200 and r.text.strip()[:1] in "{[":
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"{url} -> {last}")


def write(name: str, payload) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"  -> {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


# ---------------------------------------------------------------- 수급 (핵심)

ACTOR_KEYS = ["individual", "foreign", "institution"]

# 네이버 투자자별 매매동향 표의 컬럼 순서 (단위: 억원)
FLOW_COLS = [
    "date",
    "individual",      # 개인
    "foreign",         # 외국인
    "institution",     # 기관계
    "inst_fin_inv",    # 금융투자
    "inst_insurance",  # 보험
    "inst_trust",      # 투신(사모)
    "inst_bank",       # 은행
    "inst_other_fin",  # 기타금융기관
    "inst_pension",    # 연기금등
    "other_corp",      # 기타법인
]


def fetch_investor_flows(sosok: str, days: int = 40) -> list[dict]:
    """
    네이버 '투자자별 매매동향' 일별 표를 긁는다. 한 페이지 10거래일.
    sosok: '01'=코스피, '02'=코스닥. 단위 억원.
    """
    url = "https://finance.naver.com/sise/investorDealTrendDay.naver"
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = datetime.now(KST).date()

    while len(rows) < days:
        r = session.get(
            url, params={"bizdate": cursor.strftime("%Y%m%d"), "sosok": sosok}, timeout=20
        )
        r.encoding = "euc-kr"
        page_rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
            cells = [
                re.sub(r"<[^>]+>", "", c).replace("\xa0", "").replace("&nbsp;", "").strip()
                for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            ]
            cells = [c for c in cells if c != ""]
            if not cells or not re.match(r"^\d\d\.\d\d\.\d\d$", cells[0]):
                continue
            if len(cells) < len(FLOW_COLS):
                continue
            yy, mm, dd = cells[0].split(".")
            iso = f"20{yy}-{mm}-{dd}"
            if iso in seen:
                continue
            seen.add(iso)
            rec = {"date": iso}
            for key, raw in zip(FLOW_COLS[1:], cells[1 : len(FLOW_COLS)]):
                rec[key] = num(raw)
            page_rows.append(rec)

        if not page_rows:
            break
        rows.extend(page_rows)
        oldest = min(r_["date"] for r_ in page_rows)
        cursor = date.fromisoformat(oldest) - timedelta(days=1)
        time.sleep(0.4)

    rows.sort(key=lambda r_: r_["date"])
    return rows[-days:]


def streak(rows: list[dict], key: str) -> dict:
    """가장 최근 값 기준으로 같은 부호가 며칠 연속인지."""
    if not rows:
        return {"days": 0, "side": "flat", "total": 0.0}
    last = rows[-1].get(key)
    if last is None or last == 0:
        return {"days": 0, "side": "flat", "total": 0.0}
    side = "buy" if last > 0 else "sell"
    total, n = 0.0, 0
    for r in reversed(rows):
        v = r.get(key)
        if v is None or (v > 0) != (last > 0) or v == 0:
            break
        total += v
        n += 1
    return {"days": n, "side": side, "total": round(total, 1)}


CHART_DAYS = 120   # 화면 차트에 넣을 일수
STAT_DAYS  = 750   # 성적표 통계에 쓸 일수 (약 3년)


def build_flows() -> tuple[dict, dict]:
    """(화면용 flows, 통계용 전체 히스토리) 를 함께 돌려준다."""
    out: dict = {"unit": "억원", "chartDays": CHART_DAYS, "markets": {}}
    full: dict = {}
    for code, sosok in (("KOSPI", "01"), ("KOSDAQ", "02")):
        try:
            rows = fetch_investor_flows(sosok, days=STAT_DAYS)
            if not rows:
                warn(f"{code} 수급 데이터가 비어 있음")
                continue
            full[code] = rows

            chart = rows[-CHART_DAYS:]
            cum = {"individual": 0.0, "foreign": 0.0, "institution": 0.0}
            series = []
            for r in chart:
                for k in cum:
                    cum[k] += r.get(k) or 0.0
                series.append({**r, "cum": {k: round(v, 1) for k, v in cum.items()}})
            out["markets"][code] = {
                "daily": series,
                "latest": series[-1],
                "streaks": {k: streak(rows, k) for k in ("individual", "foreign", "institution")},
            }
            print(f"  {code} 수급 {len(rows)}일 ({rows[0]['date']} ~ {rows[-1]['date']})")
        except Exception as e:  # noqa: BLE001
            warn(f"{code} 수급 수집 실패: {type(e).__name__}: {e}")
    return out, full


# ---------------------------------------------------------------- 선물 (외국인의 선행 포지션)

def build_futures() -> dict:
    """
    코스피200 선물 투자자별 순매수 (네이버 sosok=03, 단위: 계약).
    외국인은 현물보다 선물을 먼저 움직이는 경우가 많아 선행 지표로 쓴다.
    """
    out: dict = {"unit": "계약", "daily": [], "latest": None, "streaks": {}, "divergence": None}
    try:
        rows = fetch_investor_flows("03", days=60)
        if not rows:
            warn("선물 수급 데이터가 비어 있음")
            return out
        out["daily"] = rows
        out["latest"] = rows[-1]
        out["streaks"] = {k: streak(rows, k) for k in ACTOR_KEYS}
        print(f"  선물 수급 {len(rows)}일 (최근 {rows[-1]['date']})")
    except Exception as e:  # noqa: BLE001
        warn(f"선물 수급 수집 실패: {type(e).__name__}: {e}")
    return out


def attach_futures_divergence(futures: dict, flows: dict) -> None:
    """최근 5거래일 기준 외국인의 현물 방향과 선물 방향을 비교한다."""
    try:
        spot_rows = flows["markets"]["KOSPI"]["daily"][-5:]
        fut_rows = futures["daily"][-5:]
        if len(spot_rows) < 5 or len(fut_rows) < 5:
            return
        spot = sum(r.get("foreign") or 0 for r in spot_rows)      # 억원
        fut = sum(r.get("foreign") or 0 for r in fut_rows)        # 계약
        futures["divergence"] = {
            "window": 5,
            "spotForeign": round(spot, 1),
            "futuresForeign": round(fut),
            "aligned": (spot >= 0) == (fut >= 0),
        }
    except Exception as e:  # noqa: BLE001
        warn(f"현·선물 비교 실패: {e}")


# ---------------------------------------------------------------- 신용융자 / 증시자금 (KOFIA)

KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"


def kofia_query(obj: str, start: str, end: str) -> list[dict]:
    r = session.post(
        KOFIA_URL,
        json={"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
                            "tmpV45": start, "tmpV46": end, "OBJ_NM": obj}},
        headers={"Content-Type": "application/json",
                 "Referer": "https://freesis.kofia.or.kr/"},
        timeout=25,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    rows = r.json().get("ds1") or []
    rows.sort(key=lambda x: x.get("TMPV1") or "")
    return rows


def build_credit() -> dict:
    """
    금융투자협회(KOFIA) 공개 통계.
      STATSCU0100000070BO: 신용공여 잔고 — 신용거래융자(유가/코스닥), 신용대주, 예탁증권담보융자
      STATSCU0100000060BO: 증시자금 — 투자자예탁금, 위탁매매 미수금, 반대매매 금액·비율
    원본 단위는 백만원, 저장은 억원(÷100)으로 통일. 반대매매 비율은 %.
    """
    out: dict = {"unit": "억원", "loans": [], "money": [], "latest": {}}
    end = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=220)).strftime("%Y%m%d")

    def to_eok(v):
        return round(v / 100, 1) if isinstance(v, (int, float)) else None

    try:
        for row in kofia_query("STATSCU0100000070BO", start, end):
            d = row.get("TMPV1")
            if not d or len(str(d)) != 8:
                continue
            out["loans"].append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "total": to_eok(row.get("TMPV2")),
                "kospi": to_eok(row.get("TMPV3")),
                "kosdaq": to_eok(row.get("TMPV4")),
                "shortSale": to_eok(row.get("TMPV5")),
                "collateral": to_eok(row.get("TMPV9")),
            })
        print(f"  신용융자 잔고 {len(out['loans'])}일")
    except Exception as e:  # noqa: BLE001
        warn(f"신용융자 잔고(KOFIA 70BO) 실패: {type(e).__name__}: {e}")

    try:
        for row in kofia_query("STATSCU0100000060BO", start, end):
            d = row.get("TMPV1")
            if not d or len(str(d)) != 8:
                continue
            out["money"].append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                "deposits": to_eok(row.get("TMPV2")),        # 투자자예탁금
                "receivables": to_eok(row.get("TMPV5")),     # 위탁매매 미수금
                "liquidation": to_eok(row.get("TMPV6")),     # 반대매매 금액
                "liqRatio": row.get("TMPV7"),                # 미수금 대비 반대매매 %
            })
        print(f"  증시자금(예탁금·반대매매) {len(out['money'])}일")
    except Exception as e:  # noqa: BLE001
        warn(f"증시자금(KOFIA 60BO) 실패: {type(e).__name__}: {e}")

    # 요약: 최신값과 5/20거래일 변화
    if out["loans"]:
        last = out["loans"][-1]
        def delta(n):
            if len(out["loans"]) > n and out["loans"][-1 - n]["total"]:
                return round(last["total"] - out["loans"][-1 - n]["total"], 1)
            return None
        out["latest"]["loans"] = {**last, "d5": delta(5), "d20": delta(20)}
    if out["money"]:
        lastm = out["money"][-1]
        liq20 = [m["liquidation"] for m in out["money"][-21:-1] if m["liquidation"] is not None]
        avg20 = round(sum(liq20) / len(liq20), 1) if liq20 else None
        out["latest"]["money"] = {**lastm, "liqAvg20": avg20}
    return out


# ---------------------------------------------------------------- 지수 / 종목

INDEX_META = {"KOSPI": {"name": "코스피", "fdr": "KS11"}, "KOSDAQ": {"name": "코스닥", "fdr": "KQ11"}}


def build_market() -> tuple[dict, dict]:
    """(화면용 market, 지수별 {날짜: 종가} 전체) 를 함께 돌려준다."""
    out: dict = {"indices": {}}
    closes: dict = {}
    for code, meta in INDEX_META.items():
        entry = {"code": code, "name": meta["name"]}
        try:
            b = get_json(f"https://m.stock.naver.com/api/index/{code}/basic")
            entry.update(
                {
                    "price": num(b.get("closePrice")),
                    "change": num(b.get("compareToPreviousClosePrice")),
                    "changeRate": num(b.get("fluctuationsRatio")),
                    "marketStatus": b.get("marketStatus"),
                    "tradedAt": b.get("localTradedAt"),
                }
            )
        except Exception as e:  # noqa: BLE001
            warn(f"{code} 지수 시세 실패: {e}")

        try:
            import FinanceDataReader as fdr

            df = fdr.DataReader(meta["fdr"], (date.today() - timedelta(days=1200)).isoformat())
            closes[code] = {i.strftime("%Y-%m-%d"): float(v) for i, v in df["Close"].items()}
            hist = []
            for idx, row in df.tail(CHART_DAYS).iterrows():
                hist.append(
                    {
                        "date": idx.strftime("%Y-%m-%d"),
                        "close": round(float(row["Close"]), 2),
                        "open": round(float(row["Open"]), 2),
                        "high": round(float(row["High"]), 2),
                        "low": round(float(row["Low"]), 2),
                        "volume": int(row["Volume"]) if row.get("Volume") == row.get("Volume") else None,
                        "value": int(row["Amount"]) if "Amount" in row and row["Amount"] == row["Amount"] else None,
                    }
                )
            entry["history"] = hist
            if entry.get("price") is None and hist:
                entry["price"] = hist[-1]["close"]
            print(f"  {code} 일봉 {len(hist)}개")
        except Exception as e:  # noqa: BLE001
            warn(f"{code} 일봉 실패: {type(e).__name__}: {e}")
        out["indices"][code] = entry
    return out, closes


def build_stocks(n_kospi: int = 60, n_kosdaq: int = 40) -> dict:
    out: dict = {"top": [], "industries": []}
    for market, top_n in (("KOSPI", n_kospi), ("KOSDAQ", n_kosdaq)):
        try:
            got = 0
            for page in range(1, top_n // 20 + 2):
                if got >= top_n:
                    break
                data = get_json(
                    f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
                    f"?page={page}&pageSize=20"
                )
                stocks = data.get("stocks", [])
                if not stocks:
                    break
                for s in stocks:
                    if got >= top_n:
                        break
                    out["top"].append(
                        {
                            "code": s.get("itemCode"),
                            "name": s.get("stockName"),
                            "market": market,
                            "price": num(s.get("closePrice")),
                            "change": num(s.get("compareToPreviousClosePrice")),
                            "changeRate": num(s.get("fluctuationsRatio")),
                            "marketCap": num(s.get("marketValue")),          # 백만원
                            "tradingValue": num(s.get("accumulatedTradingValue")),
                            "marketCapText": s.get("marketValueHangeul"),
                        }
                    )
                    got += 1
                time.sleep(0.15)
            print(f"  {market} 시총상위 {got}종목")
        except Exception as e:  # noqa: BLE001
            warn(f"{market} 시총상위 실패: {e}")

    # 종목별 개인/외인/기관 (수량 기준)
    for s in out["top"]:
        try:
            tr = get_json(
                f"https://m.stock.naver.com/api/stock/{s['code']}/trend?pageSize=5&page=1"
            )
            days = []
            for d in tr if isinstance(tr, list) else []:
                days.append(
                    {
                        "date": d.get("bizdate"),
                        "individual": num(d.get("individualPureBuyQuant")),
                        "foreign": num(d.get("foreignerPureBuyQuant")),
                        "institution": num(d.get("organPureBuyQuant")),
                        "foreignHoldRatio": num(d.get("foreignerHoldRatio")),
                    }
                )
            days.sort(key=lambda d: d["date"] or "")
            s["flow"] = days
            if days:
                s["foreignHoldRatio"] = days[-1]["foreignHoldRatio"]
            time.sleep(0.15)
        except Exception as e:  # noqa: BLE001
            warn(f"{s['name']} 수급 실패: {e}")
            s["flow"] = []

    try:
        ind = get_json("https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=100")
        for g in ind.get("groups", []):
            out["industries"].append(
                {
                    "name": g.get("name"),
                    "changeRate": num(g.get("changeRate")),
                    "count": g.get("totalCount"),
                    "rise": g.get("riseCount"),
                    "fall": g.get("fallCount"),
                }
            )
        out["industries"].sort(key=lambda g: g["changeRate"] if g["changeRate"] is not None else 0)
        print(f"  업종 {len(out['industries'])}개")
    except Exception as e:  # noqa: BLE001
        warn(f"업종 실패: {e}")
    return out


# ---------------------------------------------------------------- 글로벌 / 매크로

GLOBAL_TICKERS = [
    ("^GSPC", "S&P 500", "us"),
    ("^IXIC", "나스닥", "us"),
    ("^DJI", "다우", "us"),
    ("^SOX", "필라델피아 반도체", "us"),
    ("^VIX", "VIX 공포지수", "risk"),
    ("ES=F", "S&P500 선물", "futures"),
    ("NQ=F", "나스닥100 선물", "futures"),
    ("^TNX", "미국 10년물 금리", "rates"),
    ("KRW=X", "원/달러 환율", "fx"),
    ("DX-Y.NYB", "달러인덱스", "fx"),
    ("CL=F", "WTI 유가", "commodity"),
    ("GC=F", "금", "commodity"),
]


def build_global() -> dict:
    out = {"items": [], "sparkDays": 30}
    try:
        import yfinance as yf

        symbols = [t[0] for t in GLOBAL_TICKERS]
        df = yf.download(
            symbols, period="3mo", interval="1d",
            progress=False, auto_adjust=False, group_by="ticker", threads=True,
        )
        for sym, name, cat in GLOBAL_TICKERS:
            try:
                closes = df[sym]["Close"].dropna()
                if len(closes) < 2:
                    warn(f"{name}({sym}) 데이터 부족")
                    continue
                last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                spark = [round(float(v), 4) for v in closes.tail(30)]
                out["items"].append(
                    {
                        "symbol": sym, "name": name, "category": cat,
                        "price": round(last, 4),
                        "change": round(last - prev, 4),
                        "changeRate": round((last - prev) / prev * 100, 2) if prev else None,
                        "asOf": closes.index[-1].strftime("%Y-%m-%d"),
                        "spark": spark,
                    }
                )
            except Exception as e:  # noqa: BLE001
                warn(f"{name}({sym}) 처리 실패: {type(e).__name__}: {e}")
        print(f"  글로벌 지표 {len(out['items'])}개")
    except Exception as e:  # noqa: BLE001
        warn(f"yfinance 실패: {type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- 이벤트 일정

MONTHS = {
    m: i
    for i, m in enumerate(
        "January February March April May June July August September October "
        "November December".split(),
        start=1,
    )
}


def scrape_fomc() -> list[dict]:
    """
    연준 캘린더 페이지에서 FOMC 회의 일정 파싱.
    회의는 이틀이며 결정은 마지막 날 발표된다(미 동부 오후 2시 = 한국 새벽).
    날짜 뒤의 '*' 는 경제전망요약(SEP, 이른바 점도표)이 함께 나오는 회의를 뜻한다.
    """
    r = session.get(
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", timeout=25
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")

    events: list[dict] = []
    parts = re.split(r"(\d{4})\s+FOMC\s+Meetings", r.text)
    for k in range(1, len(parts), 2):
        year, seg = int(parts[k]), parts[k + 1]
        for m in re.finditer(
            r"fomc-meeting__month[^>]*>\s*(?:<strong>)?([A-Za-z/]+)(?:</strong>)?\s*</div>\s*"
            r"<div[^>]*fomc-meeting__date[^>]*>\s*([0-9\-–\s\*]+)",
            seg,
        ):
            mon_raw, day_raw = m.group(1), m.group(2).strip()
            months = [MONTHS[x] for x in mon_raw.split("/") if x in MONTHS]
            days = [int(d) for d in re.findall(r"\d+", day_raw)]
            if not months or not days:
                continue
            end_month = months[-1]
            end_year = year + (1 if len(months) > 1 and months[-1] < months[0] else 0)
            has_sep = "*" in day_raw
            events.append(
                {
                    "type": "FOMC",
                    "date": f"{end_year}-{end_month:02d}-{days[-1]:02d}",
                    "title": "FOMC 금리 결정" + (" + 점도표(SEP)" if has_sep else ""),
                    "note": "결과 발표는 한국시간 다음날 새벽 3시경",
                    "impact": "high",
                }
            )
    return events


FRED_BASE = "https://api.stlouisfed.org/fred"
FRED_RELEASES = {
    "Consumer Price Index": ("CPI", "미국 CPI 발표", "high"),
    "Employment Situation": ("고용", "미국 고용보고서", "high"),
    "Personal Income and Outlays": ("PCE", "미국 PCE 물가", "high"),
    "Gross Domestic Product": ("GDP", "미국 GDP", "mid"),
}


def fetch_fred_events(api_key: str) -> list[dict]:
    """FRED 의 릴리스 캘린더에서 향후 발표 예정일을 가져온다."""
    rel = get_json(f"{FRED_BASE}/releases?api_key={api_key}&file_type=json&limit=1000")
    wanted = {}
    for r in rel.get("releases", []):
        for needle, meta in FRED_RELEASES.items():
            if r.get("name", "").strip() == needle:
                wanted[r["id"]] = meta

    today = datetime.now(KST).date()
    end = (today + timedelta(days=120)).isoformat()
    events: list[dict] = []
    for rid, (tag, title, impact) in wanted.items():
        try:
            d = get_json(
                f"{FRED_BASE}/release/dates?release_id={rid}&api_key={api_key}"
                f"&file_type=json&include_release_dates_with_no_data=true"
                f"&realtime_start={today.isoformat()}&realtime_end={end}&limit=20"
            )
            for row in d.get("release_dates", []):
                events.append({"type": tag, "date": row["date"], "title": title, "impact": impact})
        except Exception as e:  # noqa: BLE001
            warn(f"FRED 릴리스 {rid} 실패: {e}")
    return events


def fetch_fred_series(api_key: str) -> list[dict]:
    """주요 매크로 지표의 최신 실측치."""
    want = [
        ("CPIAUCSL", "미국 CPI(전년비)", "yoy"),
        ("CPILFESL", "미국 근원 CPI(전년비)", "yoy"),
        ("FEDFUNDS", "미국 기준금리", "level"),
        ("UNRATE", "미국 실업률", "level"),
    ]
    out = []
    for sid, name, mode in want:
        try:
            d = get_json(
                f"{FRED_BASE}/series/observations?series_id={sid}&api_key={api_key}"
                f"&file_type=json&sort_order=desc&limit=14"
            )
            obs = [o for o in d.get("observations", []) if o.get("value") not in (".", None)]
            if not obs:
                continue
            latest = float(obs[0]["value"])
            value = latest
            if mode == "yoy" and len(obs) >= 13:
                year_ago = float(obs[12]["value"])
                value = (latest / year_ago - 1) * 100
            out.append({"id": sid, "name": name, "value": round(value, 2),
                        "asOf": obs[0]["date"], "unit": "%"})
        except Exception as e:  # noqa: BLE001
            warn(f"FRED 시계열 {sid} 실패: {e}")
    return out


def build_events() -> tuple[dict, list[dict]]:
    events: list[dict] = []
    macro: list[dict] = []
    try:
        events = scrape_fomc()
        print(f"  FOMC 일정 {len(events)}건 (연준 사이트)")
    except Exception as e:  # noqa: BLE001
        warn(f"FOMC 일정 스크래핑 실패: {type(e).__name__}: {e}")

    # 우선순위: 환경변수 → collector/.fred_key 파일 (한 줄짜리, git 에는 올라가지 않음)
    fred_key = os.environ.get("FRED_API_KEY", "").strip()
    key_file = Path(__file__).resolve().parent / ".fred_key"
    if not fred_key and key_file.exists():
        fred_key = key_file.read_text(encoding="utf-8").strip()
    if fred_key:
        try:
            fe = fetch_fred_events(fred_key)
            events.extend(fe)
            print(f"  FRED 발표일정 {len(fe)}건")
            macro = fetch_fred_series(fred_key)
            print(f"  FRED 매크로 지표 {len(macro)}건")
        except Exception as e:  # noqa: BLE001
            warn(f"FRED 실패: {type(e).__name__}: {e}")
    else:
        warn("FRED_API_KEY 미설정 — 미국 CPI/고용/PCE 발표 일정과 매크로 실측치를 건너뜁니다. "
             "무료 키: https://fred.stlouisfed.org/docs/api/api_key.html")

    if SEED.exists():
        try:
            seed = json.loads(SEED.read_text(encoding="utf-8"))
            have = {(e["type"], e["date"]) for e in events}
            for e in seed.get("events", []):
                if (e["type"], e["date"]) not in have:
                    events.append(e)
            print(f"  events_seed.json 에서 {len(seed.get('events', []))}건 병합")
        except Exception as e:  # noqa: BLE001
            warn(f"events_seed.json 로드 실패: {e}")

    today = datetime.now(KST).date()
    for e in events:
        try:
            e["dday"] = (date.fromisoformat(e["date"]) - today).days
        except Exception:  # noqa: BLE001
            e["dday"] = None
    events = [e for e in events if e.get("dday") is not None]
    # (type, date) 중복 제거
    uniq: dict[tuple, dict] = {}
    for e in events:
        uniq.setdefault((e["type"], e["date"]), e)
    events = sorted(uniq.values(), key=lambda e: e["date"])
    future = [e for e in events if e["dday"] >= 0]
    upcoming = future[:8]
    # FOMC 는 영향이 가장 큰 이벤트 — 8개 제한에 밀려나지 않게 보장한다
    next_fomc = next((e for e in future if e["type"] == "FOMC"), None)
    if next_fomc and next_fomc not in upcoming:
        upcoming = upcoming[:7] + [next_fomc]
    recent = [e for e in events if e["dday"] < 0][-4:]
    return {"upcoming": upcoming, "recent": recent, "today": today.isoformat()}, macro


# ---------------------------------------------------------------- 개미 성적표

HORIZONS = (1, 5, 20)


def _corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


def _percentile(values: list[float], v: float) -> float:
    """v 가 values 분포에서 몇 퍼센타일인지 (0~100)."""
    if not values:
        return 50.0
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (below + equal / 2) / len(values) * 100


def _grade(excess: float | None) -> str:
    """시장 평균 대비 초과수익(%p) 을 학점으로."""
    if excess is None:
        return "?"
    for cut, g in ((3, "A"), (1, "B"), (-1, "C"), (-3, "D")):
        if excess >= cut:
            return g
    return "F"


def build_ant(full: dict, closes: dict, code: str = "KOSPI") -> dict:
    """
    '개미는 정말 반대로 움직이고, 그래서 틀렸는가' 를 실제 데이터로 채점한다.
    상관관계일 뿐 인과가 아니며, 표본 기간의 장세에 크게 좌우된다는 점을 함께 실어 보낸다.
    """
    rows = full.get(code) or []
    px = closes.get(code) or {}
    rows = [r for r in rows if r["date"] in px]
    if len(rows) < 60:
        warn(f"개미 성적표: {code} 표본 부족({len(rows)}일) — 건너뜀")
        return {}

    C = [px[r["date"]] for r in rows]
    S = {k: [r[k] or 0.0 for r in rows] for k in ACTOR_KEYS}
    n = len(rows)

    # 이후 h거래일 지수 수익률(%)
    fwd = {h: [(C[i + h] / C[i] - 1) * 100 for i in range(n - h)] for h in HORIZONS}
    baseline = {h: sum(fwd[h]) / len(fwd[h]) for h in HORIZONS}

    actors: dict = {}
    for k in ACTOR_KEYS:
        v = S[k]
        timing = {}
        for h in HORIZONS:
            timing[f"corr{h}"] = round(_corr(v[: n - h], fwd[h]) or 0, 3)

        # 상위 20% 강하게 순매수한 날들 이후 성적
        thr = sorted(v)[int(n * 0.8)]
        heavy = [i for i, x in enumerate(v) if x >= thr]
        top = {}
        for h in HORIZONS:
            ii = [i for i in heavy if i + h < n]
            top[f"r{h}"] = round(sum((C[i + h] / C[i] - 1) * 100 for i in ii) / len(ii), 2) if ii else None
        top["n"] = len(heavy)

        excess = None if top["r20"] is None else round(top["r20"] - baseline[20], 2)
        actors[k] = {
            "timing": timing,
            "heavyBuy": top,
            "excess20": excess,
            "grade": _grade(excess),
            "todayValue": v[-1],
            "todayPercentile": round(_percentile(v, v[-1]), 1),
        }

    # 오늘 개인의 매수강도가 극단이면, 과거 같은 구간의 20일 성적을 참고치로 붙인다
    iv = S["individual"]
    p = actors["individual"]["todayPercentile"]
    srt = sorted(iv)
    bucket, label, idx = None, None, []
    if p >= 80:
        cut = srt[int(n * 0.8)]
        idx = [i for i, x in enumerate(iv) if x >= cut]
        label = "개미가 지금처럼 강하게 사들였던 날(상위 20%)"
    elif p <= 20:
        cut = srt[int(n * 0.2)]
        idx = [i for i, x in enumerate(iv) if x <= cut]
        label = "개미가 지금처럼 강하게 팔았던 날(하위 20%)"
    if idx:
        ii = [i for i in idx if i + 20 < n]
        if ii:
            r20 = sum((C[i + 20] / C[i] - 1) * 100 for i in ii) / len(ii)
            bucket = {
                "label": label,
                "side": "buy" if p >= 80 else "sell",
                "n": len(ii),
                "r20": round(r20, 2),
                "baseline20": round(baseline[20], 2),
                "excess": round(r20 - baseline[20], 2),
            }

    # 개인이 가장 크게 사들였던 날들의 그 후 20일
    ranked = sorted(range(n - 20), key=lambda i: -iv[i])[:5]
    hall = [
        {
            "date": rows[i]["date"],
            "amount": iv[i],
            "close": round(C[i], 2),
            "after20": round(C[i + 20], 2),
            "return20": round((C[i + 20] / C[i] - 1) * 100, 2),
        }
        for i in ranked
    ]

    opp = {
        "vsForeign": round(sum(1 for a, b in zip(S["individual"], S["foreign"]) if a * b < 0) / n * 100, 1),
        "vsInstitution": round(sum(1 for a, b in zip(S["individual"], S["institution"]) if a * b < 0) / n * 100, 1),
    }

    # 연도별 분해 — "F학점이 장세 탓인지"를 검증할 수 있게 한다
    yearly = []
    years = sorted({r["date"][:4] for r in rows})
    for y in years:
        yi = [i for i, r in enumerate(rows) if r["date"].startswith(y)]
        if len(yi) < 60:
            continue
        yv = [iv[i] for i in yi]
        yf = [S["foreign"][i] for i in yi]
        y_fwd = [i for i in yi if i + 20 < n]
        if len(y_fwd) < 40:
            continue
        y_base = sum((C[i + 20] / C[i] - 1) * 100 for i in y_fwd) / len(y_fwd)
        thr_y = sorted(yv)[int(len(yv) * 0.8)]
        hv = [i for i in y_fwd if iv[i] >= thr_y]
        if len(hv) < 8:
            continue
        y_r20 = sum((C[i + 20] / C[i] - 1) * 100 for i in hv) / len(hv)
        excess_y = y_r20 - y_base
        yearly.append({
            "year": y,
            "days": len(yi),
            "corrIF": round(_corr(yv, yf) or 0, 3),
            "baseline20": round(y_base, 2),
            "indivHeavyR20": round(y_r20, 2),
            "excess": round(excess_y, 2),
            "grade": _grade(excess_y),
        })

    return {
        "market": code,
        "yearly": yearly,
        "sample": {"days": n, "from": rows[0]["date"], "to": rows[-1]["date"]},
        "baseline": {f"r{h}": round(baseline[h], 2) for h in HORIZONS},
        "correlation": {
            "individual_foreign": round(_corr(S["individual"], S["foreign"]) or 0, 3),
            "individual_institution": round(_corr(S["individual"], S["institution"]) or 0, 3),
            "foreign_institution": round(_corr(S["foreign"], S["institution"]) or 0, 3),
        },
        "oppositeRate": opp,
        "actors": actors,
        "contrarianRead": bucket,
        "hallOfFame": hall,
        "caveats": [
            f"표본은 {rows[0]['date']}~{rows[-1]['date']} {n}거래일뿐입니다. 다른 기간에는 다른 결과가 나옵니다.",
            f"이 기간 지수의 20거래일 평균 수익률은 {baseline[20]:+.2f}% 였습니다. "
            f"상승장에서는 '떨어질 때 사는' 쪽이 불리하게 보이기 쉽습니다.",
            "상관관계이지 인과관계가 아닙니다. 개인이 사서 떨어진 게 아니라, "
            "떨어지는 국면에서 개인이 사는 쪽에 서는 것에 가깝습니다.",
            "순매수 총합은 0입니다. 개인이 외국인과 반대로 가는 것은 아이러니가 아니라 산수입니다.",
        ],
    }


# ---------------------------------------------------------------- 해석 레이어

def build_insights(flows: dict, market: dict, glob: dict, ant: dict,
                   futures: dict | None = None, credit: dict | None = None) -> list[dict]:
    """수치에서 바로 읽히는 사실만 문장으로. 예측이나 매매 조언은 하지 않는다."""
    tips: list[dict] = []
    ks = flows.get("markets", {}).get("KOSPI")
    if not ks:
        return tips
    latest, st = ks["latest"], ks["streaks"]

    def cho(v):
        return f"{v / 10000:.2f}조" if abs(v) >= 10000 else f"{abs(v):,.0f}억"

    for key, label in (("foreign", "외국인"), ("institution", "기관"), ("individual", "개인")):
        s = st[key]
        if s["days"] >= 3:
            verb = "순매수" if s["side"] == "buy" else "순매도"
            tips.append({
                "tone": "buy" if s["side"] == "buy" else "sell",
                "text": f"{label}이 {s['days']}거래일 연속 {verb} 중입니다. 누적 {cho(abs(s['total']))}원.",
            })

    f, i, o = latest.get("foreign") or 0, latest.get("individual") or 0, latest.get("institution") or 0
    if f < 0 and i > 0:
        tips.append({"tone": "neutral",
                     "text": f"오늘은 외국인이 판 물량({cho(abs(f))}원)을 개인이 받아내는 구도입니다."})
    elif f > 0 and i < 0:
        tips.append({"tone": "neutral",
                     "text": f"오늘은 외국인이 사고({cho(f)}원) 개인이 파는 구도입니다."})
    if i < 0 and f < 0 and o > 0:
        tips.append({"tone": "neutral",
                     "text": "개인과 외국인이 동시에 팔고 기관 홀로 받았습니다. 흔치 않은 조합입니다."})

    det = {k: latest.get(k) or 0 for k in ("inst_pension", "inst_trust", "inst_fin_inv")}
    top = max(det, key=lambda k: abs(det[k]))
    if abs(det[top]) > 1000:
        nm = {"inst_pension": "연기금", "inst_trust": "투신", "inst_fin_inv": "금융투자(증권)"}[top]
        tips.append({"tone": "buy" if det[top] > 0 else "sell",
                     "text": f"기관 안에서는 {nm}이 {cho(abs(det[top]))}원 "
                             f"{'순매수' if det[top] > 0 else '순매도'}로 가장 크게 움직였습니다."})

    kospi = market.get("indices", {}).get("KOSPI", {})
    if kospi.get("changeRate") is not None and abs(kospi["changeRate"]) >= 3:
        tips.append({"tone": "sell" if kospi["changeRate"] < 0 else "buy",
                     "text": f"코스피가 {kospi['changeRate']:+.2f}% 움직였습니다. "
                             f"변동성이 매우 큰 국면입니다."})

    g = {x["name"]: x for x in glob.get("items", [])}
    sox = g.get("필라델피아 반도체")
    if sox and sox.get("changeRate") is not None and abs(sox["changeRate"]) >= 2:
        tips.append({"tone": "sell" if sox["changeRate"] < 0 else "buy",
                     "text": f"간밤 필라델피아 반도체 지수가 {sox['changeRate']:+.2f}%. "
                             f"국내 반도체 대형주와 외국인 수급에 직결되는 지표입니다."})
    fx = g.get("원/달러 환율")
    if fx and fx.get("price"):
        note = "환율이 높을수록 외국인은 환차손 부담으로 순매도 쪽에 서기 쉽습니다." if fx["price"] >= 1400 else ""
        tips.append({"tone": "neutral", "text": f"원/달러 {fx['price']:,.1f}원. {note}".strip()})
    vix = g.get("VIX 공포지수")
    if vix and vix.get("price"):
        lvl = "공포 구간" if vix["price"] >= 30 else ("경계 구간" if vix["price"] >= 20 else "안정 구간")
        tips.append({"tone": "neutral", "text": f"VIX {vix['price']:.1f} — {lvl}입니다."})

    # 개미 관점 한 줄
    if ant:
        ia = ant["actors"]["individual"]
        pctl = ia["todayPercentile"]
        if pctl >= 80:
            tips.append({"tone": "sell", "text":
                f"오늘 개미의 매수 강도는 최근 {ant['sample']['days']}거래일 중 상위 {100 - pctl:.0f}% 수준입니다. "
                f"개미가 몰릴수록 뒤가 좋지 않았다는 것이 이 표본의 기록입니다."})
        elif pctl <= 20:
            tips.append({"tone": "buy", "text":
                f"오늘 개미는 최근 {ant['sample']['days']}거래일 중 하위 {pctl:.0f}% 수준으로 팔고 있습니다. "
                f"개미가 던지는 국면이 어떻게 끝났는지는 아래 성적표에 있습니다."})
        cr = ant.get("contrarianRead")
        if cr:
            tips.append({"tone": "neutral", "text":
                f"과거 {cr['label']} {cr['n']}번의 20거래일 뒤 지수는 평균 {cr['r20']:+.2f}% "
                f"(같은 기간 시장 평균 {cr['baseline20']:+.2f}%)였습니다. 예언이 아니라 기록입니다."})

    # 현물 ↔ 선물 다이버전스
    div = (futures or {}).get("divergence")
    if div and not div["aligned"]:
        spot_dir = "팔면서" if div["spotForeign"] < 0 else "사면서"
        fut_dir = "사고" if div["futuresForeign"] > 0 else "팔고"
        tips.append({"tone": "neutral", "text":
            f"최근 5거래일 외국인이 현물은 {cho(abs(div['spotForeign']))}원 {spot_dir} "
            f"선물은 {abs(div['futuresForeign']):,}계약 {fut_dir} 있습니다. "
            f"현물과 선물의 방향이 갈릴 때는 선물이 먼저 도는 경우가 많았습니다."})

    # 빚투·반대매매
    cl = (credit or {}).get("latest", {})
    if cl.get("loans"):
        ln = cl["loans"]
        if ln.get("d5") is not None and abs(ln["d5"]) >= 3000:
            verb = "늘었습니다" if ln["d5"] > 0 else "줄었습니다"
            tone = "sell" if ln["d5"] > 0 else "neutral"
            tips.append({"tone": tone, "text":
                f"신용융자(빚투) 잔고가 5거래일 만에 {cho(abs(ln['d5']))}원 {verb}. "
                f"현재 {cho(ln['total'])}원. "
                + ("빚으로 산 물량은 하락장에서 반대매매로 되돌아옵니다." if ln["d5"] > 0
                   else "레버리지가 정리되는 중입니다.")})
    if cl.get("money"):
        m = cl["money"]
        if m.get("liquidation") is not None and m.get("liqAvg20"):
            ratio = m["liquidation"] / m["liqAvg20"] if m["liqAvg20"] else 1
            if ratio >= 2:
                tips.append({"tone": "sell", "text":
                    f"반대매매가 {cho(m['liquidation'])}원 — 최근 20일 평균({cho(m['liqAvg20'])}원)의 "
                    f"{ratio:.1f}배입니다. 빚으로 버티던 계좌들이 강제 청산되고 있다는 신호입니다."})
    return tips


# ---------------------------------------------------------------- main

def market_phase(now: datetime) -> str:
    if now.weekday() >= 5:
        return "주말"
    hm = now.hour * 60 + now.minute
    if hm < 8 * 60 + 30:
        return "장전"
    if hm < 9 * 60:
        return "동시호가"
    if hm <= 15 * 60 + 30:
        return "장중"
    if hm <= 18 * 60:
        return "장마감(수급 확정 반영중)"
    return "장마감"


def main() -> int:
    now = datetime.now(KST)
    print(f"수집 시작 {now:%Y-%m-%d %H:%M:%S} KST  ({market_phase(now)})")

    print("[1/8] 투자자 수급")
    flows, full = build_flows()
    print("[2/8] 지수")
    market, closes = build_market()
    print("[3/8] 개미 성적표")
    ant = build_ant(full, closes, "KOSPI")
    if ant:
        a = ant["actors"]
        print(f"  표본 {ant['sample']['days']}일 · 개인↔외인 상관 {ant['correlation']['individual_foreign']:+.3f}"
              f" · 반대 비율 {ant['oppositeRate']['vsForeign']}%")
        for k in ACTOR_KEYS:
            print(f"    {k:12} 학점 {a[k]['grade']}  20일 초과수익 {a[k]['excess20']:+.2f}%p")
    print("[4/8] 선물 수급")
    futures = build_futures()
    attach_futures_divergence(futures, flows)
    print("[5/8] 신용융자·증시자금 (KOFIA)")
    credit = build_credit()
    print("[6/8] 종목·업종")
    stocks = build_stocks()
    print("[7/8] 글로벌 지표")
    glob = build_global()
    print("[8/8] 이벤트 일정 / 매크로")
    events, macro = build_events()
    glob["macro"] = macro

    insights = build_insights(flows, market, glob, ant, futures, credit)

    write("flows.json", flows)
    write("ant.json", ant)
    write("futures.json", futures)
    write("credit.json", credit)
    write("market.json", market)
    write("stocks.json", stocks)
    write("global.json", glob)
    write("events.json", events)
    write("insights.json", {"items": insights})
    write("meta.json", {
        "generatedAt": now.isoformat(),
        "generatedAtText": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "phase": market_phase(now),
        "warnings": WARNINGS,
        "sources": [
            {"name": "네이버 금융", "for": "개인/외국인/기관 수급, 지수, 종목, 업종"},
            {"name": "FinanceDataReader", "for": "코스피/코스닥 일봉"},
            {"name": "yfinance", "for": "미국 지수·선물·금리·환율·원자재"},
            {"name": "Federal Reserve", "for": "FOMC 일정"},
        ],
        "caveat": "장중 수급은 잠정치이며 장 마감 후 확정치로 정정됩니다.",
    })

    print(f"\n완료. 경고 {len(WARNINGS)}건")
    for w in WARNINGS:
        print(f"  - {w}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
