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
    dst_end = nth_weekday_utc(year, 11, 6, 1, 6)
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
    syms = ','.join(url_quote(s) for s in symbols)
    url = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}'
    data = fetch_json(url)
    rows = data.get('quoteResponse', {}).get('result', [])
    return {q.get('symbol', ''): q for q in rows if q.get('symbol')}

def fetch_v6(symbols):
    syms = ','.join(url_quote(s) for s in symbols)
    url = f'https://query1.finance.yahoo.com/v6/finance/quote?symbols={syms}'
    data = fetch_json(url)
    rows = data.get('quoteResponse', {}).get('result', [])
    return {q.get('symbol', ''): q for q in rows if q.get('symbol')}

def fetch_v8_chart(symbol):
    url = (
        f'https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol)}'
        f'?interval=1m&range=1d&includePrePost=true'
    )
    data = fetch_json(url)
    result = data['chart']['result'][0]
    meta = result.get('meta', {}) or {}
    quote = result.get('indicators', {}).get('quote', [{}])[0]

    closes = [float(v) for v in (quote.get('close') or []) if v is not None]
    volumes = [int(v or 0) for v in (quote.get('volume') or []) if v is not None]
    return meta, closes, volumes

def quote_from_yahoo_row(q, session):
    price = q.get('regularMarketPrice')
    change_pct = q.get('regularMarketChangePercent')
    volume = q.get('regularMarketVolume') or 0

    if price is None or change_pct is None:
        raise ValueError('quote missing regularMarketPrice/changePercent')

    if session == 'pre':
        pm_price = q.get('preMarketPrice')
        pm_change = q.get('preMarketChangePercent')
        live_price = pm_price if pm_price is not None else price
        live_change = pm_change if pm_change is not None else change_pct
    elif session == 'after':
        post_price = q.get('postMarketPrice')
        post_change = q.get('postMarketChangePercent')
        live_price = post_price if post_price is not None else price
        live_change = post_change if post_change is not None else change_pct
    else:
        live_price = price
        live_change = change_pct

    return {
        'price': round(float(price), 2),
        'change': round(float(change_pct), 2),
        'volume': int(volume or 0),
        'live_price': round(float(live_price), 2),
        'live_change': round(float(live_change), 2),
    }

def quote_from_v8_meta(symbol, session):
    meta, closes_1m, volumes_1m = fetch_v8_chart(symbol)

    price = meta.get('regularMarketPrice')
    prev_close = (
        meta.get('regularMarketPreviousClose')
        or meta.get('previousClose')
        or meta.get('chartPreviousClose')
    )
    volume = meta.get('regularMarketVolume') or (volumes_1m[-1] if volumes_1m else 0)

    if price is None:
        if closes_1m:
            price = closes_1m[-1]
        else:
            raise ValueError('v8 meta missing regularMarketPrice')

    price = float(price)

    if prev_close is not None and float(prev_close) != 0:
        change_pct = ((price - float(prev_close)) / float(prev_close)) * 100
    else:
        change_pct = 0.0

    if session in ('pre', 'after') and closes_1m:
        live_price = float(closes_1m[-1])
        if price is not None and float(price) != 0:
            live_change = ((live_price - float(price)) / float(price)) * 100
        else:
            live_change = change_pct
    else:
        live_price = price
        live_change = change_pct

    return {
        'price': round(price, 2),
        'change': round(change_pct, 2),
        'volume': int(volume or 0),
        'live_price': round(float(live_price), 2),
        'live_change': round(float(live_change), 2),
        'meta_prev_close': round(float(prev_close), 2) if prev_close else None,
        'close_count_1m': len(closes_1m),
    }

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        symbols_raw = params.get('symbols', params.get('symbol', ['SPY']))[0]
        symbols = [s.strip().upper() for s in symbols_raw.split(',')][:20]
        session = get_market_session()

        quote_data = {}
        quote_source = None
        try:
            quote_data = fetch_v7(symbols)
            quote_source = 'v7'
        except Exception:
            try:
                quote_data = fetch_v6(symbols)
                quote_source = 'v6'
            except Exception:
                quote_data = {}
                quote_source = None

        results = {}
        for symbol in symbols:
            try:
                q = quote_data.get(symbol)
                if q:
                    item = quote_from_yahoo_row(q, session)
                    item['session'] = session
                    item['source'] = quote_source
                    results[symbol] = item
                    continue

                item = quote_from_v8_meta(symbol, session)
                item['session'] = session
                item['source'] = 'v8_meta'
                results[symbol] = item

            except Exception as e:
                results[symbol] = {'error': str(e), 'session': session}

        payload = json.dumps(results).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
