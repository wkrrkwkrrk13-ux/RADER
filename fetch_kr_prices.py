"""
GICS Close Radar - fetch_kr_prices.py v1 KR close + RVOL
========================================================
목적:
- KRX 전체 국장 종목의 최신 정규장 종가를 pykrx로 수집
- 통합 GICS 마스터의 KR 종목 2,500여 개만 cache/kr_prices.json에 저장
- 전일 대비 등락률, 거래대금, 20거래일 평균 거래대금, RVOL 계산

주의:
- pykrx는 장 마감 직후 데이터 반영이 늦을 수 있다.
- GitHub Actions에서는 requirements.txt의 pykrx 설치가 필요하다.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

MASTER_PATH = "GICS_통합마스터_관종번호순.json"
OUTPUT_PATH = "cache/kr_prices.json"

LOOKBACK_CALENDAR_DAYS = 45
MAX_TRADING_DAYS = 24


def now_kst():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


def ymd(dt):
    return dt.strftime("%Y%m%d")


def ymd_dash(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


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


def load_kr_master():
    with open(MASTER_PATH, encoding="utf-8") as f:
        master = json.load(f)

    rows = []
    for r in master.get("records", []):
        if r.get("market") != "KR":
            continue
        code = str(r.get("code", "")).strip().zfill(6)
        if not code:
            continue
        item = dict(r)
        item["code"] = code
        rows.append(item)

    # 혹시 마스터에 중복이 남아 있으면 첫 항목만 유지
    seen = set()
    out = []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)

    return out


def fetch_recent_market_frames():
    from pykrx import stock

    kst = now_kst()
    frames = []

    for d in range(LOOKBACK_CALENDAR_DAYS):
        day = kst.date() - timedelta(days=d)
        date_str = day.strftime("%Y%m%d")

        try:
            df = stock.get_market_ohlcv(date_str, market="ALL")
        except Exception as e:
            print(f"[KR] {date_str} fetch error: {e}")
            time.sleep(0.3)
            continue

        if df is None or len(df) == 0:
            continue

        # 거래대금이 거의 없고 종가도 없으면 휴장/미반영으로 본다.
        if "종가" not in df.columns or "거래대금" not in df.columns:
            continue

        total_value = float(df["거래대금"].fillna(0).sum())
        if total_value <= 0:
            continue

        df = df.copy()
        df.index = df.index.map(lambda x: str(x).zfill(6))
        frames.append((date_str, df))

        print(f"[KR] trading day {len(frames)}: {date_str} rows={len(df)} value={int(total_value):,}")

        if len(frames) >= MAX_TRADING_DAYS:
            break

        time.sleep(0.15)

    # 최신순으로 모았으므로 오래된순으로 정렬
    frames = list(reversed(frames))
    if len(frames) < 2:
        raise RuntimeError("KR trading frames are insufficient")

    return frames


def main():
    try:
        import pykrx  # noqa
    except Exception as e:
        raise RuntimeError("pykrx가 설치되어 있지 않습니다. requirements.txt 또는 workflow에서 pip install pykrx가 필요합니다.") from e

    start_utc = datetime.now(timezone.utc)
    kst = now_kst()

    print("=" * 70)
    print("국장 종가 가격 수집 시작 v1 CLOSE RADAR")
    print("UTC:", start_utc.isoformat())
    print("KST:", kst.isoformat())
    print("=" * 70)

    master_rows = load_kr_master()
    print(f"KR 마스터 종목: {len(master_rows)}개")

    frames = fetch_recent_market_frames()
    latest_date, latest_df = frames[-1]
    prev_date, prev_df = frames[-2]

    # 최근 20거래일 평균 거래대금은 최신일 제외 직전 20거래일 사용
    hist_frames = frames[:-1][-20:]
    hist_dates = [d for d, _ in hist_frames]

    kr = {}
    failures = {}

    for r in master_rows:
        code = r["code"]
        try:
            if code not in latest_df.index:
                failures[code] = "missing_latest"
                continue

            row = latest_df.loc[code]
            close = sf(row.get("종가"))
            volume = si(row.get("거래량"), 0)
            value = sf(row.get("거래대금")) or 0

            if code in prev_df.index:
                prev_close = sf(prev_df.loc[code].get("종가"))
            else:
                prev_close = None

            change = pct(close, prev_close)

            hist_values = []
            for _, hdf in hist_frames:
                if code in hdf.index:
                    v = sf(hdf.loc[code].get("거래대금"))
                    if v is not None and v > 0:
                        hist_values.append(v)

            avg20 = sum(hist_values) / len(hist_values) if len(hist_values) >= 3 else None
            rvol = round(value / avg20, 4) if avg20 and avg20 > 0 and value > 0 else None

            kr[code] = {
                "symbol": code,
                "name": r.get("name", ""),
                "market": "KR",
                "currency": "KRW",
                "prev_close": prev_close,
                "regular_price": close,
                "regular_change": change,
                "regular_volume": volume,
                "regular_dollar_volume": value,
                "regular_avg20_dollar_volume": round(avg20, 2) if avg20 else None,
                "regular_avg20_days": len(hist_values),
                "regular_dollar_rvol": rvol,
                "regular_date": ymd_dash(latest_date),
                "price": close,
                "change": change,
                "volume": volume,
                "source": "pykrx_market_ohlcv",
                "data_quality": "krx_close",
            }

        except Exception as e:
            failures[code] = str(e)

    output = {
        "schema_version": "gics_close_kr_prices_v1",
        "updated_at": start_utc.isoformat(),
        "updated_at_kst": now_kst().isoformat(),
        "date_key": ymd_dash(latest_date),
        "previous_date_key": ymd_dash(prev_date),
        "source": "pykrx_market_ohlcv",
        "counts": {
            "symbols": len(master_rows),
            "ok": len(kr),
            "failed": len(failures),
            "hist_days": len(hist_frames),
        },
        "hist_dates": [ymd_dash(d) for d in hist_dates],
        "failures": failures,
        "kr": kr,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print("저장 완료:", OUTPUT_PATH)
    print("date_key:", ymd_dash(latest_date))
    print("previous_date_key:", ymd_dash(prev_date))
    print("ok:", len(kr), "failed:", len(failures))
    print("=" * 70)


if __name__ == "__main__":
    main()
