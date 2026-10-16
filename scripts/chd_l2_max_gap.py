"""Detect actual silent spans in CHD L2 around the missing Trades.

For two selected hourly depth files, scan every row, de-duplicate price
level rows into grouped depth messages, and report largest consecutive
received-time gaps plus neighboring snapshot/update metadata.
"""
import json,tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
import pyarrow.parquet as pq
from datetime import datetime,timezone

for day,hour in [("2026-09-02",16),("2026-09-03",8)]:
 remote=f"binance_futures/{day}/{hour:02d}/SOLUSDT_orderbook.parquet"
 with tempfile.TemporaryDirectory() as td:
  fn=Path(td)/"hour.parquet"
  with urlopen(Request("https://api.cryptohftdata.com/v1/download?"+urlencode({"file":remote}),headers={"User-Agent":"chd-l2-actual-silence-audit/1.0"}),timeout=100) as src,fn.open("wb") as out:
   while b:=src.read(1_048_576):out.write(b)
  pf=pq.ParquetFile(fn)
  items=[];oldkey=None
  for batch in pf.iter_batches(batch_size=200000,
       columns=["received_time","event_time","event_type","first_update_id","final_update_id","prev_final_update_id","last_update_id"]):
   values=batch.to_pydict()
   for tup in zip(*(values[c] for c in ("received_time","event_time","event_type","first_update_id","final_update_id","prev_final_update_id","last_update_id"))):
    recv,E,typ,U,u,pu,L=tup
    key=(recv,typ,U,u,pu,L)
    if key==oldkey:continue
    oldkey=key
    items.append({"recv":recv,"E":E,"typ":typ,"U":U,"u":u,"pu":pu,"L":L})
  items.sort(key=lambda x:x["recv"])
  diffs=sorted(((items[i+1]["recv"]-items[i]["recv"],i) for i in range(len(items)-1)),reverse=True)[:8]
  def fmt(x):
   return {"recv":datetime.fromtimestamp(x["recv"]/1e9,timezone.utc).isoformat(timespec="milliseconds"),
    "E":datetime.fromtimestamp(x["E"]/1000,timezone.utc).isoformat(timespec="milliseconds") if x["E"] else None,
    "type":x["typ"],"first":x["U"],"u":x["u"],"pu":x["pu"],"snapshot_id":x["L"]}
  out={"day":day,"hour":hour,"groups":len(items),
       "snapshots":[fmt(x) for x in items if x["typ"]=="snapshot"][:15],
       "top_silent_spans":[{"seconds":round(dt/1e9,3),"before":fmt(items[i]),"after":fmt(items[i+1])} for dt,i in diffs]}
  print("L2_SILENCE "+json.dumps(out),flush=True)
