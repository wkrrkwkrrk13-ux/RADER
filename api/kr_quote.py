"""
kr_quote.py - 국장 종목 시세 조회 (Vercel 서버리스 함수)
=============================================================
미장 quote.py와 동일한 v8 chart API 기반 구조.
국장은 시간대 변환이 단순함 (KST 09:00~15:30 정규장 고정).

엔드포인트: /api/kr_quote?symbols=005930.KS,000660.KS,...

응답 형식 (종목당):
{
  "price": 전일 확정 종가,
  "change": 전일 등락률(%),
  "live_price": 현재가(정규장중) 또는 종가(장외시),
  "live_change": 현재 등락률(%),
  "session": "regular" | "closed",
  "volume": 거래량
}
"""
from http.server import BaseHTTPRequestHandler
import urllib.request
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs


def is_kr_market_open():
    """한국 정규장 여부: 평일 09:00~15:30 KST"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30


def fetch_chart(symbol):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=1mo&includePrePost=false"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def parse_symbol(symbol):
    try:
        data = fetch_chart(symbol)
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]

        closes = [c for c in quote.get("close", []) if c is not None]
        volumes = [v for v in quote.get("volume", []) if v is not None]

        regular_price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")

        if regular_price is None or prev_close is None or prev_close == 0:
            if len(closes) >= 2:
                regular_price = closes[-1]
                prev_close = closes[-2]
            else:
                return None

        change_pct = round((regular_price - prev_close) / prev_close * 100, 2)
        volume = volumes[-1] if volumes else meta.get("regularMarketVolume", 0)

        session = "regular" if is_kr_market_open() else "closed"

        return {
            "price": round(regular_price, 2),
            "change": change_pct,
            "live_price": round(regular_price, 2),
            "live_change": change_pct,
            "session": session,
            "volume": volume,
        }
    except Exception:
        return None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        symbols_param = qs.get("symbols", [""])[0]
        symbols = [s.strip() for s in symbols_param.split(",") if s.strip()]

        result = {}
        for sym in symbols:
            result[sym] = parse_symbol(sym)

        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate")
        self.end_headers()
        self.wfile.write(body)
