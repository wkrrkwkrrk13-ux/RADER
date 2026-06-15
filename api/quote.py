from http.server import BaseHTTPRequestHandler
import urllib.request
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        
        # 여러 종목 한번에 처리 (쉼표 구분)
        symbols_raw = params.get('symbols', params.get('symbol', ['SPY']))[0]
        symbols = [s.strip().upper() for s in symbols_raw.split(',')][:20]  # 최대 20개

        results = {}
        for symbol in symbols:
            try:
                url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d&includePrePost=true'
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())

                result = data['chart']['result'][0]
                meta = result.get('meta', {})
                closes = [v for v in result['indicators']['quote'][0]['close'] if v is not None]
                volumes = [v for v in result['indicators']['quote'][0].get('volume', []) if v is not None]

                if len(closes) >= 2:
                    prev = closes[-2]
                    last = closes[-1]   # 정규장 종가 (고정)
                    vol = volumes[-1] if volumes else 0
                    change = ((last - prev) / prev) * 100

                    # 현재가: 프리/애프터장 우선, 없으면 정규장 종가
                    live_price = meta.get('preMarketPrice') or \
                                 meta.get('postMarketPrice') or \
                                 meta.get('regularMarketPrice') or last
                    # 전일 종가 대비 현재가 등락률 (자금흐름/국장타점 계산용)
                    live_change = ((live_price - prev) / prev) * 100

                    results[symbol] = {
                        'price': round(last, 2),        # 종가
                        'change': round(change, 2),     # 전일 종가 대비 오늘 종가 등락률
                        'volume': int(vol),
                        'live_price': round(live_price, 2),   # 현재가
                        'live_change': round(live_change, 2), # 전일 종가 대비 현재가 등락률
                    }
                else:
                    results[symbol] = {'error': 'Not enough data'}
            except Exception as e:
                results[symbol] = {'error': str(e)}

        payload = json.dumps(results).encode()

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
