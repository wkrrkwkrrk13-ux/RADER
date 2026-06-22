"""
GICS Close Radar - fetch_prices.py v4 US close + EODHD + RVOL
==================================================
목적:
- Yahoo v8 chart로 미장 전체 수집
- SPCX 같은 데이터 정합성 의심 종목만 EODHD로 교차검증
- Yahoo와 EODHD가 크게 다르면 EODHD 값으로 보정
- 보정 로그를 cache/prices.json 안에 저장

API 키:
- GitHub Secrets에 EODHD_API_TOKEN 이름으로 저장
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_quote, urlencode
from collections import Counter

SECTORS_PATH = "sectors_data.json"
MASTER_PATH = "GICS_통합마스터_관종번호순.json"
OUTPUT_PATH = "cache/prices.json"

REQUEST_DELAY = 0.035
TIMEOUT = 10
MAX_RETRIES = 2

# EODHD 교차검증 설정
EODHD_API_TOKEN = os.getenv("EODHD_API_TOKEN", "").strip()
EODHD_ALWAYS_VERIFY = [
    s.strip().upper()
    for s in os.getenv("EODHD_ALWAYS_VERIFY", "SPCX").split(",")
    if s.strip()
]
EODHD_ANOMALY_CHANGE_ABS = float(os.getenv("EODHD_ANOMALY_CHANGE_ABS", "15"))
EODHD_MAX_VERIFY = int(os.getenv("EODHD_MAX_VERIFY", "20"))
EODHD_PRICE_DIFF_PCT = float(os.getenv("EODHD_PRICE_DIFF_PCT", "0.7"))
EODHD_CHANGE_DIFF_PCTP = float(os.getenv("EODHD_CHANGE_DIFF_PCTP", "1.0"))


def nth_weekday_utc(year, month, weekday, nth, hour_utc):
    d = datetime(year, month, 1, hour_utc, 0, tzinfo=timezone.utc)
    return d + timedelta(days=((weekday - d.weekday()) % 7) + 7 * (nth - 1))


def is_us_dst(dt_utc):
    y = dt_utc.year
    return nth_weekday_utc(y, 3, 6, 2, 7) <= dt_utc < nth_weekday_utc(y, 11, 6, 1, 6)


def get_market_session(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    off = -4 if is_us_dst(now_utc) else -5
    et = now_utc + timedelta(hours=off)

    if et.weekday() >= 5:
        return "closed", et, off

    m = et.hour * 60 + et.minute
    if 240 <= m < 570:
        return "pre", et, off
    if 570 <= m < 960:
        return "regular", et, off
    if 960 <= m < 1200:
        return "after", et, off
    return "closed", et, off


def sf(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def si(v, default=0):
    try:
        return default if v is None else int(float(v))
    except Exception:
        return default


def pct(price, base):
    price, base = sf(price), sf(base)
    if price is None or base in (None, 0):
        return None
    return round((price - base) / base * 100, 4)


def et_date(ts, off):
    try:
        return (datetime.fromtimestamp(int(ts), tz=timezone.utc) + timedelta(hours=off)).strftime("%Y-%m-%d")
    except Exception:
        return None


def fetch_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_symbols():
    """
    미장 수집 대상은 통합 GICS 마스터의 US 레코드를 우선 사용한다.
    과거 sectors_data.json도 fallback으로 지원한다.
    """
    symbols = []
    meta = {}
    sectors = []

    if os.path.exists(MASTER_PATH):
        with open(MASTER_PATH, encoding="utf-8") as f:
            master = json.load(f)

        industry_codes = set()
        for r in master.get("records", []):
            if r.get("market") != "US":
                continue
            t = str(r.get("code", "")).strip().upper()
            if not t:
                continue
            symbols.append(t)
            industry_codes.add(str(r.get("industry_code", "")))
            meta[t] = {
                "display_ticker": t,
                "name": r.get("name", ""),
                "market_cap_usd": r.get("market_cap_usd", 0),
                "industry": r.get("industry", ""),
                "gics11": r.get("sector", ""),
                "industry_code": r.get("industry_code", ""),
                "industry_group_code": r.get("industry_group_code", ""),
                "sector_code": r.get("sector_code", ""),
                "watchlist_no": r.get("watchlist_no", ""),
            }

        sectors = sorted(industry_codes)
        return sorted(set(symbols)), meta, sectors

    with open(SECTORS_PATH, encoding="utf-8") as f:
        sectors = json.load(f)

    for sec in sectors:
        for it in sec.get("tickers", []):
            t = str(it.get("ticker", "")).strip().upper()
            if not t:
                continue
            symbols.append(t)
            meta[t] = {
                "display_ticker": it.get("display_ticker", t),
                "name": it.get("name", ""),
                "market_cap_usd": it.get("market_cap_usd", 0),
                "industry": sec.get("industry", ""),
                "gics11": sec.get("gics11", ""),
            }

    return sorted(set(symbols)), meta, sectors


def chart(symbol):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol)}"
        f"?interval=1d&range=30d&includePrePost=true"
    )
    return fetch_json(url)


def extract(data):
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None, []

    meta = result.get("meta", {}) or {}
    q = (result.get("indicators", {}).get("quote") or [{}])[0] or {}
    ts = result.get("timestamp") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []

    valid = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        v = vols[i] if i < len(vols) else None
        if c is not None:
            valid.append((t, sf(c), si(v, 0)))

    return meta, valid


def parse_symbol(symbol, session, off, info):
    last_err = None
    data = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            data = chart(symbol)
            break
        except Exception as e:
            last_err = e
            time.sleep(0.35 + attempt * 0.45)

    if data is None:
        raise RuntimeError(f"v8 daily failed: {last_err}")

    meta, valid = extract(data)

    if not meta:
        raise ValueError("v8 chart missing meta")
    if not valid:
        raise ValueError("v8 chart has no valid daily close")

    meta_regular_price = sf(meta.get("regularMarketPrice"))
    meta_prev_close = (
        sf(meta.get("regularMarketPreviousClose"))
        or sf(meta.get("previousClose"))
        or sf(meta.get("chartPreviousClose"))
    )
    meta_regular_volume = si(meta.get("regularMarketVolume"), 0)
    regular_time = meta.get("regularMarketTime")

    if len(valid) >= 2:
        daily_prev = valid[-2][1]
        daily_last = valid[-1][1]
        daily_vol = valid[-1][2]
    else:
        daily_prev = None
        daily_last = valid[-1][1]
        daily_vol = valid[-1][2]

    # RVOL용 최근 평균 거래대금
    # 마지막 봉은 오늘 본장 마감값이므로 제외하고, 직전 최대 20거래일 평균을 사용한다.
    # 신규상장 등으로 3거래일 미만이면 RVOL 계산을 생략한다.
    hist_for_avg = valid[-21:-1] if len(valid) >= 2 else []
    hist_dollars = [
        float(c) * int(v)
        for _, c, v in hist_for_avg
        if c is not None and v is not None and int(v) > 0
    ]
    avg20_dollar = (sum(hist_dollars) / len(hist_dollars)) if len(hist_dollars) >= 3 else None
    avg20_days = len(hist_dollars)

    # v3.1 핵심 수정:
    # Yahoo meta의 regularMarketPreviousClose가 신규상장/일부 종목에서 틀어지는 경우가 있음.
    # 등락률은 chart 일봉의 마지막 종가와 직전 일봉 종가로 계산하는 쪽이 더 안정적이다.
    # 예: SPCX는 가격은 185로 맞았지만 meta previousClose 기준이 꼬여 +23%로 계산됐음.
    regular_price = daily_last if daily_last is not None else meta_regular_price
    prev_close = daily_prev if daily_prev not in (None, 0) else meta_prev_close

    if regular_price is None:
        regular_price = meta_regular_price

    if prev_close is None or prev_close == 0:
        prev_close = meta_prev_close if meta_prev_close not in (None, 0) else regular_price

    # 거래량도 일봉 quote의 마지막 volume을 우선 사용한다.
    regular_volume = daily_vol or meta_regular_volume or 0

    regular_change = pct(regular_price, prev_close) or 0.0

    pre_price = sf(meta.get("preMarketPrice"))
    post_price = sf(meta.get("postMarketPrice"))
    pre_volume = si(meta.get("preMarketVolume"), 0)
    post_volume = si(meta.get("postMarketVolume"), 0)
    pre_change = pct(pre_price, prev_close) if pre_price is not None else None
    post_change = pct(post_price, prev_close) if post_price is not None else None

    if session == "pre":
        live_price = pre_price if pre_price is not None else regular_price
        live_change = pre_change if pre_change is not None else pct(live_price, prev_close)
        live_volume = pre_volume
    elif session == "after":
        live_price = post_price if post_price is not None else regular_price
        live_change = post_change if post_change is not None else pct(live_price, prev_close)
        live_volume = post_volume
    elif session == "regular":
        live_price = regular_price
        live_change = regular_change
        live_volume = regular_volume
    else:
        live_price = regular_price
        live_change = regular_change
        live_volume = regular_volume

    if live_change is None:
        live_change = regular_change

    regular_date = et_date(regular_time, off) or (et_date(valid[-1][0], off) if valid else None)

    return {
        "symbol": symbol,
        "display_ticker": info.get("display_ticker", symbol),
        "name": info.get("name", meta.get("shortName") or meta.get("longName") or ""),
        "gics11": info.get("gics11", ""),
        "industry": info.get("industry", ""),
        "market_cap_usd": info.get("market_cap_usd", 0),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("exchangeName"),
        "instrument_type": meta.get("instrumentType"),
        "prev_close": round(float(prev_close), 4),
        "regular_price": round(float(regular_price), 4),
        "regular_change": round(float(regular_change), 4),
        "regular_volume": regular_volume,
        "regular_dollar_volume": round(float(regular_price) * regular_volume, 2),
        "regular_avg20_dollar_volume": round(float(avg20_dollar), 2) if avg20_dollar else None,
        "regular_avg20_days": avg20_days,
        "regular_dollar_rvol": round((float(regular_price) * regular_volume) / avg20_dollar, 4) if avg20_dollar and avg20_dollar > 0 else None,
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
        "live_dollar_rvol": round((float(live_price) * live_volume) / avg20_dollar, 4) if avg20_dollar and avg20_dollar > 0 else None,
        "live_valid": True,
        "price": round(float(regular_price), 4),
        "change": round(float(regular_change), 4),
        "volume": regular_volume,
        "source": "yahoo_v8_chart_fast",
        "data_quality": "yahoo_raw",
    }


def collect(symbols, meta, session, off):
    results = {}
    failures = {}
    total = len(symbols)

    for i, sym in enumerate(symbols):
        if i % 50 == 0:
            print(f"  [미장] {i}/{total} symbols")

        try:
            results[sym] = parse_symbol(sym, session, off, meta.get(sym, {}))
        except Exception as e:
            failures[sym] = str(e)
            results[sym] = None

        time.sleep(REQUEST_DELAY)

    print(f"  [미장] 완료: {sum(1 for v in results.values() if v)}/{total}, 실패 {len(failures)}")

    if failures:
        print("  [미장] 실패 일부:")
        for sym, err in list(failures.items())[:20]:
            print(f"    - {sym}: {err}")

    return results, failures


def to_eodhd_symbol(symbol):
    s = str(symbol).strip().upper()
    return f"{s.replace('.', '-')}.US"


def from_eodhd_code(code):
    c = str(code or "").upper()
    if c.endswith(".US"):
        c = c[:-3]
    return c.replace("-", ".")


def fetch_eodhd_realtime(symbols):
    if not EODHD_API_TOKEN or not symbols:
        return {}

    eod_symbols = [to_eodhd_symbol(s) for s in symbols]
    first = eod_symbols[0]
    others = eod_symbols[1:]

    params = {
        "api_token": EODHD_API_TOKEN,
        "fmt": "json",
    }
    if others:
        params["s"] = ",".join(others)

    url = f"https://eodhd.com/api/real-time/{url_quote(first)}?{urlencode(params)}"
    data = fetch_json(url, timeout=20)

    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = from_eodhd_code(row.get("code") or row.get("symbol"))
        if sym:
            out[sym] = row

    return out


def choose_eodhd_candidates(us_data):
    candidates = []

    for s in EODHD_ALWAYS_VERIFY:
        if s and s in us_data:
            candidates.append(s)

    anomaly = []
    for sym, q in us_data.items():
        if not q:
            continue
        ch = sf(q.get("regular_change"))
        dollar = sf(q.get("regular_dollar_volume")) or 0
        if ch is not None and abs(ch) >= EODHD_ANOMALY_CHANGE_ABS:
            anomaly.append((sym, dollar, abs(ch)))

    anomaly.sort(key=lambda x: (x[1], x[2]), reverse=True)

    for sym, _, _ in anomaly:
        if sym not in candidates:
            candidates.append(sym)

    return candidates[:EODHD_MAX_VERIFY]


def eodhd_valid(row):
    if not row:
        return False
    close = sf(row.get("close"))
    prev = sf(row.get("previousClose"))
    change_p = sf(row.get("change_p"))
    return close is not None and close > 0 and prev is not None and prev > 0 and change_p is not None


def apply_eodhd_corrections(us_data):
    summary = {
        "enabled": bool(EODHD_API_TOKEN),
        "candidates": [],
        "validated": 0,
        "corrected": 0,
        "skipped": 0,
        "errors": [],
        "corrections": [],
    }

    if not EODHD_API_TOKEN:
        print("EODHD: token 없음 → 교차검증 생략")
        summary["errors"].append("EODHD_API_TOKEN missing")
        return us_data, summary

    candidates = choose_eodhd_candidates(us_data)
    summary["candidates"] = candidates

    if not candidates:
        print("EODHD: 검증 후보 없음")
        return us_data, summary

    print(f"EODHD: 교차검증 후보 {len(candidates)}개: {', '.join(candidates)}")

    try:
        eod_quotes = fetch_eodhd_realtime(candidates)
    except Exception as e:
        msg = f"EODHD fetch failed: {e}"
        print(msg)
        summary["errors"].append(msg)
        return us_data, summary

    for sym in candidates:
        yahoo = us_data.get(sym)
        eod = eod_quotes.get(sym)

        if not yahoo:
            summary["skipped"] += 1
            continue

        if not eodhd_valid(eod):
            summary["skipped"] += 1
            summary["errors"].append(f"{sym}: EODHD quote invalid/missing")
            continue

        summary["validated"] += 1

        y_price = sf(yahoo.get("regular_price") or yahoo.get("price"))
        y_change = sf(yahoo.get("regular_change") or yahoo.get("change"))
        y_volume = si(yahoo.get("regular_volume") or yahoo.get("volume"), 0)

        e_price = sf(eod.get("close"))
        e_prev = sf(eod.get("previousClose"))
        e_change = sf(eod.get("change_p"))
        e_change_abs = sf(eod.get("change"))
        e_volume = si(eod.get("volume"), 0)
        e_timestamp = eod.get("timestamp")

        price_diff_pct = abs(y_price - e_price) / e_price * 100 if y_price and e_price else None
        change_diff_pctp = abs(y_change - e_change) if y_change is not None and e_change is not None else None

        should_correct = False
        reasons = []

        if price_diff_pct is not None and price_diff_pct >= EODHD_PRICE_DIFF_PCT:
            should_correct = True
            reasons.append(f"price_diff_pct={price_diff_pct:.2f}")

        if change_diff_pctp is not None and change_diff_pctp >= EODHD_CHANGE_DIFF_PCTP:
            should_correct = True
            reasons.append(f"change_diff_pctp={change_diff_pctp:.2f}")

        if sym in EODHD_ALWAYS_VERIFY and (price_diff_pct or 0) >= 0.2:
            should_correct = True
            reasons.append("always_verify")

        if not should_correct:
            continue

        before = {
            "price": y_price,
            "change": y_change,
            "volume": y_volume,
            "dollar_volume": sf(yahoo.get("regular_dollar_volume")),
        }

        corrected = dict(yahoo)
        corrected["prev_close"] = round(float(e_prev), 4)
        corrected["regular_price"] = round(float(e_price), 4)
        corrected["regular_change"] = round(float(e_change), 4)
        corrected["regular_volume"] = e_volume
        corrected["regular_dollar_volume"] = round(float(e_price) * e_volume, 2)
        avg20 = sf(corrected.get("regular_avg20_dollar_volume"))
        corrected["regular_dollar_rvol"] = round(corrected["regular_dollar_volume"] / avg20, 4) if avg20 and avg20 > 0 else None
        corrected["regular_market_time"] = e_timestamp
        corrected["price"] = corrected["regular_price"]
        corrected["change"] = corrected["regular_change"]
        corrected["volume"] = corrected["regular_volume"]
        corrected["source"] = "yahoo_v8_chart_fast__corrected_by_eodhd"
        corrected["data_quality"] = "corrected_by_eodhd"
        corrected["eodhd"] = {
            "close": e_price,
            "previousClose": e_prev,
            "change": e_change_abs,
            "change_p": e_change,
            "volume": e_volume,
            "timestamp": e_timestamp,
        }

        # 본장 마감 이후 스냅샷 수집일 때 live도 정규장값과 맞춰 둔다.
        corrected["live_price"] = corrected["regular_price"]
        corrected["live_change"] = corrected["regular_change"]
        corrected["live_volume"] = corrected["regular_volume"]
        corrected["live_dollar_volume"] = corrected["regular_dollar_volume"]
        corrected["live_dollar_rvol"] = corrected.get("regular_dollar_rvol")
        corrected["live_valid"] = True

        us_data[sym] = corrected
        summary["corrected"] += 1

        correction = {
            "symbol": sym,
            "reason": ",".join(reasons),
            "price_diff_pct": round(price_diff_pct, 4) if price_diff_pct is not None else None,
            "change_diff_pctp": round(change_diff_pctp, 4) if change_diff_pctp is not None else None,
            "yahoo_before": before,
            "eodhd_after": {
                "price": e_price,
                "previousClose": e_prev,
                "change": e_change_abs,
                "change_p": e_change,
                "volume": e_volume,
            },
        }
        summary["corrections"].append(correction)

        print(
            f"EODHD 보정: {sym} "
            f"Yahoo {before['price']} / {before['change']}% "
            f"→ EODHD {e_price} / {e_change}% "
            f"({','.join(reasons)})"
        )

    print(f"EODHD: 검증 {summary['validated']}개, 보정 {summary['corrected']}개, 스킵 {summary['skipped']}개")
    return us_data, summary


def infer_date(us, fallback):
    dates = [v.get("regular_date") for v in us.values() if v and v.get("regular_date")]
    return Counter(dates).most_common(1)[0][0] if dates else fallback


def main():
    now_utc = datetime.now(timezone.utc)
    session, et, off = get_market_session(now_utc)
    kst = timezone(timedelta(hours=9))
    now_kst = now_utc.astimezone(kst)

    print("=" * 70)
    print("미장 종가 가격 수집 시작 v4 CLOSE RADAR")
    print("UTC:", now_utc.isoformat())
    print("ET :", et.strftime("%Y-%m-%d %H:%M:%S"))
    print("KST:", now_kst.isoformat())
    print("SESSION:", session)
    print("SOURCE: Yahoo v8 chart 30d + EODHD crosscheck + RVOL")
    print("=" * 70)

    symbols, meta, sectors = load_symbols()
    print(f"미장 티커 {len(symbols)}개 / GICS 섹터 {len(sectors)}개")

    us, failures = collect(symbols, meta, session, off)
    us, eodhd_summary = apply_eodhd_corrections(us)

    date_key = infer_date(us, et.strftime("%Y-%m-%d"))

    output = {
        "schema_version": "gics_close_us_prices_v4",
        "updated_at": now_utc.isoformat(),
        "updated_at_kst": now_kst.isoformat(),
        "date_key": date_key,
        "session": session,
        "is_us_dst": is_us_dst(now_utc),
        "regular_final_locked": session != "regular",
        "source": "yahoo_v8_chart_daily_fast_eodhd_crosscheck",
        "eodhd_crosscheck": eodhd_summary,
        "counts": {
            "sectors": len(sectors),
            "symbols": len(symbols),
            "ok": sum(1 for v in us.values() if v),
            "failed": len(failures),
            "eodhd_validated": eodhd_summary.get("validated", 0),
            "eodhd_corrected": eodhd_summary.get("corrected", 0),
        },
        "failures": failures,
        "us": us,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print("저장 완료:", OUTPUT_PATH)
    print("date_key:", date_key)
    print("regular_final_locked:", session != "regular")
    print("failures:", len(failures))
    print("eodhd_corrected:", eodhd_summary.get("corrected", 0))
    print("=" * 70)


if __name__ == "__main__":
    main()
