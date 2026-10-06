# -*- coding: utf-8 -*-
"""
수집 방식 정하기 — 워크플로가 깨어날 때마다(외부 스케줄러가 하루 종일 5분마다 깨워도 되게) 무엇을 할지 고른다.
표준 라이브러리만 쓴다(의존성 설치 전에 돈다).

  python collector/mode.py            → 'mode=full|intraday|skip' 한 줄 (GITHUB_OUTPUT 에 붙인다)
  FORCE=true python collector/mode.py → 늘 full (수동 실행 버튼)
  python collector/mode.py --record-failure META MODE
                                      → 실패한 실행을 META(meta.json 사본)에 적는다(다음 판단이 쉬어 가게)

거래일 (장 시간은 특별 장 시간·새해 첫 거래일을 따른다)
  - 장중(개장 5분 전 ~ 마감 10분 뒤): 1시간 안에 전체 수집을 시도했으면 가벼운 수집, 아니면 전체
  - 장 전(07:30 ~ 장중 전): 오늘 07:30 이후 성공한 전체 수집이 없으면 한 번
  - 마감 뒤(장중 뒤 ~ 20:15): 잠정치가 바뀌는 동안 1시간에 한 번
  - 확정(20:15 ~ 23:30): 20:15 이후 성공한 전체 수집이 없으면 한 번. 그 뒤에도 종목 순위·종목별 수급이 덜 들어왔으면
    (meta.pendingFinal — 2026-10-06 엔 20:05 뒤에 올라왔다) 30분마다 다시
  - 밤(23:30 ~ 07:30): 쉼(못 받은 것은 07:30 장 전 수집이 채운다)
휴장일·주말: 6시간에 한 번 전체
어느 때든 2.5분 안에 이미 돈(또는 실패한) 실행이 있으면 건너뜀.
실패한 전체 수집은 'fullAt' 을 바꾸지 못하므로, 실패 기록(lastAttempt)으로 쉬는 간격을 잰다 — 실패가 이어져도
5분마다 무거운 수집을 다시 하지 않는다(장중엔 그동안 가벼운 수집을 계속한다).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def calendar(root: Path = ROOT) -> tuple[set[str], dict[str, tuple[str, str]]]:
    seed = _load(root / "collector" / "krx_calendar.json")
    saved = _load(root / "docs" / "data" / "calendar.json")
    hols = set(seed.get("holidays") or {})
    for days in (saved.get("holidays") or {}).values():
        hols |= set(days or {})
    hols |= set(saved.get("extraHolidays") or {})
    special = {d: tuple(v) for d, v in {**(saved.get("specialSessions") or {}),
                                         **(seed.get("special_sessions") or {})}.items() if len(v) == 2}
    return hols, special


def _parse(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(v)
        return t if t.tzinfo else t.replace(tzinfo=KST)
    except Exception:  # noqa: BLE001
        return None


def _session(day: date, hols: set[str], special: dict) -> tuple[int, int]:
    """그날 정규장 (시작, 끝) 분. 특별 장 시간, 새해 첫 거래일 10시 개장."""
    iso = day.isoformat()
    if iso in special:
        o, c = special[iso]
        return int(o[:2]) * 60 + int(o[2:]), int(c[:2]) * 60 + int(c[2:])
    first = date(day.year, 1, 1)
    while first.weekday() >= 5 or first.isoformat() in hols:
        first += timedelta(days=1)
    return (10 * 60 if day == first else 9 * 60), 15 * 60 + 30


def decide(meta: dict, hols: set[str], now: datetime, force: bool = False, special: dict | None = None) -> str:
    if force:
        return "full"
    last_any = _parse(meta.get("generatedAt"))
    last_full = _parse(meta.get("fullAt")) or last_any               # 마지막으로 '성공한' 전체 수집
    att = meta.get("lastAttempt") or {}
    failed_at = _parse(att.get("at")) if not att.get("ok", True) else None
    failed_full = failed_at if att.get("mode") == "full" else None
    tried_full = max([t for t in (last_full, failed_full) if t], default=None)   # 성공이든 실패든 마지막 시도
    touched = max([t for t in (last_any, failed_at) if t], default=None)
    age = lambda t: (now - t).total_seconds() / 60 if t else 1e9
    done_since = lambda h, m: last_full is not None and last_full >= now.replace(hour=h, minute=m, second=0, microsecond=0)
    if age(touched) < 2.5:
        return "skip"
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5 or now.date().isoformat() in hols:
        return "full" if age(tried_full) >= 360 else "skip"
    o, c = _session(now.date(), hols, special or {})
    if o - 5 <= hm <= c + 10:
        return "intraday" if age(tried_full) < 55 else "full"
    if hm < 7 * 60 + 30:
        return "skip"
    if hm < o - 5:
        return "full" if not done_since(7, 30) and age(tried_full) >= 30 else "skip"
    if hm < 20 * 60 + 15:
        return "full" if age(tried_full) >= 60 else "skip"
    if hm < 23 * 60 + 30:
        if age(tried_full) < 30:
            return "skip"
        return "full" if not done_since(20, 15) or meta.get("pendingFinal") else "skip"
    return "skip"


def record_failure(meta_path: Path, mode: str, root: Path = ROOT, now: datetime | None = None) -> dict:
    """실패한 실행을 meta.json 사본에 적는다: lastAttempt 와 그 실행이 쓴 요청 수(.cache/attempt.json)."""
    now = now or datetime.now(KST)
    meta = _load(meta_path)
    meta["lastAttempt"] = {"at": now.isoformat(timespec="seconds"), "mode": mode, "ok": False}
    n = int((_load(root / ".cache" / "attempt.json") or {}).get("requests") or 0)
    used = meta.get("requests") or {}
    today = now.date().isoformat()
    meta["requests"] = {"date": today, "count": (used.get("count", 0) if used.get("date") == today else 0) + n}
    Path(meta_path).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--record-failure":
        m = record_failure(Path(sys.argv[2]), sys.argv[3])
        print(f"[mode] 실패 기록: {m['lastAttempt']} · 오늘 요청 {m['requests']['count']}", file=sys.stderr)
        sys.exit(0)
    meta = _load(ROOT / "docs" / "data" / "meta.json")
    force = os.environ.get("FORCE", "").lower() == "true"
    hols, special = calendar()
    mode = decide(meta, hols, datetime.now(KST), force, special)
    print(f"mode={mode}")
    print(f"[mode] {mode} (force={force}, generatedAt={meta.get('generatedAt')}, fullAt={meta.get('fullAt')}, "
          f"lastAttempt={meta.get('lastAttempt')})", file=sys.stderr)
