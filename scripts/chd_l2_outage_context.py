"""Inspect L2 snapshots and recovery around two suspicious trade feed outages.

Complements root cause trade-ID crosscheck. Source data ephemeral;
stdout contains metadata only (timestamps and update IDs).
"""
import json
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
import pyarrow.parquet as pq
from datetime import datetime,timezone

TESTS=[
 ("2026-09-02",16,1788367108135,1788367230005),
 ("2026-09-03",8,1788424727089,1788424733049),
]
def iso(ms):
 return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec="milliseconds")
def main():
 with tempfile.TemporaryDirectory() as td:
  dst=Path(td)/"part.parquet"
  for day,hour,start,end in TESTS:
   rel=f"binance_futures/{day}/{hour:02d}/SOLUSDT_orderbook.parquet"
   url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
   with urlopen(Request(url,headers={"User-Agent":"quant-test-depth-outage-window/1.0"}),timeout=100) as src,dst.open("wb") as f:
    while c:=src.read(1_048_576):f.write(c)
   pf=pq.ParquetFile(dst)
   records=[];prev=None
   for batch in pf.iter_batches(batch_size=100000,
      columns=["event_time","received_time","event_type","first_update_id",
               "final_update_id","prev_final_update_id","last_update_id"]):
    d=batch.to_pydict()
    for e,r,typ,U,u,pu,last_id in zip(*(d[k] for k in ("event_time","received_time","event_type","first_update_id","final_update_id","prev_final_update_id","last_update_id"))):
     # snapshots sometimes have event_time far earlier than receipt
     localms=r//1_000_000 if r is not None else 0
     if localms<start-20000 or localms>end+20000:continue
     key=(typ,r,U,u,pu,last_id)
     if key==prev:continue
     prev=key
     records.append({"kind":typ,"E":e,"received":localms,
                     "first":U,"last":u,"pu":pu,"snapshot_id":last_id})
   records.sort(key=lambda x:x["received"])
   b=[x for x in records if x["received"]<start]
   inside=[x for x in records if start<=x["received"]<=end]
   a=[x for x in records if x["received"]>end]
   snaps=[x for x in records if x["kind"]=="snapshot"]
   simplified=lambda xs:[{"kind":x["kind"],"E":iso(x["E"]) if x["E"] else None,
     "received":iso(x["received"]),"first":x["first"],"u":x["last"],
     "pu":x["pu"],"snapshot_id":x["snapshot_id"]} for x in xs]
   result={"day":day,"hour":hour,"trade_gap_start":iso(start),"trade_gap_end":iso(end),
           "book_groups_before_20s":len(b),"during":len(inside),"after_20s":len(a),
           "last_before":simplified(b[-4:]),"first_during":simplified(inside[:4]),
           "last_during":simplified(inside[-4:]),"first_after":simplified(a[:7]),
           "snapshots_around":simplified(snaps)}
   print("OUTAGE_BOOK_CONTEXT "+json.dumps(result),flush=True)
   dst.unlink(missing_ok=True)
if __name__=="__main__":main()
