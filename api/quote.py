from http.server import BaseHTTPRequestHandler
import urllib.request
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 쿼리에서 symbol 추출
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        symbol = params.get('symbol', ['SPY'])[0].upper()

        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            result = data['chart']['result'][0]
            closes = [v for v in result['indicators']['quote'][0]['close'] if v is not None]
            volumes = [v for v in result['indicators']['quote'][0].get('volume', []) if v is not None]

            if len(closes) < 2:
                raise ValueError('Not enough data')

            prev = closes[-2]
            last = closes[-1]
            vol = volumes[-1] if volumes else 0
            change = ((last - prev) / prev) * 100

            payload = json.dumps({
                'symbol': symbol,
                'price': round(last, 2),
                'change': round(change, 2),
                'volume': int(vol),
            }).encode()

        except Exception as e:
            payload = json.dumps({'symbol': symbol, 'error': str(e)}).encode()

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
