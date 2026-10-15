"""Locate a valid CryptoHFTData Binance Futures SOLUSDT book checkpoint on Tardis' free day.
No raw rows are printed, uploaded or committed.
"""
import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

DATE="2026-09-01"
SYMBOL="SOLUSDT"
out=[]
with TemporaryDirectory() as td:
    path=Path(td)/"data.parquet"
    for hour in range(1, 13):
        remote=f"binance_futures/{DATE}/{hour:02d}/{SYMBOL}_orderbook.parquet"
        url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":remote})
        result={"hour":hour}
        try:
            with urlopen(Request(url,headers={"User-Agent":"quant-test-l2-validation/1.0"}), timeout=90) as resp, path.open("wb") as f:
                while data:=resp.read(1024*1024):
                    f.write(data)
            parquet=pq.ParquetFile(path)
            result["size_bytes"]=path.stat().st_size
            result["rows"]=parquet.metadata.num_rows
            snaps=[]
            for rg in range(parquet.metadata.num_row_groups):
                d=parquet.read_row_group(rg,columns=["event_type","received_time","event_time","last_update_id"])
                types=d.column("event_type")
                indices=pc.indices_nonzero(pc.equal(types,pa.scalar("snapshot")))
                if len(indices):
                    samples=d.take(indices.slice(0,min(3,len(indices)))).to_pylist()
                    snaps+=samples
            result["snapshot_rows_first_sampled_groups"]=len(snaps)
            result["snapshot_examples"]=snaps[:3]
        except Exception as e:
            result["error"]=repr(e)
        out.append(result)
        print("CHECKPOINT_SCAN "+json.dumps(result,default=str),flush=True)
        path.unlink(missing_ok=True)
        if result.get("snapshot_rows_first_sampled_groups"):
            break
print("CHECKPOINT_SCAN_SUMMARY "+json.dumps(out,default=str),flush=True)
