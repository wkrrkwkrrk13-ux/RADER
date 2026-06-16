from http.server import BaseHTTPRequestHandler
import urllib.request
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, quote

# ------------------------------------------------------------
# US market session helpers
# ------------------------------------------------------------

def nth_weekday_utc(year, month, weekday, nth, hour_utc):
    d = datetime(year, month, 1, hour_utc, 0, tzinfo=timezone.utc)
    days_until = (weekday - d.weekday()) % 7
    return d + timedelta(days=days_until + 7 * (nth - 1))


def is_us_dst(dt_utc):
    year = dt_utc.year
    dst_start = nth_weekday_utc(year, 3, 6, 2, 7)   # 3월 둘째 일요일 02:00 ET = 07:00 UTC
    dst_end = nth_weekday_utc(year, 11, 6, 1, 6)    # 11월 첫째 일요일 02:00 ET = 06:00 UTC
    return dst_start <= dt_utc < dst_end


def get_market_session(now_utc=None):
    """
    반환값:
    - regular : 정규장 09:30~16:00 ET
    - pre     : 프리마켓 09:00~09:30 ET, 요구사항 기준
    - closed  : 그 외
    """
    now_utc = now_utc or datetime.now(timezone.utc)

    if now_utc.weekday() >= 5:
        return 'closed'

    offset_hours = -4 if is_us_dst(now_utc) else -5
    et = now_utc + timedelta(hours=offset_hours)
    minutes = et.hour * 60 + et.minute

    pre_start = 9 * 60
    regular_start = 9 * 60 + 30
    regular_end = 16 * 60

    if regular_start <= minutes < regular_end:
        return 'regular'
    if pre_start <= minutes < regular_start:
        return 'pre'
    return 'closed'


def fetch_yahoo_json(url, timeout=8):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_v7_quote(symbol):
    """
    Yahoo v7 quote fallback.
    기대 구조:
    {
      "quoteResponse": {
        "result": [
          {
            "regularMarketPrice": ...,
            "regularMarketChangePercent": ...,
            "regularMarketVolume": ...
          }
        ]
      }
    }
    """
    url = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={quote(symbol)}'
    data = fetch_yahoo_json(url)
    rows = data.get('quoteResponse', {}).get('result', [])
    if not rows:
        return None
    q = rows[0]

    price = q.get('regularMarketPrice')
    change_pct = q.get('regularMarketChangePercent')
    volume = q.get('regularMarketVolume') or q.get('averageDailyVolume3Month') or 0

    if price is None or change_pct is None:
        return None

    return {
        'price': float(price),
        'change': float(change_pct),
        'volume': int(volume or 0),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        symbols_raw = params.get('symbols', params.get('symbol', ['SPY']))[0]
        symbols = [s.strip().upper() for s in symbols_raw.split(',')][:20]

        session = get_market_session()

        results = {}
        for symbol in symbols:
            try:
                # 1) 일봉 데이터: 신규 상장 방어를 위해 1mo
                url_daily = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo'
                data = fetch_yahoo_json(url_daily)

                result = data['chart']['result'][0]
                quote_data = result['indicators']['quote'][0]

                raw_closes = quote_data.get('close', []) or []
                raw_volumes = quote_data.get('volume', []) or []

                # close/volume 인덱스를 맞춰서 유효봉 생성
                bars = []
                for i, close in enumerate(raw_closes):
                    if close is None:
                        continue
                    volume = raw_volumes[i] if i < len(raw_volumes) and raw_volumes[i] is not None else 0
                    bars.append({'close': float(close), 'volume': int(volume or 0)})

                if not bars:
                    # v8이 비어 있으면 v7 직접 fallback
                    v7 = fetch_v7_quote(symbol)
                    if v7:
                        results[symbol] = {
                            'price': round(v7['price'], 2),
                            'change': round(v7['change'], 2),
                            'volume': int(v7['volume']),
                            'live_price': round(v7['price'], 2),
                            'live_change': round(v7['change'], 2),
                            'session': session,
                            'source': 'v7_empty_v8',
                        }
                    else:
                        results[symbol] = {'error': 'No price data available'}
                    continue

                closes = [b['close'] for b in bars]
                volumes = [b['volume'] for b in bars]

                if session == 'regular' and len(closes) >= 3:
                    # 정규장 중: closes[-1]은 장중 미확정 캔들일 수 있음
                    prev_for_change = closes[-3]
                    last = closes[-2]
                    vol = volumes[-2] if len(volumes) >= 2 else (volumes[-1] if volumes else 0)

                elif session == 'regular' and len(closes) == 2:
                    # 신규 상장 + 정규장 중: 종가 패널 오염 방지
                    last = closes[-2]
                    prev_for_change = closes[-2]
                    vol = volumes[-2] if len(volumes) >= 2 else (volumes[-1] if volumes else 0)

                elif len(closes) >= 2:
                    # 프리/장외/주말: 최신 확정 종가 기준
                    prev_for_change = closes[-2]
                    last = closes[-1]
                    vol = volumes[-1] if volumes else 0

                else:
                    # 데이터 1개뿐인 신규상장 초기
                    last = closes[-1]
                    meta_open = result.get('meta', {}).get('regularMarketOpen')
                    prev_for_change = float(meta_open) if meta_open else last
                    vol = volumes[-1] if volumes else 0

                if not prev_for_change:
                    results[symbol] = {'error': 'Invalid previous close'}
                    continue

                change = ((last - prev_for_change) / prev_for_change) * 100

                # 1차 기본값: 장외에는 현재가 패널도 종가와 동일
                live_price = last
                live_change = change
                source = 'v8_chart'

                # 2) 신규상장/오염 의심 fallback
                # change=0인데 거래량이 있으면 v8 일봉 인덱스가 오염됐을 가능성이 있음.
                # 단, 진짜 보합도 있을 수 있으므로 v7 성공 시에만 교체.
                if len(closes) <= 5 and len(closes) >= 2 and abs(closes[-1] - closes[-2]) < 0.0001 and vol > 0:
                    try:
                        v7 = fetch_v7_quote(symbol)
                        if v7:
                            last = v7['price']
                            change = v7['change']
                            vol = v7['volume'] or vol
                            live_price = last
                            live_change = change
                            source = 'v7_fallback_zero_change'
                    except Exception:
                        pass

                # 3) 현재가: 정규장/프리마켓에서만 1분봉 사용
                if session in ('regular', 'pre'):
                    url_live = (
                        f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
                        f'?interval=1m&range=1d&includePrePost=true'
                    )
                    try:
                        data2 = fetch_yahoo_json(url_live)
                        result2 = data2['chart']['result'][0]
                        quote2 = result2['indicators']['quote'][0]
                        closes2 = [float(v) for v in quote2.get('close', []) if v is not None]

                        if closes2:
                            live_price = closes2[-1]
                            # 현재가 등락률은 종가 패널 기준가(last) 대비
                            live_change = ((live_price - last) / last) * 100 if last else change
                    except Exception:
                        live_price = last
                        live_change = change

                results[symbol] = {
                    'price': round(last, 2),
                    'change': round(change, 2),
                    'volume': int(vol or 0),
                    'live_price': round(live_price, 2),
                    'live_change': round(live_change, 2),
                    'session': session,
                    'source': source,
                    'close_count': len(closes),
                }

            except Exception as e:
                results[symbol] = {'error': str(e)}

        payload = json.dumps(results).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
