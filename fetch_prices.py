"""
GICS74 Radar - GitHub Actions 가격 수집기 v2.1
=============================================================
수정 이유:
- GitHub Actions 환경에서 Yahoo v7 finance/quote batch API가 401 Unauthorized 발생
- 따라서 기존에 동작하던 Yahoo v8 finance/chart API를 기본 조회 방식으로 사용

역할:
- 새 sectors_data.json 구조를 읽음
- 국장 가격 수집 제거
- 미장 1,014개 티커만 Yahoo Finance v8 chart API로 수집
- pre / regular / after / closed 세션 판정
- regular_* / live_* 필드 분리
- cache/prices.json 저장

실행:
python fetch_prices.py
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_quote
from collections import Counter

SECTORS_PATH = "sectors_data.json"
OUTPUT_PATH = "cache/prices.json"

REQUEST_DELAY = 0.12
TIMEOUT = 12
MAX_RETRIES = 2


# ------------------------------------------------------------
# 미국장 시간 판정
# ------------------------------------------------------------
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
    offset_hours = -4 if is_us_dst(now_utc) else -5
    et = now_utc + timedelta(hours=offset_hours)

    if et.weekday() >= 5:
        return "closed", et, offset_hours

    minutes = et.hour * 60 + et.minute

    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre", et, offset_hours
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular", et, offset_hours
    if 16 * 60 <= minutes < 20 * 60:
        return "after", et, offset_hours

    return "closed", et, offset_hours


# ------------------------------------------------------------
# 공통 유틸
# ------------------------------------------------------------
def safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def safe_int(v, default=0):
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


def pct_change(price, base):
    price = safe_float(price)
    base = safe_float(base)
    if price is None or base is None or base == 0:
        return None
    return round((price - base) / base * 100, 4)


def unix_to_et_date(ts, offset_hours):
    try:
        dt_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        dt_et = dt_utc + timedelta(hours=offset_hours)
        return dt_et.strftime("%Y-%m-%d")
    except Exception:
        return None


def fetch_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
            "Connection": "keep-alive",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------
# sectors_data.json 로딩
# ------------------------------------------------------------
def load_all_us_symbols():
    with open(SECTORS_PATH, encoding="utf-8") as f:
        sectors = json.load(f)

    symbols = []
    symbol_meta = {}

    for sector in sectors:
        industry = sector.get("industry")
        gics11 = sector.get("gics11")

        for item in sector.get("tickers", []):
            ticker = str(item.get("ticker", "")).strip().upper()
            if not ticker:
                continue

            symbols.append(ticker)
            symbol_meta[ticker] = {
                "display_ticker": item.get("display_ticker", ticker),
                "name": item.get("name", ""),
                "market_cap_usd": item.get("market_cap_usd", 0),
                "industry": industry,
                "gics11": gics11,
            }

    return sorted(set(symbols)), symbol_meta, sectors


# ------------------------------------------------------------
# Yahoo v8 chart 조회
# ------------------------------------------------------------
def fetch_chart_daily(symbol):
    """
    GitHub Actions에서 v7 quote가 401로 막힐 때가 있어 v8 chart를 기본 사용.
    1d/5d는 본장 종가·거래량 계산에 비교적 안정적.
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol)}"
        f"?interval=1d&range=5d&includePrePost=true"
    )
    return fetch_json(url)


def fetch_chart_intraday(symbol):
    """
    프리장/애프터장 실시간 보조용.
    실패해도 본장 데이터만 있으면 레이더는 작동.
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol)}"
        f"?interval=1m&range=1d&includePrePost=true"
    )
    return fetch_json(url, timeout=TIMEOUT)


def extract_last_valid_daily(chart_data):
    result = (chart_data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None, None, None, None

    meta = result.get("meta", {}) or {}
    quote = (result.get("indicators", {}).get("quote") or [{}])[0] or {}
    timestamps = result.get("timestamp") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    valid = []
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        vol = volumes[i] if i < len(volumes) else None
        if close is not None:
            valid.append((ts, safe_float(close), safe_int(vol, 0)))

    return meta, valid, quote, result


def extract_intraday_live(chart_data):
    result = (chart_data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None

    meta = result.get("meta", {}) or {}
    quote = (result.get("indicators", {}).get("quote") or [{}])[0] or {}
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    # 마지막 유효 1분봉
    for i in range(len(closes) - 1, -1, -1):
        close = closes[i]
        if close is not None:
            vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
            return {
                "price": safe_float(close),
                "volume": safe_int(vol, 0),
                "meta": meta,
            }

    return None


def parse_symbol_v8(symbol, session, offset_hours, meta_info):
    """
    반환 원칙:
    - regular_* = 본장 기준
    - live_* = 현재 세션 기준
    - 프리/애프터 실시간 가격은 가능하면 1m chart에서 가져옴
    - 프리/애프터 거래량은 Yahoo가 누적 세션 거래량을 안 줄 수 있으므로 없으면 0
    """

    daily_data = None
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            daily_data = fetch_chart_daily(symbol)
            break
        except Exception as e:
            last_error = e
            time.sleep(0.5 + attempt * 0.5)

    if daily_data is None:
        raise RuntimeError(f"v8 daily failed: {last_error}")

    meta, valid, quote, result = extract_last_valid_daily(daily_data)

    if not meta:
        raise ValueError("v8 chart missing meta")
    if not valid:
        raise ValueError("v8 chart has no valid daily close")

    # Yahoo meta 우선
    regular_price = safe_float(meta.get("regularMarketPrice"))
    prev_close = (
        safe_float(meta.get("regularMarketPreviousClose"))
        or safe_float(meta.get("previousClose"))
        or safe_float(meta.get("chartPreviousClose"))
    )
    regular_volume = safe_int(meta.get("regularMarketVolume"), 0)
    regular_time = meta.get("regularMarketTime")

    # daily valid fallback
    if len(valid) >= 2:
        daily_prev_close = valid[-2][1]
        daily_last_close = valid[-1][1]
        daily_last_volume = valid[-1][2]
    else:
        daily_prev_close = prev_close
        daily_last_close = valid[-1][1]
        daily_last_volume = valid[-1][2]

    if regular_price is None:
        regular_price = daily_last_close

    if prev_close is None or prev_close == 0:
        prev_close = daily_prev_close if daily_prev_close else regular_price

    if not regular_volume:
        regular_volume = daily_last_volume or 0

    regular_change = pct_change(regular_price, prev_close)
    if regular_change is None:
        regular_change = 0.0

    # pre/post meta가 있으면 먼저 사용
    pre_price = safe_float(meta.get("preMarketPrice"))
    post_price = safe_float(meta.get("postMarketPrice"))
    pre_volume = safe_int(meta.get("preMarketVolume"), 0)
    post_volume = safe_int(meta.get("postMarketVolume"), 0)

    pre_change = pct_change(pre_price, prev_close) if pre_price is not None else None
    post_change = pct_change(post_price, prev_close) if post_price is not None else None

    # 프리/애프터/본장 실시간 보조: 1m chart
    intraday = None
    if session in ("pre", "regular", "after"):
        try:
            intraday_data = fetch_chart_intraday(symbol)
            intraday = extract_intraday_live(intraday_data)
        except Exception:
            intraday = None

    if session == "pre":
        live_price = pre_price if pre_price is not None else (intraday["price"] if intraday else regular_price)
        live_change = pre_change if pre_change is not None else pct_change(live_price, prev_close)
        live_volume = pre_volume
        if not live_volume and intraday:
            # 1분봉의 마지막 봉 거래량일 뿐이라 세션 누적 거래량은 아님.
            live_volume = intraday.get("volume", 0)

    elif session == "after":
        live_price = post_price if post_price is not None else (intraday["price"] if intraday else regular_price)
        live_change = post_change if post_change is not None else pct_change(live_price, prev_close)
        live_volume = post_volume
        if not live_volume and intraday:
            live_volume = intraday.get("volume", 0)

    elif session == "regular":
        live_price = intraday["price"] if intraday and intraday.get("price") else regular_price
        live_change = pct_change(live_price, prev_close)
        live_volume = regular_volume

    else:
        live_price = regular_price
        live_change = regular_change
        live_volume = regular_volume

    if live_change is None:
        live_change = regular_change

    regular_date = unix_to_et_date(regular_time, offset_hours)
    if regular_date is None and valid:
        regular_date = unix_to_et_date(valid[-1][0], offset_hours)

    item = {
        "symbol": symbol,
        "display_ticker": meta_info.get("display_ticker", symbol),
        "name": meta_info.get("name", meta.get("shortName") or meta.get("longName") or ""),
        "gics11": meta_info.get("gics11", ""),
        "industry": meta_info.get("industry", ""),
        "market_cap_usd": meta_info.get("market_cap_usd", 0),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("exchangeName"),
        "instrument_type": meta.get("instrumentType"),

        "prev_close": round(float(prev_close), 4),
        "regular_price": round(float(regular_price), 4),
        "regular_change": round(float(regular_change), 4),
        "regular_volume": regular_volume,
        "regular_dollar_volume": round(float(regular_price) * regular_volume, 2),
        "regular_market_time": regular_time,
        "regular_date": regular_date,

        "pre_price": round(float(pre_price), 4) if pre_price is not None else None,
        "pre_change": round(float(pre_change), 4) if pre_change is not None else None,
        "pre_volume": pre_volume,

        "post_price": round(float(post_price), 4) if post_price is not None else None,
        "post_change": round(float(post_change), 4) if post_change is not None else None,
        "post_volume": post_volume,

        "live_session": session,
        "live_price": round(float(live_price), 4),
        "live_change": round(float(live_change), 4),
        "live_volume": live_volume,
        "live_dollar_volume": round(float(live_price) * live_volume, 2),

        # 구버전 호환 필드
        "price": round(float(regular_price), 4),
        "change": round(float(regular_change), 4),
        "volume": regular_volume,

        "source": "v8_chart",
    }

    return item


def collect_us(symbols, symbol_meta, session, offset_hours):
    results = {}
    failures = {}

    total = len(symbols)

    for idx, sym in enumerate(symbols):
        if idx % 50 == 0:
            print(f"  [미장] {idx}/{total} symbols")

        try:
            results[sym] = parse_symbol_v8(
                sym,
                session=session,
                offset_hours=offset_hours,
                meta_info=symbol_meta.get(sym, {}),
            )
        except Exception as e:
            failures[sym] = str(e)
            results[sym] = None

        time.sleep(REQUEST_DELAY)

    ok = sum(1 for v in results.values() if v)
    print(f"  [미장] 완료: {ok}/{total}, 실패 {len(failures)}")

    if failures:
        print("  [미장] 실패 일부:")
        for sym, err in list(failures.items())[:20]:
            print(f"    - {sym}: {err}")

    return results, failures


def infer_regular_date(us_data, fallback_et_date):
    dates = [
        item.get("regular_date")
        for item in us_data.values()
        if item and item.get("regular_date")
    ]
    if not dates:
        return fallback_et_date
    return Counter(dates).most_common(1)[0][0]


def main():
    now_utc = datetime.now(timezone.utc)
    session, now_et, offset_hours = get_market_session(now_utc)
    kst = timezone(timedelta(hours=9))
    now_kst = now_utc.astimezone(kst)

    print("=" * 70)
    print("GICS74 가격 수집 시작 v2.1")
    print("UTC:", now_utc.isoformat())
    print("ET :", now_et.strftime("%Y-%m-%d %H:%M:%S"))
    print("KST:", now_kst.isoformat())
    print("SESSION:", session)
    print("SOURCE: Yahoo v8 chart")
    print("=" * 70)

    symbols, symbol_meta, sectors = load_all_us_symbols()
    print(f"미장 티커 {len(symbols)}개 / GICS 섹터 {len(sectors)}개")

    us_data, failures = collect_us(symbols, symbol_meta, session, offset_hours)

    date_key = infer_regular_date(us_data, now_et.strftime("%Y-%m-%d"))

    # 본장 중에는 본장값이 움직이는 상태. 본장 밖에서는 마지막 regular_*를 확정값으로 봄.
    regular_final_locked = session != "regular"

    output = {
        "schema_version": "gics74_prices_v2_1",
        "updated_at": now_utc.isoformat(),
        "updated_at_kst": now_kst.isoformat(),
        "date_key": date_key,
        "session": session,
        "is_us_dst": is_us_dst(now_utc),
        "regular_final_locked": regular_final_locked,
        "source": "yahoo_v8_chart",
        "counts": {
            "sectors": len(sectors),
            "symbols": len(symbols),
            "ok": sum(1 for v in us_data.values() if v),
            "failed": len(failures),
        },
        "failures": failures,
        "us": us_data,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print("저장 완료:", OUTPUT_PATH)
    print("date_key:", date_key)
    print("regular_final_locked:", regular_final_locked)
    print("failures:", len(failures))
    print("=" * 70)


if __name__ == "__main__":
    main()
