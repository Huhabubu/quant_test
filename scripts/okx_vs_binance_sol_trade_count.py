"""Compare exact 2026-09-02 UTC raw trade counts, OKX vs Binance.
Data only; no API keys or trading. Maximum Binance ZIP download 1.5 GiB.
"""
import csv
from datetime import datetime, timezone
import io
import os
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile
from okx_sol_day_audit import files

DAY="2026-09-02"
START=int(datetime(2026,9,2,tzinfo=timezone.utc).timestamp()*1000)
END=START+86400000
LIMIT=1536*1024*1024

def okx():
    archives=files(1,DAY,"2026-09-03")
    total=0
    fcount={}
    included={}
    for a in archives:
        url=a["url"]
        with urlopen(Request(url,headers={"User-Agent":"market-data-research-audit/1.0"}),timeout=90) as response:
            b=response.read(30000000)
        if len(b)>29999999:
            raise RuntimeError("Unexpected OKX archive size")
        daily=0; taken=0
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            for n in z.namelist():
                if not n.endswith(".csv"): continue
                with z.open(n) as f:
                    for row in csv.DictReader(io.TextIOWrapper(f,encoding="utf-8")):
                        t=int(row["created_time"]);daily+=1
                        if START<=t<END:taken+=1
        fcount[a["filename"]]=daily
        included[a["filename"]]=taken
        total+=taken
    print("OKX UTC counts from neighboring UTC+8 files:",fcount,flush=True)
    print("OKX rows falling on 2026-09-02 UTC:",included,flush=True)
    print("OKX EXACT UTC DAY TRADES",total,flush=True)
    return total

def binance():
    url=f"https://data.binance.vision/data/futures/um/daily/trades/SOLUSDT/SOLUSDT-trades-{DAY}.zip"
    print("Binance URL:",url,flush=True)
    request=Request(url,headers={"User-Agent":"market-data-research-audit/1.0"})
    with urlopen(request,timeout=120) as resp:
        length=int(resp.headers.get("Content-Length") or 0)
        print("Binance HTTP:",resp.status,"zip-size-MB:",round(length/1e6,2) if length else "unknown",flush=True)
        if length and length>LIMIT:raise RuntimeError("Binance archive exceeds limit")
        fd,tmp=tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            downloaded=0
            with open(tmp,"wb") as dest:
                while True:
                    chunk=resp.read(8*1024*1024)
                    if not chunk:break
                    downloaded+=len(chunk)
                    if downloaded>LIMIT:raise RuntimeError("Binance archive exceeds size cap")
                    dest.write(chunk)
            print("Binance downloaded bytes",downloaded,flush=True)
            with zipfile.ZipFile(tmp) as z:
                n=[n for n in z.namelist() if n.endswith(".csv")]
                if len(n)!=1:raise RuntimeError(f"Expected one CSV: {n}")
                with z.open(n[0]) as f:
                    header=f.readline().decode("utf-8","replace").strip()
                    if not header.lower().startswith("id,") and not header.lower().startswith("tradeid,"):
                        # Binance futures CSV files may omit a header, so count first line too.
                        base=1
                        print("First Binance CSV record, no header detected:",header[:150],flush=True)
                    else:
                        base=0
                        print("Binance CSV header:",header[:150],flush=True)
                    # read chunks; count LF bytes instead of per-row decoding
                    count=base
                    while True:
                        b=f.read(8*1024*1024)
                        if not b:break
                        count+=b.count(b"\n")
                    print("Binance EXACT UTC DAY TRADES",count,flush=True)
                    return count
        finally:
            os.remove(tmp)

if __name__=="__main__":
    o=okx()
    b=binance()
    print("COUNT RATIO Binance/OKX",round(b/o,4))
