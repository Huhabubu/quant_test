"""Estimate Binance futures hourly storage for selected coins using provider file metadata.

Only opens file headers and reads zero market-data bytes.
Results are aggregate size estimates, not guaranteed future growth rates.
"""
import json
import os
import time
import statistics
from collections import defaultdict
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SYMBOLS=os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,1000PEPEUSDT").split(",")
DATES=["2026-09-01","2026-09-03","2026-09-07"]
HOURS=["00","12","20"]
TYPES=["orderbook","trades"]
samples=defaultdict(list)
errors=[]
requests=0

for date in DATES:
    for hour in HOURS:
        for symbol in SYMBOLS:
            for typ in TYPES:
                rel=f"binance_futures/{date}/{hour}/{symbol}_{typ}.parquet"
                api="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
                req=Request(api,headers={"User-Agent":"quant-test-sizing-metadata/1.0"})
                info={"date":date,"hour":hour,"symbol":symbol,"type":typ}
                for attempt in range(3):
                    try:
                        with urlopen(req,timeout=22) as response:
                            size=int(response.headers.get("Content-Length") or 0)
                            kind=response.headers.get("Content-Type")
                            if response.status != 200 or size<=0:
                                raise ValueError(f"status={response.status} size={size} content_type={kind}")
                            info.update(bytes=size, content_type=kind)
                            samples[(symbol,typ)].append(info)
                        break
                    except Exception as exc:
                        if attempt==2:
                            info["error"]=repr(exc)
                            errors.append(info)
                        else:
                            time.sleep(2*(attempt+1))
                requests+=1
                if requests%24==0:
                    print("SIZING_PROGRESS "+json.dumps({"requests":requests,"success":sum(map(len,samples.values())),"errors":len(errors)}),flush=True)
                time.sleep(0.65)

output={}
for symbol in SYMBOLS:
    val={}
    for typ in TYPES:
        sizes=[x["bytes"] for x in samples[(symbol,typ)]]
        val[typ]={
            "samples":len(sizes),
            "sampled_hour_sizes_MB":[round(n/1e6,3) for n in sizes],
            "mean_MB_per_hour":round(statistics.mean(sizes)/1e6,3) if sizes else None,
            "median_MB_per_hour":round(statistics.median(sizes)/1e6,3) if sizes else None,
            "range_MB_per_hour":[round(min(sizes)/1e6,3),round(max(sizes)/1e6,3)] if sizes else None,
            "estimate_GB_per_day":round(statistics.mean(sizes)*24/1e9,3) if sizes else None,
            "estimate_GB_per_365_days":round(statistics.mean(sizes)*24*365/1e9,2) if sizes else None,
        }
    output[symbol]=val
print("SIZE_ESTIMATE_REPORT "+json.dumps({
    "symbols":SYMBOLS,"dates":DATES,"hours":HOURS,
    "requests":requests,"success":sum(map(len,samples.values())),"errors":errors,
    "data":output},ensure_ascii=False),flush=True)
