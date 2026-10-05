"""
화면 문구 검사 — 방향·추천으로 읽히는 말을 화면에 쓰지 않는다(수집기의 BANNED_WORDS 와 같은 목록).
주석 줄은 검사하지 않는다.
"""
from pathlib import Path

import collect as c

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _visible_lines(path: Path):
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        t = line.strip()
        if t.startswith(("//", "/*", "*", "<!--")):
            continue
        yield no, line


def test_screen_copy_has_no_banned_words():
    hits = []
    for name in ("index.html", "app.js", "evidence.json"):
        for no, line in _visible_lines(DOCS / name):
            for w in c.BANNED_WORDS:
                if w in line:
                    hits.append(f"{name}:{no}: {w} — {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_collector_copy_has_no_banned_words():
    src = (Path(c.__file__)).read_text(encoding="utf-8")
    hits = []
    for no, line in enumerate(src.splitlines(), 1):
        t = line.strip()
        if t.startswith("#") or "BANNED_WORDS" in line or "check_copy" in line:
            continue
        code_part = line.split("  #")[0]                 # 줄 끝 주석은 빼고 문자열만
        for w in c.BANNED_WORDS:
            if w in code_part and ('"' in code_part or "'" in code_part):
                hits.append(f"collect.py:{no}: {w} — {t[:80]}")
    assert not hits, "\n".join(hits)
