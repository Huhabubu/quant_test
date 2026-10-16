"""Inspect CHD L2 stream activity in exact six independently confirmed Trade gap intervals.

Purpose: separate a trade-stream outage from market-wide missing L2.
For each gap, count distinct L2 update messages with E inside the
trade gap and report max update arrival interval, without retaining
or publishing raw source data.
"""
import json
import urllib.request
import tempfile
from pathlib import Path
from urllib.parse import urlencode
import pyarrow.parquet as pq
import numpy as np

INTERVALS=[
    ("2026-09-01",15,1788276773082,1788276775735),
    ("2026-09-02",15,1788362577642,1788362580119),
    ("2026-09-02",16,1788367108135,1788367230005),
    ("2026-09-02",22,1788386519755,1788386523625),
    ("2026-09-03",8,1788424727089,1788424733049),
    ("2026-09-03",21,1788472325613,1788472328079),
]
def main():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        for day,hour,lo,hi in INTERVALS:
            fn=root/"tmp.parquet"
            remote=f"binance_futures/{day}/{hour:02d}/SOLUSDT_orderbook.parquet"
            url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":remote})
            with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"quant-test-L2-during-trade-gaps/1.0"}),timeout=100) as resp,fn.open("wb") as out:
                while c:=resp.read(2*1024*1024):out.write(c)
            pf=pq.ParquetFile(fn)
            by_update={}
            total_rows=0;total_groups=0
            for batch in pf.iter_batches(batch_size=200000,columns=["event_time","received_time","event_type","final_update_id","prev_final_update_id"]):
                dt=batch.to_pydict()
                for e,r,typ,u,pu in zip(*(dt[k] for k in ("event_time","received_time","event_type","final_update_id","prev_final_update_id"))):
                    if typ!="update" or e is None or e<lo or e>hi:continue
                    total_rows+=1
                    if u not in by_update:by_update[u]={"E":e,"recv":r,"pu":pu}
            records=sorted(by_update.values(),key=lambda x:x["E"])
            diffs=[records[i+1]["E"]-records[i]["E"] for i in range(len(records)-1)]
            chain_jumps=sum(a["pu"]!=b["u"] for a,b in []) # update IDs not all saved as key, see below
            rec_by_id=sorted(by_update.items(),key=lambda p:p[1]["recv"])
            seq_jumps=sum(rec_by_id[i+1][1]["pu"]!=rec_by_id[i][0] for i in range(len(rec_by_id)-1))
            outcome={"date":day,"hour":hour,"trade_gap_ms":hi-lo,
                "l2_update_groups_within_gap":len(records),
                "l2_price_level_rows_within_gap":total_rows,
                "l2_first_E":records[0]["E"] if records else None,
                "l2_last_E":records[-1]["E"] if records else None,
                "max_exchange_event_gap_ms":max(diffs) if diffs else None,
                "l2_update_pu_mismatches_within_gap":seq_jumps,
                "note":"L2 events counted from provider's CHD dataset; sequence tested within selected interval only, first and last group outside range not included"}
            print("L2_DURING_TRADE_GAP "+json.dumps(outcome),flush=True)
            fn.unlink(missing_ok=True)
if __name__=="__main__":main()
