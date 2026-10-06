# -*- coding: utf-8 -*-
"""
한국 증시 수급 대시보드 - 데이터 수집기

공개 데이터만 사용해서 docs/data/*.json 을 생성한다.
브라우저는 CORS 때문에 이 소스들을 직접 못 부르므로, 수집은 여기(서버/Actions)서 하고
프론트엔드는 같은 오리진의 정적 JSON만 읽는다.

소스
  - 네이버 증권      : 개인/외국인/기관 수급(현물·선물), 지수 일봉·시세, 시총상위, 업종별 등락
  - 다음 금융        : 수급 예비 소스 (네이버가 실패할 때만)
  - FinanceDataReader: 지수 일봉 예비 소스
  - 금융투자협회     : 신용융자 잔고, 예탁금·반대매매
  - yfinance         : 미국 지수/선물/금리/환율/원자재
  - federalreserve.gov / FRED : FOMC·CPI 등 발표 일정, 매크로 지표 (+ events_seed.json)
  - 네이버 증권      : 프로그램 매매(차익·비차익)
  - 한국거래소(KRX)  : 공매도 거래·순보유잔고(시장 단위), 휴장일
  - 한국은행         : 금통위 통화정책방향 결정회의 날짜 (krx_calendar.json 에 해마다 적어 두고 실행 때 다시 확인)

소스가 실패하거나 멈추면 그 섹션은 직전 정상본을 유지하고(meta.json 의 stale/freshAt),
핵심인 코스피 수급·일봉이 갱신되지 않으면 exit 3 으로 끝나 워크플로가 실패로 표시된다.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

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

# 공개 소스에 보내는 요청 수. 하루 합계를 meta.json 에 이어 적고, 상한을 넘으면 무거운 섹션을 건너뛴다.
# (stock.naver.com 의 robots.txt 는 전면 금지다 — 개인용 갱신 주기를 지키고 요청을 아낀다)
REQUESTS = {"n": 0}
REQUEST_CAP = 3500
_session_request = session.request


def _counted_request(method, url, **kw):
    REQUESTS["n"] += 1
    return _session_request(method, url, **kw)


session.request = _counted_request

WARNINGS: list[str] = []

# 경고는 meta.json 으로 공개 저장소에 커밋되고 화면에도 찍힌다.
# Actions 의 시크릿 마스킹은 로그에만 적용되므로, 키가 섞인 오류 메시지는 여기서 가린다.
SECRETS: set[str] = set()
_KEY_PARAM = re.compile(r"((?:api_?key|serviceKey|token)=)[^&\s'\"]+", re.I)


def scrub(text) -> str:
    s = _KEY_PARAM.sub(r"\1***", str(text))
    for secret in SECRETS:
        s = s.replace(secret, "***")
    return s


def warn(msg: str) -> None:
    msg = scrub(msg)
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
    raise RuntimeError(scrub(f"{url} -> {last}"))


def write(name: str, payload, compact: bool = False) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, **({"separators": (",", ":")} if compact else {"indent": 1})),
        encoding="utf-8",
    )
    print(f"  -> {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


# ---------------------------------------------------------------- 직전 정상본 유지
# 소스 하나가 죽었다고 마지막 정상 데이터를 빈 값으로 덮어쓰면, 공개 페이지가 빈칸과 '+0억'이 된다.
# 섹션마다 '마지막으로 정상 수집된 시각'을 meta.json 에 이어서 기록하고,
# 이번 수집이 비었으면 직전 정상본을 내보내되 화면에 그 사실을 알린다.

FRESH_AT: dict[str, str] = {}   # 섹션 키 -> 마지막 정상 수집 시각(ISO)
STALE: list[dict] = []          # 이번 실행에서 갱신되지 못한 섹션 (화면 맨 위 '지난 데이터' 안내)


def mark_stale(key: str, label: str, *, asOf: str | None = None, since: str | None = None) -> None:
    """같은 섹션은 한 번만 — 나중 판단(실제로 내보낸 데이터 기준)이 앞의 것을 덮는다."""
    for s in STALE:
        if s["key"] == key:
            s.update(label=label, asOf=asOf, since=since)
            return
    STALE.append({"key": key, "label": label, "asOf": asOf, "since": since})


def load_prev(name: str):
    """docs/data 에 이미 있는(직전 실행이 쓴) 파일. 없거나 깨졌으면 None."""
    try:
        return json.loads((OUT / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _last_date(fn):
    """payload 의 마지막 데이터 날짜를 꺼내는 함수. 형식이 어긋나면 ''."""
    def get(x):
        try:
            return fn(x) or ""
        except Exception:  # noqa: BLE001
            return ""
    return get


def keep_good(key: str, label: str, new, prev, ok, now_iso: str,
              stale_keys: set[str] = frozenset(), last=None, worse=None):
    """
    new 가 정상이면 그대로, 비었으면 직전 정상본(prev)을 돌려준다.
    비지 않았어도 멈춘 데이터(stale_keys)라면 '정상 수집'으로 치지 않고,
    직전 정상본이 더 최신이면 그쪽을 내보낸다 (예: 네이버 일봉 실패 → 멈춰 있는 FDR 로 떨어졌을 때).
    """
    since = FRESH_AT.get(key)
    if ok(new) and worse and prev is not None and ok(prev) and worse(new, prev):
        # 계산 기준이 직전본보다 나빠졌다(예: 거래대금이 없어 금액 기준으로 떨어짐) — 직전본을 쓴다
        warn(f"{label}: 이번 계산은 기준이 낮아져 직전 데이터({last(prev) if last else '직전 수집'})를 유지합니다")
        mark_stale(key, label, asOf=last(prev) if last else None, since=since)
        return prev
    if ok(new):
        if key not in stale_keys:
            FRESH_AT[key] = now_iso
            return new
        if last and prev is not None and ok(prev) and last(prev) > last(new):
            warn(f"{label}: 이번 데이터가 {last(new)}에서 멈춰 있어 더 최신인 직전 데이터({last(prev)}까지)를 유지합니다")
            mark_stale(key, label, asOf=last(prev), since=since)
            return prev
        return new
    if prev is not None and ok(prev):
        when = f" (마지막 정상 수집 {since[:16].replace('T', ' ')})" if since else ""
        warn(f"{label}: 이번 수집이 비어 직전 데이터를 유지합니다{when}")
        mark_stale(key, label, asOf=last(prev) if last else None, since=since)
        return prev
    return new


def apply_last_good(now_iso: str, out: dict, stale_keys: set[str] = frozenset()) -> dict:
    """out: {파일명: 이번 수집 결과}. 비었거나 멈춘 섹션만 직전 정상본으로 갈아 끼워 돌려준다."""
    FRESH_AT.update((load_prev("meta.json") or {}).get("freshAt") or {})

    def keep(key, label, new, prev, ok=bool, last=None, worse=None):
        return keep_good(key, label, new, prev, ok, now_iso, stale_keys, last, worse)

    # 강도 기준(거래대금 대비) 직전본이 있는데 이번엔 금액 기준으로 떨어졌으면 직전본이 낫다
    def degraded(new, prev):
        return new.get("basis") == "amount" and prev.get("basis") == "intensity"

    rows_last = _last_date(lambda xs: xs[-1]["date"])

    flows = out["flows.json"]
    prev = load_prev("flows.json") or {}
    for code, name in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        got = keep(f"flows.{code}", f"{name} 투자자 수급", flows["markets"].get(code),
                   (prev.get("markets") or {}).get(code), lambda m: bool(m and m.get("daily")),
                   _last_date(lambda m: m["daily"][-1]["date"]))
        if got:
            flows["markets"][code] = got

    out["futures.json"] = keep("futures", "선물 수급", out["futures.json"], load_prev("futures.json"),
                               lambda f: bool(f and f.get("daily")),
                               _last_date(lambda f: f["daily"][-1]["date"]))
    out["ant.json"] = keep("ant", "개미 성적표", out["ant.json"], load_prev("ant.json"),
                           last=_last_date(lambda a: a["sample"]["to"]), worse=degraded)
    out["analog.json"] = keep("analog", "유사 국면", out["analog.json"], load_prev("analog.json"),
                              last=_last_date(lambda a: a["today"]["date"]), worse=degraded)
    out["antstocks.json"] = keep("antstocks", "장바구니 비교", out["antstocks.json"],
                                 load_prev("antstocks.json"), last=_last_date(lambda a: f"{a['window']['to'][:4]}-{a['window']['to'][4:6]}-{a['window']['to'][6:]}"))

    credit = out["credit.json"]
    prev = load_prev("credit.json") or {}
    for part, label in (("loans", "신용융자 잔고"), ("money", "증시자금·반대매매")):
        got = keep(f"credit.{part}", label, credit.get(part), prev.get(part), last=rows_last)
        if got is not credit.get(part):
            credit[part] = got
            if (prev.get("latest") or {}).get(part):
                credit.setdefault("latest", {})[part] = prev["latest"][part]

    market = out["market.json"]
    prev = (load_prev("market.json") or {}).get("indices") or {}
    for code, e in market["indices"].items():
        p = prev.get(code) or {}
        hist = keep(f"market.{code}.history", f"{e.get('name', code)} 일봉", e.get("history"), p.get("history"),
                    last=rows_last)
        if hist is not e.get("history"):
            e["history"] = hist
        price = keep(f"market.{code}.price", f"{e.get('name', code)} 현재가", e.get("price"), p.get("price"),
                     lambda v: v is not None)
        if price is not e.get("price"):
            for k in ("price", "change", "changeRate", "marketStatus", "tradedAt"):
                e[k] = p.get(k)
        if e.get("price") is None and e.get("history"):       # 처음 실행인데 시세 API 가 죽었을 때만
            e["price"] = e["history"][-1]["close"]

    stocks = out["stocks.json"]
    prev = load_prev("stocks.json") or {}
    stocks["top"] = keep("stocks.top", "시총 상위 종목", stocks.get("top"), prev.get("top"))
    if "universe" in stocks or prev.get("universe"):
        stocks["universe"] = keep("stocks.universe", "350종목 수급 요약", stocks.get("universe"), prev.get("universe"),
                                  last=_last_date(lambda u: max(x.get("asOf") or "" for x in u)))
        if stocks.get("universe") and not stocks.get("concentration"):
            stocks["concentration"] = concentration(stocks["universe"])
    if "stockflows.json" in out:
        out["stockflows.json"] = keep("stockflows", "종목 60일 수급", out["stockflows.json"],
                                      load_prev("stockflows.json"), lambda x: bool(x and x.get("stocks")))
    stocks["industries"] = keep("stocks.industries", "업종 등락", stocks.get("industries"),
                                prev.get("industries"))

    # 글로벌은 종목마다 asOf 가 붙어 있으니 빠진 것만 직전 값으로 채운다
    glob = out["global.json"]
    prev = load_prev("global.json") or {}
    have = {x["symbol"] for x in glob.get("items", [])}
    order = {t[0]: n for n, t in enumerate(GLOBAL_TICKERS)}
    kept = [x for x in prev.get("items", []) if x.get("symbol") in order and x["symbol"] not in have]
    if kept:
        warn(f"글로벌 지표 {', '.join(x['name'] for x in kept)}: 이번 수집이 비어 직전 값을 유지합니다")
        glob["items"] = sorted(glob.get("items", []) + kept, key=lambda x: order[x["symbol"]])
    if glob.get("macro") is None:          # FRED 키를 일부러 안 쓴 경우 — 실패가 아니므로 직전 값도 쓰지 않는다
        glob["macro"] = []
        FRESH_AT.pop("global.macro", None)
    else:
        glob["macro"] = keep("global.macro", "미국 매크로 지표", glob["macro"], prev.get("macro"))

    if "ranks.json" in out:
        out["ranks.json"] = keep("ranks", "외국인·기관 종목 순위", out["ranks.json"], load_prev("ranks.json"),
                                 lambda r: bool(r and r.get("markets")), _last_date(lambda r: r["asOf"]))
        if out["ranks.json"] is None:
            del out["ranks.json"]

    if "program.json" in out:
        prog = out["program.json"]
        prev = load_prev("program.json") or {}
        for code, name in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            got = keep(f"program.{code}", f"{name} 프로그램 매매", prog["markets"].get(code),
                       (prev.get("markets") or {}).get(code), lambda m: bool(m and m.get("daily")),
                       _last_date(lambda m: m["daily"][-1]["date"]))
            if got:
                prog["markets"][code] = got

    if "short.json" in out:
        # build_short 가 직전 행을 이미 이어 붙인다. 여기서는 실패를 화면 안내로 올리기만 한다
        sh = out["short.json"]
        failed = sh.get("failed", [])
        for code, name in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            m = sh["markets"].get(code) or {}
            bad = False
            for part, key, label in (("daily", f"short.{code}", f"{name} 공매도"),
                                     ("balance", f"short.{code}.balance", f"{name} 공매도 잔고")):
                if f"{code}.{part}" in failed and m.get(part):
                    mark_stale(key, label, asOf=m[part][-1]["date"], since=FRESH_AT.get(f"short.{code}"))
                    bad = True
            if m.get("daily") and not bad and not stale_keys & {f"short.{code}", f"short.{code}.balance"} \
                    and krx_short_due(datetime.fromisoformat(now_iso)):
                FRESH_AT[f"short.{code}"] = now_iso

    events = out["events.json"]
    ev = keep("events", "이벤트 일정", events, load_prev("events.json"),
              lambda e: bool(e and e.get("upcoming")))
    if ev is not events:
        today = datetime.now(KST).date()
        for e in ev["upcoming"]:
            e["dday"] = (date.fromisoformat(e["date"]) - today).days
        ev["upcoming"] = [e for e in ev["upcoming"] if e["dday"] >= 0]
        ev["today"] = today.isoformat()
        out["events.json"] = ev
    return out


# ---------------------------------------------------------------- 수급 (핵심)

ACTOR_KEYS = ["individual", "foreign", "institution"]
MARKET_NAME = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "FUT": "코스피200 선물"}

# 네이버 Npay 증권 '투자자별 매매동향' 페이지가 쓰는 공개 JSON.
# 옛 finance.naver.com/sise/investorDealTrendDay.naver 는 2026-09-17 폐지(HTTP 410)됐고, 이게 그 후속이다.
# 옛 표와 153거래일 × 10필드(선물 94거래일)가 전부 일치함을 확인했다. 시장당 200행씩, 750거래일이면 요청 4번.
NAVER_TREND_URL = "https://stock.naver.com/api/domestic/market/trend/daily"
NAVER_TREND_REFERER = "https://stock.naver.com/market/stock/kr/trend/trader"

# investorGubun 코드 -> 필드. 현물은 원 단위 금액, 선물은 계약 수.
INVESTOR_GROUPS = {
    "individual":     ("8000",),                         # 개인
    "foreign":        ("9000", "9001"),                  # 외국인 + 기타외국인
    "institution":    ("1000", "2000", "3000", "3100", "4000", "5000", "6000", "7000"),   # 기관계
    "inst_fin_inv":   ("1000",),                         # 금융투자
    "inst_insurance": ("2000",),                         # 보험
    "inst_trust":     ("3000", "3100"),                  # 투신 + 사모
    "inst_bank":      ("4000",),                         # 은행
    "inst_other_fin": ("5000",),                         # 기타금융
    "inst_pension":   ("6000", "7000"),                  # 연기금 + 국가·지자체
    "other_corp":     ("7100",),                         # 기타법인
}
_REQUIRED_CODES = {"1000", "2000", "3000", "4000", "5000", "6000", "7000", "7100", "8000", "9000"}


def fetch_naver_trend(market: str, days: int, page_size: int = 200) -> list[dict]:
    """
    market: 'KOSPI' | 'KOSDAQ' | 'FUT'(코스피200 선물). 날짜 오름차순, 최근 days거래일.
    현물은 억원(원 단위 합계를 반올림), 선물은 계약. 장중이면 당일 잠정치가 들어올 수 있다.
    """
    # 모르는 marketType 을 주면 서버가 오류 없이 '코스피+코스닥 합계'를 돌려준다 — 반드시 셋 중 하나
    if market not in MARKET_NAME:
        raise ValueError(market)
    by_date: dict[str, dict] = {}
    skipped = 0
    for page in range(days // page_size + 2):      # startIdx 는 오프셋이 아니라 페이지 번호
        if len(by_date) >= days:
            break
        try:
            j = get_json(NAVER_TREND_URL, headers={"Referer": NAVER_TREND_REFERER},
                         params={"tradeType": "KRX", "marketType": market, "startIdx": page, "pageSize": page_size})
        except Exception as e:  # noqa: BLE001
            if not by_date:
                raise
            warn(f"{MARKET_NAME[market]} 수급: 네이버 {page + 1}번째 페이지 실패 — 앞서 받은 {len(by_date)}일만 씀 ({e})")
            break
        content = j.get("content") if isinstance(j, dict) else None
        if not isinstance(content, list):
            raise RuntimeError(f"응답 형식이 바뀜: {str(j)[:120]}")
        for c in content:
            d = str(c.get("bizdate") or "")
            if not re.fullmatch(r"\d{8}", d) or d in by_date:
                continue
            amt: dict[str, int] = {}
            traded = 0                                # 거래대금 = 모든 투자자의 매수금액 합(= 매도금액 합)
            for n in c.get("netAmounts") or []:
                try:
                    amt[str(n["investorGubun"])] = int(str(n["diffValue"]).replace(",", ""))
                    if str(n["investorGubun"]) != "9999":
                        traded += int(str(n.get("buyPrice") or 0).replace(",", ""))
                except (KeyError, TypeError, ValueError):
                    pass
            if market == "FUT" and amt.keys() & {"3100", "9001"}:
                raise RuntimeError("선물을 요청했는데 현물 응답이 왔음")
            if not _REQUIRED_CODES <= amt.keys():
                skipped += 1
                continue
            row = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}"}
            for key, codes in INVESTOR_GROUPS.items():
                v = sum(amt.get(code, 0) for code in codes)
                row[key] = float(v) if market == "FUT" else float(round(v / 1e8))
            if market != "FUT" and traded > 0:
                # 순매수 강도(거래대금 대비 순매수)의 분모. 시장 규모가 3년 새 2~3배 커져 금액끼리는 비교가 안 된다
                row["tradingValue"] = float(round(traded / 1e8))
            by_date[d] = row
        if not content or str(j.get("last")).lower() == "true":
            break
        time.sleep(0.3)
    if skipped:
        warn(f"{MARKET_NAME[market]} 수급: 투자자 구분이 빠진 {skipped}행을 건너뜀")
    rows = sorted(by_date.values(), key=lambda r_: r_["date"])
    return rows[-days:]


# 예비 소스: 다음 금융. Referer 가 finance.daum.net 아래가 아니면 403 이다.
DAUM = "https://finance.daum.net"
_DAUM_SPOT_TOP = {
    "individual":  "individualStraightPurchasePrice",
    "foreign":     "foreignStraightPurchasePrice",
    "institution": "institutionStraightPurchasePrice",
}
_DAUM_SPOT_DETAIL = {
    "inst_fin_inv":   ("FINANCIAL_INVESTOR",),
    "inst_insurance": ("INSURANCE_COMPANIES",),
    "inst_trust":     ("MUTUAL_FUND", "PRIVATE_EQUITY_FUND"),
    "inst_bank":      ("BANK",),
    "inst_other_fin": ("ETC_FINANCIAL_INSTITUTION",),
    "inst_pension":   ("PENSION_FUND",),
    "other_corp":     ("ETC_CORPORATION",),
}
_DAUM_FUT = {
    "individual": "privateSettlement", "foreign": "foreignSettlement",
    "institution": "institutionalSettlement", "inst_fin_inv": "financialInvestment",
    "inst_insurance": "insuranceInvestment", "inst_trust": "trustInvestment",
    "inst_bank": "bankInvestment", "inst_other_fin": "etcInvestment",
    "inst_pension": "pensionFundInvestment", "other_corp": "etcCorporationSettlement",
}


def fetch_daum_flows(market: str, days: int) -> list[dict]:
    """네이버와 같은 형식(현물 억원, 선물 계약). 한 번 요청에 days+10 행."""
    if market == "FUT":
        path, ref = "/api/investor/future/days", f"{DAUM}/domestic/investors/DERIVATIVES"
        params = {"terms": "days", "type": "VOLUME"}          # terms 가 없으면 500
    else:
        path, ref = f"/api/investor/{market}/days", f"{DAUM}/domestic/investors/{market}"
        params = {"details": "true"}                          # 없으면 기관 세부가 빠진다
    j = get_json(DAUM + path, headers={"Referer": ref}, params={**params, "page": 1, "perPage": days + 10})
    if j.get("code") != 200 or not isinstance(j.get("data"), list):
        raise RuntimeError(f"code={j.get('code')} {j.get('message')}")

    by_date: dict[str, dict] = {}
    for x in j["data"]:
        d = str(x.get("date") or "")[:10]
        try:
            if date.fromisoformat(d).weekday() >= 5:          # 2024-06-16(일) 같은 가짜 주말 행이 있다
                continue
            if market == "FUT":
                row = {k: float(x[src]) for k, src in _DAUM_FUT.items()}
            else:
                det = x.get("details") or {}
                v = {k: float(x[src]) for k, src in _DAUM_SPOT_TOP.items()}
                # 다음의 '외국인'은 2024-06-14까지 기타외국인을 뺀 값이다. 네 주체 합이 0이 아니고
                # 기타외국인을 더하면 0이 되는 날만 더해 네이버(기타외국인 포함)와 정의를 맞춘다.
                ef, corp = float(det.get("ETC_FOREIGN") or 0.0), float(det["ETC_CORPORATION"])
                r4 = v["individual"] + v["foreign"] + v["institution"] + corp
                if abs(r4) > 1e6 and abs(r4 + ef) <= 1e6:
                    v["foreign"] += ef
                row = {k: float(round(v[k] / 1e8)) for k in v}
                for k, srcs in _DAUM_SPOT_DETAIL.items():
                    row[k] = float(round(sum(float(det[s]) for s in srcs) / 1e8))
        except (KeyError, TypeError, ValueError):
            continue
        by_date.setdefault(d, {"date": d, **row})
    rows = sorted(by_date.values(), key=lambda r_: r_["date"])
    return rows[-days:]


def _merge_by_date(*sources: list[dict]) -> list[dict]:
    """날짜별로 합친다. 뒤에 온 소스가 같은 날짜를 덮는다."""
    by: dict[str, dict] = {}
    for rows in sources:
        for r in rows:
            by[r["date"]] = {k: v for k, v in r.items() if k != "cum"}
    return sorted(by.values(), key=lambda r_: r_["date"])


# ---------------------------------------------------------------- 국내 장 일정 (휴장일·특별 장 시간·금통위)
# collector/krx_calendar.json 에 해마다 적어 두고(수능일 장 시간도 여기), 실행 때 KRX·한국은행에서 다시 받아
# docs/data/calendar.json 에 이어 둔다. 받지 못해도 적어 둔 목록으로 돈다.

CAL_FILE = Path(__file__).resolve().parent / "krx_calendar.json"
HOLIDAYS: dict[str, str] = {}                        # 'YYYY-MM-DD' -> 휴일 이름 (평일 휴장일만)
MPC: dict[str, list[str]] = {}                       # 'YYYY' -> 금통위 통화정책방향 결정회의 날짜
SPECIAL_SESSIONS: dict[str, tuple[str, str]] = {}    # 정규장이 평소(09:00~15:30)와 다른 날 -> (시작, 끝) 'HHMM'
CAL_SEED: dict = {}


def _load_calendar_seed() -> None:
    try:
        cal = json.loads(CAL_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        warn(f"{CAL_FILE.name} 를 읽지 못해 직전 실행이 받아 둔 일정만 씁니다: {type(e).__name__}: {e}")
        return
    CAL_SEED.update(cal)
    HOLIDAYS.update(cal.get("holidays") or {})
    MPC.update({str(y): sorted(v) for y, v in (cal.get("mpc") or {}).items()})
    SPECIAL_SESSIONS.update({d: tuple(v) for d, v in (cal.get("special_sessions") or {}).items()})


_load_calendar_seed()

REF_FILE = Path(__file__).resolve().parent / "reference.json"
try:
    REF: dict = json.loads(REF_FILE.read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    REF = {}


def streak_rarity(code: str, key: str, st: dict) -> dict:
    """지금 연속 기록이 2009년 이후 같은 방향 연속 구간 중 몇 %만 도달한 길이인지(코스피만 기준표가 있다)."""
    t = ((REF.get("streaks") or {}).get(code) or {}).get(key) or {}
    if not t or not st.get("days") or st.get("side") not in ("buy", "sell"):
        return {"since": "2009-03", "pctRuns": None, "longest": None}
    reach = (t.get(st["side"]) or {}).get("reach") or []
    L = st["days"]
    pct = reach[L - 1] if 0 < L <= len(reach) else 0.0
    return {"since": (REF.get("period") or {}).get("from", "2009-03")[:7], "pctRuns": pct,
            "longest": {side: (t.get(side) or {}).get("longest") for side in ("buy", "sell")}}

FINAL_AFTER = "200000"    # KRX 투자자별 값은 20시 전후까지 바뀐다 — 그 뒤 값만 '확정'으로 부른다


def session_hours(day: str) -> tuple[str, str]:
    """그날 정규장 (시작, 끝) 'HHMM'. 새해 첫 거래일은 해마다 10시에 연다(마감은 평소대로)."""
    if day in SPECIAL_SESSIONS:
        return SPECIAL_SESSIONS[day]
    if day[5:7] == "01" and day == first_trading_day(int(day[:4])).isoformat():
        return ("1000", "1530")
    return ("0900", "1530")


NAVER_TREND_TIME_URL = "https://stock.naver.com/api/domestic/market/trend/time"
INTRADAY_KEYS = ("individual", "foreign", "institution", "other_corp")
INTRADAY_STEP = 5          # 분. 화면에는 5분 간격이면 충분하다(하루 약 80점)


def fetch_intraday(market: str) -> dict | None:
    """
    가장 최근 거래일의 분 단위 투자자별 '누적' 순매수(억원). 장중이면 지금까지, 장 마감 뒤면 하루 전체.
    정규장은 5분 간격으로 줄인다. 종가 단일가 체결분은 마감 다음 분(15:31) 행에 찍히므로 마감 점에 반영한다.
    마감 뒤 값은 20시 전이면 after(아직 바뀌는 중), 20시 뒤면 final(확정)로 따로 싣는다.
    """
    if market not in ("KOSPI", "KOSDAQ"):
        raise ValueError(market)
    by_time: dict[str, dict] = {}
    day = None
    for page in range(5):                                   # 하루 약 440행 = 200행씩 3페이지
        j = get_json(NAVER_TREND_TIME_URL, headers={"Referer": NAVER_TREND_REFERER},
                     params={"tradeType": "KRX", "marketType": market, "startIdx": page, "pageSize": 200})
        content = j.get("content") if isinstance(j, dict) else None
        if not isinstance(content, list):
            raise RuntimeError(f"응답 형식이 바뀜: {str(j)[:120]}")
        for c in content:
            d, t = str(c.get("bizdate") or ""), str(c.get("time") or "")
            if not re.fullmatch(r"\d{8}", d) or not re.fullmatch(r"\d{6}", t):
                continue
            day = day or d
            if d != day:                                    # 가장 최근 하루만
                continue
            amt = {}
            for n in c.get("netAmounts") or []:
                try:
                    amt[str(n["investorGubun"])] = int(str(n["diffValue"]).replace(",", ""))
                except (KeyError, TypeError, ValueError):
                    pass
            if not _REQUIRED_CODES <= amt.keys():
                continue
            by_time[t] = {k: float(round(sum(amt.get(code, 0) for code in INVESTOR_GROUPS[k]) / 1e8))
                          for k in INTRADAY_KEYS}
        if not content or str(j.get("last")).lower() == "true":
            break
        time.sleep(0.3)
    if not by_time:
        return None
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    open_, close = session_hours(iso)
    times = sorted(by_time)
    session = [t for t in times if open_ + "00" <= t <= close + "00"]
    # 분 단위 기록에 빈 분이 있어, 5분 구간마다 그 구간의 마지막 값을 쓴다(지금까지의 마지막 값도 자연히 들어간다)
    buckets: dict[int, str] = {}
    for t in session:
        buckets[(int(t[:2]) * 60 + int(t[2:4]) - 1) // INTRADAY_STEP] = t
    hm = lambda t: f"{t[:2]}:{t[2:4]}"
    points = [{"t": hm(buckets[b]), **by_time[buckets[b]]} for b in sorted(buckets)]
    # 종가 단일가(마감 직전 10분 동시호가) 체결 결과는 마감 다음 분 행에 찍힌다 — 마감 점을 그 값으로
    end_m = int(close[:2]) * 60 + int(close[2:])
    after_close = [t for t in times if close + "00" < t < f"{(end_m + 10) // 60:02d}{(end_m + 10) % 60:02d}00"]
    if after_close:
        settled = {"t": hm(close + "00"), **by_time[after_close[0]]}
        if points and points[-1]["t"] == settled["t"]:
            points[-1] = settled
        else:
            points.append(settled)
    last = times[-1]
    tail = {"t": hm(last), **by_time[last]} if last > close + "00" else None
    return {
        "date": iso,
        "open": hm(open_ + "00"), "close": hm(close + "00"),
        "points": points,
        "final": tail if tail and last >= FINAL_AFTER else None,
        "after": tail if tail and last < FINAL_AFTER else None,
    }


def fetch_flows(market: str, days: int, prev_rows: list[dict] | None = None) -> list[dict]:
    """
    주 소스(네이버)가 실패하거나 비면 예비 소스(다음)로. 둘 다 안 되면 빈 리스트.
    다음 금융은 2026-09-11 까지는 네이버와 ±1억 안에서 같지만, 그 뒤 날짜는 수천억씩(부호까지) 다른 날이 있다.
    그래서 다음 행은 네이버에 없는 날짜만 채운다:
      - 네이버 과거 페이지만 실패했으면 → 네이버 행보다 오래된 날짜만 다음으로 보충
      - 네이버가 통째로 실패했으면 → 직전 정상본(prev_rows)에 있는 날짜는 직전 값을 우선
    """
    name = MARKET_NAME[market]
    rows: list[dict] = []
    try:
        rows = fetch_naver_trend(market, days)
        if not rows:
            warn(f"{name} 수급: 네이버 응답이 비어 있음")
    except Exception as e:  # noqa: BLE001
        warn(f"{name} 수급: 네이버 실패 — {type(e).__name__}: {e}")
    if len(rows) >= days:
        return rows
    try:
        daum = fetch_daum_flows(market, days)
    except Exception as e:  # noqa: BLE001
        if not rows:
            warn(f"{name} 수급: 다음 금융도 실패 — {type(e).__name__}: {e}")
        return rows
    if rows:
        older = [r for r in daum if r["date"] < rows[0]["date"]]
        if older:
            warn(f"{name} 수급: {rows[0]['date']} 이전 {len(older)}일은 예비 소스(다음 금융)로 채웠습니다")
        return (older + rows)[-days:]
    if daum:
        warn(f"{name} 수급은 예비 소스(다음 금융)로 받았습니다. "
             "직전 데이터에 없던 최근 날짜는 주 소스 확정치와 다를 수 있습니다")
    return _merge_by_date(daum, prev_rows or [])[-days:] if daum else []


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
    out: dict = {"unit": "억원", "chartDays": CHART_DAYS, "markets": {},
                 "streakBaseline": REF.get("streakBaseline")}
    full: dict = {}
    prev = (load_prev("flows.json") or {}).get("markets") or {}
    for code in ("KOSPI", "KOSDAQ"):
        try:
            rows = fetch_flows(code, STAT_DAYS, (prev.get(code) or {}).get("daily"))
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
                "streaks": {k: {**streak(rows, k), "rarity": streak_rarity(code, k, streak(rows, k))}
                            for k in ("individual", "foreign", "institution", "other_corp")},
            }
            print(f"  {code} 수급 {len(rows)}일 ({rows[0]['date']} ~ {rows[-1]['date']})")
            try:
                intra = fetch_intraday(code)
                if intra:
                    out["markets"][code]["intraday"] = intra
                    print(f"  {code} 장중 흐름 {len(intra['points'])}점 ({intra['date']})")
            except Exception as e:  # noqa: BLE001
                warn(f"{code} 장중 수급 흐름 실패: {type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            warn(f"{code} 수급 수집 실패: {type(e).__name__}: {e}")
    return out, full


# ---------------------------------------------------------------- 선물 (외국인의 선행 포지션)

FUT_CHART_DAYS = 60    # 화면에 싣는 일수. 통계(갈림 기록)는 STAT_DAYS 전체로 낸다


def build_futures() -> tuple[dict, list[dict]]:
    """
    코스피200 선물 투자자별 순매수 (단위: 계약). (화면용 futures, 통계용 전체 히스토리) 를 돌려준다.
    외국인의 현물 매매와 선물 포지션이 같은 방향인지 본다.
    """
    out: dict = {"unit": "계약", "daily": [], "latest": None, "streaks": {}, "divergence": None}
    rows: list[dict] = []
    try:
        rows = fetch_flows("FUT", STAT_DAYS, (load_prev("futures.json") or {}).get("daily"))
        if not rows:
            warn("선물 수급 데이터가 비어 있음")
            return out, []
        out["daily"] = rows[-FUT_CHART_DAYS:]
        out["latest"] = rows[-1]
        out["streaks"] = {k: streak(rows, k) for k in ACTOR_KEYS}
        print(f"  선물 수급 {len(rows)}일 (최근 {rows[-1]['date']})")
    except Exception as e:  # noqa: BLE001
        warn(f"선물 수급 수집 실패: {type(e).__name__}: {e}")
    return out, rows


DIV_WINDOW = 5
DIV_TYPICAL_DAYS = 120   # '평소 크기' = 직전 120거래일 5일 합계 절댓값의 중앙값 (3년치로 잡으면 거래 규모 증가에 끌려간다)


def attach_futures_divergence(futures: dict, full: dict, fut_full: list[dict], closes: dict) -> None:
    """
    외국인의 최근 5거래일 현물 순매수(억원)와 선물 순매수(계약) 방향을 비교한다.
    - 같은 날짜끼리만 더한다 (현물·선물 표의 마지막 5행이 서로 다른 날일 수 있다)
    - 부호만 보면 선물 +44계약 같은 잡음도 '갈림'이 된다. 그래서 둘 다 평소 크기
      (직전 120거래일 5일 합계 절댓값의 중앙값) 이상일 때만 갈림/일치로 보고, 아니면 '뚜렷하지 않음'
    - '선물이 먼저 돈다'는 주장 대신, 과거 같은 모양으로 갈렸던 날 이후 20거래일 코스피 기록을 붙인다
    """
    try:
        spot = {r["date"]: r.get("foreign") or 0.0 for r in full.get("KOSPI") or []}
        fut = {r["date"]: r.get("foreign") or 0.0 for r in fut_full}
        dates = sorted(set(spot) & set(fut))
        if len(dates) < 60:
            return
        w = DIV_WINDOW
        d5 = dates[w - 1:]
        s5 = [sum(spot[d] for d in dates[i - w + 1: i + 1]) for i in range(w - 1, len(dates))]
        f5 = [sum(fut[d] for d in dates[i - w + 1: i + 1]) for i in range(w - 1, len(dates))]
        def typical(xs, i):
            """i 번째 날 직전 DIV_TYPICAL_DAYS 일의 5일 합계 절댓값 중앙값. 그날 이후 정보는 쓰지 않는다."""
            win = sorted(abs(x) for x in xs[max(0, i - DIV_TYPICAL_DAYS): i])
            return win[len(win) // 2] if len(win) >= DIV_TYPICAL_DAYS // 2 else None

        def state(i):
            st, ft = typical(s5, i), typical(f5, i)
            if st is None or ft is None:
                return None
            if abs(s5[i]) < st or abs(f5[i]) < ft:
                return "weak"
            return "aligned" if (s5[i] >= 0) == (f5[i] >= 0) else "split"

        last = len(d5) - 1
        s_typ, f_typ = typical(s5, last), typical(f5, last)
        now = state(last)
        if now is None:
            return
        history = None
        if now == "split":
            px = closes.get("KOSPI") or {}
            pdates = sorted(px)
            pos = {d: k for k, d in enumerate(pdates)}

            def fwd20(d):
                k = pos.get(d)
                return (px[pdates[k + 20]] / px[d] - 1) * 100 if k is not None and k + 20 < len(pdates) else None

            same = [i for i in range(last) if state(i) == "split" and (s5[i] >= 0) == (s5[-1] >= 0)]
            outs = [(i, fwd20(d5[i])) for i in same]
            outs = [(i, x) for i, x in outs if x is not None]
            every = [x for x in (fwd20(d) for d in d5) if x is not None]
            if outs and every:
                episodes = 1 + sum(1 for (a, _), (b, _) in zip(outs, outs[1:]) if b - a >= 20)
                history = {
                    "n": len(outs),
                    "episodes": episodes,
                    "r20": round(sum(x for _, x in outs) / len(outs), 2),
                    "baseline20": round(sum(every) / len(every), 2),
                    "from": d5[0],
                }
        futures["divergence"] = {
            "window": w,
            "from": dates[-w], "to": dates[-1],
            "spotForeign": round(s5[-1], 1),
            "futuresForeign": round(f5[-1]),
            "spotTypical": round(s_typ, 1),
            "futuresTypical": round(f_typ),
            "typicalDays": DIV_TYPICAL_DAYS,
            "state": now,
            "aligned": now != "split",
            "history": history,
        }
    except Exception as e:  # noqa: BLE001
        warn(f"현·선물 비교 실패: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 프로그램 매매 (시장 전체)
# 네이버 '프로그램 매매동향' 화면(stock.naver.com/market/stock/kr/trend/program)이 쓰는 공개 JSON.
# bizdate 는 필수지만 서버가 무시하고 늘 최신 거래일부터 내림차순으로 준다. startIdx 는 페이지 번호, pageSize ≤ 200.
# 값은 '원' 단위 정수 문자열. 2023-06~2026-10 800거래일 × 두 시장 전부 차익+비차익=전체, 매수−매도=순매수가 원 단위까지
# 맞았고, 다음 금융과 550일 중 549일 일치했다. 휴장일에는 행이 없다.

NAVER_PROGRAM_URL = "https://stock.naver.com/api/domestic/market/trendProgram"
NAVER_PROGRAM_REFERER = "https://stock.naver.com/market/stock/kr/trend/program"
_PROGRAM_KINDS = {"diff": "arb", "biDiff": "nonarb", "totalDiff": "total"}     # 차익·비차익·전체
_PROGRAM_SIDES = {"BuyAmt": "buy", "SellAmt": "sell", "PureBuyAmt": "net"}
PROGRAM_FINAL_AFTER = "2005"   # 10-02 코스피 전체 순매수 15:30 −970억 → 18:00 −742억 → 20:00 −225억(확정)
PROGRAM_DAYS = 250             # 1년 — 오늘 규모를 견줄 분포
PROGRAM_CHART_DAYS = 60


def _program_won(c: dict) -> dict[str, float] | None:
    """한 행의 9개 값(원). 필드가 빠졌거나 항등식이 깨지면 None — 필드 이름·의미가 바뀐 걸 잡는다."""
    try:
        won = {f"{k}_{s}": float(str(c[pre + suf]).replace(",", ""))
               for pre, k in _PROGRAM_KINDS.items() for suf, s in _PROGRAM_SIDES.items()}
    except (KeyError, TypeError, ValueError):
        return None
    tol = 1e6                                          # 100만원. 실측에선 원 단위까지 맞았다
    for k in _PROGRAM_KINDS.values():
        if abs(won[f"{k}_buy"] - won[f"{k}_sell"] - won[f"{k}_net"]) > tol:
            return None
    for s_ in _PROGRAM_SIDES.values():
        if abs(won[f"arb_{s_}"] + won[f"nonarb_{s_}"] - won[f"total_{s_}"]) > tol:
            return None
    if won["total_buy"] <= 0:                          # 거래일이면 프로그램 매수가 0 일 수 없다 — 빈 행
        return None
    return won


def fetch_program_daily(market: str, days: int, now: datetime | None = None, page_size: int = 200) -> list[dict]:
    """
    시장 전체 일별 프로그램 매매(억원), 날짜 오름차순 최근 days거래일. 키: arb_*·nonarb_*·total_* × buy/sell/net.
    마지막 행이 오늘이고 20:05 전이면 provisional — KRX 값이 장 마감 뒤에도 20시 무렵까지 바뀐다.
    """
    if market not in ("KOSPI", "KOSDAQ"):        # 소문자나 모르는 값은 오류 없이 '두 시장 합계'가 온다
        raise ValueError(market)
    now = now or datetime.now(KST)
    name = MARKET_NAME[market]
    by_date: dict[str, dict] = {}
    skipped = seen = 0
    for page in range(days // page_size + 2):
        if len(by_date) >= days:
            break
        try:
            j = get_json(NAVER_PROGRAM_URL, headers={"Referer": NAVER_PROGRAM_REFERER},
                         params={"tradeType": "KRX", "krxMarketType": market, "bizdate": now.strftime("%Y%m%d"),
                                 "startIdx": page, "pageSize": page_size, "periodType": "DATE"})
            if not isinstance(j, dict) or not isinstance(j.get("content"), list):
                raise RuntimeError(f"응답 형식이 바뀜: {str(j)[:120]}")
        except Exception as e:  # noqa: BLE001
            if not by_date:
                raise
            warn(f"{name} 프로그램 매매: {page + 1}번째 페이지 실패 — 앞서 받은 {len(by_date)}일만 씁니다 ({e})")
            break
        content = j["content"]
        for c in content:
            seen += 1
            d = str(c.get("bizdate") or "")
            try:
                ok_day = bool(re.fullmatch(r"\d{8}", d)) and date(int(d[:4]), int(d[4:6]), int(d[6:])).weekday() < 5
            except ValueError:
                ok_day = False
            won = _program_won(c) if ok_day else None
            if won is None:
                skipped += 1
                continue
            row = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}"}
            row.update({k: float(round(v / 1e8)) for k, v in won.items()})
            by_date.setdefault(row["date"], row)
        if not content or str(j.get("last")).lower() == "true":
            break
        time.sleep(0.3)
    if seen and not by_date:
        raise RuntimeError(f"{seen}행이 모두 형식 검사에서 빠짐 — 필드가 바뀐 듯")
    if skipped:
        warn(f"{name} 프로그램 매매: 형식이 어긋난 {skipped}행을 건너뜀")
    rows = sorted(by_date.values(), key=lambda r_: r_["date"])[-days:]
    if rows and rows[-1]["date"] == now.date().isoformat() and now.strftime("%H%M") < PROGRAM_FINAL_AFTER:
        rows[-1]["provisional"] = True
    return rows


def build_program(now: datetime | None = None) -> dict:
    """코스피·코스닥 프로그램 매매. 화면에는 최근 60일, '오늘'의 위치는 최근 1년 확정치 분포로."""
    out: dict = {"unit": "억원", "chartDays": PROGRAM_CHART_DAYS, "markets": {}}
    got: dict[str, list[dict]] = {}
    for code in ("KOSPI", "KOSDAQ"):
        try:
            rows = fetch_program_daily(code, PROGRAM_DAYS, now)
            if rows:
                got[code] = rows
                print(f"  {code} 프로그램 매매 {len(rows)}일 ({rows[0]['date']} ~ {rows[-1]['date']})")
            else:
                warn(f"{MARKET_NAME[code]} 프로그램 매매: 응답이 비어 있음")
        except Exception as e:  # noqa: BLE001
            warn(f"{MARKET_NAME[code]} 프로그램 매매 수집 실패: {type(e).__name__}: {e}")
    a, b = got.get("KOSPI"), got.get("KOSDAQ")
    if a and b and a[-1]["date"] == b[-1]["date"] and a[-1]["total_buy"] == b[-1]["total_buy"]:
        warn("프로그램 매매: 코스피와 코스닥이 똑같이 와서(시장 구분이 무시된 응답) 버립니다")
        got = {}
    for code, rows in got.items():
        last = rows[-1]
        settled = [r["total_net"] for r in rows if not r.get("provisional")]
        out["markets"][code] = {
            "settled": sorted(round(x) for x in settled),      # 장중 가벼운 수집이 오늘 잠정치의 위치를 다시 잴 때 쓴다
            "daily": [{"date": r["date"], "arb": r["arb_net"], "nonarb": r["nonarb_net"], "total": r["total_net"],
                       **({"provisional": True} if r.get("provisional") else {})}
                      for r in rows[-PROGRAM_CHART_DAYS:]],
            "latest": {
                "date": last["date"], "arb": last["arb_net"], "nonarb": last["nonarb_net"], "total": last["total_net"],
                "provisional": bool(last.get("provisional")),
                "pctl": round(_percentile(settled, last["total_net"]), 1) if len(settled) >= 60 else None,
                "days": len(settled),
                "streak": streak(rows, "total_net"),
            },
        }
    return out


# ---------------------------------------------------------------- 공매도 (KRX 공매도 통계 임베드 경로)
# 네이버·다음 종목 '공매도' 탭이 iframe 으로 띄우는 data.krx.co.kr srtLoader 화면이 부르는 공개 경로.
# 쿠키·로그인은 필요 없지만 Referer(data.krx.co.kr 아래)와 브라우저 UA 가 없으면 403.
# 회원제 원본 경로(dbms/MDC/STAT/srt/...)는 이미 400 'LOGOUT' — 이 경로도 언제든 같은 처지가 될 수 있어 실패해도 조용히 넘어간다.
# 2년 창 조회는 서버에서 15초 넘게 걸린다 → 평소엔 최근 40일만 받아 직전 파일에 덮어 합치고, 비었을 때만 이력을 채운다.

KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_SRT_REFERER = "https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT302"
KRX_SRT_MKT = {"KOSPI": "1", "KOSDAQ": "2"}     # indTpCd = mktTpCd. idxIndCd 001 = 시장 전체
KRX_MAX_SPAN = 700                              # 서버 한도 2년(넘으면 400 INVALIDPERIOD2)
KRX_REFRESH_DAYS = 40                           # 평소 다시 받는 구간 — 잔고 정정(T+2)·잠정치 확정을 덮는다
SHORT_RESUMED = "2025-03-31"                    # 공매도 전면 재개 첫날. 금지 기간 값(예외분만, 비중 0.1~0.5%)은 성격이 달라 싣지 않는다


def krx_short_due(now: datetime) -> bool:
    """거래일 09:00~15:45 에는 KRX 에 새 값이 없다(당일 정규장분이 15:40 이후). 그 시간대 실행은 건너뛴다."""
    hm = now.hour * 60 + now.minute
    return not (is_trading_day(now.date()) and 9 * 60 <= hm < 15 * 60 + 45)


class KrxBlocked(RuntimeError):
    """접속 자체가 막힘(연결 실패·시간 초과·403·회원제 전환·요청 과다) — 이번 실행의 남은 KRX 요청은 건너뛴다."""


def krx_srt_post(bld: str, **params) -> list[dict]:
    """KRX 공매도 bld 호출 → OutBlock_1. 잘못된 파라미터도 200 + 빈 배열이라 빈 결과 판단은 호출부에서."""
    form = {"bld": f"dbms/MDC_OUT/STAT/srt/{bld}", "locale": "ko_KR", "share": "1", "money": "1", **params}
    headers = {"Referer": KRX_SRT_REFERER, "X-Requested-With": "XMLHttpRequest",   # session 의 네이버 Referer 를 덮는다
               "Origin": "https://data.krx.co.kr"}
    last, blocked = None, False
    for i in range(2):
        try:
            r = session.post(KRX_JSON_URL, data=form, headers=headers, timeout=(10, 45))
        except requests.RequestException as e:
            last, blocked = f"{type(e).__name__}: {e}", True
        else:
            body = r.text.strip()
            if r.status_code == 200 and body[:1] == "{":
                rows = r.json().get("OutBlock_1")       # Content-Type 은 text/html 이지만 본문은 JSON
                if not isinstance(rows, list):
                    raise RuntimeError(f"응답 형식이 바뀜 ({body[:120]})")
                return rows
            # 400 본문이 곧 오류 코드: INVALIDPERIOD2(2년 초과) / LOGOUT(회원제 전환) / TEMPBLOCK(요청 과다)
            last = body if r.status_code == 400 and len(body) < 40 else f"HTTP {r.status_code}"
            blocked = r.status_code == 403 or last in ("LOGOUT", "TEMPBLOCK")
            if r.status_code in (400, 403):            # 다시 해도 같거나(형식·차단) 더 나빠진다(TEMPBLOCK)
                break
        time.sleep(2.0 * (i + 1))
    raise (KrxBlocked if blocked else RuntimeError)(f"KRX {bld}: {last}")


def _krx_market_rows(bld: str, market: str, start: date, end: date, date_key: str) -> dict[str, dict]:
    """[start, end] 를 2년 이하 창으로 쪼개 받아 {YYYY-MM-DD: 원본행}."""
    tp = KRX_SRT_MKT[market]
    out: dict[str, dict] = {}
    e = end
    while e >= start:
        s_ = max(start, e - timedelta(days=KRX_MAX_SPAN))
        for x in krx_srt_post(bld, indTpCd=tp, mktTpCd=tp, indAggClssCd="001", idxIndCd="001",
                              strtDd=s_.strftime("%Y%m%d"), endDd=e.strftime("%Y%m%d")):
            d = str(x.get(date_key) or "")
            if re.fullmatch(r"\d{4}/\d{2}/\d{2}", d):
                out[d.replace("/", "-")] = x
        e = s_ - timedelta(days=1)
        if e >= start:
            time.sleep(0.6)
    return out


def _eok_int(v):
    return round(v / 1e8) if v is not None else None


def _check_parsed(raw: dict[str, dict], rows: list[dict], label: str) -> None:
    """날짜는 왔는데 값이 거의 안 읽혔으면 필드 이름이 바뀐 것 — 조용히 직전 값에 머물지 않게 실패로."""
    n = sum(1 for d in raw if d >= SHORT_RESUMED)
    if n >= 3 and len(rows) < n // 2:
        raise RuntimeError(f"{label}: {n}행 중 값이 읽힌 행 {len(rows)}개 — 형식 변경 의심")


def parse_short_daily(raw: dict[str, dict], now: datetime) -> list[dict]:
    """30201 행 → {date, value(공매도 거래대금 억원), total(전체 거래대금 억원), pct(%)}. 형식·단위가 바뀐 낌새면 예외."""
    today = now.date().isoformat()
    settled = now.hour * 60 + now.minute >= 20 * 60 + 10     # NXT 를 포함한 당일 값은 20:10 이후 확정
    rows = []
    for d, x in sorted(raw.items()):
        if d < SHORT_RESUMED:
            continue
        sv, tv = num(x.get("CVSRTSELL_TRDVAL")), num(x.get("ACC_TRDVAL"))
        if sv is None or not tv:
            continue
        if sv > tv:
            raise RuntimeError(f"{d}: 공매도 대금이 전체 거래대금보다 큼 — 형식 변경 의심")
        pct = num(x.get("TRDVAL_WT"))
        if pct is None or abs(sv / tv * 100 - pct) > 0.02:
            pct = round(sv / tv * 100, 2)                 # 제공 비중이 비거나 어긋나면 직접 계산
        row = {"date": d, "value": _eok_int(sv), "total": _eok_int(tv), "pct": pct}
        if d == today and not settled:
            row["provisional"] = True
        rows.append(row)
    _check_parsed(raw, rows, "공매도 거래")
    tv_ = sorted(r["total"] for r in rows[-20:])
    if tv_ and tv_[len(tv_) // 2] < 1000:                 # 원 단위가 아니라 백만원 등으로 바뀌면 하루 거래대금이 1000억 아래로 찍힌다
        raise RuntimeError(f"거래대금 중앙값 {tv_[len(tv_) // 2]}억원 — 단위 변경 의심")
    return rows


def parse_short_balance(raw: dict[str, dict]) -> list[dict]:
    """30601 행 → {date, value(순보유잔고 억원), pct(시가총액 대비 %)}. 보고 의무자 합산이라 실제 잔고보다 작다."""
    rows = []
    for d, x in sorted(raw.items()):
        if d < SHORT_RESUMED:
            continue
        amt, cap = num(x.get("BAL_AMT")), num(x.get("MKTCAP"))
        if amt is None or not cap:
            continue
        if amt > cap:
            raise RuntimeError(f"{d}: 잔고 금액이 시가총액보다 큼 — 형식 변경 의심")
        rows.append({"date": d, "value": _eok_int(amt), "pct": num(x.get("BAL_RTO2"))})
    _check_parsed(raw, rows, "공매도 잔고")
    return rows


def _merge_short(prev: list, new: list[dict]) -> list[dict]:
    by = {r["date"]: r for r in (prev or []) if isinstance(r, dict) and str(r.get("date", "")) >= SHORT_RESUMED}
    by.update({r["date"]: r for r in new})      # 새로 받은 값이 이긴다 (잠정치 → 확정치, 잔고 정정)
    return [by[d] for d in sorted(by)]


def build_short(now: datetime | None = None) -> dict:
    """
    KRX 공매도: 시장별 일별 거래(30201) + 순보유잔고(30601, 2거래일 늦음). 평소 요청 4번.
    직전 short.json 이 비었거나 끊겼으면 재개일(2025-03-31)부터 다시 채운다.
    한 번 막히면(차단·시간 초과) 이번 실행의 나머지 요청은 건너뛰고 직전 값을 둔다. 실패한 쪽은 failed 에 남긴다.
    """
    now = now or datetime.now(KST)
    end = now.date()
    prev = load_prev("short.json") or {}
    out: dict = {"unit": "억원", "resumed": SHORT_RESUMED, "markets": {}, "failed": []}
    due = krx_short_due(now)
    down = None
    for market in ("KOSPI", "KOSDAQ"):
        old = (prev.get("markets") or {}).get(market) or {}
        block: dict = {}
        for part, bld, key, parse in (
            ("daily", "MDCSTAT30201_OUT", "TRD_DD", lambda raw: parse_short_daily(raw, now)),
            ("balance", "MDCSTAT30601_OUT", "RPT_DUTY_OCCR_DD", parse_short_balance),
        ):
            prev_rows = _merge_short(old.get(part) if isinstance(old.get(part), list) else [], [])
            block[part] = prev_rows
            if not due and prev_rows:                   # 장중이라도 갖고 있는 이력이 없으면 받는다
                continue
            if down:
                out["failed"].append(f"{market}.{part}")
                continue
            last = prev_rows[-1]["date"] if prev_rows else ""
            fresh = bool(prev_rows) and prev_rows[0]["date"] <= "2025-04-07" and \
                last >= (end - timedelta(days=KRX_REFRESH_DAYS - 10)).isoformat()
            start = end - timedelta(days=KRX_REFRESH_DAYS) if fresh else date.fromisoformat(SHORT_RESUMED)
            try:
                raw = _krx_market_rows(bld, market, start, end, key)
                if not raw:                             # 40일 창이 통째로 휴장일 수는 없다 — 파라미터·형식이 바뀐 것
                    raise RuntimeError(f"{start}~{end} 창에 행이 하나도 없음")
                block[part] = _merge_short(prev_rows, parse(raw))
                print(f"  {market} 공매도 {part} {len(block[part])}일 (받은 {len(raw)}행, 마지막 {block[part][-1]['date']})")
            except Exception as e:  # noqa: BLE001
                out["failed"].append(f"{market}.{part}")
                warn(f"{MARKET_NAME[market]} 공매도 {'거래' if part == 'daily' else '잔고'}(KRX) 실패 — "
                     f"직전 {len(prev_rows)}일 유지: {type(e).__name__}: {e}")
                if isinstance(e, KrxBlocked):
                    down = e
            time.sleep(0.6)
        latest: dict = {}
        daily = [r for r in block["daily"] if not r.get("provisional")]
        if daily:
            pcts = [r["pct"] for r in daily]
            shown = block["daily"][-1]
            prior = [r["pct"] for r in daily if r["date"] < shown["date"]][-20:]   # 보여 주는 날 앞의 확정 20일
            latest["daily"] = {
                **block["daily"][-1],
                "pctAvg20": round(sum(prior) / len(prior), 2) if prior else None,
                "pctPctl": round(_percentile(pcts, block["daily"][-1]["pct"]), 1) if len(pcts) >= 60 else None,
                "days": len(pcts),
            }
        bal = block["balance"]
        if bal:
            delta = lambda n: bal[-1]["value"] - bal[-1 - n]["value"] if len(bal) > n else None
            latest["balance"] = {**bal[-1], "d5": delta(5), "d20": delta(20)}
        if block["daily"] or block["balance"]:
            out["markets"][market] = {**block, "latest": latest}
    return out


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
    # 경보 기준을 고정 금액(예: 3,000억)으로 두면 잔고가 커질수록 거의 매일 울린다.
    # 그래서 최신 변화가 이 기간의 같은 길이 변화들 가운데 어디쯤인지(백분위)를 함께 싣는다.
    if out["loans"]:
        tot = [r["total"] for r in out["loans"]]
        last = out["loans"][-1]

        def delta(n):
            if len(tot) > n and tot[-1] is not None and tot[-1 - n] is not None:
                return round(tot[-1] - tot[-1 - n], 1)
            return None

        def delta_pctl(n):
            ds = [tot[i] - tot[i - n] for i in range(n, len(tot)) if tot[i] is not None and tot[i - n] is not None]
            return round(_percentile(ds, ds[-1]), 1) if len(ds) >= 20 and delta(n) is not None else None

        out["latest"]["loans"] = {**last, "d5": delta(5), "d20": delta(20),
                                  "d5Pctl": delta_pctl(5), "d20Pctl": delta_pctl(20), "days": len(tot)}
    if out["money"]:
        lastm = out["money"][-1]
        liq = [m["liquidation"] for m in out["money"] if m["liquidation"] is not None]
        liq20 = [m["liquidation"] for m in out["money"][-21:-1] if m["liquidation"] is not None]
        avg20 = round(sum(liq20) / len(liq20), 1) if liq20 else None
        pctl = (round(_percentile(liq, lastm["liquidation"]), 1)
                if len(liq) >= 20 and lastm["liquidation"] is not None else None)
        out["latest"]["money"] = {**lastm, "liqAvg20": avg20, "liqPctl": pctl, "days": len(liq)}
    return out


# ---------------------------------------------------------------- 지수 / 종목

INDEX_META = {"KOSPI": {"name": "코스피", "fdr": "KS11"}, "KOSDAQ": {"name": "코스닥", "fdr": "KQ11"}}
INDEX_DAYS = 1200   # 달력일. 성적표 750거래일 + 20일 선행을 덮는다


def fetch_index_daily(code: str) -> list[dict]:
    """
    네이버 지수 일봉 (요청 1번, 날짜 오름차순). 확정 종가가 옛 FDR 캐시와 782거래일 전부 일치.
    거래대금은 이 소스에 없어 value=None.
    """
    end = datetime.now(KST).date()
    start = end - timedelta(days=INDEX_DAYS)
    arr = get_json(f"https://api.stock.naver.com/chart/domestic/index/{code}/day",
                   params={"startDateTime": start.strftime("%Y%m%d") + "0000",
                           "endDateTime": end.strftime("%Y%m%d") + "0000"})
    bars = []
    for x in arr if isinstance(arr, list) else []:
        d = str(x.get("localDate") or "")
        close = num(x.get("closePrice"))
        if not re.fullmatch(r"\d{8}", d) or close is None:
            continue
        vol = num(x.get("accumulatedTradingVolume"))          # 천주
        bars.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "close": round(close, 2),
            "open": round(num(x.get("openPrice")) or close, 2),
            "high": round(num(x.get("highPrice")) or close, 2),
            "low": round(num(x.get("lowPrice")) or close, 2),
            "volume": int(vol * 1000) if vol is not None else None,
            "value": None,
        })
    bars.sort(key=lambda b: b["date"])
    return bars


def fetch_index_daily_fdr(symbol: str) -> list[dict]:
    """예비 소스: FinanceDataReader (GitHub 캐시라 멈출 수 있다 — 2026-09-17에 실제로 멈췄다)."""
    import FinanceDataReader as fdr

    df = fdr.DataReader(symbol, (datetime.now(KST).date() - timedelta(days=INDEX_DAYS)).isoformat())
    bars = []
    for idx, row in df.iterrows():
        close = float(row["Close"])
        if close != close:                                    # NaN 이면 JSON 이 깨진다
            continue
        def f(k):
            v = row.get(k)
            return round(float(v), 2) if v == v and v is not None else close
        bars.append({
            "date": idx.strftime("%Y-%m-%d"),
            "close": round(close, 2), "open": f("Open"), "high": f("High"), "low": f("Low"),
            "volume": int(row["Volume"]) if row.get("Volume") == row.get("Volume") else None,
            "value": int(row["Amount"]) if "Amount" in row and row["Amount"] == row["Amount"] else None,
        })
    return bars


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

        bars: list[dict] = []
        try:
            bars = fetch_index_daily(code)
        except Exception as e:  # noqa: BLE001
            warn(f"{code} 일봉: 네이버 실패 — {type(e).__name__}: {e}")
        if not bars:
            try:
                bars = fetch_index_daily_fdr(meta["fdr"])
                if bars:
                    warn(f"{code} 일봉은 예비 소스(FinanceDataReader)로 받았습니다")
            except Exception as e:  # noqa: BLE001
                warn(f"{code} 일봉: FinanceDataReader도 실패 — {type(e).__name__}: {e}")
        if bars:
            closes[code] = {b["date"]: b["close"] for b in bars}
            entry["history"] = bars[-CHART_DAYS:]
            # 국면 한 줄 — 최근 250거래일 고점·저점과 지금의 거리
            year = bars[-250:]
            hi, lo = max(year, key=lambda b: b["close"]), min(year, key=lambda b: b["close"])
            cur = entry.get("price") or bars[-1]["close"]
            entry["high1y"] = {"value": hi["close"], "date": hi["date"]}
            entry["low1y"] = {"value": lo["close"], "date": lo["date"]}
            entry["fromHigh"] = round((cur / hi["close"] - 1) * 100, 2) if hi["close"] else None
            print(f"  {code} 일봉 {len(bars)}개 ({bars[0]['date']} ~ {bars[-1]['date']})")
        out["indices"][code] = entry
    return out, closes


def _weekdays_between(a: str, b: str) -> int:
    """a 다음 날부터 b 까지(포함)의 거래일 수 — 평일에서 알려진 휴장일(HOLIDAYS)을 뺀다."""
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return sum(1 for k in range(1, (db - da).days + 1) if is_trading_day(da + timedelta(days=k)))


def check_freshness(flows: dict, market: dict, closes: dict, today: str | None = None) -> set[str]:
    """
    '성공했는데 멈춘' 소스를 잡는다 — 2026-09 에 FDR 지수 캐시가 경고 없이 9/17 에 멈춰 있었다.
    멈춘 것으로 판단한 섹션 키를 돌려준다.

    - 지수 일봉의 마지막 날짜를 기준일과 비교한다. 기준일은 네이버 현재가의 tradedAt,
      시세 API 가 죽었으면 수급 최신일이다.
    - 빠진 날이 기준일 하루뿐이면(장 시작 전·장중이라 일봉에 오늘 봉이 아직 없을 때) 멈춤이 아니다.
      장중이면 현재가로 그날 종가를 채워, 성적표·유사 국면의 '오늘'을 수급 표의 '오늘'과 맞춘다.
    - '하루뿐'은 수급 날짜로 판단한다. 수급에도 그날이 없으면,
      기준일이 오늘이고 일봉과 수급이 같은 마지막 거래일에 머물러 있을 때만 하루뿐으로 본다
      날짜 간격은 휴장일 목록을 뺀 거래일로 세고, 목록에 없는 휴장 하루를 봐주려고 2거래일까지 허용한다
      (예전엔 휴장일 목록이 없어 평일 5일까지 봐줬는데, 그러면 연휴를 낀 멈춤을 며칠씩 놓친다).
    - 수급 최신일 뒤로 지수 거래일이 2일 이상 쌓였으면 수급 소스가 멈춘 것으로 본다.
    """
    stale: set[str] = set()
    today = today or datetime.now(KST).date().isoformat()
    for code, e in market["indices"].items():
        px = closes.get(code)
        if not px:
            continue
        name = e.get("name", code)
        rows = (flows["markets"].get(code) or {}).get("daily") or []
        flow_last = rows[-1]["date"] if rows else None
        traded = str(e.get("tradedAt") or "")[:10]
        live = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", traded))
        if not live:
            traded = flow_last
        if not traded:
            continue
        last = max(px)
        hist_stale = False
        if last < traded:
            pending = {r["date"] for r in rows if r["date"] > last}
            if pending:
                only_today = pending == {traded}
            elif live and traded == today:
                agree = flow_last == last if rows else _weekdays_between(last, traded) <= 1
                only_today = agree and _weekdays_between(last, traded) <= 2
            else:
                only_today = False
            if only_today:
                if live and e.get("marketStatus") != "PREOPEN" and e.get("price") is not None:
                    px[traded] = float(e["price"])
            else:
                hist_stale = True
                warn(f"{name} 일봉이 {last}에서 멈춤 (기준일 {traded})")
                mark_stale(f"market.{code}.history", f"{name} 일봉", asOf=last)
                stale.add(f"market.{code}.history")
        if rows:
            behind = [d for d in px if d > flow_last]
            # 일봉까지 멈췄으면 거래일을 셀 수 없으니 평일 수로 어림한다
            if len(behind) >= 2 or (hist_stale and _weekdays_between(flow_last, traded) >= 2):
                warn(f"{name} 수급이 {flow_last}에서 멈춤 (지수 기준일 {traded})")
                mark_stale(f"flows.{code}", f"{name} 투자자 수급", asOf=flow_last)
                stale.add(f"flows.{code}")
    return stale


def build_stocks(n_kospi: int = 60, n_kosdaq: int = 40, closes: dict | None = None,
                 store_rows: dict | None = None, reuse: dict | None = None) -> dict:
    """
    store_rows: {code: [[YYYYMMDD, f, o, i, hold, close, vol], ...]} — 350종목 저장소에 있는 종목은 다시 받지 않는다.
    reuse: {'top': {code: 직전 top 항목}, 'series': {code: 직전 60일 시계열}} — 주면 저장소에 없는 종목도 직전 값을 쓴다
    (종목별 일별 투자자 수량은 장 마감 뒤에야 하루치가 붙으므로, 장중 전체 수집마다 100번씩 다시 받을 필요가 없다).
    """
    out: dict = {"top": [], "industries": [], "series": {}}
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
                            "type": s.get("stockEndType"),              # stock / etf
                        }
                    )
                    got += 1
                time.sleep(0.15)
            print(f"  {market} 시총상위 {got}종목")
        except Exception as e:  # noqa: BLE001
            warn(f"{market} 시총상위 실패: {e}")

    # 종목별 개인/외인/기관 — 네이버 API 는 최대 60거래일까지 준다.
    # 화면 상세에는 최근 5일만 싣고, 60일 전체는 장바구니 통계(stat60)로 요약한다.
    reused = 0
    for s in out["top"]:
        old = ((reuse or {}).get("top") or {}).get(s["code"])
        if not (store_rows or {}).get(s["code"]) and old and old.get("flow") is not None:
            for k in ("flow", "stat60", "foreignHoldRatio"):
                if k in old:
                    s[k] = old[k]
            if ((reuse or {}).get("series") or {}).get(s["code"]):
                out["series"][s["code"]] = reuse["series"][s["code"]]
            reused += 1
            continue
        try:
            rows_ = (store_rows or {}).get(s["code"])
            if rows_:
                tr = [{"bizdate": r[0], "foreignerPureBuyQuant": r[1], "organPureBuyQuant": r[2],
                       "individualPureBuyQuant": r[3], "foreignerHoldRatio": r[4], "closePrice": r[5]}
                      for r in rows_[-60:]]
            else:
                tr = get_json(
                    f"https://m.stock.naver.com/api/stock/{s['code']}/trend?pageSize=60&page=1"
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
                        "close": num(d.get("closePrice")),
                    }
                )
            days.sort(key=lambda d: d["date"] or "")

            # 60일 장바구니 요약: 순매수 수량 × 그날 종가 ≈ 순매수 금액(억원, 근사)
            valid = [d for d in days if d["close"]]
            if len(valid) >= 20:
                def net_value(key):
                    return round(sum((d[key] or 0) * d["close"] for d in valid) / 1e8, 1)

                idx_px = (closes or {}).get(s["market"]) or {}
                iso = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"

                def since_buy(key, px=None):
                    """
                    순매수한 날들의 종가를 순매수 수량으로 가중한 평균 가격 대비 마지막 종가 등락(%).
                    px 를 주면 같은 날짜·같은 수량 비중으로 '그 시장 지수를 샀다면'의 등락(기준선)을 잰다.
                    """
                    price = (lambda d: d["close"]) if px is None else (lambda d: px.get(iso(d["date"])))
                    days = [d for d in valid if price(d)]
                    q = [(d[key], price(d)) for d in days if (d[key] or 0) > 0]
                    qty = sum(x for x, _ in q)
                    if not qty or not days:
                        return None
                    return round((price(days[-1]) / (sum(x * c for x, c in q) / qty) - 1) * 100, 2)

                s["stat60"] = {
                    "days": len(valid),
                    "from": valid[0]["date"], "to": valid[-1]["date"],
                    "indivValue": net_value("individual"),
                    "foreignValue": net_value("foreign"),
                    "instValue": net_value("institution"),
                    "change": round((valid[-1]["close"] / valid[0]["close"] - 1) * 100, 2),
                    "indivSinceBuy": since_buy("individual"),
                    "foreignSinceBuy": since_buy("foreign"),
                    "indivMarketSinceBuy": since_buy("individual", idx_px) if idx_px else None,
                    "foreignMarketSinceBuy": since_buy("foreign", idx_px) if idx_px else None,
                }

            s["flow"] = [{k: d[k] for k in ("date", "individual", "foreign", "institution", "foreignHoldRatio")}
                         for d in days[-5:]]
            # 상세 화면의 60일 차트용 — 날짜·주체별 순매수 수량(주)·종가를 열 단위로 짧게
            whole = lambda v: int(v) if v is not None else None
            out["series"][s["code"]] = {
                "d": [d["date"] for d in days],
                "i": [whole(d["individual"]) for d in days],
                "f": [whole(d["foreign"]) for d in days],
                "o": [whole(d["institution"]) for d in days],
                "c": [d["close"] for d in days],
            }
            if days:
                s["foreignHoldRatio"] = days[-1]["foreignHoldRatio"]
            if not rows_:
                time.sleep(0.12)
        except Exception as e:  # noqa: BLE001
            warn(f"{s['name']} 수급 실패: {e}")
            s["flow"] = []

    if reused:
        print(f"  종목 60일 수급 {reused}종목은 직전 값 재사용(새 일별 행이 붙기 전)")
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


# ---------------------------------------------------------------- 종목 범위 350 (코스피 시총 상위 200 + 코스닥 150)
# stock.naver.com 의 종목 일별 투자자 API 는 한 번에 2018-08 부터(2000거래일) 준다. 처음 한 번 받아 두고(실행당 일부씩),
# 그 뒤로는 최근 며칠만 받아 이어 붙인다. 원천 이력은 공개 data 브랜치에 올리지 않고 Actions 캐시(.cache/)에만 둔다.
# 화면에는 파생값(연속 일수, 5·20·60일 합계, 보유율 변화, 2018년 이후 최장 기록)과 종목별 1년 시계열만 싣는다.

UNIVERSE_N = {"KOSPI": 200, "KOSDAQ": 150}
STOCK_BACKFILL_DAYS = 2000
STOCK_BACKFILL_PER_RUN = 70          # 한 실행에 처음부터 받는 종목 수(나머지는 우선 60일만, 다음 실행에서 이어서)
STOCK_SERIES_DAYS = 250
STOCK_BUDGET_SEC = 7 * 60            # 한 실행에서 350종목에 쓰는 시간 상한(넘으면 남은 종목은 다음 실행)
STOCK_MAX_CONSECUTIVE_FAIL = 8       # 연달아 이만큼 실패하면 소스가 막힌 것으로 보고 멈춘다
NAVER_STOCK_LIST_URL = "https://stock.naver.com/api/domestic/market/stock/default"
_EXCLUDE_NAME = re.compile(r"(우[A-C]?|우\(전환\))$|스팩|리츠")


def stock_store_path() -> Path:
    return Path(os.environ.get("STOCK_STORE") or (ROOT / ".cache" / "stockhist.json.gz"))


def stock_store_marker() -> Path:
    """'.cache/stockhist.json.gz.changed' — collect.yml 의 hashFiles(...) 와 같은 이름이어야 한다(테스트가 확인)."""
    path = stock_store_path()
    return path.with_name(path.name + ".changed")


def load_stock_store() -> dict:
    try:
        with gzip.open(stock_store_path(), "rt", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st.get("codes"), dict) else {"codes": {}}
    except Exception:  # noqa: BLE001
        return {"codes": {}}


def save_stock_store(store: dict) -> None:
    path = stock_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
    stock_store_marker().write_text("1", encoding="utf-8")   # 워크플로가 이 표시를 보고 캐시를 저장한다
    print(f"  종목 이력 저장 {path} ({path.stat().st_size:,} bytes)")


def fetch_universe(market: str, n: int) -> list[dict]:
    """시가총액 순 상위 n 종목(보통주만: 우선주·스팩·리츠·ETF/ETN 제외)과 현재가·등락률·시총·외국인 보유율."""
    out: list[dict] = []
    for page in range(n // 100 + 3):
        rows = get_json(NAVER_STOCK_LIST_URL, headers={"Referer": "https://stock.naver.com/market/stock/kr/stocklist"},
                        params={"tradeType": "KRX", "marketType": market, "orderType": "marketSum",
                                "startIdx": page, "pageSize": 100})
        if not isinstance(rows, list):
            raise RuntimeError(f"응답 형식이 바뀜: {str(rows)[:120]}")
        for r in rows:
            name = str(r.get("itemname") or "")
            if r.get("type") != "ST" or _EXCLUDE_NAME.search(name):
                continue
            out.append({"code": str(r.get("itemcode")), "name": name, "market": market,
                        "price": num(r.get("nowPrice")), "chg": num(r.get("prevChangeRate")),
                        "marketCap": round(num(r.get("marketSum")) / 1e6) if num(r.get("marketSum")) else None,  # 백만원
                        "holdRatio": num(r.get("frgnHoldRate"))})
        if len(out) >= n or len(rows) < 100:
            break
        time.sleep(0.3)
    return out[:n]


def fetch_stock_trend(code: str, days: int) -> list[list]:
    """[[YYYYMMDD, 외국인, 기관, 개인(주), 외국인 보유율, 종가, 거래량], ...] 날짜 오름차순."""
    rows = get_json(f"https://stock.naver.com/api/domestic/detail/{code}/trend", tries=2,
                    headers={"Referer": f"https://stock.naver.com/domestic/stock/{code}/price"},
                    params={"tradeType": "KRX", "startIdx": 0, "pageSize": min(days, STOCK_BACKFILL_DAYS)})
    if not isinstance(rows, list):
        raise RuntimeError(f"응답 형식이 바뀜: {str(rows)[:120]}")
    out = []
    for r in rows:
        d = str(r.get("bizdate") or "")
        if not re.fullmatch(r"\d{8}", d):
            continue
        out.append([d, _int(r.get("foreignerPureBuyQuant")), _int(r.get("organPureBuyQuant")),
                    _int(r.get("individualPureBuyQuant")),
                    round(num(r.get("frgnHoldRatio")), 2) if num(r.get("frgnHoldRatio")) is not None else None,
                    num(r.get("closePrice")), _int(r.get("tradeVolume"))])
    out.sort(key=lambda x: x[0])
    return out


def _merge_rows(old: list[list], new: list[list]) -> list[list]:
    by = {r[0]: r for r in old}
    by.update({r[0]: r for r in new})       # 새로 받은 값이 이긴다(장 마감 뒤 정정)
    return [by[d] for d in sorted(by)]


def _longest(vals: list) -> dict:
    best = {"buy": 0, "sell": 0}
    side, n = None, 0
    for v in vals:
        s_ = "buy" if (v or 0) > 0 else "sell" if (v or 0) < 0 else None
        n = n + 1 if s_ and s_ == side else (1 if s_ else 0)
        side = s_
        if s_:
            best[s_] = max(best[s_], n)
    return best


def stock_summary(u: dict, rows: list[list]) -> dict:
    """종목 하나의 파생값. 금액은 수량×종가 근사(억원)."""
    keys = (("foreign", 1), ("institution", 2), ("individual", 3))
    out = {k: u.get(k) for k in ("code", "name", "market", "price", "chg", "marketCap")}
    for key, i in keys:
        series = [{"date": r[0], key: r[i]} for r in rows]
        st = streak(series, key)
        val = lambda n: round(sum((r[i] or 0) * (r[5] or 0) for r in rows[-n:]) / 1e8, 1)
        out[key] = {"streak": {"days": st["days"], "side": st["side"]},
                    "d5": sum(r[i] or 0 for r in rows[-5:]), "d20": sum(r[i] or 0 for r in rows[-20:]),
                    "v20": val(20), "v60": val(60), "longest": _longest([r[i] for r in rows])}
    holds = [r[4] for r in rows if r[4] is not None]
    out["holdRatio"] = holds[-1] if holds else u.get("holdRatio")
    out["holdChg20"] = round(holds[-1] - holds[-21], 2) if len(holds) > 20 else None
    out["since"] = f"{rows[0][0][:4]}-{rows[0][0][4:6]}" if rows else None
    out["asOf"] = f"{rows[-1][0][:4]}-{rows[-1][0][4:6]}-{rows[-1][0][6:]}" if rows else None
    return out


def build_universe(now: datetime, backfill_only: bool = False) -> tuple[list[dict], dict] | None:
    """
    350종목 요약과 저장소 행. 장중엔 부르지 않는다(main 이 직전 요약을 쓴다).
    처음 보는 종목은 실행당 STOCK_BACKFILL_PER_RUN 개까지 2000거래일, 나머지는 우선 60일만 받는다.
    backfill_only: 이미 최신 거래일 행이 다 붙어 있을 때 — 아직 2018년부터 채우지 못한 종목만 받는다(요청 절약).
    """
    store = load_stock_store()
    codes = store["codes"]
    try:
        uni = fetch_universe("KOSPI", UNIVERSE_N["KOSPI"]) + fetch_universe("KOSDAQ", UNIVERSE_N["KOSDAQ"])
    except Exception as e:  # noqa: BLE001
        warn(f"종목 범위(시총 상위) 실패: {type(e).__name__}: {e}")
        return None
    if len(uni) < 200:
        warn(f"종목 범위가 {len(uni)}개뿐이라 이번엔 갱신하지 않습니다")
        return None
    backfilled = failed = consecutive = attempted = 0
    changed = blocked = False
    skipped: set[str] = set()
    t0 = time.monotonic()
    for u in uni:
        if blocked or time.monotonic() - t0 > STOCK_BUDGET_SEC:
            skipped.add(u["code"])                         # 이번엔 못 받은 종목 — 저장된 행으로 요약하고 다음 실행에서 이어서
            continue
        rec = dict(codes.get(u["code"]) or {})
        rows = rec.get("rows") or []
        if backfill_only and rows and (not rec.get("partial") or backfilled >= STOCK_BACKFILL_PER_RUN):
            continue                                       # 이미 최신 — 이번엔 처음부터 채울 차례인 종목만
        attempted += 1
        try:
            if not rows or rec.get("partial"):
                full_ = backfilled < STOCK_BACKFILL_PER_RUN
                new = fetch_stock_trend(u["code"], STOCK_BACKFILL_DAYS if full_ else 60)
                backfilled += full_
                if new:                                    # 처음부터 받은 이력이 저장된 것보다 앞까지 닿았을 때만 '다 채움'
                    rec["partial"] = not (full_ and (not rows or new[0][0] <= rows[0][0]))
            else:
                gap = _weekdays_between(f"{rows[-1][0][:4]}-{rows[-1][0][4:6]}-{rows[-1][0][6:]}", now.date().isoformat())
                new = fetch_stock_trend(u["code"], min(STOCK_BACKFILL_DAYS, gap + 5))
            if new:
                rec["rows"] = _merge_rows(rows, new)
                rec.update(name=u["name"], market=u["market"])
                codes[u["code"]] = rec
                changed = True
            consecutive = 0
        except Exception as e:  # noqa: BLE001
            failed += 1
            consecutive += 1
            if failed <= 5:
                warn(f"{u['name']} 종목 수급 실패: {type(e).__name__}: {e}")
            if consecutive >= STOCK_MAX_CONSECUTIVE_FAIL:
                blocked = True
                warn(f"종목 수급이 {consecutive}번 연달아 실패해 이번 실행에서는 멈춥니다(소스가 막힌 듯)")
        time.sleep(0.3)
    if skipped and not blocked:
        warn(f"350종목 시간 예산({STOCK_BUDGET_SEC // 60}분)을 넘어 {len(skipped)}종목은 다음 실행에서 받습니다")
    if failed > 5:
        warn(f"종목 수급 실패 {failed}개(처음 5개만 적음)")
    if changed:
        store["updated"] = now.isoformat(timespec="seconds")
        save_stock_store(store)                            # 멈추더라도 받은 만큼은 저장
    if blocked or (attempted >= 30 and failed > attempted // 3):
        warn("종목 수급 실패가 많아 이번 요약은 버리고 직전 요약을 씁니다")
        return None
    pending = sum(1 for u in uni if (codes.get(u["code"]) or {}).get("partial")) + len(skipped)
    print(f"  종목 범위 {len(uni)}개 · 처음 받은 종목 {backfilled}개 · 남은 종목 {pending}개 · 실패 {failed}개")
    rows_by = {u["code"]: (codes.get(u["code"]) or {}).get("rows") or [] for u in uni}
    summaries = [stock_summary(u, rows_by[u["code"]]) for u in uni if rows_by[u["code"]]]
    return summaries, rows_by, pending


def universe_as_of(stocks: dict) -> str:
    return max((u.get("asOf") or "" for u in stocks.get("universe") or []), default="")


def universe_due(prev_stocks: dict, data_day: str | None, now: datetime) -> bool:
    """350종목을 다시 돌릴 때: 처음이거나, 채우는 중이거나, 새 거래일 데이터가 생겼거나, 그 거래일 20:05(확정) 뒤 아직 안 돌았을 때."""
    uni = prev_stocks.get("universe") or []
    if not uni or prev_stocks.get("universePending"):
        return True
    as_of = universe_as_of(prev_stocks)
    if data_day and as_of < data_day:
        return True
    at = prev_stocks.get("universeAt") or ""
    ref = data_day or now.date().isoformat()
    cut = f"{ref}T{PROGRAM_FINAL_AFTER[:2]}:{PROGRAM_FINAL_AFTER[2:]}"
    return (ref < now.date().isoformat() or now.strftime("%H%M") >= PROGRAM_FINAL_AFTER) and at < cut


def write_stock_series(rows_by: dict, keep: set) -> int:
    """종목 상세의 1년 시계열(docs/data/stockseries/{code}.json). 범위에서 빠진 종목 파일은 지운다."""
    d = OUT / "stockseries"
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for code, rows in rows_by.items():
        rows = rows[-STOCK_SERIES_DAYS:]
        if not rows:
            continue
        payload = {"code": code, "unit": "주", "d": [r[0] for r in rows], "f": [r[1] for r in rows],
                   "o": [r[2] for r in rows], "i": [r[3] for r in rows], "h": [r[4] for r in rows],
                   "c": [r[5] for r in rows]}
        (d / f"{code}.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n += 1
    for f in d.glob("*.json"):
        if f.stem not in keep:
            f.unlink()
    return n


# ---------------------------------------------------------------- 개미 장바구니 vs 외인 장바구니

def rank_streak_fn(stocks: dict, rows_by: dict | None = None):
    """
    (code, 'foreign'|'institution', to) → 그 목록 날짜(to)까지의 연속 기록. 350종목 이력(이번 실행 또는 직전 stockseries 파일),
    없으면 시총 상위 종목의 60일 행에서 센다. 셀 수 있는 행 맨 앞까지 이어지면 atLeast(그 이상일 수 있음).
    """
    idx = {"foreign": 1, "institution": 2}
    cache: dict[str, list] = {}

    def rows_of(code: str) -> list[tuple[str, float, float]]:
        if code in cache:
            return cache[code]
        rows: list = []
        if rows_by and rows_by.get(code):
            rows = [(r[0], r[1], r[2]) for r in rows_by[code]]
        else:
            f = OUT / "stockseries" / f"{code}.json"
            try:
                x = json.loads(f.read_text(encoding="utf-8"))
                rows = list(zip(x["d"], x["f"], x["o"]))
            except Exception:  # noqa: BLE001
                x = (stocks.get("series") or {}).get(code)
                if x:
                    rows = list(zip(x["d"], x["f"], x["o"]))
        cache[code] = rows
        return rows

    def fn(code: str, key: str, to: str | None):
        rows = rows_of(code)
        if to:
            cut = to.replace("-", "")
            rows = [r for r in rows if str(r[0]).replace("-", "") <= cut]
        if not rows:
            return None
        series = [{"date": r[0], key: r[idx[key]]} for r in rows]
        st = streak(series, key)
        if st["days"] and st["days"] >= len(rows):
            st["atLeast"] = True
        return st
    return fn


def stock_streaks(stocks: dict) -> dict:
    """{code: {'foreign': {days, side}, 'institution': {...}}} — 순위 행에 붙일 연속 일수."""
    out = {}
    for u in stocks.get("universe") or []:
        out[u["code"]] = {k: (u.get(k) or {}).get("streak") for k in ("foreign", "institution")}
    for s_ in stocks.get("top") or []:
        if s_.get("code") in out or not s_.get("flow"):
            continue
        rows = [{"date": d["date"], "foreign": d.get("foreign"), "institution": d.get("institution")} for d in s_["flow"]]
        out[s_["code"]] = {k: streak(rows, k) for k in ("foreign", "institution")}
    return out


def build_ant_stocks(stocks: dict, closes: dict | None = None) -> dict:
    """
    최근 60거래일, 개미와 외국인이 각각 어떤 종목을 담았고 그 뒤 어떻게 됐는지.
    - 순매수 금액은 (일별 순매수 수량 × 그날 종가)의 합 — 근사치
    - '산 뒤 등락'은 그 주체가 순매수한 날들의 종가를 수량으로 가중한 평균 가격 대비 마지막 종가.
      60일 전체 등락으로 재면, 떨어진 뒤에 사들인 종목도 '담았는데 빠졌다'가 된다
    - ETF 는 뺀다(지수를 산 것이지 종목을 고른 게 아니다)
    - 기준선은 같은 순매수일·같은 수량 비중으로 그 시장 지수를 샀다면의 등락(marketSinceBuy).
      60일 지수 등락(market)은 60일 종목 등락(change)과만 견준다
    """
    pool = [s for s in stocks.get("top", []) if s.get("stat60") and s.get("type") != "etf"]
    if len(pool) < 10:
        warn(f"장바구니 비교: stat60 있는 종목 부족({len(pool)})")
        return {}

    def pick(key, since_key, n=10):
        ranked = sorted(pool, key=lambda s: s["stat60"][key], reverse=True)
        return [
            {
                "code": s["code"], "name": s["name"], "market": s["market"],
                "value": s["stat60"][key], "change": s["stat60"]["change"],
                "sinceBuy": s["stat60"].get(since_key),
                "marketSinceBuy": s["stat60"].get(since_key.replace("SinceBuy", "MarketSinceBuy")),
                "price": s["price"],
            }
            for s in ranked if s["stat60"][key] > 0
        ][:n]

    ant_basket = pick("indivValue", "indivSinceBuy")          # 개미가 가장 많이 담은 종목
    foreign_basket = pick("foreignValue", "foreignSinceBuy")  # 외인이 가장 많이 담은 종목

    def avg(basket, key):
        xs = [b[key] for b in basket if b.get(key) is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    # 눈물과 승리는 부호로 가른다 — 순위만으로 고르면 떨어진 종목이 '승리'에 들어간다
    known = [b for b in ant_basket if b.get("sinceBuy") is not None]
    tears = sorted((b for b in known if b["sinceBuy"] < 0), key=lambda b: b["sinceBuy"])[:5]
    wins = sorted((b for b in known if b["sinceBuy"] > 0), key=lambda b: -b["sinceBuy"])[:5]

    sample = pool[0]["stat60"]
    iso = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"

    def index_change(code):
        px = (closes or {}).get(code) or {}
        a = [v for d, v in sorted(px.items()) if d >= iso(sample["from"])]
        b = [v for d, v in sorted(px.items()) if d <= iso(sample["to"])]
        return round((b[-1] / a[0] - 1) * 100, 2) if a and b else None

    return {
        "window": {"days": sample["days"], "from": sample["from"], "to": sample["to"]},
        "universe": len(pool),
        "antBasket": ant_basket,
        "foreignBasket": foreign_basket,
        "antAvgChange": avg(ant_basket, "change"),
        "foreignAvgChange": avg(foreign_basket, "change"),
        "antAvgSinceBuy": avg(ant_basket, "sinceBuy"),
        "foreignAvgSinceBuy": avg(foreign_basket, "sinceBuy"),
        # 같은 날짜·같은 수량 비중으로 그 종목의 시장 지수를 샀다면 — '산 뒤 등락'과 같은 잣대의 기준선
        "antAvgMarketSinceBuy": avg(ant_basket, "marketSinceBuy"),
        "foreignAvgMarketSinceBuy": avg(foreign_basket, "marketSinceBuy"),
        "market": {"KOSPI": index_change("KOSPI"), "KOSDAQ": index_change("KOSDAQ")},
        "tears": tears,
        "wins": wins,
        "note": "순매수 금액은 일별 순매수 수량 × 그날 종가의 합산 근사치입니다. "
                "'산 뒤 등락'은 그 주체가 순매수한 날들의 종가를 수량으로 가중한 평균 가격 대비 마지막 종가의 "
                "등락으로, 판 날은 반영하지 않은 추정치입니다. '지수였다면'은 같은 날짜·같은 비중으로 그 종목의 "
                "시장 지수를 샀을 때의 같은 계산입니다. 대상은 지금의 시총 상위 종목(ETF 제외)이라, "
                "그사이 크게 빠져 순위 밖으로 밀린 종목은 빠져 있습니다.",
    }


# ---------------------------------------------------------------- 글로벌 / 매크로

PREOPEN_SYMBOLS = ("^SOX", "^GSPC", "EWY", "KRW=X")   # 장 전 '간밤' 4칸 — 시가 갭과 상관 0.64~0.75(2009~2023)

GLOBAL_TICKERS = [
    ("^GSPC", "S&P 500", "us"),
    ("^IXIC", "나스닥", "us"),
    ("^DJI", "다우", "us"),
    ("^SOX", "필라델피아 반도체", "us"),
    ("EWY", "한국 ETF(EWY)", "us"),             # 미국에 상장된 한국 주식 ETF — 간밤 한국 주식에 대한 미국장 평가
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
    out = {"items": [], "sparkDays": 30, "preopen": list(PREOPEN_SYMBOLS)}
    try:
        import yfinance as yf

        symbols = [t[0] for t in GLOBAL_TICKERS]
        df = yf.download(
            symbols, period="1y", interval="1d",
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
                # 오늘 움직임 크기가 최근 1년 하루 움직임 중 어디쯤인지(방향이 아니라 크기)
                moves = [abs(float(x)) for x in closes.pct_change().dropna().tail(250)]
                move_pctl = round(_percentile(moves, abs(last / prev - 1)), 1) if prev and len(moves) >= 120 else None
                out["items"].append(
                    {
                        "symbol": sym, "name": name, "category": cat,
                        "price": round(last, 4),
                        "change": round(last - prev, 4),
                        "changeRate": round((last - prev) / prev * 100, 2) if prev else None,
                        "asOf": closes.index[-1].strftime("%Y-%m-%d"),
                        "spark": spark,
                        "movePctl": move_pctl,
                    }
                )
            except Exception as e:  # noqa: BLE001
                warn(f"{name}({sym}) 처리 실패: {type(e).__name__}: {e}")
        print(f"  글로벌 지표 {len(out['items'])}개")
    except Exception as e:  # noqa: BLE001
        warn(f"yfinance 실패: {type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- 국내 일정 소스 (한국은행·KRX·네이버 캘린더)

BOK_MPC_URL = "https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do"
_BOK_ROW = re.compile(r'<th[^>]*scope="row"[^>]*>\s*(\d{1,2})월\s*(\d{1,2})일\s*\((.)\)\s*</th>')
_WEEKDAY_KO = "월화수목금토일"


def parse_bok_mpc(html: str, year: int) -> list[str]:
    """
    한국은행 '통화정책방향 결정회의' 연간 목록. 표의 행 머리가 'MM월 DD일(요일)'.
    아직 공표되지 않은 해(예: 2026-10 기준 2027)는 표가 비어 있어 [] — 오류가 아니다.
    연도·요일이 어긋나거나 행은 있는데 날짜가 하나도 안 읽히면 형식이 바뀐 것으로 본다.
    """
    m = re.search(r'<div class="h-group">\s*<h3>\s*(\d{1,4})년\s*</h3>', html)
    if not m or int(m.group(1)) != year:
        raise RuntimeError(f"연도 불일치 (요청 {year}, 응답 {m.group(1) if m else '없음'})")
    s = html.find('<table id="tableId"')
    b = html.find("<tbody", s) if s >= 0 else -1
    if b < 0:
        raise RuntimeError("회의 목록 표가 없음 — 형식이 바뀐 듯")
    body = html[b:html.find("</tbody>", b)]
    rows = re.split(r"<tr[\s>]", body)[1:]
    out: set[str] = set()
    for row in rows:
        mm = _BOK_ROW.search(row)
        if not mm:
            continue
        d = date(year, int(mm.group(1)), int(mm.group(2)))
        if _WEEKDAY_KO[d.weekday()] != mm.group(3):
            raise RuntimeError(f"요일 불일치 {d} ({mm.group(3)})")
        out.add(d.isoformat())
    if rows and not out:
        raise RuntimeError(f"행 {len(rows)}개에서 날짜를 하나도 못 읽음 — 형식이 바뀐 듯")
    if out and not 4 <= len(out) <= 16:
        raise RuntimeError(f"회의 수가 이상함 ({len(out)}회)")
    return sorted(out)


def fetch_bok_mpc(year: int) -> list[str]:
    r = session.get(BOK_MPC_URL, params={"mtgSe": "A", "menuNo": "200755", "pYear": year},
                    headers={"Referer": "https://www.bok.or.kr/"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return parse_bok_mpc(r.text, year)


KRX_OPEN = "https://open.krx.co.kr"
_KRX_HOL_PAGE = "/contents/MKD/01/0110/01100305/MKD01100305.jsp"
_KRX_HOL_BLD = "MKD/01/0110/01100305/mkd01100305_01"


def fetch_krx_holidays(years: list[int]) -> dict[int, dict[str, str]]:
    """
    KRX 휴장일 (open.krx.co.kr: OTP 발급 → 조회). 브라우저 UA 가 없으면 OTP 가 빈 문자열로 온다.
    잘못된 요청은 302 로 엉뚱한 주소에 보내므로 리다이렉트를 따라가지 않는다. 다음 해 목록은 KRX 잠정치다.
    """
    hdr = {"Referer": KRX_OPEN + _KRX_HOL_PAGE}
    r = session.get(KRX_OPEN + "/contents/COM/GenerateOTP.jspx", headers=hdr, timeout=20, allow_redirects=False,
                    params={"bld": _KRX_HOL_BLD, "name": "form", "_": int(time.time() * 1000)})
    otp = r.text.strip()
    if r.status_code != 200 or not otp or "<" in otp:
        raise RuntimeError(f"OTP 발급 실패 (HTTP {r.status_code})")
    out: dict[int, dict[str, str]] = {}
    for y in years:
        time.sleep(0.4)
        r = session.post(KRX_OPEN + "/contents/OPN/99/OPN99000001.jspx", headers=hdr, timeout=20,
                         allow_redirects=False,
                         data={"search_bas_yy": str(y), "gridTp": "KRX", "pagePath": _KRX_HOL_PAGE, "code": otp})
        if r.status_code != 200 or not r.content.strip():
            raise RuntimeError(f"{y}년 조회 실패 (HTTP {r.status_code})")
        rows = json.loads(r.content.decode("utf-8")).get("block1")
        if not isinstance(rows, list):
            raise RuntimeError(f"{y}년 응답 형식이 바뀜")
        days: dict[str, str] = {}
        for x in rows:
            d = date.fromisoformat(str(x.get("calnd_dd")))
            if d.year != y or d.weekday() > 4:
                raise RuntimeError(f"{y}년 목록에 이상한 날짜 {d}")
            days[d.isoformat()] = (x.get("holdy_nm") or "").strip() or "휴장"
        out[y] = days
    return out


NAVER_CAL_URL = "https://stock.naver.com/api/marketCalendars/v1/events/search"


def fetch_naver_calendar(category: str, start: date, end: date) -> list[tuple[str, str, str]]:
    """네이버 증시 캘린더의 한국 일정 (날짜, 이름, 부제). 한 번에 42일까지, 데이터는 오늘 앞뒤 약 90일만 있다."""
    out: list[tuple[str, str, str]] = []
    cur = start
    while cur <= end:
        e = min(cur + timedelta(days=41), end)
        r = session.post(NAVER_CAL_URL, timeout=20,
                         headers={"Referer": "https://stock.naver.com/calendar", "Origin": "https://stock.naver.com"},
                         json={"codes": [], "from": cur.isoformat(), "to": e.isoformat(),
                               "category": category, "myStocksOnly": False})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        for g in r.json().get("dateGroups") or []:
            for ev in g.get("events") or []:
                if ev.get("nationType") == "KOR" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(g.get("date"))):
                    out.append((str(g["date"]), str(ev.get("eventName") or ""), str(ev.get("subtitle") or "")))
        cur = e + timedelta(days=1)
        time.sleep(0.3)
    return out


CAL_RECHECK_DAYS = 7      # 일정 소스는 일주일에 한 번만 다시 받는다(실패했거나 다음 해가 비었으면 하루에 한 번)


def refresh_calendar(now: datetime, fetch: bool = True) -> dict:
    """
    휴장일·금통위 날짜를 최신으로 맞추고 calendar.json 에 쓸 내용을 돌려준다.
    연도마다 우선순위: 이번에 받은 공식 목록(KRX·한국은행) > 직전 실행이 받아 둔 공식 목록 > krx_calendar.json.
    공식 소스가 실패하면 네이버 증시 캘린더(앞으로 약 90일)에서 찾은 휴장일·금통위를 더한다.
    전역 HOLIDAYS·MPC 를 그 결과로 바꾼다 — 장 상태, 멈춤 판단, 만기 계산이 이 목록을 쓴다.
    """
    today = now.date()
    years = [today.year, today.year + 1]
    prev = load_prev("calendar.json") or {}
    attempt = dict(prev.get("attempt") or {})          # 소스 -> {"at": 시각, "ok": bool}
    source = dict(prev.get("source") or {})            # "holidays.2026" -> "KRX" | "한국은행" | "seed"
    extra_hol = dict(prev.get("extraHolidays") or {})  # 네이버에서 찾아 더한 휴장일
    extra_mpc = sorted(prev.get("extraMpc") or [])

    seed_hol: dict[str, dict[str, str]] = {}
    for d, nm in (CAL_SEED.get("holidays") or {}).items():
        seed_hol.setdefault(d[:4], {})[d] = nm
    seed_mpc = {str(y): sorted(v) for y, v in (CAL_SEED.get("mpc") or {}).items()}
    if not CAL_SEED:            # krx_calendar.json 이 깨졌으면 직전 실행이 쓴 목록을 seed 로
        seed_hol = {y: dict(v) for y, v in (prev.get("holidays") or {}).items() if isinstance(v, dict)}
        seed_mpc = {y: sorted(v) for y, v in (prev.get("mpc") or {}).items() if isinstance(v, list)}
        SPECIAL_SESSIONS.update({d: tuple(v) for d, v in (prev.get("specialSessions") or {}).items()
                                 if d >= today.isoformat() and isinstance(v, list) and len(v) == 2})
    # 공식 목록이 이기지만, seed 에 손으로 더한 날짜는 늘 합친다(공식 소스가 막혔을 때 새 임시공휴일을 넣는 길)
    hol = {y: dict(v) for y, v in seed_hol.items()}
    mpc = dict(seed_mpc)
    for y, v in (prev.get("holidays") or {}).items():
        if source.get(f"holidays.{y}") == "KRX" and isinstance(v, dict):
            hol[y] = {**seed_hol.get(y, {}), **v}
    for y, v in (prev.get("mpc") or {}).items():
        if source.get(f"mpc.{y}") == "한국은행" and isinstance(v, list):
            mpc[y] = sorted(v)
    fresh_hol: set[str] = set()                         # 이번 실행에 공식 목록을 받은 해
    fresh_mpc: set[str] = set()

    def due(kind: str, complete: bool) -> bool:
        if not fetch:
            return False
        a = attempt.get(kind) or {}
        try:
            age = now - datetime.fromisoformat(a["at"])
        except Exception:  # noqa: BLE001
            return True
        return age >= timedelta(days=CAL_RECHECK_DAYS if a.get("ok") and complete else 1)

    def stamp(kind: str, ok: bool) -> None:
        attempt[kind] = {"at": now.isoformat(timespec="seconds"), "ok": ok}

    naver_from, naver_to = today, today + timedelta(days=84)

    if due("holidays", all(source.get(f"holidays.{y}") == "KRX" for y in years)):
        try:
            got = fetch_krx_holidays(years)
            for y, days in got.items():
                if len(days) >= 8:                      # 확정 연도는 15~20건. 너무 적으면 미공표·장애로 보고 쓰지 않는다
                    hol[str(y)] = {**seed_hol.get(str(y), {}), **days}
                    source[f"holidays.{y}"] = "KRX"
                    fresh_hol.add(str(y))
            print(f"  KRX 휴장일 {', '.join(f'{y}년 {len(v)}일' for y, v in got.items())}")
            if str(today.year) not in fresh_hol:        # 올해 목록이 비거나 너무 짧으면 '받았다'고 치지 않는다
                raise RuntimeError(f"{today.year}년 휴장일이 {len(got.get(today.year) or {})}건뿐")
            stamp("holidays", True)
        except Exception as e:  # noqa: BLE001
            stamp("holidays", False)
            warn(f"KRX 휴장일 갱신 실패 — 저장된 목록을 씁니다: {type(e).__name__}: {e}")
            try:
                before = len(extra_hol)
                for d, name, sub in fetch_naver_calendar("holiday", naver_from, naver_to):
                    if "휴장" in name and date.fromisoformat(d).weekday() < 5:
                        extra_hol[d] = sub.strip() or "휴장"
                if len(extra_hol) > before:
                    print(f"  네이버 증시 캘린더로 휴장일 {len(extra_hol) - before}일 보충")
            except Exception as e2:  # noqa: BLE001
                warn(f"네이버 증시 캘린더(휴장일)도 실패: {type(e2).__name__}: {e2}")

    if due("mpc", all(mpc.get(str(y)) for y in years)):
        try:
            for y in years:
                got = fetch_bok_mpc(y)
                if got:                                 # 미공표 연도는 [] — 갖고 있던 값을 지우지 않는다
                    mpc[str(y)] = got
                    source[f"mpc.{y}"] = "한국은행"
                    fresh_mpc.add(str(y))
                elif y == today.year:                   # 올해가 비었으면 형식이 바뀐 것 — 다음 해만 미공표일 수 있다
                    raise RuntimeError(f"{y}년 회의 목록이 비어 있음")
                time.sleep(0.4)
            stamp("mpc", True)
            print(f"  금통위 일정 {', '.join(f'{y}년 {len(mpc.get(str(y)) or [])}회' for y in years)} (한국은행)")
        except Exception as e:  # noqa: BLE001
            stamp("mpc", False)
            warn(f"한국은행 금통위 일정 갱신 실패 — 저장된 목록을 씁니다: {type(e).__name__}: {e}")
            try:
                found = {d for d, name, _ in fetch_naver_calendar("economicIndicators", naver_from, naver_to)
                         if "기준금리" in name}
                extra_mpc = sorted(set(extra_mpc) | found)
            except Exception as e2:  # noqa: BLE001
                warn(f"네이버 증시 캘린더(금통위)도 실패: {type(e2).__name__}: {e2}")

    # 지난 보충분, 그리고 이번 실행에 공식 목록을 새로 받은 해의 보충분은 버린다
    keep_from = (today - timedelta(days=40)).isoformat()
    extra_hol = {d: n for d, n in extra_hol.items() if d >= keep_from and d[:4] not in fresh_hol}
    extra_mpc = [d for d in extra_mpc if d >= keep_from and d[:4] not in fresh_mpc]

    HOLIDAYS.clear()
    for v in hol.values():
        HOLIDAYS.update(v)
    for d, n in extra_hol.items():
        HOLIDAYS.setdefault(d, n)
    MPC.clear()
    MPC.update({y: sorted(v) for y, v in mpc.items()})
    for d in extra_mpc:
        MPC[d[:4]] = sorted(set(MPC.get(d[:4]) or []) | {d})
    for y in hol:
        source.setdefault(f"holidays.{y}", "seed")
    for y in mpc:
        source.setdefault(f"mpc.{y}", "seed")
    return {
        "holidays": {y: dict(sorted(v.items())) for y, v in sorted(hol.items())},
        "mpc": {y: v for y, v in sorted(mpc.items())},
        "extraHolidays": dict(sorted(extra_hol.items())),
        "extraMpc": extra_mpc,
        "specialSessions": {d: list(v) for d, v in sorted(SPECIAL_SESSIONS.items())},
        "source": dict(sorted(source.items())),
        "attempt": attempt,
    }


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def first_trading_day(year: int) -> date:
    d = date(year, 1, 1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def derivative_expiries(start: date, end: date) -> list[dict]:
    """
    코스피200 선물·옵션 최종거래일: 각 결제월의 두 번째 목요일, 휴장일이면 앞당긴다(KRX 상품명세).
    3·6·9·12월은 선물과 옵션이 함께 끝나는 동시만기. 위클리 옵션은 넣지 않는다.
    """
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        first = date(y, m, 1)
        d = first + timedelta(days=(3 - first.weekday()) % 7 + 7)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        if start <= d <= end:
            out.append({"date": d.isoformat(), "quarterly": m % 3 == 0})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def korean_events(today: date, horizon: int = 150) -> list[dict]:
    """금통위·선물옵션 만기·휴장일. 이어진 연휴(사이 주말 포함)는 한 건으로 묶는다."""
    start, end = today - timedelta(days=31), today + timedelta(days=horizon)
    lo, hi = start.isoformat(), end.isoformat()
    md = lambda d: f"{d.month}/{d.day}({_WEEKDAY_KO[d.weekday()]})"
    ev = []
    for d in sorted({d for v in MPC.values() for d in v}):
        if lo <= d <= hi:
            ev.append({"type": "금통위", "date": d, "title": "한국은행 기준금리 결정",
                       "note": "통화정책방향 결정회의 · 결과 발표 오전 10시경", "impact": "high"})
    for x in derivative_expiries(start, end):
        q = x["quarterly"]
        ev.append({"type": "만기", "date": x["date"],
                   "title": "선물·옵션 동시만기" if q else "옵션 만기",
                   "note": ("코스피200 선물·옵션 최종거래일 · 장 막판에 프로그램 매매가 몰리기 쉬운 날" if q
                            else "코스피200 옵션 최종거래일"),
                   "impact": "mid" if q else "low"})
    groups: list[list[date]] = []
    for d in sorted(date.fromisoformat(x) for x in HOLIDAYS if lo <= x <= hi):
        if groups:
            gap = groups[-1][-1] + timedelta(days=1)
            while gap.weekday() >= 5:
                gap += timedelta(days=1)
            if gap == d:
                groups[-1].append(d)
                continue
        groups.append([d])
    for g in groups:
        names = list(dict.fromkeys(HOLIDAYS[d.isoformat()] for d in g))
        shown = next((d for d in g if d >= today), g[0])        # 연휴 중간이면 오늘 날짜로 띄운다
        span = f"{md(g[0])}~{md(g[-1])} · " if len(g) > 1 else ""
        ev.append({"type": "휴장", "date": shown.isoformat(), "title": f"증시 휴장 · {'·'.join(names)}",
                   "note": f"{span}다음 거래일 {md(next_trading_day(g[-1]))}", "impact": "low"})
    return ev


# ---------------------------------------------------------------- 이벤트 일정

MONTHS = {
    m: i
    for i, m in enumerate(
        "January February March April May June July August September October "
        "November December".split(),
        start=1,
    )
}


def fomc_kst_hour(y: int, m: int, d: int) -> int:
    """FOMC 결정(미 동부 오후 2시)의 한국 시각. 미국 서머타임(3월 둘째 일요일~11월 첫 일요일)이면 3시, 아니면 4시."""
    def nth_sunday(month, nth):
        first = date(y, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))
    return 3 if nth_sunday(3, 2) <= date(y, m, d) < nth_sunday(11, 1) else 4


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
                    "note": f"결과 발표는 한국시간 다음날 새벽 {fomc_kst_hour(end_year, end_month, days[-1])}시경",
                    "kstHour": fomc_kst_hour(end_year, end_month, days[-1]),
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
        ("FEDFUNDS", "미국 실효 연방기금금리(월평균)", "level"),
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


UPCOMING_MAX = 10


KR_INDICATORS = ("수출", "수입", "무역수지", "소비자물가", "생산자물가", "GDP", "국내총생산", "실업", "고용", "경상수지")


def korean_indicator_events(today: date, days: int = 41) -> list[dict]:
    """
    국내 주요 경제지표 발표 일정(수출입·물가·성장률·고용·경상수지). 기준금리는 금통위 일정이 따로 있어 뺀다.
    원천의 중요도 값은 모두 '매우 높음'이라 쓰지 않고, 지표 이름으로 고른다. 같은 날 여러 개는 한 건으로 묶는다.
    """
    by_day: dict[str, list[tuple[str, str]]] = {}
    for d, name, sub in fetch_naver_calendar("economicIndicators", today, today + timedelta(days=days)):
        name = (name or "").strip()
        if not name or "기준금리" in name or not any(k in name for k in KR_INDICATORS):
            continue
        when = sub.split("·")[0].strip()                  # '10:00 예정 · 시장 영향력 매우 높음' → '10:00 예정'
        by_day.setdefault(d, []).append((name, when))
    out = []
    for d, items in sorted(by_day.items()):
        names = list(dict.fromkeys(n for n, _ in items))
        when = next((w for _, w in items if re.match(r"\d{1,2}:\d{2}", w)), "")
        out.append({"type": "지표", "date": d, "title": " · ".join(names[:3]) + (" 외" if len(names) > 3 else ""),
                    "note": f"{when.replace('예정', '').strip()} 발표 예정" if when else "", "impact": "mid"})
    return out


def build_events() -> tuple[dict, list[dict] | None]:
    """(이벤트, 매크로 지표). FRED 키가 없으면 매크로는 None — 실패가 아니라 '쓰지 않음'."""
    events: list[dict] = []
    macro: list[dict] | None = []
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
        SECRETS.add(fred_key)
        try:
            fe = fetch_fred_events(fred_key)
            events.extend(fe)
            print(f"  FRED 발표일정 {len(fe)}건")
            macro = fetch_fred_series(fred_key)
            print(f"  FRED 매크로 지표 {len(macro)}건")
        except Exception as e:  # noqa: BLE001
            warn(f"FRED 실패: {type(e).__name__}: {e}")
    else:
        macro = None
        warn("FRED_API_KEY 미설정 — 미국 CPI/고용/PCE 발표 일정과 매크로 실측치를 건너뜁니다. "
             "무료 키: https://fred.stlouisfed.org/docs/api/api_key.html")

    try:
        kr = korean_events(datetime.now(KST).date())
        events.extend(kr)
        print(f"  국내 일정 {len(kr)}건 (금통위·만기·휴장)")
    except Exception as e:  # noqa: BLE001
        warn(f"국내 일정 계산 실패: {type(e).__name__}: {e}")
    try:
        ind = korean_indicator_events(datetime.now(KST).date())
        events.extend(ind)
        print(f"  국내 경제지표 {len(ind)}건 (네이버 증시 캘린더)")
    except Exception as e:  # noqa: BLE001
        warn(f"국내 경제지표 일정 실패: {type(e).__name__}: {e}")

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
        uniq.setdefault((e["type"], e["date"], e["title"] if e["type"] == "지표" else ""), e)
    events = sorted(uniq.values(), key=lambda e: e["date"])
    future = [e for e in events if e["dday"] >= 0]
    upcoming = future[:UPCOMING_MAX]
    # FOMC·금통위는 영향이 가장 큰 이벤트 — 개수 제한에 밀려나지 않게 보장한다(가까운 순서는 유지)
    for kind in ("FOMC", "금통위"):
        nxt = next((e for e in future if e["type"] == kind), None)
        if nxt and nxt not in upcoming:
            drop = next((e for e in reversed(upcoming) if e["type"] not in ("FOMC", "금통위")), upcoming[-1])
            upcoming = sorted([e for e in upcoming if e is not drop] + [nxt], key=lambda e: e["date"])
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


def _grade(excess: float | None, ci: tuple[float, float] | None = None) -> str:
    """
    시장 평균 대비 초과수익(%p) 을 학점으로.
    90% 범위(ci)가 0 을 포함하거나 범위를 구하지 못했으면 시장 평균과 구분되지 않으므로 크기와 상관없이 C.
    """
    if excess is None:
        return "?"
    if ci is None or ci[0] <= 0 <= ci[1]:      # 범위를 못 구했으면 구분할 근거도 없다
        return "C"
    for cut, g in ((3, "A"), (1, "B"), (-1, "C"), (-3, "D")):
        if excess >= cut:
            return g
    return "F"


def _excess_ci(fwd: list[float], heavy: list[bool], block: int = 20, reps: int = 600,
               seed: int = 7) -> tuple[float, float] | None:
    """
    '고른 날 이후 평균 − 전체 평균' 의 90% 범위 (이동 블록 부트스트랩).
    20일 선행수익률은 날마다 19일씩 겹치고 크게 산 날은 몰려서 나오므로, 날을 하나씩 다시 뽑으면
    표본이 실제보다 훨씬 커 보인다. 연속 block 일을 통째로 다시 뽑아 그 겹침을 보존한다.
    시드를 고정해 같은 데이터면 같은 범위가 나온다(화면 숫자가 실행마다 흔들리지 않게).
    """
    m = len(fwd)
    if m < block * 3 or not any(heavy):
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(reps):
        idx: list[int] = []
        while len(idx) < m:
            st = rng.randrange(0, m - block + 1)
            idx.extend(range(st, st + block))
        idx = idx[:m]
        hv = [fwd[i] for i in idx if heavy[i]]
        if hv:
            stats.append(sum(hv) / len(hv) - sum(fwd[i] for i in idx) / m)
    if len(stats) < reps * 0.9:
        return None
    stats.sort()
    return round(stats[int(len(stats) * 0.05)], 2), round(stats[int(len(stats) * 0.95) - 1], 2)


def _significant(ci) -> bool:
    return bool(ci and not (ci[0] <= 0 <= ci[1]))


def _pick_apart(order: list[int], gap: int, k: int) -> list[int]:
    """order 순서대로 고르되, 이미 고른 날과 gap 거래일 안쪽인 날은 건너뛴다(같은 국면 중복 방지)."""
    picked: list[int] = []
    for i in order:
        if all(abs(i - j) >= gap for j in picked):
            picked.append(i)
            if len(picked) == k:
                break
    return picked


FLOW_MIN_INTENSITY_DAYS = 250


def flow_basis(rows: list[dict]) -> tuple[list[dict], str]:
    """
    순위·임계값에 쓸 기준. 거래대금이 대부분 있으면 '순매수 강도'(거래대금 대비 %), 강도 기준이면 거래대금이 없는 날은 뺀다.
    네이버 과거 페이지만 실패하면 다음 금융 행(거래대금 없음)이 앞쪽에 붙는다 — 그때는 거래대금이 있는
    최근 구간이 FLOW_MIN_INTENSITY_DAYS 이상이면 그 구간만 써서 강도 기준을 유지한다(같은 날 판정이 뒤집히지 않게).
    그것도 안 되면 금액 기준.
    """
    with_tv = [r for r in rows if r.get("tradingValue")]
    if rows and len(with_tv) >= len(rows) * 0.9:
        return with_tv, "intensity"
    tail: list[dict] = []
    for r in reversed(rows):
        if not r.get("tradingValue"):
            break
        tail.append(r)
    if len(tail) >= FLOW_MIN_INTENSITY_DAYS:
        return tail[::-1], "intensity"
    return rows, "amount"


def flow_scale(rows: list[dict], basis: str, key: str) -> list[float]:
    """순매수를 기준에 맞는 값으로. 강도 기준이면 그날 거래대금 대비 %."""
    if basis == "intensity":
        return [(r[key] or 0.0) / r["tradingValue"] * 100 for r in rows]
    return [r[key] or 0.0 for r in rows]


# 같은 계산(개인 거래대금 대비 순매수 상·하위 20% → 20거래일 뒤 지수 − 모든 날 평균, 블록 부트스트랩 90% 범위)을
# 2009-03~2023-08 코스피 3,572거래일에 돌린 결과(2026-10-05 표본 외 검증). 사이트 표본 3년은 상승장이라 이보다 크게 나온다.
# 화면이 '이 3년'의 숫자만 보여 주면 방향 정보처럼 읽히므로 함께 싣는다. 성적표 표본을 늘리면 이 상수는 계산값으로 바꾼다.
LONG_RUN_CONTRARIAN = {
    "sell": {"period": "2009-03~2023-08", "excess": 0.22, "ci": [-0.11, 0.57], "significant": False},
    "buy": {"period": "2009-03~2023-08", "excess": -0.20, "ci": [-0.62, 0.19], "significant": False},
}


def build_ant(full: dict, closes: dict, code: str = "KOSPI") -> dict:
    """
    '개미는 정말 반대로 움직이고, 그래서 틀렸는가' 를 실제 데이터로 채점한다.
    상관관계일 뿐 인과가 아니며, 표본 기간의 장세에 크게 좌우된다는 점을 함께 실어 보낸다.

    순위·임계값(온도계 백분위, 크게 산 날, 흑역사)은 금액이 아니라 '순매수 강도'
    = 그날 거래대금 대비 순매수(%) 로 정한다. 3년 사이 거래대금이 2~3배 커져서, 금액으로 줄을 세우면
    최근 날짜만 극단으로 뽑힌다. 장중 잠정치도 분자·분모가 함께 부분값이라 비교가 덜 어긋난다.
    화면에 보이는 금액(억원)은 그대로 둔다.
    """
    px = closes.get(code) or {}
    rows, basis = flow_basis([r for r in (full.get(code) or []) if r["date"] in px])
    if basis == "amount" and rows:
        warn(f"{MARKET_NAME.get(code, code)} 성적표: 거래대금이 없는 날이 많아 금액 기준으로 계산합니다")
    if len(rows) < 60:
        warn(f"개미 성적표: {code} 표본 부족({len(rows)}일) — 건너뜀")
        return {}

    C = [px[r["date"]] for r in rows]
    S = {k: [r[k] or 0.0 for r in rows] for k in ACTOR_KEYS}           # 억원 (표시용)
    V = {k: flow_scale(rows, basis, k) for k in ACTOR_KEYS}            # 순위·임계값용
    n = len(rows)

    # 이후 h거래일 지수 수익률(%)
    fwd = {h: [(C[i + h] / C[i] - 1) * 100 for i in range(n - h)] for h in HORIZONS}
    baseline = {h: sum(fwd[h]) / len(fwd[h]) for h in HORIZONS}

    actors: dict = {}
    for k in ACTOR_KEYS:
        v = V[k]
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
        top["n20"] = sum(1 for i in heavy if i + 20 < n)     # 20일 뒤를 아는 날만 — r20 의 실제 표본

        excess = None if top["r20"] is None else round(top["r20"] - baseline[20], 2)
        ci = _excess_ci(fwd[20], [v[i] >= thr for i in range(n - 20)])
        actors[k] = {
            "timing": timing,
            "heavyBuy": top,
            "excess20": excess,
            "excessCI": list(ci) if ci else None,
            "significant": _significant(ci),
            "grade": _grade(excess, ci),
            "todayValue": S[k][-1],
            "todayIntensity": round(v[-1], 2) if basis == "intensity" else None,
            "todayPercentile": round(_percentile(v, v[-1]), 1),
        }

    # 오늘 개인의 매수강도가 극단이면, 과거 같은 구간의 20일 성적을 참고치로 붙인다
    iv = V["individual"]
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
            chosen = set(ii)
            r20 = sum(fwd[20][i] for i in ii) / len(ii)
            ci = _excess_ci(fwd[20], [i in chosen for i in range(n - 20)])
            bucket = {
                "label": label,
                "side": "buy" if p >= 80 else "sell",
                "n": len(ii),
                "r20": round(r20, 2),
                "baseline20": round(baseline[20], 2),
                "excess": round(r20 - baseline[20], 2),
                "excessCI": list(ci) if ci else None,
                "significant": _significant(ci),
            }
            if code == "KOSPI":
                bucket["longRun"] = LONG_RUN_CONTRARIAN["buy" if p >= 80 else "sell"]

    # 개인이 가장 강하게 사들였던 날들의 그 후 20일 — 같은 국면이 여러 번 뽑히지 않게 20거래일 간격
    hall = [
        {
            "date": rows[i]["date"],
            "amount": S["individual"][i],
            "intensity": round(iv[i], 2) if basis == "intensity" else None,
            "close": round(C[i], 2),
            "dayChange": round((C[i] / C[i - 1] - 1) * 100, 2) if i > 0 and C[i - 1] else None,
            "after20": round(C[i + 20], 2),
            "return20": round(fwd[20][i], 2),
        }
        for i in _pick_apart(sorted(range(n - 20), key=lambda i: -iv[i]), 20, 5)
    ]

    opp = {
        "vsForeign": round(sum(1 for a, b in zip(S["individual"], S["foreign"]) if a * b < 0) / n * 100, 1),
        "vsInstitution": round(sum(1 for a, b in zip(S["individual"], S["institution"]) if a * b < 0) / n * 100, 1),
    }

    # 연도별 분해 — 학점이 장세 탓인지 볼 수 있게 한다
    yearly = []
    for y in sorted({r["date"][:4] for r in rows}):
        yi = [i for i, r in enumerate(rows) if r["date"].startswith(y)]
        if len(yi) < 60:
            continue
        yv = [iv[i] for i in yi]
        y_fwd = [i for i in yi if i + 20 < n]
        if len(y_fwd) < 60:                       # _excess_ci 가 범위를 낼 수 있는 최소 길이
            continue
        y_base = sum(fwd[20][i] for i in y_fwd) / len(y_fwd)
        thr_y = sorted(yv)[int(len(yv) * 0.8)]
        hv = [i for i in y_fwd if iv[i] >= thr_y]
        if len(hv) < 8:
            continue
        y_r20 = sum(fwd[20][i] for i in hv) / len(hv)
        excess_y = y_r20 - y_base
        ci_y = _excess_ci([fwd[20][i] for i in y_fwd], [iv[i] >= thr_y for i in y_fwd])
        yearly.append({
            "year": y,
            "days": len(yi),
            "corrIF": round(_corr(yv, [V["foreign"][i] for i in yi]) or 0, 3),
            "baseline20": round(y_base, 2),
            "indivHeavyR20": round(y_r20, 2),
            "excess": round(excess_y, 2),
            "excessCI": list(ci_y) if ci_y else None,
            "significant": _significant(ci_y),
            "grade": _grade(excess_y, ci_y),
        })

    corr_if = round(_corr(V["individual"], V["foreign"]) or 0, 3)
    corr_ii = round(_corr(V["individual"], V["institution"]) or 0, 3)
    basis_note = ("'크게 산 날'과 온도계 백분위는 금액이 아니라 그날 거래대금 대비 순매수 비율로 정했습니다. "
                  "3년 사이 거래대금이 크게 늘어, 금액으로 줄을 세우면 최근 날짜만 뽑히기 때문입니다."
                  if basis == "intensity" else
                  "이번 수집에는 거래대금이 없어 '크게 산 날'과 온도계 백분위를 순매수 금액으로 정했습니다. "
                  "거래 규모가 커진 최근 날짜가 과대 대표될 수 있습니다.")
    return {
        "market": code,
        "basis": basis,
        "yearly": yearly,
        "sample": {"days": n, "from": rows[0]["date"], "to": rows[-1]["date"]},
        "baseline": {f"r{h}": round(baseline[h], 2) for h in HORIZONS},
        "correlation": {
            "individual_foreign": corr_if,
            "individual_institution": corr_ii,
            "foreign_institution": round(_corr(V["foreign"], V["institution"]) or 0, 3),
        },
        "oppositeRate": opp,
        "actors": actors,
        "contrarianRead": bucket,
        "hallOfFame": hall,
        "caveats": [
            f"표본은 {rows[0]['date']}~{rows[-1]['date']} {n}거래일뿐입니다. 다른 기간에는 다른 결과가 나옵니다. "
            "같은 계산을 2009-03~2023-08 코스피에 돌리면 '개인이 크게 판 날' 뒤 초과수익은 +0.22%p"
            "(90% 범위 -0.11~+0.57)로 시장 평균과 구분되지 않았고, 개인·외국인·기관이 크게 산 날의 학점은 그때도 모두 C였습니다.",
            "성적표는 각 주체의 실제 손익이 아닙니다. 크게 순매수한 날 이후 코스피 지수가 움직인 폭으로 "
            "'산 시점'만 채점한 것이라, 실제로 산 종목·체결 가격과는 다릅니다.",
            basis_note,
            "20거래일 뒤 수익률은 날마다 19일씩 겹쳐서, 실제로 독립적인 표본은 보이는 것보다 훨씬 적습니다. "
            "학점 옆 90% 범위가 0을 포함하면 시장 평균과 구분되지 않는다는 뜻이고, 그때는 C로 둡니다.",
            f"이 기간 지수의 20거래일 평균 수익률은 {baseline[20]:+.2f}% 였습니다. "
            "상승장에서는 '떨어질 때 사는' 쪽이 불리하게 보이기 쉽습니다.",
            "상관관계이지 인과관계가 아닙니다. 개인이 사서 떨어진 게 아니라, "
            "떨어지는 국면에서 개인이 사는 쪽에 서는 것에 가깝습니다.",
            "순매수 총합은 개인·외국인·기관·기타법인을 모두 더해야 0이 됩니다. 개인의 상대가 꼭 외국인일 필요는 없으므로 "
            f"개인↔외국인 상관({corr_if:+.2f})은 산수가 아니라 이 기간에 관찰된 사실입니다(개인↔기관 {corr_ii:+.2f}).",
        ],
    }


# ---------------------------------------------------------------- 유사 국면 매칭

ANALOG_FEATURES = [
    ("indiv",    "개인 순매수 강도"),
    ("foreign",  "외국인 순매수 강도"),
    ("inst",     "기관 순매수 강도"),
    ("indiv5",   "개인 5일 누적 강도"),
    ("foreign5", "외국인 5일 누적 강도"),
    ("ret1",     "당일 등락률"),
    ("ret5",     "5일 등락률"),
    ("vol5",     "5일 변동성"),
]
# 수급 특징 5개와 가격 특징 3개가 거리에서 같은 비중을 갖도록 (5 × 0.6 = 3 × 1.0).
# 개인과 외국인 순매수는 상관이 −0.8 이라, 그대로 두면 수급이 사실상 두 번 세어진다.
ANALOG_WEIGHTS = [0.6] * 5 + [1.0] * 3
ANALOG_GAP = 20      # 매칭일끼리 20거래일 이상 떨어뜨려 '20일 뒤' 결과 구간이 겹치지 않게


def build_analog(full: dict, closes: dict, code: str = "KOSPI") -> dict:
    """
    오늘의 (수급 + 가격 움직임) 조합과 가장 비슷했던 과거의 날들을 찾는다.
    예측이 아니라 '과거에 비슷한 날은 이후 어땠나'의 기록이다.
    - 수급 특징은 순매수 강도(거래대금 대비 %) — 금액이면 거래 규모가 커진 최근 날짜만 닮은 날로 뽑힌다
    - 특징마다 z-score 로 맞춘 뒤, 수급 묶음과 가격 묶음이 같은 비중이 되게 가중한 유클리드 거리
    - 오늘의 가장 가까운 거리가 평소(다른 날들의 최근접 거리) 상위 10% 보다 멀면 '닮은 날이 드물다'
    - 판정은 평균 하나가 아니라 5일 중 몇 번이 시장 평균보다 좋았는지로
    """
    px = closes.get(code) or {}
    rows, basis = flow_basis([r for r in (full.get(code) or []) if r["date"] in px])
    n = len(rows)
    if n < 120:
        warn(f"유사 국면: {code} 표본 부족({n}일) — 건너뜀")
        return {}

    C = [px[r["date"]] for r in rows]
    amt = {k: [r[k] or 0.0 for r in rows] for k in ACTOR_KEYS}
    I, F, O = (flow_scale(rows, basis, k) for k in ACTOR_KEYS)
    ret1 = [0.0] + [(C[i] / C[i - 1] - 1) * 100 for i in range(1, n)]
    ret5 = [0.0] * 5 + [(C[i] / C[i - 5] - 1) * 100 for i in range(5, n)]

    def sum5(v, i):
        return sum(v[max(0, i - 4): i + 1])

    if basis == "intensity":
        tv = [r["tradingValue"] for r in rows]
        i5 = [sum5(amt["individual"], i) / sum5(tv, i) * 100 for i in range(n)]
        f5 = [sum5(amt["foreign"], i) / sum5(tv, i) * 100 for i in range(n)]
    else:
        i5 = [sum5(amt["individual"], i) for i in range(n)]
        f5 = [sum5(amt["foreign"], i) for i in range(n)]
    vol5 = []
    for i in range(n):
        w = ret1[max(0, i - 4): i + 1]
        m = sum(w) / len(w)
        vol5.append((sum((x - m) ** 2 for x in w) / len(w)) ** 0.5)

    def zscore(v):
        m = sum(v) / len(v)
        s = (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5 or 1.0
        return [(x - m) / s for x in v]

    Z = [zscore(v) for v in (I, F, O, i5, f5, ret1, ret5, vol5)]

    def dist(a, b):
        return sum(w * (z[a] - z[b]) ** 2 for w, z in zip(ANALOG_WEIGHTS, Z)) ** 0.5

    today = n - 1
    pool = range(5, n - 20)              # 5일 워밍업 이후 ~ 20일 뒤 결과를 아는 날까지
    cands = sorted((dist(i, today), i) for i in pool)
    dmap = {i: d for d, i in cands}
    picked = _pick_apart([i for _, i in cands], ANALOG_GAP, 5)
    if not picked:
        return {}

    # 평소에 '가장 가까운 날'은 얼마나 가까운가 — 오늘과 같은 조건(20거래일 이상 앞선 날만)으로 구한
    # 최근접 거리 분포. 후보가 너무 적은 초반 날짜는 거리가 부풀므로 뺀다.
    nearest_typical = sorted(
        min(dist(i, j) for i in range(5, j - ANALOG_GAP + 1))
        for j in range(120, n, 5)
    ) or [dmap[picked[0]]]
    p90 = nearest_typical[int(len(nearest_typical) * 0.9)]
    median = nearest_typical[len(nearest_typical) // 2]

    fwd20 = [(C[i + 20] / C[i] - 1) * 100 for i in range(n - 20)]
    baseline20 = sum(fwd20) / len(fwd20)
    matches = [
        {
            "date": rows[i]["date"],
            "distance": round(dmap[i], 3),
            "close": round(C[i], 2),
            "ret1": round(ret1[i], 2),
            "individual": amt["individual"][i],
            "foreign": amt["foreign"][i],
            "ret20": round(fwd20[i], 2),
        }
        for i in picked
    ]
    outs = [m["ret20"] for m in matches]
    above = sum(1 for x in outs if x > baseline20)
    return {
        "market": code,
        "basis": basis,
        "today": {
            "date": rows[today]["date"], "close": round(C[today], 2),
            "individual": amt["individual"][today], "foreign": amt["foreign"][today],
            "institution": amt["institution"][today],
            "ret1": round(ret1[today], 2), "ret5": round(ret5[today], 2),
        },
        "matches": matches,
        "avgRet20": round(sum(outs) / len(outs), 2),
        "minRet20": min(outs), "maxRet20": max(outs),
        "above": above,
        "verdict": "better" if above >= len(outs) - 1 else "worse" if above <= 1 else "mixed",
        "similarity": {
            "nearest": round(dmap[picked[0]], 3),
            "typical": round(median, 3),
            "p90": round(p90, 3),
            "rare": dmap[picked[0]] > p90,
        },
        "baseline20": round(baseline20, 2),
        "sample": {"days": n, "from": rows[0]["date"], "to": rows[-1]["date"]},
    }


# ---------------------------------------------------------------- 해석 레이어

def rank_text(p: float) -> str:
    """상·하위 비율(%) 표기. 0.5 미만이 '0%' 로 보이지 않게."""
    return "1% 미만" if p < 1 else f"{p:.0f}%"


def _day_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({_WEEKDAY_KO[d.weekday()]})"


def build_insights(flows: dict, market: dict, glob: dict, ant: dict,
                   futures: dict | None = None, credit: dict | None = None,
                   program: dict | None = None, today: str | None = None) -> list[dict]:
    """
    수치에서 바로 읽히는 사실만 문장으로. 예측이나 매매 조언은 하지 않는다.
    입력은 모두 '이번 수집분'이어야 한다 — 직전 정상본을 넣으면 지난 날의 일을 '오늘'로 말하게 된다.
    수급이 비어도 지수·글로벌·빚투 문장은 따로 나간다.
    """
    tips: list[dict] = []

    def cho(v):
        return f"{v / 10000:.2f}조" if abs(v) >= 10000 else f"{abs(v):,.0f}억"

    today = today or datetime.now(KST).date().isoformat()
    ks = flows.get("markets", {}).get("KOSPI")
    day = (ks or {}).get("latest", {}).get("date") or today
    when = "오늘" if day == today else _day_label(day)          # 휴장일·다음 날 아침엔 '10/2(금)'
    when_topic = "오늘은" if day == today else f"{_day_label(day)}에는"
    if ks:
        latest, st = ks["latest"], ks["streaks"]
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
                         "text": f"{when_topic} 외국인이 판 물량({cho(abs(f))}원)을 개인이 받아내는 구도였습니다."})
        elif f > 0 and i < 0:
            tips.append({"tone": "neutral",
                         "text": f"{when_topic} 외국인이 사고({cho(f)}원) 개인이 파는 구도였습니다."})
        if i < 0 and f < 0:
            # 개인·외국인이 함께 판 물량을 받은 쪽 — 기관만이 아니라 기타법인도 본다(최근엔 기타법인이 더 큰 날이 많다)
            def takers(r):
                got = [(nm, r.get(k) or 0) for k, nm in (("institution", "기관"), ("other_corp", "기타법인"))]
                return tuple(nm for nm, v in sorted(got, key=lambda x: -x[1]) if v > 0)

            who = takers(latest)
            if who:
                days = ks.get("daily") or []
                same = sum(1 for r in days if (r.get("individual") or 0) < 0 and (r.get("foreign") or 0) < 0
                           and set(takers(r)) == set(who))
                took = f"{who[0]} 홀로" if len(who) == 1 else f"{who[0]}과 {who[1]}이"   # 기관·기타법인 모두 받침 ㄴ
                tips.append({"tone": "neutral",
                             "text": f"개인과 외국인이 동시에 팔고 {took} 받았습니다. "
                                     f"최근 {len(days)}거래일 중 {same}일 있었던 조합입니다."})

        det = {k: latest.get(k) or 0 for k in ("inst_pension", "inst_trust", "inst_fin_inv")}
        top = max(det, key=lambda k: abs(det[k]))
        if abs(det[top]) > 1000:
            nm = {"inst_pension": "연기금", "inst_trust": "투신", "inst_fin_inv": "금융투자(증권)"}[top]
            tips.append({"tone": "buy" if det[top] > 0 else "sell",
                         "text": f"기관 안에서는 {nm}이 {cho(abs(det[top]))}원 "
                                 f"{'순매수' if det[top] > 0 else '순매도'}로 가장 크게 움직였습니다."})

    # 프로그램 매매 — 최근 1년 중 극단일 때만
    pg = ((program or {}).get("markets") or {}).get("KOSPI")
    if pg:
        lt = pg["latest"]
        q = lt.get("pctl")
        buy = (lt.get("total") or 0) > 0
        if q is not None and ((buy and q >= 95) or (not buy and q <= 5)) and abs(lt.get("total") or 0) >= 1000:
            tips.append({"tone": "neutral", "text":
                f"코스피 프로그램 매매가 {cho(abs(lt['total']))}원 {'순매수' if buy else '순매도'}"
                f"(비차익 {'+' if lt['nonarb'] >= 0 else '−'}{cho(abs(lt['nonarb']))}원)입니다. "
                f"최근 {lt['days']}거래일 중 {'순매수' if buy else '순매도'} 쪽 상위 {rank_text(100 - q if buy else q)} 규모"
                + (" — 20시 확정 전 잠정치입니다." if lt.get("provisional") else "입니다.")})

    kospi = market.get("indices", {}).get("KOSPI", {})
    if kospi.get("changeRate") is not None and abs(kospi["changeRate"]) >= 3:
        tips.append({"tone": "sell" if kospi["changeRate"] < 0 else "buy",
                     "text": f"코스피가 {kospi['changeRate']:+.2f}% 움직였습니다. "
                             f"변동성이 매우 큰 국면입니다."})

    g = {x["name"]: x for x in glob.get("items", [])}
    sox = g.get("필라델피아 반도체")
    if sox and sox.get("changeRate") is not None and abs(sox["changeRate"]) >= 2:
        us_day = str(sox.get("asOf") or "")[:10]
        yday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        night = "간밤" if us_day in ("", yday, today) else f"{_day_label(us_day)} 미국장에서"
        tips.append({"tone": "neutral",
                     "text": f"{night} 필라델피아 반도체 지수가 {sox['changeRate']:+.2f}%. "
                             f"2009~2023년 코스피 시가 갭과 함께 움직였지만(상관 0.64), 시가 이후 방향과는 관계가 없었습니다."})
    fx = g.get("원/달러 환율")
    if fx and fx.get("price"):
        chg = f" (전일 대비 {fx['changeRate']:+.2f}%)" if fx.get("changeRate") is not None else ""
        tips.append({"tone": "neutral", "text": f"원/달러 {fx['price']:,.1f}원{chg}."})
    vix = g.get("VIX 공포지수")
    if vix and vix.get("price"):
        lvl = "공포 구간" if vix["price"] >= 30 else ("경계 구간" if vix["price"] >= 20 else "안정 구간")
        tips.append({"tone": "neutral", "text": f"VIX {vix['price']:.1f} — {lvl}입니다."})

    # 개미 관점 한 줄
    if ant:
        ia = ant["actors"]["individual"]
        pctl = ia["todayPercentile"]
        days = ant["sample"]["days"]
        how = (f"(거래대금 대비 순매수 {ia['todayIntensity']:+.1f}%)" if ia.get("todayIntensity") is not None else "")
        if pctl >= 80:
            tips.append({"tone": "neutral", "text":
                f"{when} 개미의 매수 강도{how}는 최근 {days}거래일 중 상위 {rank_text(100 - pctl)} 수준입니다."})
        elif pctl <= 20:
            tips.append({"tone": "neutral", "text":
                f"{when} 개미의 매도 강도{how}는 최근 {days}거래일 중 상위 {rank_text(pctl)} 수준입니다."})
        # 과거 '비슷한 날' 뒤의 지수 기록은 브리핑에 싣지 않는다 — 3년 표본에선 커 보이지만
        # 2009~2023 같은 계산에선 시장 평균과 구분되지 않았다(LONG_RUN_CONTRARIAN). 온도계 카드에서 단서와 함께만 보여 준다.

    # 현물 ↔ 선물 다이버전스
    div = (futures or {}).get("divergence")
    if div and div.get("state") == "split":
        spot_dir = "팔면서" if div["spotForeign"] < 0 else "사면서"
        fut_dir = "사고" if div["futuresForeign"] > 0 else "팔고"
        # 과거 갈림 뒤의 수익률은 검정하지 않은 숫자라 싣지 않는다(사실만)
        tips.append({"tone": "neutral", "text":
            f"최근 {div['window']}거래일 외국인이 현물은 {cho(abs(div['spotForeign']))}원 {spot_dir} "
            f"선물은 {abs(div['futuresForeign']):,}계약 {fut_dir} 있습니다."})

    # 빚투·반대매매
    cl = (credit or {}).get("latest", {})
    if cl.get("loans"):
        ln = cl["loans"]
        q = ln.get("d5Pctl")
        if ln.get("d5") is not None and q is not None and ((ln["d5"] > 0 and q >= 90) or (ln["d5"] < 0 and q <= 10)):
            up = ln["d5"] > 0
            tips.append({"tone": "neutral", "text":
                f"신용융자(빚투) 잔고가 5거래일 만에 {cho(abs(ln['d5']))}원 {'늘었습니다' if up else '줄었습니다'}. "
                f"최근 {ln.get('days')}거래일의 5일 변화 가운데 {'가장 크게 는 쪽' if up else '가장 크게 준 쪽'} "
                f"{rank_text(100 - q if up else q)}입니다. 현재 {cho(ln['total'])}원. "
                + ("빚으로 산 물량은 주가가 빠지면 반대매매로 나올 수 있습니다." if up else "레버리지가 정리되는 중입니다.")})
    if cl.get("money"):
        m = cl["money"]
        q = m.get("liqPctl")
        if m.get("liquidation") is not None and m.get("liqAvg20") and q is not None:
            ratio = m["liquidation"] / m["liqAvg20"]
            if ratio >= 2 and q >= 80:
                tips.append({"tone": "neutral", "text":
                    f"반대매매가 {cho(m['liquidation'])}원 — 최근 20일 평균({cho(m['liqAvg20'])}원)의 "
                    f"{ratio:.1f}배이고, 최근 {m.get('days')}거래일 중 상위 {rank_text(100 - q)} 규모입니다. "
                    f"빚으로 산 물량이 강제로 정리되고 있습니다."})
    return tips


# ---------------------------------------------------------------- 하루 요약 피드 (RSS·텔레그램)

SITE_URL = "https://coodo225.github.io/ant-mirror/"
FEED_DAYS = 30


def _eok_text(v) -> str:
    if v is None:
        return "—"
    return f"{v / 10000:+.2f}조" if abs(v) >= 10000 else f"{v:+,.0f}억"


def build_feed(prev: dict, flows: dict | None, market: dict, insights: list[dict], now: datetime,
               pre: dict | None = None) -> dict:
    """
    거래일마다 '마감 후'(id 날짜-post) 한 항목, 장 전 실행이 있으면 '장 전'(id 날짜-pre) 한 항목.
    마감 후 항목은 수급 기준일 기준으로 같은 날은 실행마다 덮어쓴다. 20:05(수급·프로그램 확정) 이후 수집분이면 final.
    flows 가 없으면(이번 수집 실패) 마감 후 항목은 직전 그대로.
    """
    entries = {}
    for e in (prev or {}).get("entries", []):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", e.get("id", "")):      # 예전 형식(날짜만) → 마감 후 항목
            e = {**e, "id": f"{e['id']}-post", "kind": "post"}
        entries[e["id"]] = e
    if pre:
        entries[pre["id"]] = pre
    ks = (flows or {}).get("markets", {}).get("KOSPI")
    if ks:
        latest = ks["latest"]
        day = latest["date"]
        final = now.date().isoformat() > day or now.strftime("%H%M") >= PROGRAM_FINAL_AFTER   # 수급 20시, 프로그램 20:05
        idx = market.get("indices", {}).get("KOSPI", {})
        head = (f"코스피 {idx['price']:,.2f} ({idx.get('changeRate') or 0:+.2f}%) · " if idx.get("price") else "")
        title = (f"{day} {head}개인 {_eok_text(latest.get('individual'))} · "
                 f"외국인 {_eok_text(latest.get('foreign'))} · 기관 {_eok_text(latest.get('institution'))}"
                 + ("" if final else " (잠정)"))
        old = entries.get(f"{day}-post") or {}
        summary = "\n".join(t["text"] for t in insights)
        same = (old.get("title"), old.get("summary"), old.get("final")) == (title, summary, final)
        entries[f"{day}-post"] = {
            "id": f"{day}-post", "kind": "post", "date": day, "title": title, "final": final,
            "summary": summary,
            "updated": old["updated"] if same and old.get("updated") else now.isoformat(timespec="seconds"),
            "sent": bool(old.get("sent")) and bool(old.get("final")),   # 확정본을 보낸 적이 있을 때만 '보냄'
        }
    # 날짜 내림차순, 같은 날이면 마감 후가 장 전보다 위
    ordered = sorted(entries.values(), key=lambda e: (e.get("date") or e["id"][:10], 1 if e.get("kind", "post") == "post" else 0),
                     reverse=True)[:FEED_DAYS * 2]
    return {"title": "개미들을 위한 투자정보 — 하루 요약", "link": SITE_URL, "entries": ordered}


def build_pre_entry(now: datetime, flows: dict, glob: dict, events: dict, today: dict) -> dict | None:
    """장 전(거래일 08:59 까지) 요약: 간밤 4칸, 지난 장 확정 수급, 오늘·내일 일정, 평소와 다른 것."""
    d = now.date()
    if not is_trading_day(d) or now.strftime("%H%M") >= "0900":
        return None
    by_sym = {x.get("symbol"): x for x in (glob or {}).get("items", [])}
    cells = []
    for sym in (glob or {}).get("preopen") or PREOPEN_SYMBOLS:
        x = by_sym.get(sym)
        if x and x.get("changeRate") is not None:
            cells.append(f"{x['name']} {x['changeRate']:+.2f}%")
    ks = (flows.get("markets") or {}).get("KOSPI") or {}
    lt = ks.get("latest") or {}
    lines = []
    if lt:
        lines.append(f"지난 장({_day_label(lt['date'])}) 코스피 수급: 개인 {_eok_text(lt.get('individual'))} · "
                     f"외국인 {_eok_text(lt.get('foreign'))} · 기관 {_eok_text(lt.get('institution'))} · "
                     f"기타법인 {_eok_text(lt.get('other_corp'))}")
    if cells:
        lines.append("간밤: " + " · ".join(cells) + " — 시가 갭과는 함께 움직였지만 장중 방향 정보는 아닙니다")
    soon = [e for e in (events or {}).get("upcoming", []) if e.get("dday") in (0, 1)]
    for e in soon:
        lines.append(f"{'오늘' if e['dday'] == 0 else '내일'} {e['title']}" + (f" ({e['note']})" if e.get("note") else ""))
    for it in (today or {}).get("items", []):
        lines.append(f"평소와 다른 것: {it['text']} — {it['detail']}")
    title = f"{_day_label(d.isoformat())} 장 전 · " + (" · ".join(cells[:2]) if cells else "간밤 지표 없음")
    return {"id": f"{d.isoformat()}-pre", "kind": "pre", "date": d.isoformat(), "title": title, "final": True,
            "summary": "\n".join(lines), "updated": now.isoformat(timespec="seconds"), "sent": False}


def feed_xml(feed: dict) -> str:
    """Atom 1.0. 요약 본문은 줄바꿈을 살린 텍스트."""
    from xml.sax.saxutils import escape
    entries = feed.get("entries", [])
    updated = entries[0]["updated"] if entries else datetime.now(KST).isoformat(timespec="seconds")
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{escape(feed['title'])}</title>",
        f'  <link href="{escape(SITE_URL)}"/>',
        f'  <link rel="self" href="{escape(SITE_URL)}data/feed.xml"/>',
        f"  <id>{escape(SITE_URL)}</id>",
        f"  <updated>{escape(updated)}</updated>",
        "  <author><name>개미들을 위한 투자정보</name></author>",
    ]
    for e in entries:
        parts += [
            "  <entry>",
            f"    <title>{escape(e['title'])}</title>",
            f'    <link href="{escape(SITE_URL)}"/>',
            f"    <id>{escape(SITE_URL)}#{escape(e['id'])}</id>",
            f"    <updated>{escape(e['updated'])}</updated>",
            f'    <content type="text">{escape(e["summary"])}\n\n투자 조언이 아닙니다.</content>',
            "  </entry>",
        ]
    parts.append("</feed>")
    return "\n".join(parts) + "\n"


def notify_telegram(feed: dict) -> None:
    """
    선택: TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID 가 있으면 그날 확정 요약을 한 번만 보낸다.
    보낸 기록(sent)은 feed.json 에 남아 다음 실행에서 다시 보내지 않는다.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    entries = feed.get("entries") or []
    if not (token and chat and entries):
        return
    SECRETS.add(token)
    e = next((x for x in entries if x.get("kind", "post") == "post"), None)
    if not e or not e.get("final") or e.get("sent"):
        return
    try:
        r = session.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, json={
            "chat_id": chat, "disable_web_page_preview": True,
            "text": f"{e['title']}\n\n{e['summary']}\n\n{SITE_URL}",
        })
        if r.status_code == 200 and r.json().get("ok"):
            e["sent"] = True
            print("  텔레그램 요약 보냄")
        else:
            warn(f"텔레그램 알림 실패: HTTP {r.status_code}")
    except Exception as ex:  # noqa: BLE001
        warn(f"텔레그램 알림 실패: {type(ex).__name__}")


# ---------------------------------------------------------------- OG 공유 카드

def find_korean_font() -> tuple[str | None, str | None]:
    """(굵은 폰트, 보통 폰트) 경로. 윈도우/리눅스(Actions) 겸용. Actions 는 OG_FONT_DIR 에 나눔고딕을 받아 둔다."""
    font_dir = os.environ.get("OG_FONT_DIR", "").strip()
    candidates = [
        *([(f"{font_dir}/NanumGothic-Bold.ttf", f"{font_dir}/NanumGothic-Regular.ttf")] if font_dir else []),
        ("C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf"),
        ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
         "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
         "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for bold, regular in candidates:
        if Path(bold).exists() and Path(regular).exists():
            return bold, regular
    return None, None


def build_og_card(flows: dict, market: dict, ant: dict) -> None:
    """카톡/트위터 공유 미리보기용 1200×630 PNG. 실패해도 치명적이지 않다."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        warn("Pillow 미설치 — OG 카드 생략 (pip install pillow)")
        return
    bold_path, reg_path = find_korean_font()
    if not bold_path:
        warn("한글 폰트를 찾지 못해 OG 카드 생략")
        return

    W, H = 1200, 630
    BG, CARD, LINE = (11, 14, 20), (21, 27, 43), (37, 45, 66)
    TEXT, DIM = (232, 236, 245), (139, 150, 173)
    UP, DOWN = (255, 77, 77), (77, 148, 255)
    COLORS = {"individual": (255, 176, 46), "foreign": (79, 195, 247), "institution": (179, 136, 255)}
    NAMES = {"individual": "개인", "foreign": "외국인", "institution": "기관"}

    def font(size, b=True):
        return ImageFont.truetype(bold_path if b else reg_path, size)

    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)

    ks = flows.get("markets", {}).get("KOSPI", {})
    latest = ks.get("latest") or {}
    idx = market.get("indices", {}).get("KOSPI", {})

    # 헤더
    dr.text((60, 48), "개미들을 위한 투자정보", font=font(46), fill=(255, 208, 138))
    date_txt = latest.get("date", "")
    day_txt = _day_label(date_txt) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date_txt)) else date_txt
    dr.text((60, 112), f"{day_txt} · 한국 증시에서 누가 사고 누가 팔았나",
            font=font(24, b=False), fill=DIM)

    # 코스피
    price = idx.get("price")
    rate = idx.get("changeRate")
    if price is not None:
        col = UP if (rate or 0) > 0 else DOWN if (rate or 0) < 0 else DIM
        dr.text((60, 180), "코스피", font=font(26, b=False), fill=DIM)
        dr.text((60, 214), f"{price:,.2f}", font=font(64), fill=TEXT)
        if rate is not None:
            arrow = "▲" if rate > 0 else ("▼" if rate < 0 else "─")
            dr.text((60, 292), f"{arrow} {abs(idx.get('change') or 0):,.2f} ({rate:+.2f}%)",
                    font=font(30), fill=col)

    # 3주체 가로 막대
    vals = {k: latest.get(k) or 0 for k in ("individual", "foreign", "institution")}
    vmax = max(abs(v) for v in vals.values()) or 1
    bx, bw_half = 640, 220     # 중앙축 x=640+220=860, 트랙 860±220
    cx = bx + bw_half
    y = 190
    dr.text((bx, 140), "그날의 수급 (억원)", font=font(24, b=False), fill=DIM)
    for k in ("individual", "foreign", "institution"):
        v = vals[k]
        half = abs(v) / vmax * bw_half
        dr.text((bx - 115, y + 6), NAMES[k], font=font(26), fill=COLORS[k])
        dr.rectangle([cx - bw_half, y, cx + bw_half, y + 40], fill=(13, 18, 32), outline=LINE)
        if v >= 0:
            dr.rectangle([cx, y + 4, cx + max(half, 3), y + 36], fill=COLORS[k])
        else:
            dr.rectangle([cx - max(half, 3), y + 4, cx, y + 36], fill=COLORS[k])
        amt = f"{v/10000:+.2f}조" if abs(v) >= 10000 else f"{v:+,.0f}억"
        dr.text((cx + bw_half + 16, y + 6), amt, font=font(24), fill=TEXT)
        dr.line([cx, y, cx, y + 40], fill=(74, 85, 112), width=2)
        y += 62

    # 개미 온도계
    if ant and ant.get("actors"):
        p = ant["actors"]["individual"]["todayPercentile"]
        ty = 430
        dr.text((60, ty), f"개미 온도계 — 최근 {ant['sample']['days']}거래일 중 {max(p, 1):.0f}번째 백분위"
                          f"{' (거래대금 대비)' if ant.get('basis') == 'intensity' else ''}",
                font=font(26), fill=TEXT)
        track_y = ty + 52
        dr.rounded_rectangle([60, track_y, 1140, track_y + 22], radius=11, fill=(13, 18, 32), outline=LINE)
        dr.rounded_rectangle([60, track_y, 60 + 216, track_y + 22], radius=11, fill=(58, 96, 156))
        dr.rounded_rectangle([1140 - 216, track_y, 1140, track_y + 22], radius=11, fill=(156, 62, 62))
        ax = 60 + (1140 - 60) * p / 100
        dr.ellipse([ax - 18, track_y - 8, ax + 18, track_y + 30], fill=(255, 176, 46))
        dr.text((60, track_y + 40), "강한 매도", font=font(20, b=False), fill=DOWN)
        dr.text((1040, track_y + 40), "강한 매수", font=font(20, b=False), fill=UP)

    dr.text((60, H - 46), "coodo225.github.io/ant-mirror · 투자 조언이 아닙니다",
            font=font(20, b=False), fill=(93, 103, 128))

    out = ROOT / "docs" / "og.png"
    img.save(out, "PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes)")


# ---------------------------------------------------------------- 외국인·기관 종목 순위 (마감 확정치)
# stock.naver.com 외국인·기관 매매 상위 화면이 쓰는 공개 JSON. 외국인·기관만 있고 개인은 400.
# 장중 외국인 DAY 는 외국계 창구 '추정'(estimated=true)이라 확정치와 부호까지 다를 수 있어 쓰지 않는다.
# KRX 체결분만(NXT 제외)이고 ETF 가 섞여 있어 주식(ST)만 남긴다. 상·하위 100 중 20개씩.

NAVER_RANK_URL = "https://stock.naver.com/api/domestic/market/trend/trendForeignOrg"
RANK_REFERER = {"FOREIGNER": "https://stock.naver.com/market/stock/kr/trend/foreigner",
                "ORGANIZATION": "https://stock.naver.com/market/stock/kr/trend/organization"}
RANK_KEEP = 20
RANK_FINAL_AFTER = "2005"   # 그날 순위를 확정치로 받는 시각 — 투자자별 값은 20시 무렵까지 바뀐다(수급·프로그램과 같게)


def _int(v) -> int | None:
    n = num(v)
    return int(n) if n is not None else None


def fetch_rank(investor: str, market: str, period: str) -> dict:
    if investor not in RANK_REFERER or market not in ("KOSPI", "KOSDAQ"):
        raise ValueError((investor, market))
    j = get_json(NAVER_RANK_URL, headers={"Referer": RANK_REFERER[investor]},
                 params={"investorType": investor, "tradeType": "KRX", "marketType": market,
                         "startIdx": 0, "pageSize": 100, "periodType": period})
    sec = j.get("sections") if isinstance(j, dict) else None
    if not isinstance(sec, dict):
        raise RuntimeError(f"응답 형식이 바뀜: {str(j)[:120]}")
    first, lists = None, {}
    for side, key in (("buy", "buyRankList"), ("sell", "sellRankList")):
        rows = []
        for x in sec.get(key) or []:
            first = first or x
            if x.get("type") != "ST":
                continue
            q, a, vol = _int(x.get("accTradeVolume")), num(x.get("accTradeAmount")), _int(x.get("dailyTradeVolume"))
            if q is None or a is None:
                continue
            if (a > 0) != (side == "buy") and a != 0:
                raise RuntimeError(f"{key} 에 부호가 다른 값({x.get('itemname')} {a}) — 형식 변경 의심")
            rows.append({"code": str(x.get("itemcode") or ""), "name": str(x.get("itemname") or ""),
                         "amt": round(a / 1e8), "qty": q,
                         "volPct": round(abs(q) / vol * 100, 1) if vol else None,
                         "chg": num(x.get("prevChangeRate"))})
        rows.sort(key=lambda r_: -r_["amt"] if side == "buy" else r_["amt"])
        lists[side] = rows[:RANK_KEEP]
    if not first:
        raise RuntimeError("순위가 비어 있음")
    iso = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d) else None
    return {"from": iso(str(first.get("bizdateFrom") or "")), "to": iso(str(first.get("bizdateTo") or "")),
            "estimated": first.get("estimated") is True, **lists}


def build_ranks(prev: dict | None, now: datetime, phase: str, streaks: dict | None = None) -> dict | None:
    """
    외국인·기관 × 코스피·코스닥 × 하루·1주 순위. 장중엔 받지 않고 직전 확정본을 쓴다(final=False 로 '전 거래일 확정' 표시).
    그날 순위는 RANK_FINAL_AFTER 뒤에만 받아들이고, 그 전 값·추정 값은 직전 것을 둔다. 한 목록이 실패하면 그 목록만 직전 것.
    streaks: {code: {'foreign': {days, side}, 'institution': {...}}} — 수집 범위 안 종목이면 행에 연속 일수를 붙인다.
    """
    prev = prev if isinstance(prev, dict) else {}
    today = now.date().isoformat()
    live = phase in ("장중", "동시호가")
    out = {"scope": "KRX 체결분(NXT 제외) · 주식만(ETF 제외)", "markets": {}}
    pending = live
    if not live:
        for mkt in ("KOSPI", "KOSDAQ"):
            for inv, key in (("FOREIGNER", "foreign"), ("ORGANIZATION", "institution")):
                for per, pk in (("DAY", "day"), ("WEEK", "week")):
                    try:
                        r = fetch_rank(inv, mkt, per)
                        time.sleep(0.3)
                    except Exception as e:  # noqa: BLE001
                        warn(f"{MARKET_NAME[mkt]} {'외국인' if key == 'foreign' else '기관'} 순위({per}) 실패: {type(e).__name__}: {e}")
                        continue
                    if r["estimated"] or not r["to"] or (r["to"] == today and now.strftime("%H%M") < RANK_FINAL_AFTER):
                        pending = True                  # 오늘 값이 아직 확정 전 — 직전 확정본을 둔다
                        continue
                    out["markets"].setdefault(mkt, {}).setdefault(key, {})[pk] = {
                        k: r[k] for k in ("from", "to", "buy", "sell")}
    # 빠진 목록은 직전 확정본으로
    for mkt, invs in (prev.get("markets") or {}).items():
        for key, pers in (invs or {}).items():
            for pk, lst in (pers or {}).items():
                out["markets"].setdefault(mkt, {}).setdefault(key, {}).setdefault(pk, lst)
    days = sorted({v["day"]["to"] for m in out["markets"].values() for v in m.values() if v.get("day", {}).get("to")})
    if not days:
        return None
    out["asOf"] = days[-1]
    out["final"] = not pending
    # 이번에 못 받아 직전 것으로 채운 목록이 더 옛날이면 표시한다(카드가 '확정'이라고만 하지 않게)
    mixed = []
    names = {"foreign": "외국인", "institution": "기관", "day": "하루", "week": "1주"}
    for mkt, m in out["markets"].items():
        for key, pers in m.items():
            for pk, lst in pers.items():
                lst.pop("stale", None)
                if pk == "day" and lst.get("to") and lst["to"] < out["asOf"]:
                    lst["stale"] = True
                    mixed.append(f"{MARKET_NAME.get(mkt, mkt)} {names[key]} {names[pk]}")
    if mixed:
        out["mixed"] = mixed
        mark_stale("ranks", f"종목 순위 일부({', '.join(mixed)})", asOf=min(
            lst["to"] for m in out["markets"].values() for pers in m.values() for lst in pers.values() if lst.get("stale")))
    if streaks:
        for m in out["markets"].values():
            for key, pers in m.items():
                for lst in pers.values():
                    for side in ("buy", "sell"):
                        for row in lst.get(side) or []:
                            if callable(streaks):
                                st = None if lst.get("stale") else streaks(row["code"], key, lst.get("to"))
                            else:
                                st = (streaks.get(row["code"]) or {}).get(key)
                            row["streak"] = ({"days": st["days"], "side": st["side"],
                                              **({"atLeast": True} if st.get("atLeast") else {})}
                                             if st and st.get("days") else None)
    return out


# ---------------------------------------------------------------- '평소와 다른 것' (자기 이력 대비 드문 사실만, 최대 3개)
# 방향을 말하지 않는다. 사전 검정에서 방향 정보가 없던 것(외국인 연속·동반 매수·강도 극단 등)은 트리거로 쓰지 않고,
# '얼마나 드문가'(빈도)만 고른다. 드문 게 없는 날은 없다고 쓴다.

TODAY_FOOTER = "드묾은 빈도이지 방향이 아닙니다 · 예측 아님"


def _share_txt(share: float) -> str:
    """99.7 을 '100%' 로 반올림하지 않는다."""
    r = round(share)
    return f"{share:.1f}%" if r == 100 and share != 100 else f"{r}%"


def build_today(flows: dict, stocks: dict, credit: dict, program: dict, glob: dict, short: dict,
                now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    items: list[tuple[float, dict]] = []
    ks = (flows.get("markets") or {}).get("KOSPI") or {}
    day = (ks.get("latest") or {}).get("date")
    provisional = bool(day) and day == now.date().isoformat() and now.strftime("%H%M") < PROGRAM_FINAL_AFTER
    prov = " (잠정)" if provisional else ""
    names = {"individual": "개인", "foreign": "외국인", "institution": "기관", "other_corp": "기타법인"}

    def cho(v):
        return f"{abs(v) / 10000:.2f}조" if abs(v) >= 10000 else f"{abs(v):,.0f}억"

    for k, st in (ks.get("streaks") or {}).items():
        r = st.get("rarity") or {}
        pct = r.get("pctRuns")
        if pct is None or st.get("days", 0) < 3 or pct > 5:
            continue
        side = "순매수" if st["side"] == "buy" else "순매도"
        detail = (f"{r.get('since', '2009')[:4]}년 이후 같은 방향 연속 구간 중 {pct:.1f}%만 이 길이까지 갔습니다"
                  if pct > 0 else f"{r.get('since', '2009')[:4]}년 이후 가장 긴 같은 방향 연속 기록입니다")
        items.append((pct, {"kind": "streak", "text": f"{names.get(k, k)} {st['days']}거래일 연속 {side}{prov}",
                            "detail": detail}))

    conc = (stocks or {}).get("concentration") or {}
    for key, nm in (("foreign", "외국인"), ("institution", "기관")):
        c = conc.get(key) or {}
        if not c.get("top") or c.get("share") is None or abs(c.get("total") or 0) < 10000 or c["share"] < 80:
            continue
        side = "순매수" if c["total"] > 0 else "순매도"
        top_sum = sum(t["value"] for t in c["top"])
        rest = c["total"] - top_sum
        n = conc.get("universe") or 0
        names2 = "·".join(t["name"] for t in c["top"])
        sgn_ = lambda v: "+" if v >= 0 else "−"
        if c["share"] > 100:
            text = (f"{nm} {conc.get('days', 60)}일 {side}: {names2} 두 종목 합계({sgn_(top_sum)}{cho(top_sum)})가 "
                    f"수집 {n}종목 합계({sgn_(c['total'])}{cho(c['total'])})보다 큼")
            detail = f"나머지 종목은 합쳐서 반대 방향({sgn_(rest)}{cho(rest)}) — 수집 {n}종목 기준, 금액은 수량×종가 근사"
        else:
            text = f"{nm} {conc.get('days', 60)}일 {side}의 {_share_txt(c['share'])}가 {names2}"
            detail = (f"수집 {n}종목 합계({sgn_(c['total'])}{cho(c['total'])}, 수량×종가 근사) 기준 · "
                      f"나머지 {max(n - 2, 0)}종목 합계 {sgn_(rest)}{cho(rest)}")
        items.append((4.0 if c["share"] >= 100 else 8.0, {"kind": "concentration", "text": text, "detail": detail}))

    ln = ((credit or {}).get("latest") or {}).get("loans") or {}
    q = ln.get("d5Pctl")
    if q is not None and ln.get("d5") is not None and ((ln["d5"] > 0 and q >= 97.5) or (ln["d5"] < 0 and q <= 2.5)):
        up = ln["d5"] > 0
        items.append((min(q, 100 - q), {"kind": "credit",
                      "text": f"신용융자 잔고 5거래일 {'+' if up else '−'}{cho(ln['d5'])}",
                      "detail": f"최근 {ln.get('days')}거래일의 5일 변화 중 {'가장 크게 는' if up else '가장 크게 준'} 쪽 {rank_text(100 - q if up else q)}"}))
    mo = ((credit or {}).get("latest") or {}).get("money") or {}
    if mo.get("liqPctl") is not None and mo["liqPctl"] >= 97.5 and mo.get("liquidation"):
        items.append((100 - mo["liqPctl"], {"kind": "credit", "text": f"반대매매 {cho(mo['liquidation'])}",
                      "detail": f"최근 {mo.get('days')}거래일 중 상위 {rank_text(100 - mo['liqPctl'])} 규모"}))

    pg = (((program or {}).get("markets") or {}).get("KOSPI") or {}).get("latest") or {}
    q = pg.get("pctl")
    if q is not None and pg.get("date") == day and (q >= 97.5 or q <= 2.5) and abs(pg.get("total") or 0) >= 1000:
        buy = pg["total"] > 0
        if (buy and q >= 97.5) or (not buy and q <= 2.5):
            items.append((min(q, 100 - q), {"kind": "program",
                          "text": f"코스피 프로그램 {'순매수' if buy else '순매도'} {cho(pg['total'])}"
                                  + (" (잠정)" if pg.get("provisional") else ""),
                          "detail": f"최근 {pg.get('days')}거래일 중 {'순매수' if buy else '순매도'} 쪽 상위 {rank_text(100 - q if buy else q)}"}))

    sh = ((((short or {}).get("markets") or {}).get("KOSPI") or {}).get("latest") or {}).get("daily") or {}
    if sh.get("pctPctl") is not None and sh["pctPctl"] >= 97.5:
        items.append((100 - sh["pctPctl"], {"kind": "short", "text": f"코스피 공매도 비중 {sh['pct']:.2f}%",
                      "detail": f"전면 재개 뒤 {sh.get('days')}거래일 중 상위 {rank_text(100 - sh['pctPctl'])}"}))

    by_sym = {x.get("symbol"): x for x in (glob or {}).get("items", [])}
    for sym in (glob or {}).get("preopen") or PREOPEN_SYMBOLS:
        x = by_sym.get(sym)
        if x and x.get("movePctl") is not None and x["movePctl"] >= 97.5 and x.get("changeRate") is not None:
            items.append((100 - x["movePctl"], {"kind": "overnight",
                          "text": f"{x['name']} {x['changeRate']:+.2f}% ({x.get('asOf', '')[5:].replace('-', '/')})",
                          "detail": f"최근 1년 하루 움직임 중 크기 상위 {rank_text(100 - x['movePctl'])}"}))

    picked = [it for _, it in sorted(items, key=lambda t: t[0])[:3]]
    return {"date": day, "provisional": provisional, "asOf": now.isoformat(timespec="seconds"), "items": picked,
            "none": "최근 이력과 견줘 특별히 드문 숫자가 없습니다.", "footer": TODAY_FOOTER}


def concentration(universe: list[dict], days: int = 60) -> dict | None:
    """외국인·기관 n일 순매수(억원, 근사) 가운데 상위 2종목이 차지하는 몫. 한 방향으로 크게 몰렸는지 보는 숫자."""
    out = {"days": days, "universe": len(universe)}
    for key in ("foreign", "institution"):
        vals = [(u["code"], u["name"], (u.get(key) or {}).get(f"v{days}")) for u in universe]
        vals = [(c_, n, v) for c_, n, v in vals if v is not None]
        if len(vals) < 20:
            return None
        total = sum(v for _, _, v in vals)
        same = sorted((x for x in vals if (x[2] > 0) == (total > 0)), key=lambda x: -abs(x[2]))[:2]
        out[key] = {"total": round(total), "top": [{"code": c_, "name": n, "value": round(v)} for c_, n, v in same],
                    "share": round(sum(v for _, _, v in same) / total * 100, 1) if total else None}
    return out


# ---------------------------------------------------------------- 장중 잠정치 기록 (원천이 그날 하루치만 주므로 지금부터 쌓는다)

HIST_TIMES = ("10:00", "11:00", "13:00", "14:30", "15:30")
HIST_DAYS = 120


def fetch_program_intraday(market: str) -> dict[str, dict]:
    """가장 최근 거래일의 분 단위 누적 프로그램 순매수(억원) {HHMMSS: {total, nonarb, arb}}. 시장당 4요청 안팎."""
    out: dict[str, dict] = {}
    day = None
    for page in range(6):
        j = get_json(NAVER_PROGRAM_URL, headers={"Referer": NAVER_PROGRAM_REFERER},
                     params={"tradeType": "KRX", "krxMarketType": market,
                             "bizdate": datetime.now(KST).strftime("%Y%m%d"),
                             "startIdx": page, "pageSize": 200, "periodType": "TIME"})
        content = j.get("content") if isinstance(j, dict) else None
        if not isinstance(content, list):
            raise RuntimeError(f"응답 형식이 바뀜: {str(j)[:120]}")
        for c_ in content:
            d, t = str(c_.get("bizdate") or ""), str(c_.get("time") or "")
            if not re.fullmatch(r"\d{8}", d) or not re.fullmatch(r"\d{6}", t):
                continue
            day = day or d
            if d != day:
                continue
            won = _program_won(c_)
            if won:
                out[t] = {k: float(round(won[f"{k}_net"] / 1e8)) for k in ("total", "nonarb", "arb")}
        if not content or str(j.get("last")).lower() == "true":
            break
        time.sleep(0.3)
    out["_date"] = f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else None   # type: ignore[assignment]
    return out


def record_intraday_hist(prev: dict | None, flows: dict, program: dict, now: datetime) -> dict | None:
    """
    20:05 이후(그날 확정 뒤) 한 번, 그날 장중 몇 시각의 잠정치와 확정치를 짝지어 남긴다.
    나중에 '15:30 값에서 확정까지 얼마나 바뀌나'를 보여 줄 바탕이다(20거래일 쌓이기 전엔 통계를 내지 않는다).
    """
    hist = prev if isinstance(prev, dict) and isinstance(prev.get("days"), list) else {"days": []}
    ks = (flows.get("markets") or {}).get("KOSPI") or {}
    day = (ks.get("latest") or {}).get("date")
    intra = ks.get("intraday") or {}
    if not day or intra.get("date") != day or not intra.get("final"):
        return None                                        # 그날 확정 전이면 기록하지 않는다
    if day == now.date().isoformat() and now.strftime("%H%M") < PROGRAM_FINAL_AFTER:
        return None                                        # 프로그램 매매까지 확정(20:05)된 뒤 한 번
    if any(d.get("date") == day for d in hist["days"]):
        return None
    entry = {"date": day, "times": {}, "final": {}}
    for code in ("KOSPI", "KOSDAQ"):
        m = (flows.get("markets") or {}).get(code) or {}
        pts = (m.get("intraday") or {}).get("points") or []
        snap = {}
        for t in HIST_TIMES:
            got = [p_ for p_ in pts if p_["t"] <= t]
            if got:
                snap[t] = {k: got[-1].get(k) for k in ("individual", "foreign", "institution", "other_corp")}
        try:
            pi = fetch_program_intraday(code)
            if pi.pop("_date", None) == day:
                for t in HIST_TIMES:
                    key = t.replace(":", "") + "00"
                    got = [v for tt, v in sorted(pi.items()) if tt <= key]
                    if got and t in snap:
                        snap[t]["program"] = got[-1]["total"]
        except Exception as e:  # noqa: BLE001
            warn(f"{MARKET_NAME[code]} 장중 프로그램 기록 실패: {type(e).__name__}: {e}")
        lt = m.get("latest") or {}
        pl = (((program or {}).get("markets") or {}).get(code) or {}).get("latest") or {}
        entry["times"][code] = snap
        entry["final"][code] = {**{k: lt.get(k) for k in ("individual", "foreign", "institution", "other_corp")},
                                "program": pl.get("total") if pl.get("date") == day else None}
    hist["days"] = (hist["days"] + [entry])[-HIST_DAYS:]
    hist["count"] = len(hist["days"])
    return hist


# ---------------------------------------------------------------- 문구 검사 (방향·추천으로 읽히는 말을 내보내지 않는다)

BANNED_WORDS = ("추천", "신호", "타이밍", "목표가", "기회", "투매", "쌍끌이", "본심", "패닉")


def check_copy(texts, where: str) -> list[str]:
    bad = sorted({w for t in texts for w in BANNED_WORDS if w in str(t)})
    if bad:
        warn(f"{where}: 쓰지 않기로 한 말({', '.join(bad)})이 들어갔습니다")
    return bad


# ---------------------------------------------------------------- main

def next_session(now: datetime) -> date:
    """지금 열려 있거나 다음에 열릴 정규장 날짜."""
    d = now.date()
    if is_trading_day(d) and now.strftime("%H%M") < session_hours(d.isoformat())[1]:
        return d
    return next_trading_day(d)


def market_phase(now: datetime, kospi: dict | None = None) -> str:
    """
    장 상태 라벨. kospi 는 이번에 받은 네이버 지수 시세(tradedAt 이 있어야 휴장일을 안다).
    평일인데 장이 열릴 시각(09:05)이 지나도 시세 기준일이 오늘이 아니면 휴장일 — 예전엔 추석에도 '장중'으로 떴다.
    """
    if now.weekday() >= 5:
        return "주말"
    today = now.date().isoformat()
    traded = str((kospi or {}).get("tradedAt") or "")[:10]
    if today in HOLIDAYS:
        if traded != today:
            return "휴장일"
        # 목록이 틀렸다(잠정 목록·오타) — 오늘 시세가 있으니 거래일로 보고, 남은 계산에서도 휴장일에서 뺀다
        warn(f"휴장일 목록의 {today}({HOLIDAYS[today]})에 오늘 시세가 있습니다 — krx_calendar.json 확인 필요")
        HOLIDAYS.pop(today, None)
    hm = now.hour * 60 + now.minute
    o_ = int(session_hours(now.date().isoformat())[0][:2]) * 60
    if hm >= o_ + 5 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", traded) and traded < now.date().isoformat():
        return "휴장일"
    open_, close = session_hours(now.date().isoformat())
    o, cl = int(open_[:2]) * 60 + int(open_[2:]), int(close[:2]) * 60 + int(close[2:])
    if hm < o - 30:
        return "장전"
    if hm < o:
        return "동시호가"
    if hm <= cl:
        return "장중"
    if hm < 20 * 60:                       # KRX 투자자별 값은 20시 전후까지 바뀐다
        return "장마감(수급 확정 반영중)"
    return "장마감"


def main() -> int:
    now = datetime.now(KST)
    print(f"수집 시작 {now:%Y-%m-%d %H:%M:%S} KST")
    print("[0/9] 국내 장 일정 (휴장일·금통위)")
    try:
        calendar = refresh_calendar(now)
    except Exception as e:  # noqa: BLE001
        warn(f"장 일정 갱신 실패 — krx_calendar.json 만 씁니다: {type(e).__name__}: {e}")
        calendar = None
    print(f"  장 상태: {market_phase(now)}")

    print("[1/8] 투자자 수급")
    flows, full = build_flows()
    print("[2/8] 지수")
    market, closes = build_market()
    phase = market_phase(now, market["indices"].get("KOSPI"))
    stale_keys = check_freshness(flows, market, closes)
    print("[3/8] 개미 성적표")
    ant = build_ant(full, closes, "KOSPI")
    # 코스닥 성적표는 코스피 성적표 안에 싣는다. 코스닥 수급이나 일봉이 멈췄으면 싣지 않는다(지난 '오늘'을 보이지 않게)
    if ant and not stale_keys & {"flows.KOSDAQ", "market.KOSDAQ.history"}:
        kq = build_ant(full, closes, "KOSDAQ")
        prev_kq = ((load_prev("ant.json") or {}).get("byMarket") or {}).get("KOSDAQ")
        if kq and kq.get("basis") == "amount" and prev_kq and prev_kq.get("basis") == "intensity":
            warn(f"코스닥 성적표: 이번 계산은 기준이 낮아져 직전 데이터({prev_kq['sample']['to']}까지)를 유지합니다")
            mark_stale("ant.KOSDAQ", "코스닥 성적표", asOf=prev_kq["sample"]["to"])
            kq = prev_kq
        if kq:
            ant["byMarket"] = {"KOSDAQ": {k: kq[k] for k in
                                          ("market", "basis", "yearly", "sample", "baseline", "actors", "correlation")}}
    if ant:
        a = ant["actors"]
        print(f"  표본 {ant['sample']['days']}일 · 개인↔외인 상관 {ant['correlation']['individual_foreign']:+.3f}"
              f" · 반대 비율 {ant['oppositeRate']['vsForeign']}%")
        for k in ACTOR_KEYS:
            print(f"    {k:12} 학점 {a[k]['grade']}  20일 초과수익 {a[k]['excess20']:+.2f}%p")
    print("[4/8] 선물 수급 · 프로그램 매매 · 공매도")
    futures, fut_full = build_futures()
    attach_futures_divergence(futures, full, fut_full, closes)
    program = build_program(now)
    short = build_short(now)
    # 수급(같은 네이버)보다 거래일이 2일 이상 뒤처졌으면 멈춘 것으로 본다 (장중엔 오늘 행이 늦게 붙을 수 있어 1일은 봐준다)
    for code, name in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        fdays = [r["date"] for r in (flows["markets"].get(code) or {}).get("daily") or []]
        for key, label, blk in ((f"program.{code}", f"{name} 프로그램 매매", program["markets"].get(code)),
                                (f"short.{code}", f"{name} 공매도", short["markets"].get(code))):
            rows = (blk or {}).get("daily") or []
            if rows and sum(1 for d in fdays if d > rows[-1]["date"]) >= 2:
                warn(f"{label}이 {rows[-1]['date']}에서 멈춤 (수급 최신 {fdays[-1]})")
                mark_stale(key, label, asOf=rows[-1]["date"])
                stale_keys.add(key)
        # 잔고는 원래 2거래일 늦다(장중엔 수급에 오늘이 붙어 3일). 5거래일 넘게 뒤처지면 멈춘 것
        bal = (short["markets"].get(code) or {}).get("balance") or []
        if bal and sum(1 for d in fdays if d > bal[-1]["date"]) >= 5:
            warn(f"{name} 공매도 잔고가 {bal[-1]['date']}에서 멈춤 (수급 최신 {fdays[-1]})")
            mark_stale(f"short.{code}.balance", f"{name} 공매도 잔고", asOf=bal[-1]["date"])
            stale_keys.add(f"short.{code}.balance")
    print("[5/8] 신용융자·증시자금 (KOFIA)")
    credit = build_credit()
    print("[6/9] 유사 국면 매칭")
    analog = build_analog(full, closes, "KOSPI")
    if analog:
        print(f"  매칭 {len(analog['matches'])}건 · 평균 20일 뒤 {analog['avgRet20']:+.2f}% "
              f"(기준 {analog['baseline20']:+.2f}%)")
    print("[7/9] 종목·업종 + 장바구니 (+ 350종목 범위는 장 밖에서만)")
    prev_meta = load_prev("meta.json") or {}
    used = (prev_meta.get("requests") or {})
    used_today = used.get("count", 0) if used.get("date") == now.date().isoformat() else 0
    universe = None
    prev_stocks = load_prev("stocks.json") or {}
    data_day0 = ((flows["markets"].get("KOSPI") or {}).get("latest") or {}).get("date")
    if phase in ("장중", "동시호가"):
        pass                                            # 장중엔 350종목을 돌리지 않는다(직전 요약 유지)
    elif not universe_due(prev_stocks, data_day0, now):
        print("  350종목: 새 거래일 데이터가 없어 직전 요약 유지")
    elif used_today + REQUESTS["n"] + 400 + 100 > REQUEST_CAP:      # 뒤의 종목 순위(약 8번)·나머지 몫을 남긴다
        warn(f"오늘 요청 수가 상한({REQUEST_CAP})에 가까워 350종목 범위를 갱신하지 않았습니다")
    else:
        # 최신 거래일 행이 이미 다 붙었고(=새 데이터 없음) 채우는 중이기만 하면, 채우는 종목만 받는다
        fresh_rows = data_day0 and universe_as_of(prev_stocks) >= data_day0 and not (
            now.strftime("%H%M") >= PROGRAM_FINAL_AFTER and (prev_stocks.get("universeAt") or "")
            < f"{data_day0}T{PROGRAM_FINAL_AFTER[:2]}:{PROGRAM_FINAL_AFTER[2:]}")
        universe = build_universe(now, backfill_only=bool(fresh_rows))
    prev_flows_by = {s_["code"]: s_ for s_ in prev_stocks.get("top") or []}
    reuse = ({"top": prev_flows_by, "series": (load_prev("stockflows.json") or {}).get("stocks") or {}}
             if not universe else None)
    stocks = build_stocks(closes=closes, store_rows=universe[1] if universe else None, reuse=reuse)
    if universe:
        stocks["universe"] = universe[0]
        stocks["universeAt"] = now.isoformat(timespec="seconds")
        stocks["universePending"] = universe[2]
        keep = {u["code"] for u in universe[0]}
        print(f"  종목 1년 시계열 {write_stock_series(universe[1], keep)}개")
    else:
        stocks["universe"] = prev_stocks.get("universe") or []
        stocks["universeAt"] = prev_stocks.get("universeAt")
        stocks["universePending"] = prev_stocks.get("universePending", 0)
    antstocks = build_ant_stocks(stocks, closes)
    if antstocks:
        print(f"  장바구니 비교(산 뒤 등락): 개미 {antstocks['antAvgSinceBuy']}% vs "
              f"외인 {antstocks['foreignAvgSinceBuy']}%")
    print("[8/9] 글로벌 지표")
    glob = build_global()
    print("[9/9] 이벤트 일정 / 매크로")
    events, macro = build_events()
    glob["macro"] = macro
    print("[+] 외국인·기관 종목 순위 · 평소와 다른 것")
    if used_today + REQUESTS["n"] >= REQUEST_CAP:
        warn(f"오늘 요청 수가 상한({REQUEST_CAP})을 넘어 종목 순위를 다시 받지 않았습니다")
        ranks = load_prev("ranks.json")
    else:
        ranks = build_ranks(load_prev("ranks.json"), now, phase, rank_streak_fn(stocks, universe[1] if universe else None))
    if stocks.get("universe"):
        stocks["concentration"] = concentration(stocks["universe"])
    today_card = build_today(flows if "flows.KOSPI" not in stale_keys else {"markets": {}},
                             stocks, credit, program, glob, short, now)
    hist = record_intraday_hist(load_prev("intraday_hist.json"), flows, program, now)

    # 성적표·유사 국면은 수급과 종가가 둘 다 있는 날까지만 계산돼, 어느 쪽이 멈춰도 '오늘'이 그날에 묶인다.
    # 여기서는 이번에 계산한 결과의 기준일로 표시해 두고, 직전 정상본이 더 최신이면 apply_last_good 이 고쳐 쓴다.
    if stale_keys & {"market.KOSPI.history", "flows.KOSPI"}:
        stale_keys |= {"ant", "analog"}
        if ant:
            mark_stale("ant", "개미 성적표", asOf=ant["sample"]["to"])
        if analog:
            mark_stale("analog", "유사 국면", asOf=analog["today"]["date"])
    ant_today = {} if "ant" in stale_keys else ant

    # 브리핑은 이번 수집분으로만 만든다 (직전 정상본이나 멈춘 데이터로 '오늘'을 말하지 않도록)
    core_ok = bool(flows["markets"].get("KOSPI")) and "flows.KOSPI" not in stale_keys
    if core_ok:
        # 수급과 같은 날의 프로그램 매매만 — 장중엔 하루 늦게 붙을 수 있는데, 날짜 없는 문장이 어제 값을 '오늘'로 말하게 된다
        fdate = flows["markets"]["KOSPI"]["latest"]["date"]
        prog_today = {"markets": {k: v for k, v in program["markets"].items()
                                  if f"program.{k}" not in stale_keys and v["latest"]["date"] == fdate}}
        insights = build_insights(flows, market, glob, ant_today, futures, credit, prog_today, now.date().isoformat())
    else:
        insights = build_insights({"markets": {}}, market, glob, {}, None, credit, None, now.date().isoformat())

    # 하루 요약 피드도 이번 수집분으로만 (apply_last_good 이 market 을 직전 값으로 바꾸기 전에)
    pre = build_pre_entry(now, flows, glob, events, today_card) if core_ok else None
    feed = build_feed(load_prev("feed.json") or {}, flows if core_ok else None, market, insights, now, pre)
    check_copy([t["text"] for t in insights], "브리핑")
    check_copy([f"{i['text']} {i['detail']}" for i in today_card["items"]], "평소와 다른 것")
    check_copy([f"{e['title']} {e['summary']}" for e in feed["entries"][:2]], "하루 요약 피드")

    out = apply_last_good(now.isoformat(), {
        "flows.json": flows, "ant.json": ant, "analog.json": analog,
        "antstocks.json": antstocks, "futures.json": futures, "credit.json": credit,
        "market.json": market, "stocks.json": stocks, "global.json": glob,
        "stockflows.json": {"unit": "주", "stocks": stocks.pop("series", {})},
        "program.json": program, "short.json": short,
        "events.json": events, "ranks.json": ranks, "today.json": today_card,
    }, stale_keys)
    for name, payload in out.items():
        write(name, payload, compact=name in ("stockflows.json", "short.json", "stocks.json"))
    if calendar:
        write("calendar.json", calendar)
    if hist:
        write("intraday_hist.json", hist, compact=True)
    write("insights.json", {"items": insights})
    notify_telegram(feed)
    write("feed.json", feed)
    (OUT / "feed.xml").write_text(feed_xml(feed), encoding="utf-8")
    if core_ok:
        try:
            build_og_card(out["flows.json"], out["market.json"], ant_today)
        except Exception as e:  # noqa: BLE001
            warn(f"공유 카드(og.png) 생성 실패: {type(e).__name__}: {e}")
    else:
        warn("코스피 수급이 갱신되지 않아 공유 카드(og.png)를 다시 그리지 않았습니다")
    data_day = ((out["flows.json"]["markets"].get("KOSPI") or {}).get("latest") or {}).get("date")
    write("meta.json", {
        "generatedAt": now.isoformat(),
        "generatedAtText": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "phase": phase,
        # 화면이 '오늘' 대신 날짜를 쓰고, 잠정/확정과 다음 장을 알리는 데 쓴다
        "dataDate": data_day,
        "dataFinal": bool(data_day) and (data_day < now.date().isoformat()
                                         or now.strftime("%H%M") >= PROGRAM_FINAL_AFTER),
        "nextSession": next_session(now).isoformat(),
        "mode": "full", "fullAt": now.isoformat(), "intradayAt": prev_meta.get("intradayAt"),
        "requests": {"date": now.date().isoformat(), "count": used_today + REQUESTS["n"]},
        "warnings": WARNINGS,
        "stale": STALE,
        "freshAt": FRESH_AT,
        "sources": [
            {"name": "네이버 증권", "for": "개인/외국인/기관 수급, 프로그램 매매, 지수 일봉·시세, 종목, 업종"},
            {"name": "한국거래소", "for": "공매도, 휴장일"},
            {"name": "한국은행", "for": "금통위 일정"},
            {"name": "다음 금융", "for": "수급 예비 소스"},
            {"name": "금융투자협회", "for": "신용융자·증시자금"},
            {"name": "yfinance", "for": "미국 지수·선물·금리·환율·원자재"},
            {"name": "Federal Reserve · FRED", "for": "FOMC·경제지표 일정, 매크로 지표"},
        ],
        "caveat": "장중 수급은 잠정치이며 장 마감 후 확정치로 정정됩니다.",
    })

    print(f"\n완료. 경고 {len(WARNINGS)}건")
    for w in WARNINGS:
        print(f"  - {w}")
    # 파일은 다 썼다(직전 정상본 유지). 핵심 데이터가 갱신되지 않았으면 워크플로가 커밋한 뒤
    # 실행을 실패로 표시해 알림이 가게 한다 — 2026-09 에는 수급과 지수 일봉이 둘 다 조용히 멈췄었다.
    failed = [label for ok, label in ((core_ok, "코스피 수급"),
                                      ("market.KOSPI.history" not in stale_keys, "코스피 일봉")) if not ok]
    if failed:
        print(f"핵심 데이터({', '.join(failed)}) 갱신 실패 — exit 3")
        return 3
    return 0


# ---------------------------------------------------------------- 장중 가벼운 수집 (--intraday)
# 외부 스케줄러가 장중 5분마다 워크플로를 깨우면, 매시 한 번의 전체 수집 사이에는 이것만 돈다(요청 약 12번, 수 초).
# 장중에 바뀌는 것(코스피·코스닥 수급 오늘 행·분 단위 흐름, 프로그램 매매 오늘 행, 지수 시세)만 받아
# 직전 정상본(data 브랜치) 위에 덮어쓴다. 실패하면 그 부분은 직전 값을 두고, 실행을 실패로 만들지 않는다
# (매시 전체 수집이 핵심 데이터 멈춤을 잡는다).

def _patch_daily(daily: list[dict], new_rows: list[dict], keys=("individual", "foreign", "institution")) -> list[dict]:
    """최근 행을 날짜로 바꿔 끼우고, 바뀐 첫 행부터 누적(cum)을 다시 잇는다."""
    if not new_rows:
        return daily
    by = {r["date"]: r for r in daily}
    first = min(r["date"] for r in new_rows)
    for r in new_rows:
        by[r["date"]] = {k: v for k, v in r.items() if k != "cum"}
    rows = [by[d] for d in sorted(by)][-CHART_DAYS:]
    base = {k: 0.0 for k in keys}
    out = []
    for r in rows:
        if r["date"] < first and r.get("cum"):
            base = dict(r["cum"])
            out.append(r)
            continue
        base = {k: round(base.get(k, 0.0) + (r.get(k) or 0.0), 1) for k in keys}
        out.append({**r, "cum": base})
    return out


def run_intraday() -> int:
    now = datetime.now(KST)
    print(f"장중 가벼운 수집 {now:%Y-%m-%d %H:%M:%S} KST")
    try:
        refresh_calendar(now, fetch=False)             # 저장된 목록만 — 받는 건 매시 전체 수집이 한다
    except Exception as e:  # noqa: BLE001
        warn(f"장 일정 확인 실패: {type(e).__name__}: {e}")
    flows, market = load_prev("flows.json"), load_prev("market.json")
    program, meta = load_prev("program.json") or {"unit": "억원", "markets": {}}, load_prev("meta.json") or {}
    if not flows or not market or not flows.get("markets"):
        print("직전 데이터가 없어 전체 수집으로 돌립니다")
        return main()
    fresh: dict[str, str] = {}
    for code in ("KOSPI", "KOSDAQ"):
        try:
            b = get_json(f"https://m.stock.naver.com/api/index/{code}/basic")
            e = market["indices"].setdefault(code, {"code": code})
            e.update(price=num(b.get("closePrice")), change=num(b.get("compareToPreviousClosePrice")),
                     changeRate=num(b.get("fluctuationsRatio")), marketStatus=b.get("marketStatus"),
                     tradedAt=b.get("localTradedAt"))
            hi = (e.get("high1y") or {}).get("value")
            if hi and e.get("price"):
                e["fromHigh"] = round((e["price"] / hi - 1) * 100, 2)
            fresh[f"market.{code}.price"] = now.isoformat()
        except Exception as ex:  # noqa: BLE001
            warn(f"{code} 지수 시세 실패: {type(ex).__name__}: {ex}")
    phase = market_phase(now, market["indices"].get("KOSPI"))
    if phase in ("주말", "휴장일"):
        print(f"  {phase} — 수급은 받지 않습니다")
    else:
        for code in ("KOSPI", "KOSDAQ"):
            m = flows["markets"].setdefault(code, {})
            try:
                rows = fetch_naver_trend(code, 5, page_size=10)
                if rows:
                    m["daily"] = _patch_daily(m.get("daily") or [], rows)
                    m["latest"] = m["daily"][-1]
                    m["streaks"] = {k: {**streak(m["daily"], k), "rarity": streak_rarity(code, k, streak(m["daily"], k))}
                                    for k in ("individual", "foreign", "institution", "other_corp")}
                    fresh[f"flows.{code}"] = now.isoformat()
                intra = fetch_intraday(code)
                if intra:
                    m["intraday"] = intra
            except Exception as ex:  # noqa: BLE001
                warn(f"{MARKET_NAME[code]} 장중 수급 실패 — 직전 값 유지: {type(ex).__name__}: {ex}")
            try:
                prow = fetch_program_daily(code, 5, now, page_size=10)
                if prow:
                    pm = program["markets"].setdefault(code, {})
                    by = {r["date"]: r for r in pm.get("daily") or []}
                    for r in prow:                                 # 받은 행을 모두 날짜로 바꿔 끼운다(하루 넘게 비었어도)
                        by[r["date"]] = {"date": r["date"], "arb": r["arb_net"], "nonarb": r["nonarb_net"],
                                         "total": r["total_net"], **({"provisional": True} if r.get("provisional") else {})}
                    pm["daily"] = [by[d] for d in sorted(by)][-PROGRAM_CHART_DAYS:]
                    new = prow[-1]
                    old = pm.get("latest") or {}
                    settled = pm.get("settled") or []
                    pm["latest"] = {"date": new["date"], "arb": new["arb_net"], "nonarb": new["nonarb_net"],
                                    "total": new["total_net"], "provisional": bool(new.get("provisional")),
                                    "pctl": round(_percentile(settled, new["total_net"]), 1) if len(settled) >= 60 else None,
                                    "days": len(settled) or old.get("days"),
                                    "streak": streak(pm["daily"], "total")}
                    fresh[f"program.{code}"] = now.isoformat()
            except Exception as ex:  # noqa: BLE001
                warn(f"{MARKET_NAME[code]} 장중 프로그램 매매 실패 — 직전 값 유지: {type(ex).__name__}: {ex}")
    write("flows.json", flows)
    write("program.json", program)
    write("market.json", market)
    data_day = ((flows["markets"].get("KOSPI") or {}).get("latest") or {}).get("date")
    # 숫자를 새로 받았으면 그 숫자로 '평소와 다른 것'·브리핑을 다시 쓴다(요청 없이 직전 파일로)
    if fresh.get("flows.KOSPI"):
        try:
            stocks_p, credit_p = load_prev("stocks.json") or {}, load_prev("credit.json") or {}
            glob_p, short_p = load_prev("global.json") or {}, load_prev("short.json") or {}
            today_card = build_today(flows, stocks_p, credit_p, program, glob_p, short_p, now)
            prog_today = {"markets": {k: v for k, v in program["markets"].items()
                                      if (v.get("latest") or {}).get("date") == data_day}}
            insights = build_insights(flows, market, glob_p, {}, load_prev("futures.json"), credit_p, prog_today,
                                      now.date().isoformat())
            check_copy([f"{i['text']} {i['detail']}" for i in today_card["items"]], "평소와 다른 것")
            check_copy([t["text"] for t in insights], "브리핑")
            write("today.json", today_card)
            write("insights.json", {"items": insights})
        except Exception as ex:  # noqa: BLE001
            warn(f"장중 '평소와 다른 것'·브리핑 다시 쓰기 실패: {type(ex).__name__}: {ex}")
    # 이번에 새로 받은 섹션의 '지난 데이터' 표시는, 받은 데이터가 그 날짜를 넘어섰을 때 지운다
    new_last = {f"flows.{c_}": (((flows["markets"].get(c_) or {}).get("daily") or [{}])[-1]).get("date") for c_ in ("KOSPI", "KOSDAQ")}
    new_last.update({f"program.{c_}": (((program["markets"].get(c_) or {}).get("daily") or [{}])[-1]).get("date")
                     for c_ in ("KOSPI", "KOSDAQ")})
    meta["stale"] = [x for x in meta.get("stale") or []
                     if not (x.get("key") in fresh and (not x.get("asOf") or (new_last.get(x["key"]) or "") > x["asOf"]))]
    used = meta.get("requests") or {}
    used_today = used.get("count", 0) if used.get("date") == now.date().isoformat() else 0
    meta.update({
        "generatedAt": now.isoformat(), "generatedAtText": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "phase": phase, "dataDate": data_day,
        "dataFinal": bool(data_day) and (data_day < now.date().isoformat() or now.strftime("%H%M") >= PROGRAM_FINAL_AFTER),
        "nextSession": next_session(now).isoformat(),
        "mode": "intraday", "intradayAt": now.isoformat(), "fullAt": meta.get("fullAt") or meta.get("generatedAt"),
        "freshAt": {**(meta.get("freshAt") or {}), **fresh},
        "requests": {"date": now.date().isoformat(), "count": used_today + REQUESTS["n"]},
        # 전체 수집의 경고는 남기고(다음 전체 수집이 다시 쓴다) 이번 경고를 덧붙인다
        "warnings": list(dict.fromkeys([*(meta.get("warnings") or []), *WARNINGS])),
    })
    write("meta.json", meta)
    print(f"완료. 요청 {REQUESTS['n']}번 · 경고 {len(WARNINGS)}건")
    return 0


if __name__ == "__main__":
    # 윈도우 콘솔에서도 한글이 깨지지 않게 (import 할 때는 건드리지 않는다 — 테스트 출력 캡처와 충돌)
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        sys.exit(run_intraday() if "--intraday" in sys.argv[1:] else main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
    finally:
        # 실패한 실행도 요청 수를 남긴다 — 워크플로의 '실패한 시도 기록' 단계가 읽어 meta.json 에 더한다
        try:
            att = ROOT / ".cache" / "attempt.json"
            att.parent.mkdir(parents=True, exist_ok=True)
            att.write_text(json.dumps({"at": datetime.now(KST).isoformat(timespec="seconds"),
                                       "requests": REQUESTS["n"]}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
