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

소스가 실패하거나 멈추면 그 섹션은 직전 정상본을 유지하고(meta.json 의 stale/freshAt),
핵심인 코스피 수급·일봉이 갱신되지 않으면 exit 3 으로 끝나 워크플로가 실패로 표시된다.
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


def write(name: str, payload) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
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
              stale_keys: set[str] = frozenset(), last=None):
    """
    new 가 정상이면 그대로, 비었으면 직전 정상본(prev)을 돌려준다.
    비지 않았어도 멈춘 데이터(stale_keys)라면 '정상 수집'으로 치지 않고,
    직전 정상본이 더 최신이면 그쪽을 내보낸다 (예: 네이버 일봉 실패 → 멈춰 있는 FDR 로 떨어졌을 때).
    """
    since = FRESH_AT.get(key)
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

    def keep(key, label, new, prev, ok=bool, last=None):
        return keep_good(key, label, new, prev, ok, now_iso, stale_keys, last)

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
                           last=_last_date(lambda a: a["sample"]["to"]))
    out["analog.json"] = keep("analog", "유사 국면", out["analog.json"], load_prev("analog.json"),
                              last=_last_date(lambda a: a["today"]["date"]))
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


def fetch_naver_trend(market: str, days: int) -> list[dict]:
    """
    market: 'KOSPI' | 'KOSDAQ' | 'FUT'(코스피200 선물). 날짜 오름차순, 최근 days거래일.
    현물은 억원(원 단위 합계를 반올림), 선물은 계약. 장중이면 당일 잠정치가 들어올 수 있다.
    """
    # 모르는 marketType 을 주면 서버가 오류 없이 '코스피+코스닥 합계'를 돌려준다 — 반드시 셋 중 하나
    if market not in MARKET_NAME:
        raise ValueError(market)
    by_date: dict[str, dict] = {}
    skipped = 0
    for page in range(days // 200 + 2):            # startIdx 는 오프셋이 아니라 페이지 번호
        if len(by_date) >= days:
            break
        try:
            j = get_json(NAVER_TREND_URL, headers={"Referer": NAVER_TREND_REFERER},
                         params={"tradeType": "KRX", "marketType": market, "startIdx": page, "pageSize": 200})
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
            for n in c.get("netAmounts") or []:
                try:
                    amt[str(n["investorGubun"])] = int(str(n["diffValue"]).replace(",", ""))
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
    out: dict = {"unit": "억원", "chartDays": CHART_DAYS, "markets": {}}
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
                "streaks": {k: streak(rows, k) for k in ("individual", "foreign", "institution")},
            }
            print(f"  {code} 수급 {len(rows)}일 ({rows[0]['date']} ~ {rows[-1]['date']})")
        except Exception as e:  # noqa: BLE001
            warn(f"{code} 수급 수집 실패: {type(e).__name__}: {e}")
    return out, full


# ---------------------------------------------------------------- 선물 (외국인의 선행 포지션)

def build_futures() -> dict:
    """
    코스피200 선물 투자자별 순매수 (단위: 계약).
    외국인은 현물보다 선물을 먼저 움직이는 경우가 많아 선행 지표로 쓴다.
    """
    out: dict = {"unit": "계약", "daily": [], "latest": None, "streaks": {}, "divergence": None}
    try:
        rows = fetch_flows("FUT", 60, (load_prev("futures.json") or {}).get("daily"))
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
            print(f"  {code} 일봉 {len(bars)}개 ({bars[0]['date']} ~ {bars[-1]['date']})")
        out["indices"][code] = entry
    return out, closes


def _weekdays_between(a: str, b: str) -> int:
    """a 다음 날부터 b 까지(포함)의 평일 수."""
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return sum(1 for k in range(1, (db - da).days + 1) if (da + timedelta(days=k)).weekday() < 5)


def check_freshness(flows: dict, market: dict, closes: dict) -> set[str]:
    """
    '성공했는데 멈춘' 소스를 잡는다 — 2026-09 에 FDR 지수 캐시가 경고 없이 9/17 에 멈춰 있었다.
    멈춘 것으로 판단한 섹션 키를 돌려준다.

    - 지수 일봉의 마지막 날짜를 기준일과 비교한다. 기준일은 네이버 현재가의 tradedAt,
      시세 API 가 죽었으면 수급 최신일이다.
    - 빠진 날이 기준일 하루뿐이면(장 시작 전·장중이라 일봉에 오늘 봉이 아직 없을 때) 멈춤이 아니다.
      장중이면 현재가로 그날 종가를 채워, 성적표·유사 국면의 '오늘'을 수급 표의 '오늘'과 맞춘다.
    - 휴장일 달력이 없으므로 '하루뿐'은 수급 날짜로 판단한다. 수급에도 그날이 없으면,
      기준일이 오늘이고 일봉과 수급이 같은 마지막 거래일에 머물러 있을 때만 하루뿐으로 본다
      (평일 휴장 다음 날 아침의 오탐을 막기 위해, 명절 연휴를 덮는 평일 5일까지).
    - 수급 최신일 뒤로 지수 거래일이 2일 이상 쌓였으면 수급 소스가 멈춘 것으로 본다.
    """
    stale: set[str] = set()
    today = datetime.now(KST).date().isoformat()
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
                only_today = agree and _weekdays_between(last, traded) <= 5
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

    # 종목별 개인/외인/기관 — 네이버 API 는 최대 60거래일까지 준다.
    # 화면 상세에는 최근 5일만 싣고, 60일 전체는 장바구니 통계(stat60)로 요약한다.
    for s in out["top"]:
        try:
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
                s["stat60"] = {
                    "days": len(valid),
                    "from": valid[0]["date"], "to": valid[-1]["date"],
                    "indivValue": net_value("individual"),
                    "foreignValue": net_value("foreign"),
                    "instValue": net_value("institution"),
                    "change": round((valid[-1]["close"] / valid[0]["close"] - 1) * 100, 2),
                }

            s["flow"] = [{k: d[k] for k in ("date", "individual", "foreign", "institution", "foreignHoldRatio")}
                         for d in days[-5:]]
            if days:
                s["foreignHoldRatio"] = days[-1]["foreignHoldRatio"]
            time.sleep(0.12)
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


# ---------------------------------------------------------------- 개미 장바구니 vs 외인 장바구니

def build_ant_stocks(stocks: dict) -> dict:
    """
    최근 60거래일, 개미와 외국인이 각각 어떤 종목을 담았고 그 종목들이 어떻게 됐는지.
    순매수 금액은 (일별 순매수 수량 × 그날 종가)의 합 — 근사치임을 화면에 명시한다.
    """
    pool = [s for s in stocks.get("top", []) if s.get("stat60")]
    if len(pool) < 10:
        warn(f"장바구니 비교: stat60 있는 종목 부족({len(pool)})")
        return {}

    def pick(key, reverse=True, n=10):
        ranked = sorted(pool, key=lambda s: s["stat60"][key], reverse=reverse)
        chosen = [s for s in ranked if (s["stat60"][key] > 0 if reverse else s["stat60"][key] < 0)][:n]
        return [
            {
                "code": s["code"], "name": s["name"], "market": s["market"],
                "value": s["stat60"][key], "change": s["stat60"]["change"],
                "price": s["price"],
            }
            for s in chosen
        ]

    ant_basket = pick("indivValue")            # 개미가 가장 많이 담은 종목
    foreign_basket = pick("foreignValue")      # 외인이 가장 많이 담은 종목

    def avg_change(basket):
        return round(sum(b["change"] for b in basket) / len(basket), 2) if basket else None

    # 개미의 눈물: 많이 담았는데(순매수 상위) 많이 빠진 순
    tears = sorted(ant_basket, key=lambda b: b["change"])[:5]
    wins = sorted(ant_basket, key=lambda b: -b["change"])[:5]

    sample = pool[0]["stat60"]
    return {
        "window": {"days": sample["days"], "from": sample["from"], "to": sample["to"]},
        "universe": len(pool),
        "antBasket": ant_basket,
        "foreignBasket": foreign_basket,
        "antAvgChange": avg_change(ant_basket),
        "foreignAvgChange": avg_change(foreign_basket),
        "tears": tears,
        "wins": wins,
        "note": "순매수 금액은 일별 순매수 수량 × 그날 종가의 합산 근사치입니다. "
                "대상은 수집된 시총 상위 종목이며, 등락률은 최근 60거래일 기준입니다.",
    }


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


# ---------------------------------------------------------------- 유사 국면 매칭

ANALOG_FEATURES = [
    ("indiv",    "개인 순매수"),
    ("foreign",  "외국인 순매수"),
    ("inst",     "기관 순매수"),
    ("indiv5",   "개인 5일 누적"),
    ("foreign5", "외국인 5일 누적"),
    ("ret1",     "당일 등락률"),
    ("ret5",     "5일 등락률"),
    ("vol5",     "5일 변동성"),
]


def build_analog(full: dict, closes: dict, code: str = "KOSPI") -> dict:
    """
    오늘의 (수급 + 가격 움직임) 조합과 가장 비슷했던 과거의 날들을 찾는다.
    8개 특징을 z-score 로 정규화한 뒤 유클리드 거리로 비교한다.
    예측이 아니라 '과거에 비슷한 날은 이후 어땠나'의 기록이다.
    """
    px = closes.get(code) or {}
    rows = [r for r in (full.get(code) or []) if r["date"] in px]
    n = len(rows)
    if n < 120:
        warn(f"유사 국면: {code} 표본 부족({n}일) — 건너뜀")
        return {}

    C = [px[r["date"]] for r in rows]
    I = [r["individual"] or 0.0 for r in rows]
    F = [r["foreign"] or 0.0 for r in rows]
    O = [r["institution"] or 0.0 for r in rows]
    ret1 = [0.0] + [(C[i] / C[i - 1] - 1) * 100 for i in range(1, n)]
    ret5 = [0.0] * 5 + [(C[i] / C[i - 5] - 1) * 100 for i in range(5, n)]
    i5 = [sum(I[max(0, i - 4): i + 1]) for i in range(n)]
    f5 = [sum(F[max(0, i - 4): i + 1]) for i in range(n)]
    vol5 = []
    for i in range(n):
        w = ret1[max(0, i - 4): i + 1]
        m = sum(w) / len(w)
        vol5.append((sum((x - m) ** 2 for x in w) / len(w)) ** 0.5)

    feats = [I, F, O, i5, f5, ret1, ret5, vol5]

    def zscore(v):
        m = sum(v) / len(v)
        s = (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5 or 1.0
        return [(x - m) / s for x in v]

    Z = [zscore(v) for v in feats]
    today = n - 1

    # 후보: 5일 워밍업 이후 ~ 결과(20일)를 아는 날까지, 최근 10일은 제외(자기 자신과의 중복 방지)
    cands = []
    for i in range(5, n - 20):
        if i >= n - 10:
            continue
        d = sum((Z[k][i] - Z[k][today]) ** 2 for k in range(len(Z))) ** 0.5
        cands.append((d, i))
    cands.sort()

    picked: list[tuple[float, int]] = []
    for d, i in cands:
        if any(abs(i - j) < 5 for _, j in picked):   # 붙어 있는 날짜는 하나로
            continue
        picked.append((d, i))
        if len(picked) == 5:
            break

    matches = [
        {
            "date": rows[i]["date"],
            "distance": round(d, 3),
            "close": round(C[i], 2),
            "ret1": round(ret1[i], 2),
            "individual": I[i],
            "foreign": F[i],
            "ret20": round((C[i + 20] / C[i] - 1) * 100, 2),
        }
        for d, i in picked
    ]
    if not matches:
        return {}

    fwd20 = [(C[i + 20] / C[i] - 1) * 100 for i in range(n - 20)]
    return {
        "market": code,
        "today": {
            "date": rows[today]["date"], "close": round(C[today], 2),
            "individual": I[today], "foreign": F[today], "institution": O[today],
            "ret1": round(ret1[today], 2), "ret5": round(ret5[today], 2),
        },
        "matches": matches,
        "avgRet20": round(sum(m["ret20"] for m in matches) / len(matches), 2),
        "baseline20": round(sum(fwd20) / len(fwd20), 2),
        "sample": {"days": n, "from": rows[0]["date"], "to": rows[-1]["date"]},
    }


# ---------------------------------------------------------------- 해석 레이어

def build_insights(flows: dict, market: dict, glob: dict, ant: dict,
                   futures: dict | None = None, credit: dict | None = None) -> list[dict]:
    """
    수치에서 바로 읽히는 사실만 문장으로. 예측이나 매매 조언은 하지 않는다.
    입력은 모두 '이번 수집분'이어야 한다 — 직전 정상본을 넣으면 지난 날의 일을 '오늘'로 말하게 된다.
    수급이 비어도 지수·글로벌·빚투 문장은 따로 나간다.
    """
    tips: list[dict] = []

    def cho(v):
        return f"{v / 10000:.2f}조" if abs(v) >= 10000 else f"{abs(v):,.0f}억"

    ks = flows.get("markets", {}).get("KOSPI")
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


# ---------------------------------------------------------------- OG 공유 카드

def find_korean_font() -> tuple[str | None, str | None]:
    """(굵은 폰트, 보통 폰트) 경로. 윈도우/리눅스(Actions) 겸용."""
    candidates = [
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
    dr.text((60, 112), f"{date_txt} · 오늘 한국 증시에서 누가 사고 누가 팔았나",
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
    dr.text((bx, 140), "오늘의 수급 (억원)", font=font(24, b=False), fill=DIM)
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
        dr.text((60, ty), f"개미 온도계 — 최근 {ant['sample']['days']}거래일 중 {p:.0f}번째 백분위",
                font=font(26), fill=TEXT)
        track_y = ty + 52
        dr.rounded_rectangle([60, track_y, 1140, track_y + 22], radius=11, fill=(13, 18, 32), outline=LINE)
        dr.rounded_rectangle([60, track_y, 60 + 216, track_y + 22], radius=11, fill=(58, 96, 156))
        dr.rounded_rectangle([1140 - 216, track_y, 1140, track_y + 22], radius=11, fill=(156, 62, 62))
        ax = 60 + (1140 - 60) * p / 100
        dr.ellipse([ax - 18, track_y - 8, ax + 18, track_y + 30], fill=(255, 176, 46))
        dr.text((60, track_y + 40), "패닉 매도", font=font(20, b=False), fill=DOWN)
        dr.text((1040, track_y + 40), "영끌 매수", font=font(20, b=False), fill=UP)

    dr.text((60, H - 46), "coodo225.github.io/ant-mirror · 투자 조언이 아닙니다",
            font=font(20, b=False), fill=(93, 103, 128))

    out = ROOT / "docs" / "og.png"
    img.save(out, "PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes)")


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
    stale_keys = check_freshness(flows, market, closes)
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
    print("[6/9] 유사 국면 매칭")
    analog = build_analog(full, closes, "KOSPI")
    if analog:
        print(f"  매칭 {len(analog['matches'])}건 · 평균 20일 뒤 {analog['avgRet20']:+.2f}% "
              f"(기준 {analog['baseline20']:+.2f}%)")
    print("[7/9] 종목·업종 + 장바구니")
    stocks = build_stocks()
    antstocks = build_ant_stocks(stocks)
    if antstocks:
        print(f"  장바구니 비교: 개미 {antstocks['antAvgChange']:+.2f}% vs "
              f"외인 {antstocks['foreignAvgChange']:+.2f}% (60일)")
    print("[8/9] 글로벌 지표")
    glob = build_global()
    print("[9/9] 이벤트 일정 / 매크로")
    events, macro = build_events()
    glob["macro"] = macro

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
        insights = build_insights(flows, market, glob, ant_today, futures, credit)
    else:
        insights = build_insights({"markets": {}}, market, glob, {}, None, credit)

    out = apply_last_good(now.isoformat(), {
        "flows.json": flows, "ant.json": ant, "analog.json": analog,
        "antstocks.json": antstocks, "futures.json": futures, "credit.json": credit,
        "market.json": market, "stocks.json": stocks, "global.json": glob,
        "events.json": events,
    }, stale_keys)
    for name, payload in out.items():
        write(name, payload)
    write("insights.json", {"items": insights})
    if core_ok:
        build_og_card(out["flows.json"], out["market.json"], ant_today)
    else:
        warn("코스피 수급이 갱신되지 않아 공유 카드(og.png)를 다시 그리지 않았습니다")
    write("meta.json", {
        "generatedAt": now.isoformat(),
        "generatedAtText": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "phase": market_phase(now),
        "warnings": WARNINGS,
        "stale": STALE,
        "freshAt": FRESH_AT,
        "sources": [
            {"name": "네이버 증권", "for": "개인/외국인/기관 수급, 지수 일봉·시세, 종목, 업종"},
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
