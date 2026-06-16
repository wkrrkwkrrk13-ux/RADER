from http.server import BaseHTTPRequestHandler
import urllib.request
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, quote as url_quote

def nth_weekday_utc(year, month, weekday, nth, hour_utc):
    d = datetime(year, month, 1, hour_utc, 0, tzinfo=timezone.utc)
    days_until = (weekday - d.weekday()) % 7
    return d + timedelta(days=days_until + 7 * (nth - 1))

def is_us_dst(dt_utc):
    year = dt_utc.year
    dst_start = nth_weekday_utc(year, 3, 6, 2, 7)
    dst_end   = nth_weekday_utc(year, 11, 6, 1, 6)
    return dst_start <= dt_utc < dst_end

def get_market_session(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        return 'closed'
    offset_hours = -4 if is_us_dst(now_utc) else -5
    et = now_utc + timedelta(hours=offset_hours)
    minutes = et.hour * 60 + et.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return 'regular'
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return 'pre'
    if 16 * 60 <= minutes < 20 * 60:
        return 'after'
    return 'closed'

def fetch_json(url, timeout=8):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def fetch_v7(symbols):
    """v7 quote — 메인 데이터소스"""
    syms = ','.join(url_quote(s) for s in symbols)
    url = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}'
    data = fetch_json(url)
    rows = data.get('quoteResponse', {}).get('result', [])
    out = {}
    for q in rows:
        sym = q.get('symbol','')
        out[sym] = q
    return out

def fetch_v8_close(symbol):
    """v8 chart fallback — 종가 보조용"""
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol)}?interval=1d&range=5d'
    data = fetch_json(url)
    result = data['chart']['result'][0]
    quote = result['indicators']['quote'][0]
    closes = [v for v in quote.get('close', []) if v is not None]
    volumes = [v for v in quote.get('volume', []) if v is not None]
    return closes, volumes

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        symbols_raw = params.get('symbols', params.get('symbol', ['SPY']))[0]
        symbols = [s.strip().upper() for s in symbols_raw.split(',')][:20]
        session = get_market_session()

        # v7 배치 조회
        try:
            v7_data = fetch_v7(symbols)
        except Exception:
            v7_data = {}

        results = {}
        for symbol in symbols:
            try:
                q = v7_data.get(symbol)

                if q:
                    # ── v7 메인 경로 ──
                    price      = q.get('regularMarketPrice')
                    change_pct = q.get('regularMarketChangePercent')
                    volume     = q.get('regularMarketVolume') or 0

                    if price is None or change_pct is None:
                        raise ValueError('v7 missing price/change')

                    # 현재가 패널
                    if session == 'pre':
                        pm_price    = q.get('preMarketPrice')
                        pm_change   = q.get('preMarketChangePercent')
                        live_price  = pm_price  if pm_price  is not None else price
                        live_change = pm_change if pm_change is not None else change_pct
                    elif session == 'after':
                        post_price  = q.get('postMarketPrice')
                        post_change = q.get('postMarketChangePercent')
                        live_price  = post_price  if post_price  is not None else price
                        live_change = post_change if post_change is not None else change_pct
                    elif session == 'regular':
                        live_price  = price
                        live_change = change_pct
                    else:  # closed
                        live_price  = price
                        live_change = change_pct

                    results[symbol] = {
                        'price':       round(float(price), 2),
                        'change':      round(float(change_pct), 2),
                        'volume':      int(volume),
                        'live_price':  round(float(live_price), 2),
                        'live_change': round(float(live_change), 2),
                        'session':     session,
                        'source':      'v7',
                    }

                else:
                    # ── v8 fallback ──
                    closes, volumes = fetch_v8_close(symbol)
                    if len(closes) < 2:
                        # 신규 상장 데이터 1개
                        last = closes[-1] if closes else None
                        if last is None:
                            results[symbol] = {'error': 'No data'}
                            continue
                        results[symbol] = {
                            'price': round(last, 2), 'change': 0.0,
                            'volume': int(volumes[-1]) if volumes else 0,
                            'live_price': round(last, 2), 'live_change': 0.0,
                            'session': session, 'source': 'v8_new_listing',
                        }
                        continue

                    last = closes[-1]
                    prev = closes[-2]
                    vol  = volumes[-1] if volumes else 0
                    change = ((last - prev) / prev) * 100 if prev else 0

                    results[symbol] = {
                        'price':       round(last, 2),
                        'change':      round(change, 2),
                        'volume':      int(vol),
                        'live_price':  round(last, 2),
                        'live_change': round(change, 2),
                        'session':     session,
                        'source':      'v8_fallback',
                    }

            except Exception as e:
                results[symbol] = {'error': str(e)}

        payload = json.dumps(results).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
