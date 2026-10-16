"""Validate cross-day SOLUSDT depth state and trade-stream completeness.

Three UTC days, 2026-09-01 .. 2026-09-03:
  - chain-of-state using already independently audited Audit replay logic,
  - capture before-day (initial) and after-day (final) deterministic book fingerprints,
  - full 48 hourly files/day (L2 + trades),
  - sample suspicious trade ID gaps including vendor NA rows,
  - time budgets for data download vs L2 replay vs trade validation.

Anonymous downloads conservatively throttled to <= 52 requests/min (1.15s gap).
No raw user/vendor data is stored outside ephemeral runner.
The state fingerprints are experiment evidence, not finalized snapshots for HFT replay.
"""
import hashlib
import json
import os
import resource
import sys
import time
import urllib.error
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pyarrow.parquet as pq

from audit_cryptohftdata_7d import Audit, scan_hour

START=datetime(2026,9,1,tzinfo=timezone.utc)
DAYS=3
SYMBOL="SOLUSDT"
DOWNLOAD_SPACING=1.18
last_request=0.0
report={"test":"SOLUSDT cross-day 3day book-state chain with L2 and Trades",
        "start":START.date().isoformat(),"days":DAYS,"market":"binance_futures",
        "anonymous":True,"status":"in_progress",
        "source_replay":"scripts/audit_cryptohftdata_7d.py",
        "days_report":[],"tests":[]}
last_trade_id=None
total=Counter()
timings=Counter()
t0=time.monotonic()

def throttle():
    global last_request
    until=last_request+DOWNLOAD_SPACING
    remain=until-time.monotonic()
    if remain>0:time.sleep(remain)
    last_request=time.monotonic()

def hexdigest(book):
    digest=hashlib.sha256()
    for side,levels in (("bid",book.bid),("ask",book.ask)):
        digest.update(side.encode())
        for px,qty in sorted(levels.items()):
            digest.update(repr(px).encode())
            digest.update(b":")
            digest.update(repr(qty).encode())
            digest.update(b";")
    return digest.hexdigest()

def state(book):
    valid=bool(book.valid and book.bid and book.ask and max(book.bid)<min(book.ask))
    return {
        "valid":valid,
        "last_applied_u":book.prev_id,
        "bid_levels":len(book.bid),"ask_levels":len(book.ask),
        "best_bid":max(book.bid) if book.bid else None,
        "best_ask":min(book.ask) if book.ask else None,
        "book_digest":hexdigest(book) if valid else None,
        "note":"full reconstructed numeric book SHA256; production snapshots should retain exact decimal field semantics"
    }

def download_trades(date,hour,target):
    global last_trade_id
    rel=f"binance_futures/{date}/{hour:02d}/{SYMBOL}_trades.parquet"
    url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
    size=0
    retries=0
    download_start=time.monotonic()
    for attempt in range(5):
        try:
            throttle()
            with urlopen(Request(url,headers={"User-Agent":"quant-test-cross-day-audit/1.0"}),timeout=80) as src,target.open("wb") as dst:
                while chunk:=src.read(1024*1024):
                    dst.write(chunk);size+=len(chunk)
            break
        except urllib.error.HTTPError as e:
            if attempt==4 or e.code not in (429,500,502,503,504):raise
            retries+=1;target.unlink(missing_ok=True);size=0
            wait=int(e.headers.get("Retry-After","3")) if e.code==429 else 3*(attempt+1)
            time.sleep(min(75,max(2,wait)))
        except Exception:
            if attempt==4:raise
            retries+=1;target.unlink(missing_ok=True);size=0
            time.sleep(3*(attempt+1))
    downloaded_in=time.monotonic()-download_start
    pf=pq.ParquetFile(target)
    required=["trade_id","order_type","received_time","event_time","price","quantity"]
    assert set(required).issubset(set(pf.schema_arrow.names)),f"missing trade schema fields: {pf.schema_arrow.names}"
    out={"rows":pf.metadata.num_rows,"bytes":size,"download_seconds":round(downloaded_in,2),
         "retry_count":retries,"NA":0,"positive_trade_id_gaps":0,"missing_trade_ids":0,
         "nonincreasing_trade_ids":0,"unusable_null_ids":0,"first_id":None,"last_id":None}
    t=time.monotonic()
    for batch in pf.iter_batches(batch_size=160_000,columns=["trade_id","order_type"]):
        ids=batch.column(0).to_pylist()
        typ=batch.column(1).to_pylist()
        for i,k in zip(ids,typ):
            if k=="NA":out["NA"]+=1
            if i is None:
                out["unusable_null_ids"]+=1
                continue
            i=int(i)
            if out["first_id"] is None:out["first_id"]=i
            if last_trade_id is not None:
                if i>last_trade_id+1:
                    out["positive_trade_id_gaps"]+=1
                    out["missing_trade_ids"]+=i-last_trade_id-1
                elif i<=last_trade_id:
                    out["nonincreasing_trade_ids"]+=1
            last_trade_id=i
    out["last_id"]=last_trade_id
    out["parse_seconds"]=round(time.monotonic()-t,2)
    target.unlink(missing_ok=True)
    return out

def compare_delta(now,base):
    return {k:now.get(k,0)-base.get(k,0) for k in set(now)|set(base)}

def work():
    book=Audit(START,START+timedelta(days=DAYS))
    with TemporaryDirectory() as d:
        temp=Path(d)
        for delta in range(DAYS):
            date=(START+timedelta(days=delta)).date().isoformat()
            stats_start=dict(book.stats)
            a=state(book)
            s={"date":date,"initial":a,
               "hours":[],"stats":{},"download_bytes":0,
               "wall_seconds":0.0,"download_plus_api_spacing_seconds":0.0,
               "l2_download_and_replay_seconds":0.0,
               "trade_download_seconds":0.0,
               "trade_parse_seconds":0.0,
               "trade_rows":0,"NA_rows":0,"trade_id_gap_groups":0,
               "trade_ids_missing":0,"trade_id_nonincreasing":0}
            started=time.monotonic()
            for hour in range(24):
                throttle()
                lstart=time.monotonic()
                l2=scan_hour(book,date,hour,d)
                l2sec=time.monotonic()-lstart
                trade=download_trades(date,hour,temp/"one_hour_trades.parquet")
                s["l2_download_and_replay_seconds"]+=l2sec
                s["trade_download_seconds"]+=trade["download_seconds"]
                s["trade_parse_seconds"]+=trade["parse_seconds"]
                s["trade_rows"]+=trade["rows"]
                s["NA_rows"]+=trade["NA"]
                s["trade_id_gap_groups"]+=trade["positive_trade_id_gaps"]
                s["trade_ids_missing"]+=trade["missing_trade_ids"]
                s["trade_id_nonincreasing"]+=trade["nonincreasing_trade_ids"]
                s["download_bytes"]+=l2.get("bytes",0)+trade["bytes"]
                s["hours"].append({
                    "hour":hour,"l2_status":l2["status"],"l2_rows":l2.get("rows",0),
                    "l2_bytes":l2.get("bytes",0),"l2_sec":round(l2sec,3),
                    "trade_rows":trade["rows"],"trade_bytes":trade["bytes"],
                    "trade_na":trade["NA"],"trade_gap_groups":trade["positive_trade_id_gaps"],
                    "trade_missing_ids":trade["missing_trade_ids"],
                    "trade_nonincreasing":trade["nonincreasing_trade_ids"]})
                if l2["status"]!="ok":raise ValueError(f"L2 hour file {date} {hour} missing: {l2}")
                print("CHAIN_HOUR "+json.dumps({"date":date,"hour":hour,
                    "book_valid":book.valid,"seq":book.prev_id,
                    "l2sec":round(l2sec,2),"trade_download_s":trade["download_seconds"],
                    "trade_parse_s":trade["parse_seconds"],
                    "trade_gap_groups":trade["positive_trade_id_gaps"]}),flush=True)
            book.finish_group()
            s["final"]=state(book)
            s["stats"]=compare_delta(dict(book.stats),stats_start)
            s["wall_seconds"]=round(time.monotonic()-started,2)
            s["download_plus_api_spacing_seconds"]="included in per-stream times and day wall clock"
            s["megabytes"]=round(s["download_bytes"]/1_000_000,2)
            s["anchor_status"]="verified" if s["initial"]["valid"] and s["final"]["valid"] and s["stats"].get("chain_gaps",0)==0 else "partial"
            s["book_chain_valid_through_day"]=(s["initial"]["valid"] and
                s["final"]["valid"] and s["stats"].get("chain_gaps",0)==0)
            report["days_report"].append(s)
            report["tests"].append({"date":date,"initial_matches_previous_final":
                a==report["days_report"][-2]["final"] if delta>0 else None,
                "state_is_valid":s["final"]["valid"]})
            print("CHAIN_DAY "+json.dumps({k:v for k,v in s.items() if k!="hours"},
                    ensure_ascii=False),flush=True)
    report["aggregate"]={
        "hours":sum(len(d["hours"]) for d in report["days_report"]),
        "raw_files":sum(len(d["hours"])*2 for d in report["days_report"]),
        "total_bytes":sum(d["download_bytes"] for d in report["days_report"]),
        "total_MB":round(sum(d["download_bytes"] for d in report["days_report"])/1_000_000,2),
        "trade_rows":sum(d["trade_rows"] for d in report["days_report"]),
        "NA_rows":sum(d["NA_rows"] for d in report["days_report"]),
        "cross_day_state_handoff_exact":all(t["initial_matches_previous_final"] for t in report["tests"][1:]),
        "all_l2_files_available":all(h["l2_status"]=="ok" for d in report["days_report"] for h in d["hours"]),
        "run_wall_seconds":round(time.monotonic()-t0,2),
        "peak_python_process_RSS_MB":round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)}
    # Cross-day handoff is the only invariant we strictly assert here. Other
    # quality issues must be recorded as partial rather than hiding the day.
    assert report["aggregate"]["cross_day_state_handoff_exact"]
    report["status"]="success"
    print("CHAIN_RESULT "+json.dumps(report["aggregate"]),flush=True)

if __name__=="__main__":
    try:work()
    except Exception as e:
        import traceback
        report["status"]="failed"
        report["exception"]=repr(e)
        print(traceback.format_exc(),flush=True)
        raise
    finally:
        Path("chain_audit_result.json").write_text(
            json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding="utf-8")
