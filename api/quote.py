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
                # 1) 종가 데이터 (5일치 일봉)
                url_daily = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d'
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

                prev = closes[-2]
                last = closes[-1]   # 정규장 종가 (고정)
                vol = volumes[-1] if volumes else 0
                change = ((last - prev) / prev) * 100

                # 2) 현재가 데이터 (1분봉, 프리/애프터 포함)
                url_live = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d&includePrePost=true'
                req2 = urllib.request.Request(url_live, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
                live_price = last  # 기본값: 종가
                try:
                    with urllib.request.urlopen(req2, timeout=8) as resp2:
                        data2 = json.loads(resp2.read().decode())
                    result2 = data2['chart']['result'][0]
                    closes2 = [v for v in result2['indicators']['quote'][0]['close'] if v is not None]
                    if closes2:
                        live_price = closes2[-1]  # 가장 최근 1분봉 종가
                except Exception:
                    pass  # 실패 시 종가 사용

                # 전일 종가 대비 현재가 등락률
                live_change = ((live_price - prev) / prev) * 100

                results[symbol] = {
                    'price': round(last, 2),              # 정규장 종가
                    'change': round(change, 2),            # 전일 종가 대비 등락률
                    'volume': int(vol),
                    'live_price': round(live_price, 2),    # 현재가 (프리/애프터 포함)
                    'live_change': round(live_change, 2),  # 전일 종가 대비 현재가 등락률
                }

            except Exception as e:
                results[symbol] = {'error': str(e)}

        payload = json.dumps(results).encode()

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
