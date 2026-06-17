"""
GitHub Actions 백그라운드 가격 수집기
=============================================================
주기적으로(예: 10분마다) 실행되어 미장+국장 전체 종목 가격을
수집하고 cache/prices.json 으로 저장.
레이더 웹페이지는 이 JSON 파일만 읽어서 즉시 렌더링.

실행: python fetch_prices.py
출력: cache/prices.json
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

SECTORS_PATH = "sectors_data.json"   # {n, kr, t:[...], k:[{c,n,y}]} 93개
OUTPUT_PATH = "cache/prices.json"
BATCH_SIZE = 30
REQUEST_DELAY = 0.1


def load_all_symbols():
    with open(SECTORS_PATH, encoding="utf-8") as f:
        sectors = json.load(f)
    us_syms = sorted(set(t for s in sectors for t in s["t"]))
    kr_syms = sorted(set(st["y"] for s in sectors for st in s.get("k", [])))
    return us_syms, kr_syms


def fetch_chart_batch_us(symbol):
    """미장: v8 chart API, 프리/애프터마켓 포함"""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=5d&includePrePost=true"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def parse_us_symbol(symbol):
    try:
        data = fetch_chart_batch_us(symbol)
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        r = result[0]
        meta = r.get("meta", {})

        regular_price = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        post_price = meta.get("postMarketPrice")
        pre_price = meta.get("preMarketPrice")
        volume = meta.get("regularMarketVolume", 0)

        if regular_price is None or prev_close is None or prev_close == 0:
            return None

        change_pct = round((regular_price - prev_close) / prev_close * 100, 2)

        # 프리/애프터마켓이 있으면 그게 더 최신 "현재가"
        live_price = post_price or pre_price or regular_price
        live_change = round((live_price - prev_close) / prev_close * 100, 2)

        return {
            "price": round(prev_close, 2),
            "change": change_pct,
            "live_price": round(live_price, 2),
            "live_change": live_change,
            "volume": volume,
        }
    except Exception:
        return None


def fetch_chart_kr(symbol):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=5d&includePrePost=false"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def parse_kr_symbol(symbol):
    try:
        data = fetch_chart_kr(symbol)
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        r = result[0]
        meta = r.get("meta", {})
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        timestamps = r.get("timestamp", [])
        closes_raw = quote.get("close", [])
        volumes_raw = quote.get("volume", [])

        valid = [
            (ts, c, volumes_raw[i] if i < len(volumes_raw) else None)
            for i, (ts, c) in enumerate(zip(timestamps, closes_raw))
            if c is not None
        ]
        if len(valid) < 1:
            return None

        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime("%Y-%m-%d")

        last_ts, last_close, last_vol = valid[-1]
        last_date_str = datetime.fromtimestamp(last_ts, tz=kst).strftime("%Y-%m-%d")

        if last_date_str == today_str and len(valid) >= 2:
            regular_price = last_close
            prev_close = valid[-2][1]
            volume = last_vol
        elif last_date_str == today_str and len(valid) == 1:
            regular_price = last_close
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            volume = last_vol
        else:
            regular_price = meta.get("regularMarketPrice", last_close)
            prev_close = last_close
            volume = meta.get("regularMarketVolume") or last_vol

        if regular_price is None or regular_price <= 0:
            regular_price = last_close
        if prev_close is None or prev_close <= 0:
            return None

        change_pct = round((regular_price - prev_close) / prev_close * 100, 2)

        return {
            "price": round(prev_close, 2),
            "change": change_pct,
            "live_price": round(regular_price, 2),
            "live_change": change_pct,
            "volume": volume or 0,
        }
    except Exception:
        return None


def collect(symbols, parse_fn, label):
    result = {}
    total = len(symbols)
    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        print(f"  [{label}] {i}/{total}")
        for sym in batch:
            result[sym] = parse_fn(sym)
            time.sleep(REQUEST_DELAY)
    ok = sum(1 for v in result.values() if v)
    print(f"  [{label}] 완료: {ok}/{total}")
    return result


def main():
    print("=" * 60)
    print("가격 수집 시작:", datetime.now().isoformat())

    us_syms, kr_syms = load_all_symbols()
    print(f"미장 {len(us_syms)}개, 국장 {len(kr_syms)}개")

    us_data = collect(us_syms, parse_us_symbol, "미장")
    kr_data = collect(kr_syms, parse_kr_symbol, "국장")

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "us": us_data,
        "kr": kr_data,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print("저장 완료:", OUTPUT_PATH)
    print("=" * 60)


if __name__ == "__main__":
    main()
