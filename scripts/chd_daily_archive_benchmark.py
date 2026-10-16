"""One-day archive compression benchmark; market data stays on ephemeral runner.

A: ZIP_STORED containing vendor's 48 byte-identical hourly files.
B1: one Parquet/ZSTD per type, all fields and row order preserved.
B2: one Parquet/ZSTD per type, price/quantity stored as decimal-scaled
    int64 (NUMERIC lossless with scale 1e8; lexical strings not preserved).
C: hftbacktest Event-layout NPZ SIZE PROXY, not an order-corrected
    or market-replay-ready conversion (no book clear/order corrections).

Reports size, disk use, timing, RAM peak, schemas, and FULL content
equivalence for B1 and B2. No raw event data is uploaded anywhere.
"""
import concurrent.futures
import hashlib
import json
import os
import resource
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import zstandard as zstd

SYMBOL=os.environ.get("CHD_SYMBOL","SOLUSDT")
DAY=os.environ.get("CHD_DAY","2026-09-01")
OUT=Path(os.environ.get("CHD_OUT","archive_benchmark_result.json"))
CHUNK=65536
SCALE=100_000_000
SCALAR=pa.scalar(SCALE,type=pa.decimal128(9,0))
DECIMAL=pa.decimal128(24,8)
KINDS=("orderbook","trades")
t0=time.perf_counter()
def stamp(): return round(time.perf_counter()-t0,2)
def emit(key,data): print("BENCH "+key+" "+json.dumps(data,default=str,sort_keys=True),flush=True)
def mem_mb(): return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)
def mb(n): return round(n/1_000_000,3)

def download_one(item, dest):
    hour,kind=item
    rel=f"binance_futures/{DAY}/{hour:02d}/{SYMBOL}_{kind}.parquet"
    url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
    target=dest/f"{hour:02d}_{kind}.raw"
    for attempt in range(4):
        try:
            req=Request(url,headers={"User-Agent":"quant-test-archive-benchmark/1.0"})
            h=hashlib.sha256()
            with urlopen(req,timeout=75) as resp,open(target,"wb") as f:
                while True:
                    b=resp.read(2*1024*1024)
                    if not b: break
                    f.write(b);h.update(b)
            with open(target,"rb") as f: outer_zstd=f.read(4)==bytes.fromhex("28b52ffd")
            return {"hour":hour,"kind":kind,"path":str(target),
                    "arcname":f"{hour:02d}_{kind}.parquet"+(".zst" if outer_zstd else ""),
                    "bytes":target.stat().st_size,"sha256":h.hexdigest(),
                    "outer_zstd":outer_zstd}
        except Exception as e:
            if attempt==3: raise RuntimeError(f"download failed {rel}: {e}") from e
            time.sleep(3*(attempt+1))

def parquet_path(item, folder):
    src=Path(item["path"])
    if not item["outer_zstd"]: return src
    dst=folder/(item["arcname"][:-4])
    if not dst.exists():
        with open(src,"rb") as inp,open(dst,"wb") as out:
            zstd.ZstdDecompressor().copy_stream(inp,out)
    return dst

def fixed_int(array):
    # Casting directly from source decimal string: no float intermediate.
    num=pc.cast(array,DECIMAL,safe=True)
    return pc.cast(pc.multiply(num,SCALAR),pa.int64(),safe=True)

def fixed_batch(batch):
    arrays=[]
    for i,field in enumerate(batch.schema):
        c=batch.column(i)
        if field.name in ("price","quantity"):
            c=fixed_int(c)
        arrays.append(c)
    fields=[pa.field(f.name,pa.int64() if f.name in ("price","quantity") else f.type,
                     nullable=f.nullable) for f in batch.schema]
    schema=pa.schema(fields,metadata={b"price_quantity_scale":b"100000000",
                                      b"value_semantics":b"numeric_lossless_not_original_lexical"})
    return pa.RecordBatch.from_arrays(arrays,schema=schema)

class Cursor:
    def __init__(self,path):
        self.it=iter(pq.ParquetFile(path).iter_batches(batch_size=CHUNK))
        self.buf=None; self.i=0
    def take(self,n):
        pieces=[]
        while n:
            if self.buf is None or self.i>=len(self.buf):
                self.buf=next(self.it)
                self.i=0
            k=min(n,len(self.buf)-self.i)
            pieces.append(self.buf.slice(self.i,k))
            self.i+=k;n-=k
        return pa.Table.from_batches(pieces)
    def done(self):
        if self.buf is not None and self.i<len(self.buf):return False
        return next(self.it,None) is None

def verify(kind, source_files, b1, b2, decoded):
    c1=Cursor(b1);c2=Cursor(b2)
    total=0; groups=0
    for item in source_files:
        for batch in pq.ParquetFile(parquet_path(item,decoded)).iter_batches(batch_size=CHUNK):
            t=pa.Table.from_batches([batch])
            reconstructed1=c1.take(len(batch))
            if not t.equals(reconstructed1,check_metadata=False):
                raise AssertionError(f"B1 columns differ: {kind} {item['hour']} group {groups}")
            t2=pa.Table.from_batches([fixed_batch(batch)])
            reconstructed2=c2.take(len(batch))
            if not t2.equals(reconstructed2,check_metadata=False):
                raise AssertionError(f"B2 numeric fields differ: {kind} {item['hour']} group {groups}")
            total+=len(batch);groups+=1
    assert c1.done() and c2.done(),f"unexpected extra B1/B2 rows: {kind}"
    return {"rows_verified":total,"batches":groups,
            "B1_full_column_order_exact":True,"B2_scaled_integer_exact":True,
            "B2_original_price_strings_preserved":False}

def npz_proxy(source_files, decoded, folder):
    # Valid column layout; NOT a correct_event_order() output.
    dt=np.dtype([("ev","<u8"),("exch_ts","<i8"),("local_ts","<i8"),
                 ("px","<f8"),("qty","<f8"),("order_id","<u8"),
                 ("ival","<i8"),("fval","<f8")])
    maxrows=sum(pq.ParquetFile(parquet_path(a,decoded)).metadata.num_rows for a in source_files)
    memfile=folder/"benchmark_events.memmap"
    arr=np.memmap(memfile,mode="w+",dtype=dt,shape=(maxrows,))
    used=0
    flags={"exch_local":(2**31|2**30),"buy":2**29,"sell":2**28}
    na=0
    for item in source_files:
        kind=item["kind"]
        for batch in pq.ParquetFile(parquet_path(item,decoded)).iter_batches(batch_size=CHUNK):
            if kind=="orderbook":
                side=pc.equal(batch.column("side"),"bid").to_numpy(zero_copy_only=False)
                ask=pc.equal(batch.column("side"),"ask").to_numpy(zero_copy_only=False)
                valid=side|ask
                snap=pc.equal(batch.column("event_type"),"snapshot").to_numpy(zero_copy_only=False)
                bits=np.where(side,flags["buy"],flags["sell"]).astype(np.uint64)
                bits|=np.where(snap,4,1).astype(np.uint64)
                delay=677_000
            else:
                order=batch.column("order_type")
                is_na=pc.equal(order,"NA").to_numpy(zero_copy_only=False)
                na+=int(np.count_nonzero(is_na))
                valid=~is_na
                buyer_maker=pc.fill_null(batch.column("is_buyer_maker"),False).to_numpy(zero_copy_only=False)
                bits=np.where(buyer_maker,flags["sell"],flags["buy"]).astype(np.uint64)|np.uint64(2)
                delay=1_398_000
            n=int(np.count_nonzero(valid))
            if not n:continue
            sl=arr[used:used+n]
            sl["ev"]=bits[valid]|np.uint64(flags["exch_local"])
            exch=batch.column("event_time").to_numpy(zero_copy_only=False)[valid].astype(np.int64)*1_000_000
            sl["exch_ts"]=exch
            sl["local_ts"]=exch+delay
            sl["px"]=pc.cast(batch.column("price"),pa.float64()).to_numpy(zero_copy_only=False)[valid]
            sl["qty"]=pc.cast(batch.column("quantity"),pa.float64()).to_numpy(zero_copy_only=False)[valid]
            sl["order_id"]=0;sl["ival"]=0;sl["fval"]=0
            used+=n
    arr.flush()
    zipped=folder/"events_npz_size_proxy.npz"
    np.savez_compressed(zipped,data=arr[:used])
    del arr
    memfile.unlink()
    return {"npz_bytes":zipped.stat().st_size,"bytes_per_event":round(zipped.stat().st_size/used,3),
            "event_rows":used,"excluded_NA_trade_rows":na,
            "official_order_validation":False,
            "limitations":"only size proxy: events are source-file ordered, do not include snapshot reset/clear semantics or EXCH/LOCAL chronological correction; do not use for backtesting"}

def main():
    result={"symbol":SYMBOL,"date":DAY,"market":"binance_futures",
            "test_started_utc":datetime.now(timezone.utc).isoformat(),"status":"running",
            "versions":{"pyarrow":pa.__version__,"numpy":np.__version__,"zstandard":zstd.__version__}}
    try:
        with tempfile.TemporaryDirectory(prefix="chd-archive-") as tmp:
            root=Path(tmp); raw=root/"raw";raw.mkdir()
            decoded=root/"decoded";decoded.mkdir()
            packaged=root/"packages";packaged.mkdir()
            items=[(hour,kind) for hour in range(24) for kind in KINDS]
            t=time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                downloaded=list(pool.map(lambda item:download_one(item,raw),items))
            result["download_seconds"]=round(time.perf_counter()-t,2)
            result["raw_total_bytes"]=sum(x["bytes"] for x in downloaded)
            result["outer_zstd_files"]=sum(x["outer_zstd"] for x in downloaded)
            emit("download",{"raw_MB":mb(result["raw_total_bytes"]),"seconds":result["download_seconds"],
                             "outer_zstd_files":result["outer_zstd_files"]})
            codecs={}
            schema={}
            for kind in KINDS:
                first=next(x for x in downloaded if x["kind"]==kind)
                p=pq.ParquetFile(parquet_path(first,decoded))
                schema[kind]=str(p.schema_arrow)
                codecs[kind]=[{"column":p.metadata.row_group(0).column(i).path_in_schema,
                               "codec":p.metadata.row_group(0).column(i).compression}
                              for i in range(p.metadata.row_group(0).num_columns)]
            result["source_codecs"]=codecs
            result["source_schemas"]=schema
            t=time.perf_counter()
            azip=packaged/f"{SYMBOL}_{DAY}_A.zip"
            with zipfile.ZipFile(azip,"w",compression=zipfile.ZIP_STORED,allowZip64=True) as z:
                for x in downloaded:z.write(x["path"],x["arcname"],compress_type=zipfile.ZIP_STORED)
                z.writestr("manifest.json",json.dumps(
                    {"market":"binance_futures","date":DAY,"symbol":SYMBOL,
                     "source_files":[{k:v for k,v in x.items() if k!="path"} for x in downloaded]},
                    sort_keys=True),compress_type=zipfile.ZIP_STORED)
            result["A"]={"zip_bytes":azip.stat().st_size,
                         "zip_overhead_bytes":azip.stat().st_size-result["raw_total_bytes"],
                         "seconds":round(time.perf_counter()-t,2),"exact_vendor_bytes":True}
            emit("A",result["A"])
            result["B1"]={};result["B2"]={}
            for kind in KINDS:
                subset=sorted((x for x in downloaded if x["kind"]==kind),key=lambda x:x["hour"])
                firstpf=pq.ParquetFile(parquet_path(subset[0],decoded))
                s1=firstpf.schema_arrow.remove_metadata()
                first_batch=next(firstpf.iter_batches(batch_size=CHUNK))
                s2=fixed_batch(first_batch).schema
                fn1=packaged/f"{kind}_B1.parquet"
                fn2=packaged/f"{kind}_B2.parquet"
                wt1=0;wt2=0;transform=0;count=0
                with pq.ParquetWriter(fn1,s1,compression="zstd",compression_level=6,
                                      use_dictionary=True) as w1, pq.ParquetWriter(
                                      fn2,s2,compression="zstd",compression_level=6,
                                      use_dictionary=True) as w2:
                    for x in subset:
                        for batch in pq.ParquetFile(parquet_path(x,decoded)).iter_batches(batch_size=CHUNK):
                            start=time.perf_counter()
                            b1=batch.cast(s1)
                            w1.write_batch(b1,row_group_size=CHUNK)
                            wt1+=time.perf_counter()-start
                            start=time.perf_counter()
                            b2=fixed_batch(batch)
                            transform+=time.perf_counter()-start
                            start=time.perf_counter()
                            w2.write_batch(b2,row_group_size=CHUNK)
                            wt2+=time.perf_counter()-start
                            count+=len(batch)
                result["B1"][kind]={"bytes":fn1.stat().st_size,"rows":count,"write_seconds":round(wt1,2)}
                result["B2"][kind]={"bytes":fn2.stat().st_size,"rows":count,
                                    "transform_seconds":round(transform,2),"write_seconds":round(wt2,2)}
                emit("parquet_"+kind,{"B1":result["B1"][kind],"B2":result["B2"][kind]})
            # Entire files, all columns, all rows: compare streaming Arrow batches.
            t=time.perf_counter()
            result["logical_equivalence"]={}
            for kind in KINDS:
                subset=sorted((x for x in downloaded if x["kind"]==kind),key=lambda x:x["hour"])
                result["logical_equivalence"][kind]=verify(kind,subset,packaged/f"{kind}_B1.parquet",
                                                             packaged/f"{kind}_B2.parquet",decoded)
            result["verify_seconds"]=round(time.perf_counter()-t,2)
            result["B1"]["total_bytes"]=sum(result["B1"][k]["bytes"] for k in KINDS)
            result["B2"]["total_bytes"]=sum(result["B2"][k]["bytes"] for k in KINDS)
            emit("verify",result["logical_equivalence"])
            # C: NPZ 64-byte-per-event compression-only comparison; NOT validated feed.
            t=time.perf_counter()
            result["C"]=npz_proxy(sorted(downloaded,key=lambda a:(a["kind"]!="orderbook",a["hour"])),decoded,packaged)
            result["C"]["seconds"]=round(time.perf_counter()-t,2)
            emit("C",result["C"])
            result["comparison"]={
                "A_bytes":result["A"]["zip_bytes"],
                "B1_bytes":result["B1"]["total_bytes"],
                "B2_bytes":result["B2"]["total_bytes"],
                "C_proxy_bytes":result["C"]["npz_bytes"],
                "B1_savings_vs_A_pct":round(100*(1-result["B1"]["total_bytes"]/result["A"]["zip_bytes"]),2),
                "B2_savings_vs_A_pct":round(100*(1-result["B2"]["total_bytes"]/result["A"]["zip_bytes"]),2)}
            result["status"]="success"
            result["total_elapsed_seconds"]=stamp()
            result["peak_rss_MB"]=mem_mb()
            emit("final",result["comparison"])
    except Exception as e:
        import traceback
        result["status"]="error"
        result["exception"]=repr(e)
        result["traceback"]=traceback.format_exc()
        print(result["traceback"],flush=True)
        raise
    finally:
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf8")
        emit("status",{"status":result["status"],"elapsed_s":stamp(),"peak_rss_MB":mem_mb()})

if __name__=="__main__":
    main()
