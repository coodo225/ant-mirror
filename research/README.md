# research — 화면 숫자의 근거

`docs/evidence.json`(수급 속설 검증표)과 `collector/collect.py` 의 `LONG_RUN_CONTRARIAN`, 온도계·성적표 해설이 기대는 검정입니다.
**결과를 보기 전에** 정의와 판정 규칙을 적어 둔 파일(사전 등록)과, 그 규칙대로 돌린 스크립트·결과를 함께 둡니다.

| 파일 | 내용 |
|---|---|
| `prereg.json` | H1~H6(외국인 연속·동반 매수·강도 극단·개인 강매도·만기일·20일 누적) 정의와 판정 규칙 |
| `prereg_overnight.json` | E1~E3(간밤 미국장 → 시가 갭·장중 방향·변동폭) 정의와 판정 규칙 |
| `fetch_history.py` | 코스피 투자자별 일별 순매수·거래대금(2009~)과 지수 일봉을 네이버에서 받아 `data/` 에 저장(요청 약 30번) |
| `fetch_us.py` | 미국 지수·EWY·원/달러 일봉(yfinance)을 `data/us_daily.json` 에 저장 |
| `analyze_oos.py` | 성적표·온도계 신호의 표본 밖(2009-03~2023-08) 검증 |
| `prereg_tests.py` | H1~H6 검정(20일 블록 부트스트랩 90% 범위, Holm·BH 보정) |
| `overnight_tests.py` | E1~E3 검정 |
| `results/` | 2026-10-05 에 돌린 결과(검증표의 숫자는 여기서 옮겼습니다) |

다시 돌리기(분기마다):

```bash
python research/fetch_history.py
python research/fetch_us.py
python research/analyze_oos.py
python research/prereg_tests.py
python research/overnight_tests.py
```

`data/` 는 받아 온 원자료라 git 에 올리지 않습니다. 사전 등록 파일(`prereg*.json`)은 고치지 않습니다 —
규칙을 바꾸려면 새 파일을 만들고 바꾼 날짜와 이유를 적습니다. 결과가 달라지면 `docs/evidence.json` 의 숫자와 판정을 고칩니다.
