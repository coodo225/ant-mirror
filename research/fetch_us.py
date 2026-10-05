"""yfinance 일봉 4종을 한 번 받아 저장 (Yahoo 요청 수 회)."""
import json
from pathlib import Path
import yfinance as yf
syms = ["^GSPC", "^SOX", "EWY", "KRW=X"]
df = yf.download(syms, start="2008-06-01", end="2026-10-04", interval="1d", progress=False,
                 auto_adjust=False, group_by="ticker", threads=False)
out = {}
for s in syms:
    c = df[s]["Close"].dropna()
    out[s] = {d.strftime("%Y-%m-%d"): round(float(v), 6) for d, v in c.items()}
    print(s, len(out[s]), min(out[s]), max(out[s]))
DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)
json.dump(out, open(DATA / "us_daily.json", "w", encoding="utf-8"))
