from http.server import BaseHTTPRequestHandler
import urllib.request
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

# ------------------------------------------------------------
# US market session helpers
# - 기준: 미국 동부시간
# - 정규장: 09:30~16:00 ET
# - 프리마켓: 09:00~09:30 ET  (요구사항 기준)
# - 완전 닫힌 시간: live_price/live_change를 종가와 동일하게 고정
# ------------------------------------------------------------

def nth_weekday_utc(year, month, weekday, nth, hour_utc):
    """
    month: 1~12
    weekday: Monday=0 ... Sunday=6
    nth: 1,2,...
    """
    d = datetime(year, month, 1, hour_utc, 0, tzinfo=timezone.utc)
    days_until = (weekday - d.weekday()) % 7
    return d + timedelta(days=days_until + 7 * (nth - 1))


def is_us_dst(dt_utc):
    """
    미국 동부 기준 DST:
    - 시작: 3월 두 번째 일요일 02:00 ET = 07:00 UTC
    - 종료: 11월 첫 번째 일요일 02:00 ET = 06:00 UTC
    """
    year = dt_utc.year
    dst_start = nth_weekday_utc(year, 3, 6, 2, 7)
    dst_end = nth_weekday_utc(year, 11, 6, 1, 6)
    return dst_start <= dt_utc < dst_end


def get_market_session(now_utc=None):
    """
    반환값:
    - 'regular' : 정규장
    - 'pre'     : 프리마켓, 요구사항상 09:00~09:30 ET만 허용
    - 'closed'  : 그 외
    """
    now_utc = now_utc or datetime.now(timezone.utc)

    if now_utc.weekday() >= 5:
        return 'closed'

    # DST면 ET = UTC-4, 표준시면 ET = UTC-5
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
                # 1) 일봉 데이터
                url_daily = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=7d'
                req = urllib.request.Request(url_daily, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())

                result = data['chart']['result'][0]
                quote = result['indicators']['quote'][0]
                closes = [v for v in quote.get('close', []) if v is not None]
                volumes = [v for v in quote.get('volume', []) if v is not None]

                if len(closes) < 2:
                    results[symbol] = {'error': 'Not enough data'}
                    continue

                if session == 'regular' and len(closes) >= 3:
                    # 정규장 중에는 일봉 마지막값이 장중값일 수 있으므로
                    # 확정 종가는 closes[-2], 전일 대비 change 기준은 closes[-3]
                    prev_for_change = closes[-3]
                    last = closes[-2]
                    vol = volumes[-2] if len(volumes) >= 2 else (volumes[-1] if volumes else 0)
                else:
                    # 프리마켓/장외/주말에는 가장 최근 확정 종가 기준
                    prev_for_change = closes[-2]
                    last = closes[-1]
                    vol = volumes[-1] if volumes else 0

                if not prev_for_change:
                    results[symbol] = {'error': 'Invalid previous close'}
                    continue

                change = ((last - prev_for_change) / prev_for_change) * 100

                # 기본값: 완전 닫힌 시간에는 현재가 패널도 종가와 동일하게 고정
                live_price = last
                live_change = change

                # 2) 현재가: 정규장/프리마켓에서만 1분봉 사용
                if session in ('regular', 'pre'):
                    url_live = (
                        f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
                        f'?interval=1m&range=1d&includePrePost=true'
                    )
                    try:
                        req2 = urllib.request.Request(url_live, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Accept': 'application/json',
                        })
                        with urllib.request.urlopen(req2, timeout=8) as resp2:
                            data2 = json.loads(resp2.read().decode())

                        result2 = data2['chart']['result'][0]
                        quote2 = result2['indicators']['quote'][0]
                        closes2 = [v for v in quote2.get('close', []) if v is not None]

                        if closes2:
                            live_price = closes2[-1]
                            # live_change = 전일 확정 종가(last) 대비 현재가 등락률
                            live_change = ((live_price - last) / last) * 100 if last else change
                    except Exception:
                        # live 조회 실패 시 종가 기준으로 fallback
                        live_price = last
                        live_change = change

                results[symbol] = {
                    'price': round(last, 2),
                    'change': round(change, 2),
                    'volume': int(vol or 0),
                    'live_price': round(live_price, 2),
                    'live_change': round(live_change, 2),
                    'session': session,
                }

            except Exception as e:
                results[symbol] = {'error': str(e)}

        payload = json.dumps(results).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(payload)
