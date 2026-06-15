from http.server import BaseHTTPRequestHandler
import urllib.request
import json
from datetime import datetime, timezone, timedelta

def is_us_market_open():
    """미국 정규장 중인지 확인 (UTC 기준 14:30~21:00, 월~금)"""
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:  # 토/일
        return False
    t = now_utc.hour * 60 + now_utc.minute
    return 14 * 60 + 30 <= t < 21 * 60

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        symbols_raw = params.get('symbols', params.get('symbol', ['SPY']))[0]
        symbols = [s.strip().upper() for s in symbols_raw.split(',')][:20]

        market_open = is_us_market_open()

        results = {}
        for symbol in symbols:
            try:
                # 1) 일봉 데이터 (7일치 - 넉넉하게)
                url_daily = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=7d'
                req = urllib.request.Request(url_daily, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())

                result = data['chart']['result'][0]
                closes = [v for v in result['indicators']['quote'][0]['close'] if v is not None]
                volumes = [v for v in result['indicators']['quote'][0].get('volume', []) if v is not None]

                if len(closes) < 2:
                    results[symbol] = {'error': 'Not enough data'}
                    continue

                if market_open and len(closes) >= 3:
                    # 정규장 중: 오늘 장중가는 미확정 → 전일 종가 기준
                    prev = closes[-3]
                    last = closes[-2]
                else:
                    # 장외: 가장 최근 확정 종가
                    prev = closes[-2]
                    last = closes[-1]

                vol = volumes[-1] if volumes else 0
                change = ((last - prev) / prev) * 100

                # 2) 현재가 (1분봉, 프리/애프터 포함)
                url_live = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d&includePrePost=true'
                req2 = urllib.request.Request(url_live, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
                live_price = last
                try:
                    with urllib.request.urlopen(req2, timeout=8) as resp2:
                        data2 = json.loads(resp2.read().decode())
                    result2 = data2['chart']['result'][0]
                    closes2 = [v for v in result2['indicators']['quote'][0]['close'] if v is not None]
                    if closes2:
                        live_price = closes2[-1]
                except Exception:
                    pass

                live_change = ((live_price - prev) / prev) * 100

                results[symbol] = {
                    'price': round(last, 2),
                    'change': round(change, 2),
                    'volume': int(vol),
                    'live_price': round(live_price, 2),
                    'live_change': round(live_change, 2),
                }

            except Exception as e:
                results[symbol] = {'error': str(e)}

        payload = json.dumps(results).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
