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
        f"?interval=1d&range=5d&includePrePost=false"
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
        timestamps = r.get("timestamp", [])

        closes_raw = quote.get("close", [])
        volumes_raw = quote.get("volume", [])

        # timestamp와 close를 함께 묶어서, None이 아닌 종가만 시간순으로 정렬된 채 추출
        valid = [
            (ts, c, volumes_raw[i] if i < len(volumes_raw) else None)
            for i, (ts, c) in enumerate(zip(timestamps, closes_raw))
            if c is not None
        ]

        if len(valid) < 1:
            return None

        # 가장 최근 거래일 = 전일 확정 종가, 그 이전 = 전전일
        last_ts, last_close, last_vol = valid[-1]

        regular_price = meta.get("regularMarketPrice", last_close)
        prev_close = meta.get("previousClose")

        # meta.previousClose가 없거나 비정상적으로 차이나면 valid 리스트에서 직접 계산
        if prev_close is None or prev_close <= 0:
            if len(valid) >= 2:
                prev_close = valid[-2][1]
            else:
                prev_close = last_close

        # regularMarketPrice가 비정상(0 또는 None)이면 최근 종가로 대체
        if regular_price is None or regular_price <= 0:
            regular_price = last_close

        if prev_close is None or prev_close == 0:
            return None

        change_pct = round((regular_price - prev_close) / prev_close * 100, 2)
        volume = meta.get("regularMarketVolume") or last_vol or 0

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
        debug = qs.get("debug", ["0"])[0] == "1"

        result = {}
        for sym in symbols:
            if debug:
                result[sym] = debug_symbol(sym)
            else:
                result[sym] = parse_symbol(sym)

        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate")
        self.end_headers()
        self.wfile.write(body)


def debug_symbol(symbol):
    """원본 meta + 최근 종가 리스트를 그대로 보여주는 진단용 함수"""
    try:
        data = fetch_chart(symbol)
        result = data.get("chart", {}).get("result")
        if not result:
            return {"error": "no result", "raw": data}
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        timestamps = r.get("timestamp", [])
        closes = quote.get("close", [])
        return {
            "meta_previousClose": meta.get("previousClose"),
            "meta_regularMarketPrice": meta.get("regularMarketPrice"),
            "meta_chartPreviousClose": meta.get("chartPreviousClose"),
            "meta_currency": meta.get("currency"),
            "meta_exchangeName": meta.get("exchangeName"),
            "timestamps_count": len(timestamps),
            "last_5_closes": closes[-5:] if closes else [],
            "last_5_timestamps_kst": [
                datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
                for ts in timestamps[-5:]
            ] if timestamps else [],
        }
    except Exception as e:
        return {"error": str(e)}
