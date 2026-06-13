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
                url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d'
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())

                result = data['chart']['result'][0]
                closes = [v for v in result['indicators']['quote'][0]['close'] if v is not None]
                volumes = [v for v in result['indicators']['quote'][0].get('volume', []) if v is not None]

                if len(closes) >= 2:
                    prev = closes[-2]
                    last = closes[-1]
                    vol = volumes[-1] if volumes else 0
                    change = ((last - prev) / prev) * 100
                    results[symbol] = {
                        'price': round(last, 2),
                        'change': round(change, 2),
                        'volume': int(vol),
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
