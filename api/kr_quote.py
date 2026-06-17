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

        valid = [
            (ts, c, volumes_raw[i] if i < len(volumes_raw) else None)
            for i, (ts, c) in enumerate(zip(timestamps, closes_raw))
            if c is not None
        ]

        if len(valid) < 1:
            return None

        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime("%Y-%m-%d")

        last_ts, last_close, last_vol = valid[-1]
        last_date_str = datetime.fromtimestamp(last_ts, tz=kst).strftime("%Y-%m-%d")

        # chart의 마지막 행이 "오늘" 날짜면 그게 현재가, 그 앞이 전일 확정 종가
        if last_date_str == today_str and len(valid) >= 2:
            regular_price = last_close
            prev_close = valid[-2][1]
            volume = last_vol
        elif last_date_str == today_str and len(valid) == 1:
            # 오늘 데이터뿐이면 전일 종가를 알 수 없음 -> meta 값에 의존
            regular_price = last_close
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            volume = last_vol
        else:
            # 마지막 행이 전일(장 마감 후 등) -> 그게 곧 전일 확정 종가
            regular_price = meta.get("regularMarketPrice", last_close)
            prev_close = last_close
            volume = meta.get("regularMarketVolume") or last_vol

        if regular_price is None or regular_price <= 0:
            regular_price = last_close
        if prev_close is None or prev_close <= 0:
            return None

        change_pct = round((regular_price - prev_close) / prev_close * 100, 2)
        volume = volume or 0

        session = "regular" if is_kr_market_open() else "closed"

        return {
            "price": round(prev_close, 2),
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
