"""
KR Close Radar - fetch_kr_prices.py v3 NAVER DAILY CLOSE
========================================================
목적:
- KRX/pykrx 호출이 GitHub Actions에서 막히는 문제를 우회
- Yahoo quote batch 401 문제를 우회
- 네이버 금융 일봉(siseJson)으로 국장 종가/거래량을 수집
- cache/kr_prices.json 생성

입력:
- GICS_통합마스터_관종번호순.json

출력:
- cache/kr_prices.json
"""

import ast
import json
import os
import re
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

MASTER_PATH = "GICS_통합마스터_관종번호순.json"
OUTPUT_PATH = "cache/kr_prices.json"

REQUEST_DELAY = float(os.getenv("KR_REQUEST_DELAY", "0.035"))
TIMEOUT = int(os.getenv("KR_TIMEOUT", "8"))
MAX_RETRIES = int(os.getenv("KR_MAX_RETRIES", "2"))
LOOKBACK_DAYS = int(os.getenv("KR_LOOKBACK_DAYS", "65"))
MIN_OK_RATIO = float(os.getenv("KR_MIN_OK_RATIO", "0.35"))


def now_kst():
    return datetime.now(timezone.utc) + timedelta(hours=9)


def normalize_code(code):
    s = str(code or "").strip()
    # 일반 상장종목은 6자리 숫자. 알파뉴메릭 코드는 네이버에서 조회 불가할 수 있음.
    if re.fullmatch(r"\d{6}", s):
        return s
    return s


def load_kr_master():
    with open(MASTER_PATH, encoding="utf-8") as f:
        data = json.load(f)

    records = data.get("records", [])
    kr = []
    seen = set()
    for r in records:
        if r.get("market") != "KR":
            continue
        code = normalize_code(r.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        kr.append({
            "code": code,
            "name": r.get("name", ""),
            "watchlist_no": r.get("watchlist_no", ""),
            "sector_code": r.get("sector_code", ""),
            "sector": r.get("sector", ""),
            "industry_group_code": r.get("industry_group_code", ""),
            "industry_group": r.get("industry_group", ""),
            "industry_code": r.get("industry_code", ""),
            "industry": r.get("industry", ""),
            "sub_industry": r.get("sub_industry", ""),
        })
    return kr


def fetch_text(url, timeout=TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://finance.naver.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                return raw.decode(enc)
            except Exception:
                pass
        return raw.decode("utf-8", errors="ignore")


def naver_sise_url(code, start_yyyymmdd, end_yyyymmdd):
    params = urlencode({
        "symbol": code,
        "requestType": 1,
        "startTime": start_yyyymmdd,
        "endTime": end_yyyymmdd,
        "timeframe": "day",
    })
    return f"https://api.finance.naver.com/siseJson.naver?{params}"


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "null", "None"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def to_int(v):
    n = to_num(v)
    if n is None:
        return 0
    return int(n)


def normalize_date(v):
    s = str(v or "").strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        d = digits[:8]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}", d
    return None, None


def parse_sise_json(text):
    # 네이버 응답은 JS 배열 형태. 보통 ast.literal_eval로 처리 가능.
    cleaned = text.strip()
    if not cleaned:
        return []

    # 앞뒤에 불필요한 문자가 붙을 경우 배열 부분만 추출
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        arr = ast.literal_eval(cleaned)
    except Exception:
        # 최후 보정: 날짜와 숫자 행만 정규식으로 추출
        rows = []
        row_re = re.compile(r"\[\s*['\"]?([0-9.\-]+)['\"]?\s*,\s*([0-9,.-]+)\s*,\s*([0-9,.-]+)\s*,\s*([0-9,.-]+)\s*,\s*([0-9,.-]+)\s*,\s*([0-9,.-]+)")
        for m in row_re.finditer(cleaned):
            rows.append([m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)])
        return rows

    if not isinstance(arr, list):
        return []

    rows = []
    for row in arr:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        # 헤더 제거
        if str(row[0]).strip() in ("날짜", "date", "Date"):
            continue
        rows.append(list(row))
    return rows


def fetch_daily_rows(code, start, end):
    url = naver_sise_url(code, start, end)
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            text = fetch_text(url)
            rows = parse_sise_json(text)
            if rows:
                return rows
            last_err = RuntimeError("empty naver rows")
        except Exception as e:
            last_err = e
        time.sleep(0.25 + 0.35 * attempt)
    raise RuntimeError(str(last_err))


def rows_to_quote(code, base_info, rows):
    parsed = []
    for row in rows:
        # row: 날짜, 시가, 고가, 저가, 종가, 거래량, 외국인소진율...
        date_iso, date_key = normalize_date(row[0])
        if not date_iso:
            continue
        open_p = to_num(row[1])
        high = to_num(row[2])
        low = to_num(row[3])
        close = to_num(row[4])
        volume = to_int(row[5])
        if close is None or close <= 0:
            continue
        parsed.append({
            "date": date_iso,
            "date_key": date_key,
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "dollar_volume": close * volume,  # KRW 거래대금 추정치
        })

    if not parsed:
        raise RuntimeError("no valid parsed daily rows")

    parsed.sort(key=lambda x: x["date_key"])
    last = parsed[-1]
    prev = parsed[-2] if len(parsed) >= 2 else None

    price = last["close"]
    prev_close = prev["close"] if prev else None
    change = None
    if prev_close and prev_close != 0:
        change = round((price - prev_close) / prev_close * 100, 4)

    hist = parsed[-21:-1] if len(parsed) >= 2 else []
    hist_dv = [x["dollar_volume"] for x in hist if x.get("dollar_volume") and x.get("dollar_volume") > 0]
    avg20 = round(sum(hist_dv) / len(hist_dv), 2) if len(hist_dv) >= 3 else None
    rvol = round(last["dollar_volume"] / avg20, 4) if avg20 and avg20 > 0 else None

    return {
        "symbol": code,
        "display_ticker": code,
        "code": code,
        "name": base_info.get("name", ""),
        "market": "KR",
        "currency": "KRW",
        "watchlist_no": base_info.get("watchlist_no", ""),
        "sector_code": base_info.get("sector_code", ""),
        "sector": base_info.get("sector", ""),
        "industry_group_code": base_info.get("industry_group_code", ""),
        "industry_group": base_info.get("industry_group", ""),
        "industry_code": base_info.get("industry_code", ""),
        "industry": base_info.get("industry", ""),
        "sub_industry": base_info.get("sub_industry", ""),
        "prev_close": prev_close,
        "regular_price": price,
        "regular_change": change,
        "regular_volume": last["volume"],
        "regular_dollar_volume": round(last["dollar_volume"], 2),
        "regular_avg20_dollar_volume": avg20,
        "regular_avg20_days": len(hist_dv),
        "regular_dollar_rvol": rvol,
        "regular_date": last["date"],
        "live_session": "closed",
        "live_price": price,
        "live_change": change,
        "live_volume": last["volume"],
        "live_dollar_volume": round(last["dollar_volume"], 2),
        "live_dollar_rvol": rvol,
        "live_valid": True,
        "price": price,
        "change": change,
        "volume": last["volume"],
        "source": "naver_siseJson_daily",
        "data_quality": "naver_daily_close",
    }


def main():
    utc = datetime.now(timezone.utc)
    kst = utc + timedelta(hours=9)
    end_dt = kst.date()
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start = start_dt.strftime("%Y%m%d")
    end = end_dt.strftime("%Y%m%d")

    print("=" * 70)
    print("국장 종가 가격 수집 시작 v3 NAVER DAILY CLOSE")
    print(f"UTC: {utc.isoformat()}")
    print(f"KST: {kst.isoformat()}")
    print(f"RANGE: {start} ~ {end}")
    print("=" * 70)

    master = load_kr_master()
    print(f"KR 마스터 종목: {len(master)}개")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    kr = {}
    failures = {}
    dates = []

    for idx, info in enumerate(master):
        code = info["code"]
        if idx % 100 == 0:
            print(f"  [KR Naver] {idx}/{len(master)}")

        if not re.fullmatch(r"\d{6}", code):
            failures[code] = "non_numeric_code_not_supported_by_naver"
            continue

        try:
            rows = fetch_daily_rows(code, start, end)
            q = rows_to_quote(code, info, rows)
            kr[code] = q
            if q.get("regular_date"):
                dates.append(q["regular_date"])
        except Exception as e:
            failures[code] = str(e)[:180]
        time.sleep(REQUEST_DELAY)

    common_date = Counter(dates).most_common(1)[0][0] if dates else kst.strftime("%Y-%m-%d")
    ok = len(kr)
    failed = len(master) - ok
    ok_ratio = ok / max(1, len(master))

    out = {
        "schema_version": "kr_prices_v3_naver_daily_close",
        "updated_at": utc.isoformat(),
        "updated_at_kst": kst.isoformat(),
        "date_key": common_date,
        "session": "closed",
        "regular_final_locked": True,
        "source": "naver_siseJson_daily",
        "counts": {
            "symbols": len(master),
            "ok": ok,
            "failed": failed,
        },
        "failures": failures,
        "kr": kr,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print("=" * 70)
    print(f"저장 완료: {OUTPUT_PATH}")
    print(f"date_key: {common_date}")
    print(f"ok: {ok}/{len(master)}, failed: {failed}, ok_ratio: {ok_ratio:.2%}")
    if failures:
        print("failure sample:", list(failures.items())[:20])
    print("=" * 70)

    if ok == 0:
        raise RuntimeError("KR Naver fetch returned zero valid symbols")
    if ok_ratio < MIN_OK_RATIO:
        raise RuntimeError(f"KR Naver fetch ok ratio too low: {ok_ratio:.2%}")


if __name__ == "__main__":
    main()
