"""
GICS74 Radar - GitHub Actions 가격 수집기 v2
=============================================================
역할:
- 새 sectors_data.json 구조를 읽음
  [{id,gics11,industry,kr,hts_no,hts_name,has_kr_watchlist,tickers:[...]}]
- 국장 가격 수집 제거
- 미장 1,014개 티커만 Yahoo Finance v7 quote API로 수집
- 프리장 / 본장 / 애프터장 / 장마감 세션 판정
- cache/prices.json 저장

출력:
cache/prices.json

GitHub Actions:
기존 fetch_prices.yml이 `python fetch_prices.py`를 실행한다면 그대로 사용 가능.
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

# Yahoo quote API는 너무 크게 묶으면 실패할 수 있어서 50개 단위 권장
BATCH_SIZE = 50
REQUEST_DELAY = 0.15
TIMEOUT = 12


# ------------------------------------------------------------
# 미국장 시간 판정
# ------------------------------------------------------------
def nth_weekday_utc(year, month, weekday, nth, hour_utc):
    d = datetime(year, month, 1, hour_utc, 0, tzinfo=timezone.utc)
    days_until = (weekday - d.weekday()) % 7
    return d + timedelta(days=days_until + 7 * (nth - 1))


def is_us_dst(dt_utc):
    """
    미국 서머타임:
    - 3월 둘째 일요일 02:00 ET 시작 = 07:00 UTC
    - 11월 첫째 일요일 02:00 ET 종료 = 06:00 UTC
    """
    year = dt_utc.year
    dst_start = nth_weekday_utc(year, 3, 6, 2, 7)
    dst_end = nth_weekday_utc(year, 11, 6, 1, 6)
    return dst_start <= dt_utc < dst_end


def get_market_session(now_utc=None):
    """
    반환:
    - pre     : 04:00~09:30 ET
    - regular : 09:30~16:00 ET
    - after   : 16:00~20:00 ET
    - closed  : 그 외 / 주말
    """
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
# 데이터 로딩
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

    symbols = sorted(set(symbols))
    return symbols, symbol_meta, sectors


# ------------------------------------------------------------
# Yahoo Finance 조회
# ------------------------------------------------------------
def fetch_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_quote_batch(symbols):
    syms = ",".join(url_quote(s) for s in symbols)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}"
    data = fetch_json(url)
    rows = data.get("quoteResponse", {}).get("result", [])
    return {q.get("symbol", ""): q for q in rows if q.get("symbol")}


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
    return round((price - base) / base * 100, 2)


def unix_to_et_date(ts, offset_hours):
    try:
        dt_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        dt_et = dt_utc + timedelta(hours=offset_hours)
        return dt_et.strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_quote_row(symbol, q, session, offset_hours, meta):
    """
    핵심 원칙:
    - regular_* = 본장 기준
    - live_* = 현재 세션 기준
    - legacy price/change/live_price/live_change/volume도 같이 남겨서 프론트 전환 과정에서 안전장치 제공
    """

    prev_close = (
        safe_float(q.get("regularMarketPreviousClose"))
        or safe_float(q.get("regularMarketPreviousDayClose"))
        or safe_float(q.get("fiftyDayAverage"))  # 최후 fallback. 거의 쓰지 않음.
    )

    regular_price = safe_float(q.get("regularMarketPrice"))
    regular_change = safe_float(q.get("regularMarketChangePercent"))
    regular_volume = safe_int(q.get("regularMarketVolume"), 0)

    pre_price = safe_float(q.get("preMarketPrice"))
    pre_change = safe_float(q.get("preMarketChangePercent"))
    pre_volume = safe_int(q.get("preMarketVolume"), 0)

    post_price = safe_float(q.get("postMarketPrice"))
    post_change = safe_float(q.get("postMarketChangePercent"))
    post_volume = safe_int(q.get("postMarketVolume"), 0)

    # 전일 종가 fallback
    if prev_close is None:
        if regular_price is not None and regular_change not in (None, -100):
            # regular_change = (regular_price - prev_close) / prev_close * 100
            # prev_close = regular_price / (1 + change/100)
            denom = 1 + (regular_change / 100)
            if denom != 0:
                prev_close = regular_price / denom

    # regular change fallback
    if regular_change is None:
        regular_change = pct_change(regular_price, prev_close)

    # 현재 세션 live 값 선택
    live_session = session

    if session == "pre":
        live_price = pre_price if pre_price is not None else regular_price
        live_change = pre_change if pre_change is not None else pct_change(live_price, prev_close)
        live_volume = pre_volume
    elif session == "after":
        live_price = post_price if post_price is not None else regular_price
        live_change = post_change if post_change is not None else pct_change(live_price, prev_close)
        live_volume = post_volume
    elif session == "regular":
        live_price = regular_price
        live_change = regular_change
        live_volume = regular_volume
    else:
        # closed: 마지막 본장 값을 기본값으로 사용
        live_price = regular_price
        live_change = regular_change
        live_volume = regular_volume

    if live_change is None:
        live_change = pct_change(live_price, prev_close)

    regular_time = q.get("regularMarketTime")
    regular_date = unix_to_et_date(regular_time, offset_hours)

    if regular_price is None:
        raise ValueError("missing regularMarketPrice")

    if prev_close is None:
        # 그래도 데이터가 비면 regular price를 기준으로 0 처리
        prev_close = regular_price

    item = {
        # 메타
        "symbol": symbol,
        "display_ticker": meta.get("display_ticker", symbol),
        "name": meta.get("name", q.get("shortName") or q.get("longName") or ""),
        "gics11": meta.get("gics11", ""),
        "industry": meta.get("industry", ""),
        "market_cap_usd": meta.get("market_cap_usd", 0),
        "currency": q.get("currency", "USD"),
        "market_state_yahoo": q.get("marketState"),

        # 본장 기준
        "prev_close": round(float(prev_close), 4),
        "regular_price": round(float(regular_price), 4),
        "regular_change": round(float(regular_change or 0), 4),
        "regular_volume": regular_volume,
        "regular_dollar_volume": round(float(regular_price) * regular_volume, 2),
        "regular_market_time": regular_time,
        "regular_date": regular_date,

        # 프리/애프터 원자료
        "pre_price": round(float(pre_price), 4) if pre_price is not None else None,
        "pre_change": round(float(pre_change), 4) if pre_change is not None else None,
        "pre_volume": pre_volume,
        "post_price": round(float(post_price), 4) if post_price is not None else None,
        "post_change": round(float(post_change), 4) if post_change is not None else None,
        "post_volume": post_volume,

        # 현재 세션 기준
        "live_session": live_session,
        "live_price": round(float(live_price), 4) if live_price is not None else round(float(regular_price), 4),
        "live_change": round(float(live_change or 0), 4),
        "live_volume": live_volume,
        "live_dollar_volume": round(float(live_price or regular_price) * live_volume, 2),

        # 구버전 호환용 필드
        # 기존 index.html이 당장 깨지는 것을 줄이기 위한 안전장치
        "price": round(float(regular_price), 4),
        "change": round(float(regular_change or 0), 4),
        "volume": regular_volume,
    }

    return item


def collect_us(symbols, symbol_meta, session, offset_hours):
    results = {}
    failures = {}

    total = len(symbols)
    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        print(f"  [미장] {i}/{total} symbols")

        try:
            quote_rows = fetch_quote_batch(batch)
        except Exception as e:
            # 배치 전체 실패 시 개별 재시도
            print(f"  [미장] batch error: {e}")
            quote_rows = {}
            for sym in batch:
                try:
                    quote_rows.update(fetch_quote_batch([sym]))
                    time.sleep(REQUEST_DELAY)
                except Exception as e2:
                    failures[sym] = str(e2)

        for sym in batch:
            try:
                q = quote_rows.get(sym)
                if not q:
                    failures[sym] = "missing from Yahoo quote response"
                    results[sym] = None
                    continue

                results[sym] = parse_quote_row(
                    sym,
                    q,
                    session=session,
                    offset_hours=offset_hours,
                    meta=symbol_meta.get(sym, {}),
                )

            except Exception as e:
                failures[sym] = str(e)
                results[sym] = None

        time.sleep(REQUEST_DELAY)

    ok = sum(1 for v in results.values() if v)
    print(f"  [미장] 완료: {ok}/{total}, 실패 {len(failures)}")
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
    print("GICS74 가격 수집 시작")
    print("UTC:", now_utc.isoformat())
    print("ET :", now_et.strftime("%Y-%m-%d %H:%M:%S"))
    print("KST:", now_kst.isoformat())
    print("SESSION:", session)
    print("=" * 70)

    symbols, symbol_meta, sectors = load_all_us_symbols()
    print(f"미장 티커 {len(symbols)}개 / GICS 섹터 {len(sectors)}개")

    us_data, failures = collect_us(symbols, symbol_meta, session, offset_hours)

    date_key = infer_regular_date(us_data, now_et.strftime("%Y-%m-%d"))

    # 본장 중에는 아직 확정값이 움직이는 상태.
    # 프리/애프터/마감/주말에는 마지막 본장 regular_*가 고정값 역할.
    regular_final_locked = session != "regular"

    output = {
        "schema_version": "gics74_prices_v2",
        "updated_at": now_utc.isoformat(),
        "updated_at_kst": now_kst.isoformat(),
        "date_key": date_key,
        "session": session,
        "is_us_dst": is_us_dst(now_utc),
        "regular_final_locked": regular_final_locked,
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
