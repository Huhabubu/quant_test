"""Audit actual Binance Futures exchange and collector timestamp resolution, 1 hour.

Aggregation for actual update message groups avoids overweighting messages
containing many price levels. Market data remains ephemeral.
"""
import json, statistics, tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
import numpy as np
import pyarrow.parquet as pq

DATE="2026-09-01";HOUR="12";SYMBOL="SOLUSDT"
def load(kind,cols,temp):
    rel=f"binance_futures/{DATE}/{HOUR}/{SYMBOL}_{kind}.parquet"
    url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
    fn=Path(temp)/f"{kind}.parquet"
    with urlopen(Request(url,headers={"User-Agent":"quant-test-timestamps-audit/1.0"}),timeout=60) as r,fn.open("wb") as f:
        while chunk:=r.read(1_000_000):f.write(chunk)
    return pq.read_table(fn, columns=cols).to_pydict()

def quant(arr):
    a=np.asarray(arr,dtype=np.float64)
    if not a.size: return {"n":0}
    qs=np.percentile(a,[0,1,5,25,50,75,95,99,100])
    return {"n":int(len(a)),"min":float(qs[0]),"p01":float(qs[1]),"p05":float(qs[2]),"p25":float(qs[3]),"median":float(qs[4]),"p75":float(qs[5]),"p95":float(qs[6]),"p99":float(qs[7]),"max":float(qs[8]),"negative":int((a<0).sum()),"zero":int((a==0).sum())}

def summarize(kind,t):
    if kind=="orderbook":
        # identify each incremental-depth message by its unique final update
        # ID. Snapshots carry their own timestamps and should not distort
        # market-feed latency distribution.
        seen=set();recv=[];evt=[];tx=[];is_update=[]
        for r,e,T,ty,seq in zip(t["received_time"],t["event_time"],t["transaction_time"],t["event_type"],t["final_update_id"]):
            if ty!="update" or seq is None:continue
            if seq in seen:continue
            seen.add(seq);recv.append(r);evt.append(e);tx.append(T)
    else:
        recv=t["received_time"];evt=t["event_time"];tx=t["trade_time"]
    recv=np.asarray(recv,dtype=np.int64)
    evt=np.asarray(evt,dtype=np.int64)
    tx=np.asarray(tx,dtype=np.int64)
    valid=(tx>0)&(evt>0)
    recv,evt,tx=recv[valid],evt[valid],tx[valid]
    return {
        "market_messages":int(len(recv)),
        "E_minus_T_ms":quant(evt-tx),
        "recv_minus_E_ms":quant((recv-evt*1_000_000)/1_000_000),
        "recv_minus_T_ms":quant((recv-tx*1_000_000)/1_000_000),
        "received_time_frac_not_integer_ms":round(float((recv%1_000_000!=0).mean()),6),
        "received_time_frac_not_integer_us":round(float((recv%1000!=0).mean()),6),
        "exchange_T_repeated_ms_fraction":round(float(1-len(set(tx.tolist()))/len(tx)),6),
        "exchange_E_repeated_ms_fraction":round(float(1-len(set(evt.tolist()))/len(evt)),6),
        "E_earlier_than_T":int(np.sum(evt<tx)),
        "E_equal_T":int(np.sum(evt==tx)),
    }

with tempfile.TemporaryDirectory() as d:
    ob=load("orderbook",["received_time","event_time","transaction_time","event_type","final_update_id"],d)
    tr=load("trades",["received_time","event_time","trade_time"],d)
    print("TIMESTAMP_AUDIT "+json.dumps({
        "symbol":SYMBOL,"date":DATE,"hour":HOUR,
        "raw_level_rows":len(ob["received_time"]),"raw_trade_rows":len(tr["received_time"]),
        "depth_updates":summarize("orderbook",ob),
        "trades":summarize("trades",tr)
    },ensure_ascii=False),flush=True)
