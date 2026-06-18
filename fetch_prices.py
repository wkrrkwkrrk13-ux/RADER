
"""GICS74 Radar - GitHub Actions 가격 수집기 v2.2 FAST
- 세션 멈춤 방지용 빠른 버전
- Yahoo v8 daily chart만 사용해서 1,014개 티커 갱신 시간을 줄임
"""
import json, os, time, urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_quote
from collections import Counter

SECTORS_PATH='sectors_data.json'
OUTPUT_PATH='cache/prices.json'
REQUEST_DELAY=0.035
TIMEOUT=10
MAX_RETRIES=2

def nth_weekday_utc(year, month, weekday, nth, hour_utc):
    d=datetime(year,month,1,hour_utc,0,tzinfo=timezone.utc)
    return d+timedelta(days=((weekday-d.weekday())%7)+7*(nth-1))

def is_us_dst(dt_utc):
    y=dt_utc.year
    return nth_weekday_utc(y,3,6,2,7) <= dt_utc < nth_weekday_utc(y,11,6,1,6)

def get_market_session(now_utc=None):
    now_utc=now_utc or datetime.now(timezone.utc)
    off=-4 if is_us_dst(now_utc) else -5
    et=now_utc+timedelta(hours=off)
    if et.weekday()>=5: return 'closed', et, off
    m=et.hour*60+et.minute
    if 240 <= m < 570: return 'pre', et, off
    if 570 <= m < 960: return 'regular', et, off
    if 960 <= m < 1200: return 'after', et, off
    return 'closed', et, off

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
    price,base=sf(price),sf(base)
    if price is None or base in (None,0): return None
    return round((price-base)/base*100,4)

def et_date(ts, off):
    try:
        return (datetime.fromtimestamp(int(ts),tz=timezone.utc)+timedelta(hours=off)).strftime('%Y-%m-%d')
    except Exception:
        return None

def fetch_json(url):
    req=urllib.request.Request(url,headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept':'application/json,text/plain,*/*',
        'Accept-Language':'en-US,en;q=0.9,ko;q=0.8',
    })
    with urllib.request.urlopen(req,timeout=TIMEOUT) as r:
        return json.loads(r.read().decode('utf-8'))

def load_symbols():
    with open(SECTORS_PATH,encoding='utf-8') as f: sectors=json.load(f)
    symbols=[]; meta={}
    for sec in sectors:
        for it in sec.get('tickers',[]):
            t=str(it.get('ticker','')).strip().upper()
            if not t: continue
            symbols.append(t)
            meta[t]={
                'display_ticker':it.get('display_ticker',t),
                'name':it.get('name',''),
                'market_cap_usd':it.get('market_cap_usd',0),
                'industry':sec.get('industry',''),
                'gics11':sec.get('gics11',''),
            }
    return sorted(set(symbols)), meta, sectors

def chart(symbol):
    url=f'https://query1.finance.yahoo.com/v8/finance/chart/{url_quote(symbol)}?interval=1d&range=5d&includePrePost=true'
    return fetch_json(url)

def extract(data):
    result=(data.get('chart',{}).get('result') or [None])[0]
    if not result: return None, []
    meta=result.get('meta',{}) or {}
    q=(result.get('indicators',{}).get('quote') or [{}])[0] or {}
    ts=result.get('timestamp') or []
    closes=q.get('close') or []
    vols=q.get('volume') or []
    valid=[]
    for i,t in enumerate(ts):
        c=closes[i] if i<len(closes) else None
        v=vols[i] if i<len(vols) else None
        if c is not None: valid.append((t,sf(c),si(v,0)))
    return meta, valid

def parse_symbol(symbol, session, off, info):
    last_err=None; data=None
    for attempt in range(MAX_RETRIES+1):
        try:
            data=chart(symbol); break
        except Exception as e:
            last_err=e; time.sleep(0.35+attempt*0.45)
    if data is None: raise RuntimeError(f'v8 daily failed: {last_err}')
    meta,valid=extract(data)
    if not meta: raise ValueError('v8 chart missing meta')
    if not valid: raise ValueError('v8 chart has no valid daily close')

    regular_price=sf(meta.get('regularMarketPrice'))
    prev_close=sf(meta.get('regularMarketPreviousClose')) or sf(meta.get('previousClose')) or sf(meta.get('chartPreviousClose'))
    regular_volume=si(meta.get('regularMarketVolume'),0)
    regular_time=meta.get('regularMarketTime')
    if len(valid)>=2:
        daily_prev=valid[-2][1]; daily_last=valid[-1][1]; daily_vol=valid[-1][2]
    else:
        daily_prev=None; daily_last=valid[-1][1]; daily_vol=valid[-1][2]
    if regular_price is None: regular_price=daily_last
    if prev_close is None or prev_close==0: prev_close=daily_prev if daily_prev else regular_price
    if not regular_volume: regular_volume=daily_vol or 0
    regular_change=pct(regular_price, prev_close) or 0.0

    pre_price=sf(meta.get('preMarketPrice')); post_price=sf(meta.get('postMarketPrice'))
    pre_volume=si(meta.get('preMarketVolume'),0); post_volume=si(meta.get('postMarketVolume'),0)
    pre_change=pct(pre_price, prev_close) if pre_price is not None else None
    post_change=pct(post_price, prev_close) if post_price is not None else None

    if session=='pre':
        live_price=pre_price if pre_price is not None else regular_price
        live_change=pre_change if pre_change is not None else pct(live_price,prev_close)
        live_volume=pre_volume
    elif session=='after':
        live_price=post_price if post_price is not None else regular_price
        live_change=post_change if post_change is not None else pct(live_price,prev_close)
        live_volume=post_volume
    elif session=='regular':
        live_price=regular_price; live_change=regular_change; live_volume=regular_volume
    else:
        live_price=regular_price; live_change=regular_change; live_volume=regular_volume
    if live_change is None: live_change=regular_change
    regular_date=et_date(regular_time, off) or (et_date(valid[-1][0],off) if valid else None)

    return {
        'symbol':symbol,'display_ticker':info.get('display_ticker',symbol),'name':info.get('name',meta.get('shortName') or meta.get('longName') or ''),
        'gics11':info.get('gics11',''),'industry':info.get('industry',''),'market_cap_usd':info.get('market_cap_usd',0),
        'currency':meta.get('currency','USD'),'exchange':meta.get('exchangeName'),'instrument_type':meta.get('instrumentType'),
        'prev_close':round(float(prev_close),4),'regular_price':round(float(regular_price),4),'regular_change':round(float(regular_change),4),
        'regular_volume':regular_volume,'regular_dollar_volume':round(float(regular_price)*regular_volume,2),
        'regular_market_time':regular_time,'regular_date':regular_date,
        'pre_price':round(float(pre_price),4) if pre_price is not None else None,'pre_change':round(float(pre_change),4) if pre_change is not None else None,'pre_volume':pre_volume,
        'post_price':round(float(post_price),4) if post_price is not None else None,'post_change':round(float(post_change),4) if post_change is not None else None,'post_volume':post_volume,
        'live_session':session,'live_price':round(float(live_price),4),'live_change':round(float(live_change),4),
        'live_volume':live_volume,'live_dollar_volume':round(float(live_price)*live_volume,2),
        'price':round(float(regular_price),4),'change':round(float(regular_change),4),'volume':regular_volume,
        'source':'v8_chart_fast'
    }

def collect(symbols, meta, session, off):
    results={}; failures={}; total=len(symbols)
    for i,sym in enumerate(symbols):
        if i%50==0: print(f'  [미장] {i}/{total} symbols')
        try: results[sym]=parse_symbol(sym,session,off,meta.get(sym,{}))
        except Exception as e:
            failures[sym]=str(e); results[sym]=None
        time.sleep(REQUEST_DELAY)
    print(f'  [미장] 완료: {sum(1 for v in results.values() if v)}/{total}, 실패 {len(failures)}')
    if failures:
        print('  [미장] 실패 일부:')
        for sym,err in list(failures.items())[:20]: print(f'    - {sym}: {err}')
    return results, failures

def infer_date(us, fallback):
    dates=[v.get('regular_date') for v in us.values() if v and v.get('regular_date')]
    return Counter(dates).most_common(1)[0][0] if dates else fallback

def main():
    now_utc=datetime.now(timezone.utc); session,et,off=get_market_session(now_utc)
    kst=timezone(timedelta(hours=9)); now_kst=now_utc.astimezone(kst)
    print('='*70); print('GICS74 가격 수집 시작 v2.2 FAST')
    print('UTC:',now_utc.isoformat()); print('ET :',et.strftime('%Y-%m-%d %H:%M:%S')); print('KST:',now_kst.isoformat())
    print('SESSION:',session); print('SOURCE: Yahoo v8 chart daily fast'); print('='*70)
    symbols,meta,sectors=load_symbols(); print(f'미장 티커 {len(symbols)}개 / GICS 섹터 {len(sectors)}개')
    us,failures=collect(symbols,meta,session,off)
    date_key=infer_date(us, et.strftime('%Y-%m-%d'))
    output={
        'schema_version':'gics74_prices_v2_2_fast','updated_at':now_utc.isoformat(),'updated_at_kst':now_kst.isoformat(),
        'date_key':date_key,'session':session,'is_us_dst':is_us_dst(now_utc),'regular_final_locked':session!='regular',
        'source':'yahoo_v8_chart_daily_fast',
        'counts':{'sectors':len(sectors),'symbols':len(symbols),'ok':sum(1 for v in us.values() if v),'failed':len(failures)},
        'failures':failures,'us':us
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH),exist_ok=True)
    with open(OUTPUT_PATH,'w',encoding='utf-8') as f: json.dump(output,f,ensure_ascii=False)
    print('저장 완료:',OUTPUT_PATH); print('date_key:',date_key); print('regular_final_locked:',session!='regular'); print('failures:',len(failures)); print('='*70)
if __name__=='__main__': main()
