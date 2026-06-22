"""
GICS Close Radar - fetch_kr_prices.py v2 Yahoo KR Close Fallback
================================================================
목적:
- GitHub Actions에서 pykrx/KRX 직접 호출이 막히는 경우를 피하기 위해 Yahoo quote API로 국장 종가를 수집
- 통합 GICS 마스터의 KR 종목을 cache/kr_prices.json에 저장
- KOSPI/KOSDAQ 구분 정보가 없어도 각 종목코드에 .KS / .KQ를 모두 붙여 조회 후 성공한 쪽을 채택

주의:
- regular_dollar_volume 필드는 기존 프론트 호환을 위해 이름을 유지하지만, KR 종목에서는 KRW 거래대금 추정값(종가*거래량)이다.
- RVOL은 Yahoo quote 단건 응답만으로는 20일 평균 거래대금을 알 수 없어 null 처리한다.
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_quote

MASTER_PATH = "GICS_통합마스터_관종번호순.json"
OUTPUT_PATH = "cache/kr_prices.json"

CHUNK_SIZE = 160
REQUEST_DELAY = 0.15
TIMEOUT = 15
MAX_RETRIES = 2


def now_kst():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


def ymd_dash_from_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(
            timezone(timedelta(hours=9))
        ).strftime("%Y-%m-%d")
    except Exception:
        return now_kst().strftime("%Y-%m-%d")


def sf(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def si(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def pct(price, base):
    price, base = sf(price), sf(base)
    if price is None or base in (None, 0):
        return None
    return round((price - base) / base * 100, 4)


def fetch_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_yahoo_quote_batch(symbols):
    if not symbols:
        return {}

    syms = ",".join(url_quote(s) for s in symbols)
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}"

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            data = fetch_json(url)
            rows = data.get("quoteResponse", {}).get("result", []) or []
            return {str(q.get("symbol", "")).upper(): q for q in rows if q.get("symbol")}
        except Exception as e:
            last_err = e
            time.sleep(0.4 + attempt * 0.6)

    raise RuntimeError(f"Yahoo quote batch failed: {last_err}")


def load_kr_master():
    with open(MASTER_PATH, encoding="utf-8") as f:
        master = json.load(f)

    rows = []
    for r in master.get("records", []):
        if r.get("market") != "KR":
            continue
        code = str(r.get("code", "")).strip()
        if not code:
            continue

        # 보통 6자리 숫자. 일부 알파뉴메릭 KRX 코드는 Yahoo에서 조회가 안 될 가능성이 높다.
        code = code.zfill(6) if code.isdigit() else code.upper()
        item = dict(r)
        item["code"] = code
        rows.append(item)

    seen = set()
    out = []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)
    return out


def choose_quote(base_code, quote_map):
    candidates = []
    for suffix in (".KS", ".KQ"):
        ysym = f"{base_code}{suffix}".upper()
        q = quote_map.get(ysym)
        if not q:
            continue

        price = sf(q.get("regularMarketPrice"))
        prev = sf(q.get("regularMarketPreviousClose")) or sf(q.get("regularMarketPreviousCloseRaw"))
        volume = si(q.get("regularMarketVolume"), 0)
        quote_type = q.get("quoteType")

        if price is None:
            continue

        # 거래소 정보가 있으면 참고. 그래도 둘 다 있으면 거래량 있는 쪽 우선.
        score = 0
        if quote_type == "EQUITY":
            score += 5
        if prev not in (None, 0):
            score += 5
        if volume > 0:
            score += 3
        if suffix == ".KS":
            score += 0.1

        candidates.append((score, suffix, q))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][2]


def build_yahoo_symbol_list(codes):
    symbols = []
    for code in codes:
        # Yahoo Korea는 숫자코드.KS / 숫자코드.KQ 형식만 안정적이다.
        if code.isdigit() and len(code) == 6:
            symbols.append(f"{code}.KS")
            symbols.append(f"{code}.KQ")
    return symbols


def main():
    start_utc = datetime.now(timezone.utc)
    kst = now_kst()

    print("=" * 70)
    print("국장 종가 가격 수집 시작 v2 YAHOO KR CLOSE FALLBACK")
    print("UTC:", start_utc.isoformat())
    print("KST:", kst.isoformat())
    print("=" * 70)

    master_rows = load_kr_master()
    print(f"KR 마스터 종목: {len(master_rows)}개")

    codes = [r["code"] for r in master_rows]
    yahoo_symbols = build_yahoo_symbol_list(codes)
    print(f"Yahoo 조회 심볼: {len(yahoo_symbols)}개 (.KS/.KQ 양쪽 조회)")

    quote_map = {}
    batch_errors = []

    for i in range(0, len(yahoo_symbols), CHUNK_SIZE):
        chunk = yahoo_symbols[i : i + CHUNK_SIZE]
        try:
            quote_map.update(fetch_yahoo_quote_batch(chunk))
            print(f"[KR Yahoo] {min(i + CHUNK_SIZE, len(yahoo_symbols))}/{len(yahoo_symbols)} symbols")
        except Exception as e:
            msg = f"chunk {i}-{i + len(chunk)}: {e}"
            batch_errors.append(msg)
            print("[KR Yahoo] batch error:", msg)
        time.sleep(REQUEST_DELAY)

    kr = {}
    failures = {}
    date_counter = {}

    for r in master_rows:
        code = r["code"]
        try:
            q = choose_quote(code, quote_map)
            if not q:
                failures[code] = "yahoo_quote_missing"
                continue

            ysymbol = str(q.get("symbol", "")).upper()
            price = sf(q.get("regularMarketPrice"))
            prev_close = sf(q.get("regularMarketPreviousClose"))
            change = sf(q.get("regularMarketChangePercent"))
            if change is None:
                change = pct(price, prev_close)
            else:
                change = round(change, 4)

            volume = si(q.get("regularMarketVolume"), 0)
            value = round(price * volume, 2) if price is not None and volume else 0
            market_time = q.get("regularMarketTime")
            regular_date = ymd_dash_from_ts(market_time)
            date_counter[regular_date] = date_counter.get(regular_date, 0) + 1

            kr[code] = {
                "symbol": code,
                "yahoo_symbol": ysymbol,
                "name": r.get("name", "") or q.get("shortName", ""),
                "market": "KR",
                "currency": q.get("currency", "KRW"),
                "exchange": q.get("fullExchangeName") or q.get("exchange"),
                "prev_close": prev_close,
                "regular_price": price,
                "regular_change": change,
                "regular_volume": volume,
                "regular_dollar_volume": value,
                "regular_avg20_dollar_volume": None,
                "regular_avg20_days": 0,
                "regular_dollar_rvol": None,
                "regular_market_time": market_time,
                "regular_date": regular_date,
                "price": price,
                "change": change,
                "volume": volume,
                "source": "yahoo_v7_quote_kr",
                "data_quality": "yahoo_kr_close",
            }

        except Exception as e:
            failures[code] = str(e)

    # 가장 많이 잡힌 날짜를 date_key로 사용한다.
    if date_counter:
        date_key = sorted(date_counter.items(), key=lambda x: x[1], reverse=True)[0][0]
    else:
        date_key = now_kst().strftime("%Y-%m-%d")

    output = {
        "schema_version": "gics_close_kr_prices_v2_yahoo_quote",
        "updated_at": start_utc.isoformat(),
        "updated_at_kst": now_kst().isoformat(),
        "date_key": date_key,
        "source": "yahoo_v7_quote_kr",
        "note": "KR 종목은 Yahoo .KS/.KQ quote로 수집. regular_dollar_volume은 KRW 거래대금 추정값(price*volume). RVOL은 null.",
        "counts": {
            "symbols": len(master_rows),
            "ok": len(kr),
            "failed": len(failures),
            "quotes_returned": len(quote_map),
            "batch_errors": len(batch_errors),
        },
        "date_counter": date_counter,
        "batch_errors": batch_errors[:20],
        "failures": dict(list(failures.items())[:300]),
        "kr": kr,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("=" * 70)
    print("저장 완료:", OUTPUT_PATH)
    print("date_key:", output["date_key"])
    print(f"ok: {len(kr)}/{len(master_rows)}, failed: {len(failures)}")
    print("quotes_returned:", len(quote_map))
    if failures:
        print("failure sample:", list(failures.items())[:20])
    print("=" * 70)

    # 일부 실패는 허용. 단 한 개도 못 받으면 실패 처리.
    if len(kr) == 0:
        raise RuntimeError("KR Yahoo quote fetch returned zero valid symbols")


if __name__ == "__main__":
    main()
