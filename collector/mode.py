# -*- coding: utf-8 -*-
"""
수집 방식 정하기 — 워크플로가 깨어날 때마다(외부 스케줄러가 하루 종일 5분마다 깨워도 되게) 무엇을 할지 고른다.
표준 라이브러리만 쓴다(의존성 설치 전에 돈다).

  python collector/mode.py          → 'mode=full|intraday|skip' 한 줄 (GITHUB_OUTPUT 에 붙인다)
  FORCE=true python collector/mode.py → 늘 full (수동 실행 버튼)

거래일
  - 장중(08:55~15:40): 1시간 안에 전체 수집이 있었으면 가벼운 수집, 아니면 전체
  - 장 전(07:30~08:55): 오늘 07:30 이후 전체 수집이 없으면 한 번
  - 마감 뒤(15:40~20:05): 잠정치가 바뀌는 동안 1시간에 한 번
  - 20시 확정(20:05~21:30): 20:05 이후 전체 수집이 없으면 한 번
  - 밤(21:30~07:30): 쉼
휴장일·주말: 6시간에 한 번 전체
어느 때든 2.5분 안에 이미 돈 실행이 있으면(트리거가 겹침) 건너뜀
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def holidays(root: Path = ROOT) -> set[str]:
    out = set((_load(root / "collector" / "krx_calendar.json").get("holidays") or {}))
    for days in (_load(root / "docs" / "data" / "calendar.json").get("holidays") or {}).values():
        out |= set(days or {})
    for d in (_load(root / "docs" / "data" / "calendar.json").get("extraHolidays") or {}):
        out.add(d)
    return out


def _parse(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(v)
        return t if t.tzinfo else t.replace(tzinfo=KST)
    except Exception:  # noqa: BLE001
        return None


def decide(meta: dict, hols: set[str], now: datetime, force: bool = False) -> str:
    if force:
        return "full"
    last_any = _parse(meta.get("generatedAt"))
    last_full = _parse(meta.get("fullAt")) or last_any
    age = lambda t: (now - t).total_seconds() / 60 if t else 1e9
    full_before = lambda h, m: last_full is None or last_full < now.replace(hour=h, minute=m, second=0, microsecond=0)
    if age(last_any) < 2.5:
        return "skip"
    hm = now.hour * 60 + now.minute
    trading_day = now.weekday() < 5 and now.date().isoformat() not in hols
    if not trading_day:
        return "full" if age(last_full) >= 360 else "skip"
    if 8 * 60 + 55 <= hm <= 15 * 60 + 40:
        return "intraday" if age(last_full) < 55 else "full"
    if hm < 7 * 60 + 30:
        return "skip"
    if hm < 8 * 60 + 55:
        return "full" if full_before(7, 30) else "skip"
    if hm < 20 * 60 + 5:
        return "full" if age(last_full) >= 60 else "skip"
    if hm < 21 * 60 + 30:
        return "full" if full_before(20, 5) else "skip"
    return "skip"


if __name__ == "__main__":
    meta = _load(ROOT / "docs" / "data" / "meta.json")
    force = os.environ.get("FORCE", "").lower() == "true"
    mode = decide(meta, holidays(), datetime.now(KST), force)
    print(f"mode={mode}")
    print(f"[mode] {mode} (force={force}, generatedAt={meta.get('generatedAt')}, fullAt={meta.get('fullAt')})",
          file=sys.stderr)
