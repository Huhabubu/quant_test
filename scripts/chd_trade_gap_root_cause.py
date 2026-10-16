"""Root-cause analysis of CHD SOLUSDT trade-ID gaps, 2026-09-01 to 03.

Extract all gaps with before/after event time, trade time, receipt time,
price/qty/order_type (no vendor raw data is stored in GitHub).
Cross-check missing IDs versus Binance's FIRST-PARTY futures historical
raw trades daily CSV ZIP, and Tardis Sep 1 independent trade data.

Never upload market archives, only a small aggregated JSON report.
"""
import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
import time
import zipfile
from collections import defaultdict,Counter
from datetime import date,timedelta,datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen

import pyarrow.parquet as pq
import pyarrow.compute as pc
import zstandard

START=date(2026,9,1)
DAYS=3
SYMBOL="SOLUSDT"
OUTPUT=Path("trade_gap_root_cause.json")
report={"symbol":SYMBOL,"market":"binance_futures","dates":[],
        "gaps":[],"comparison":{},"status":"running"}
t0=time.monotonic()

def get(url,filename):
    begin=time.monotonic()
    h=hashlib.sha256()
    with urlopen(Request(url,headers={"User-Agent":"quant-test-trade-gap-root-cause/1.0"}),timeout=100) as src,open(filename,"wb") as f:
        while chunk:=src.read(2*1024*1024):
            f.write(chunk);h.update(chunk)
    return {"bytes":filename.stat().st_size,"download_seconds":round(time.monotonic()-begin,2),
            "sha256":h.hexdigest()}

def nice(row):
    if row is None:return None
    return {"trade_id":int(row["trade_id"]),
            "event_time_ms":int(row["event_time"]),
            "trade_time_ms":int(row["trade_time"]),
            "received_time_ns":int(row["received_time"]),
            "price":str(row["price"]),"quantity":str(row["quantity"]),
            "order_type":str(row["order_type"])}

def extract_chd(temp):
    old=None
    for offset in range(DAYS):
        d=START+timedelta(days=offset)
        day=d.isoformat()
        total=0;na=0;blanks=0;negatives=0
        old=None
        gaps_before=len(report["gaps"])
        for hour in range(24):
            rel=f"binance_futures/{day}/{hour:02d}/{SYMBOL}_trades.parquet"
            url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
            fn=temp/"source.parquet"
            stats=get(url,fn)
            if fn.open("rb").read(4)==bytes.fromhex("28b52ffd"):
                dec=temp/"decoded.parquet"
                with fn.open("rb") as inp,dec.open("wb") as out:
                    zstandard.ZstdDecompressor().copy_stream(inp,out)
                fn=dec
            pf=pq.ParquetFile(fn)
            cols=("trade_id","event_time","trade_time","received_time","price","quantity","order_type")
            for b in pf.iter_batches(batch_size=150000,columns=list(cols)):
                data=b.to_pydict()
                for tup in zip(*(data[x] for x in cols)):
                    row=dict(zip(cols,tup))
                    total+=1
                    if row["order_type"]=="NA":na+=1
                    if row["trade_id"] is None:
                        blanks+=1;continue
                    tid=int(row["trade_id"])
                    if old is not None:
                        prior=int(old["trade_id"])
                        if tid>prior+1:
                            missing=tid-prior-1
                            gap={"date":day,"hour":hour,"before":nice(old),
                                 "after":nice(row),"missing_count":missing,
                                 "missing_id_range":[prior+1,tid-1],
                                 "delta_exchange_trade_time_ms":int(row["trade_time"])-int(old["trade_time"]),
                                 "delta_exchange_event_time_ms":int(row["event_time"])-int(old["event_time"]),
                                 "delta_collector_received_ms":round((int(row["received_time"])-int(old["received_time"]))/1e6,6),
                                 "independent":{}}
                            report["gaps"].append(gap)
                            print("CHD_GAP "+json.dumps(gap,ensure_ascii=False),flush=True)
                        elif tid<=prior:negatives+=1
                    old=row
            if fn.exists():fn.unlink()
            (temp/"source.parquet").unlink(missing_ok=True)
        report["dates"].append({"date":day,"rows":total,"NA":na,"null_trade_ids":blanks,
                                "nonincreasing":negatives,
                                "gaps_found":len(report["gaps"])-gaps_before})
        print("CHD_DAY "+json.dumps(report["dates"][-1]),flush=True)

def crosscheck_binance(day,temp):
    # Binance official public futures raw trades; not aggregated.
    url=f"https://data.binance.vision/data/futures/um/daily/trades/{SYMBOL}/{SYMBOL}-trades-{day}.zip"
    target=temp/f"{day}-binance.zip"
    try:dl=get(url,target)
    except Exception as e:
        report["comparison"].setdefault(day,{})["binance"]={"error":repr(e)}
        print("BINANCE_ERROR "+str(e),flush=True)
        return
    gaps=[g for g in report["gaps"] if g["date"]==day]
    ranges=[(g["missing_id_range"][0],g["missing_id_range"][1],set()) for g in gaps]
    around=[set() for _ in gaps]
    count=0
    cols=[]
    started=time.monotonic()
    with zipfile.ZipFile(target) as arc:
        members=[x for x in arc.namelist() if x.endswith(".csv")]
        if not members: raise RuntimeError(f"No CSV member in {url}: {arc.namelist()}")
        with arc.open(members[0]) as f:
            for raw in f:
                # official CSV may have header; ID is first field for futures trades
                if not raw:continue
                try:
                    idx=int(raw.split(b",",1)[0].strip().strip(b'"'))
                except ValueError:
                    if count==0:
                        cols=raw.decode("utf8","replace").strip().split(",")
                    continue
                count+=1
                for j,(low,high,found) in enumerate(ranges):
                    if low<=idx<=high:
                        found.add(idx)
                    if low-2<=idx<=high+2 and len(around[j])<22:
                        around[j].add(idx)
    results=[]
    for j,g in enumerate(gaps):
        low,high,ids=ranges[j]
        results.append({"range":[low,high],"expected_missing_ids":high-low+1,
                        "official_present_count":len(ids),
                        "official_absent_count":high-low+1-len(ids),
                        "official_first_ids":sorted(ids)[:10],
                        "official_last_ids":sorted(ids)[-10:],
                        "official_nearby_sample":sorted(around[j])[:22]})
        g["independent"]["binance_official_trades"]=results[-1]
    report["comparison"].setdefault(day,{})["binance"]={
        "source":url,"file_bytes":dl["bytes"],"rows_scanned":count,
        "seconds_scanning":round(time.monotonic()-started,2),
        "csv_header":cols,"gaps":results}
    print("BINANCE_DAY "+json.dumps({"date":day,"source_zip_MB":round(dl["bytes"]/1e6,2),"rows":count,"check":results}),flush=True)
    target.unlink()

def tardis_sep1():
    day="2026-09-01"
    gaps=[g for g in report["gaps"] if g["date"]==day]
    if not gaps:return
    target=gaps[0]
    low,high=target["missing_id_range"]
    source=f"https://datasets.tardis.dev/v1/binance-futures/trades/2026/09/01/{SYMBOL}.csv.gz"
    c=0;hits=[];around=[];early_stop=False
    try:
        with urlopen(Request(source,headers={"User-Agent":"quant-test-gap-check/1.0"}),timeout=100) as response:
            with gzip.GzipFile(fileobj=response) as z:
                with io.TextIOWrapper(z,encoding="utf8",newline="") as x:
                    r=csv.DictReader(x)
                    for row in r:
                        c+=1
                        idx=int(row["id"])
                        if low<=idx<=high:
                            hits.append({"id":idx,"price":row.get("price"),"amount":row.get("amount"),
                                         "timestamp":row.get("timestamp")})
                        if low-3<=idx<=high+3:
                            around.append({"id":idx,"price":row.get("price"),"timestamp":row.get("timestamp")})
                        if idx>high+100:
                            early_stop=True
                            break
        outcome={"source":source,"scanned_rows":c,"stopped_after_gap":early_stop,
                 "found_missing_trades":len(hits),"examples":hits[:12],
                 "nearby":around[:22]}
        target["independent"]["tardis"]=outcome
        report["comparison"].setdefault(day,{})["tardis"]=outcome
        print("TARDIS_RESULT "+json.dumps(outcome),flush=True)
    except Exception as e:
        report["comparison"].setdefault(day,{})["tardis"]={"error":repr(e)}
        print("TARDIS_ERROR "+repr(e),flush=True)

def run():
    with tempfile.TemporaryDirectory() as t:
        temp=Path(t)
        extract_chd(temp)
        for i in range(DAYS):
            crosscheck_binance((START+timedelta(days=i)).isoformat(),temp)
        tardis_sep1()
    report["status"]="success"
    report["total_seconds"]=round(time.monotonic()-t0,2)
    print("GAP_ROOT_CAUSE_SUMMARY "+json.dumps({
        "gaps":len(report["gaps"]),
        "missing_ids":sum(g["missing_count"] for g in report["gaps"]),
        "confirmed_by_official_trades":sum(g["independent"].get("binance_official_trades",{}).get("official_present_count",0) for g in report["gaps"]),
        "seconds":report["total_seconds"]}),flush=True)

if __name__=="__main__":
    try: run()
    except Exception as e:
        import traceback
        report["status"]="error";report["exception"]=repr(e)
        print(traceback.format_exc(),flush=True)
        raise
    finally:
        OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
