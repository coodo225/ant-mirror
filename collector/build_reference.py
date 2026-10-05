# -*- coding: utf-8 -*-
"""
화면의 '얼마나 드문가' 기준표(collector/reference.json)를 만든다. 분기에 한 번 손으로 돌린다.

  python collector/build_reference.py

- 코스피 투자자별 일별 순매수를 네이버에서 가능한 만큼(2009-03 이후, 요청 약 23번) 받아
  주체·방향별 연속 구간 길이의 분포를 센다: 'L일 이상 이어진 구간의 비율'.
- 수집기는 이 표를 읽어 지금 연속 기록 옆에 "2009년 이후 같은 방향 연속 구간 중 X%만 이 길이까지"를 붙인다.
- 방향(그 뒤 지수)을 말하는 표가 아니다 — 연속 일수만으로는 그 뒤 지수가 평소와 구분되지 않았다(docs/evidence.json H1).
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import collect as c  # noqa: E402

KEYS = ("individual", "foreign", "institution", "other_corp")
OUT = HERE / "reference.json"


def runs(rows: list[dict], key: str) -> dict[str, list[tuple[int, str]]]:
    """방향별 연속 구간 (길이, 끝난 날짜). 0 인 날은 구간을 끊는다."""
    out: dict[str, list[tuple[int, str]]] = {"buy": [], "sell": []}
    side, n, last = None, 0, None
    for r in rows:
        v = r.get(key) or 0
        s = "buy" if v > 0 else "sell" if v < 0 else None
        if s and s == side:
            n += 1
        else:
            if side and n:
                out[side].append((n, last))
            side, n = s, (1 if s else 0)
        last = r["date"]
    if side and n:
        out[side].append((n, last))     # 지금 진행 중인 구간도 넣는다(아직 끝나지 않았으니 '최소' 길이)
    return out


def table(rows: list[dict]) -> dict:
    res = {}
    for k in KEYS:
        rs = runs(rows, k)
        res[k] = {}
        for side, lst in rs.items():
            lens = [n for n, _ in lst]
            mx = max(lens) if lens else 0
            # reach[L-1] = L일 이상 이어진 구간 비율(%) — L 은 1..최장
            reach = [round(sum(1 for x in lens if x >= L) / len(lens) * 100, 2) for L in range(1, mx + 1)] if lens else []
            longest = max(lst, key=lambda t: t[0]) if lst else (0, None)
            res[k][side] = {"runs": len(lens), "reach": reach, "longest": {"days": longest[0], "ended": longest[1]}}
    return res


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    c.ROOT, c.OUT = tmp, tmp / "docs" / "data"       # 저장소의 docs/data 를 건드리지 않는다
    rows = c.fetch_naver_trend("KOSPI", 6000)
    rows = [r for r in rows if r["date"] >= "2009-03-23"]   # 그 전에는 원천에 기타법인 구분이 없다
    if len(rows) < 3000:
        raise SystemExit(f"이력이 너무 짧음: {len(rows)}일")
    ref = {
        "_readme": "build_reference.py 가 만든다(분기 1회). 코스피 투자자별 연속 순매수·순매도 구간 길이 분포. "
                   "reach[L-1] = L거래일 이상 이어진 같은 방향 구간의 비율(%).",
        "builtAt": datetime.now(c.KST).date().isoformat(),
        "period": {"from": rows[0]["date"], "to": rows[-1]["date"], "days": len(rows)},
        "streaks": {"KOSPI": table(rows)},
        "streakBaseline": "연속 일수만으로는 1·5·20일 뒤 지수 방향이 평소와 구분되지 않았습니다"
                          "(2009~2023, 외국인 5일 이상 연속 순매수 511일, 20일 +0.00%p [−0.55, +0.54]).",
    }
    OUT.write_text(json.dumps(ref, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT} — {ref['period']}")
    for k in KEYS:
        for side in ("buy", "sell"):
            t = ref["streaks"]["KOSPI"][k][side]
            print(f"  {k:12} {side:4} 구간 {t['runs']:4} · 최장 {t['longest']}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
