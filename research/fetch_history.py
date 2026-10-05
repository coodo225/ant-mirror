# -*- coding: utf-8 -*-
"""
표본 외 검증용 장기 이력 수집.
- collector/collect.py 를 import 하되 ROOT/OUT 을 이 폴더로 돌려 저장소에 아무것도 쓰지 않는다.
- 코스피 투자자별 일별 순매수 + 거래대금: collect.fetch_naver_trend 를 그대로 호출(파싱 규칙 동일),
  get_json 을 감싸 원본 응답도 raw/ 에 남긴다. 요청 간격은 fetch_naver_trend 안의 0.3초.
- 코스피 지수 일봉: collect.fetch_index_daily 와 같은 api.stock.naver.com chart 를 기간만 넓혀 1~2번 요청.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import collect  # noqa: E402

collect.ROOT = DATA / "_tmp"
collect.OUT = DATA / "_tmp" / "out"

RAW = DATA / "raw"
RAW.mkdir(exist_ok=True)
N_REQ = {"n": 0}
_orig_get_json = collect.get_json


def logged_get_json(url, tries=3, **kw):
    N_REQ["n"] += 1
    j = _orig_get_json(url, tries=tries, **kw)
    p = kw.get("params") or {}
    tag = f"{N_REQ['n']:03d}_" + "_".join(f"{k}-{v}" for k, v in p.items() if k in ("marketType", "startIdx", "startDateTime"))
    (RAW / f"{tag}.json").write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
    return j


collect.get_json = logged_get_json


def fetch_flows():
    rows = collect.fetch_naver_trend("KOSPI", 6000)   # 페이지 27개 남짓 (totalElements 5366)
    (DATA / "kospi_flows_full.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def fetch_index():
    url = "https://api.stock.naver.com/chart/domestic/index/KOSPI/day"
    bars = {}
    # 한 번에 길게 요청해 보고, 앞이 잘리면 구간을 나눠 한 번 더
    for start, end in (("19990101", "20261005"),):
        time.sleep(0.3)
        arr = collect.get_json(url, params={"startDateTime": start + "0000", "endDateTime": end + "0000"})
        for x in arr if isinstance(arr, list) else []:
            d = str(x.get("localDate") or "")
            c = collect.num(x.get("closePrice"))
            if len(d) == 8 and c is not None:
                bars[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = round(c, 2)
    first = min(bars) if bars else None
    if first and first > "2005-01-31":
        # 앞이 잘렸으면 그 이전 구간을 따로
        time.sleep(0.3)
        arr = collect.get_json(url, params={"startDateTime": "19990101" + "0000",
                                            "endDateTime": first.replace("-", "") + "0000"})
        for x in arr if isinstance(arr, list) else []:
            d = str(x.get("localDate") or "")
            c = collect.num(x.get("closePrice"))
            if len(d) == 8 and c is not None:
                bars[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = round(c, 2)
    out = dict(sorted(bars.items()))
    (DATA / "kospi_close_full.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    rows = fetch_flows()
    px = fetch_index()
    with_tv = [r for r in rows if r.get("tradingValue")]
    first_tv = with_tv[0]["date"] if with_tv else None
    no_tv_after_first = sum(1 for r in rows if first_tv and r["date"] >= first_tv and not r.get("tradingValue"))
    info = {
        "requests": N_REQ["n"],
        "flows": {"first": rows[0]["date"], "last": rows[-1]["date"], "rows": len(rows),
                  "rows_with_tradingValue": len(with_tv), "first_with_tradingValue": first_tv,
                  "rows_without_tradingValue_after_first_tv": no_tv_after_first},
        "index": {"first": next(iter(px)), "last": list(px)[-1], "rows": len(px)},
        "warnings": collect.WARNINGS,
    }
    (DATA / "fetch_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=1))
