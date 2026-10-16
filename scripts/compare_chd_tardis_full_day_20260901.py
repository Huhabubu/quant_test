"""Full Sep 1, 2026 independent Binance SOLUSDT L2 reconstruction.

Two providers, one exchange, one UTC date, 95 fixed exchange-time cutoffs.
CHD: public hourly Parquet, raw Binance pu/u sequence and REST snapshots.
Tardis: free first-day continuous incremental_book_L2 CSV.gz.
No file credentials. No raw market data committed or uploaded.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
import gzip
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import pyarrow.parquet as pq

DAY = "2026-09-01"
SYMBOL = "SOLUSDT"
START = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = START + 86400000
CUTS = [(f"{h:02d}:{m:02d}", START + (h*60 + m)*60000)
        for h in range(24) for m in (0,15,30,45) if h or m]
TOP = (1, 10, 20, 50, 100, 200, 400)
MAX_LEVEL = 500
ROOT = Path("reports")


def format_ts(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat(timespec="milliseconds")


def new_book():
    return {"bid": {}, "ask": {}}


def apply_level(book, side, p, q):
    if side not in book:
        raise ValueError(f"Invalid side: {side}")
    price = float(p)
    amount = float(q)
    if amount <= 0.0:
        book[side].pop(price, None)
    else:
        book[side][price] = amount


def best(book):
    if not book["bid"] or not book["ask"]:
        return None
    return [max(book["bid"]), min(book["ask"])]


def top(book):
    return {side: sorted(book[side].items(), reverse=(side=="bid"))[:MAX_LEVEL]
            for side in ("bid","ask")}


def capture(book, valid, meta, ts):
    return {"valid": bool(valid), "levels": top(book) if valid else None,
            "best": best(book) if valid else None,
            "event_at_or_before": meta.get("last_event_ts"),
            "snapshot_count": meta.get("snapshots", 0),
            "stream_messages": meta.get("groups", 0)}


def compare_levels(lhs, rhs, sizes=TOP):
    if lhs is None or rhs is None:
        return None
    out = {}
    for n in sizes:
        left_both, total_both, contradictions, only_l, only_r = 0,0,0,0,0
        side_report={}
        for side in ("bid","ask"):
            a,b=lhs[side][:n],rhs[side][:n]
            ta,tb=dict(a),dict(b)
            shared=ta.keys()&tb.keys()
            rank_eq=sum(x==y for x,y in zip(a,b))
            bothrank=(len(a)==len(b)==n and a==b)
            wrong_qty=sum(abs(ta[p]-tb[p])>1e-9 for p in shared)
            side_report[side]={
                "rank_equal":rank_eq,"rank_exact":bothrank,"shared":len(shared),
                "shared_wrong_qty":wrong_qty,
                "lhs_only":len(ta.keys()-tb.keys()),"rhs_only":len(tb.keys()-ta.keys()),
                "first_conflicting_prices":[p for p in sorted(shared,reverse=(side=="bid"))
                                            if abs(ta[p]-tb[p])>1e-9][:4]}
            left_both+=rank_eq
            total_both+=n
            contradictions+=wrong_qty
            only_l+=len(ta.keys()-tb.keys())
            only_r+=len(tb.keys()-ta.keys())
        out[str(n)]={
            "rank_match":left_both,"rank_total":total_both,
            "both_sides_exact": all(side_report[s]["rank_exact"] for s in ("bid","ask")),
            "shared_wrong_qty":contradictions,
            "lhs_only_prices":only_l, "rhs_only_prices":only_r,
            "sides":side_report}
    return out


def download_chd_hour(date, hour, target):
    rel=f"binance_futures/{date}/{hour:02d}/{SYMBOL}_orderbook.parquet"
    url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":rel})
    request=Request(url,headers={"User-Agent":"quant-test-full-day-book-comparison/1.0"})
    size=0
    with urlopen(request,timeout=100) as r, open(target, "wb") as dest:
        while chunk:=r.read(1<<20):
            size+=len(chunk)
            if size>160_000_000:
                raise RuntimeError(f"CHD hourly file too large: {rel}")
            dest.write(chunk)
    if pq.ParquetFile(target).metadata.num_rows == 0:
        raise RuntimeError(f"Empty CHD parquet: {rel}")
    return size


def chd_groups(paths):
    fields=("received_time","event_time","event_type","first_update_id",
            "final_update_id","prev_final_update_id","last_update_id",
            "side","price","quantity")
    k=None
    values=[]
    cols=list(fields)
    count=0
    for hour,path in enumerate(paths):
        pf=pq.ParquetFile(path)
        print("CHD_HOUR",hour,"rows",pf.metadata.num_rows,flush=True)
        for batch in pf.iter_batches(batch_size=80000,columns=cols):
            d=batch.to_pydict()
            for row in zip(*(d[c] for c in cols)):
                received,ts,typ,first,final,prev,last,side,p,q=row
                key=(received,typ,first,final,prev,last)
                if key!=k:
                    if k is not None:
                        yield k,values
                        count+=1
                        if count%500000==0: print("CHD_GROUP_PROGRESS",count,flush=True)
                    k=key
                    values=[]
                values.append((int(ts),side,p,q))
    if k is not None:
        yield k,values


def reconstruct_chd(folder):
    # Reuse the independently audited Aug-31 07:44 valid source snapshot
    # to anchor Sep-01 midnight, rather than discarding 00:00-07:34.
    periods=[("2026-08-31",h) for h in range(7,24)]
    periods += [(DAY,h) for h in range(24)]
    paths=[folder/f"{date}_{h:02d}_orderbook.parquet" for date,h in periods]
    with ThreadPoolExecutor(max_workers=3) as ex:
        sizes=list(ex.map(lambda a:download_chd_hour(a[1][0],a[1][1],paths[a[0]]),
                          enumerate(periods)))
    print("CHD_DOWNLOAD_TOTAL",sum(sizes),"hours",len(periods),flush=True)
    book=new_book()
    meta={"snapshots":0,"groups":0,"last_event_ts":None}
    observations={}
    self_comparisons=[]
    gaps=[]
    ignored=0
    duplicates=0
    bridged_without_matching_pu=0
    valid=False
    current_id=None
    latest_snapshot_id=None
    accepted_updates_since_snapshot=0
    last_event_ts=None
    ts_backwards=0
    next_cut=0
    snapshots=0
    accepted_groups=0
    def insert_cuts(ts):
        nonlocal next_cut
        while next_cut<len(CUTS) and ts>=CUTS[next_cut][1]:
            label,cuto=CUTS[next_cut]
            observations[label]=capture(book,valid,meta,cuto)
            next_cut+=1
    for key, rows in chd_groups(paths):
        recv,typ,first,final,prev,last=key
        ts=rows[0][0]
        if last_event_ts is not None and ts<last_event_ts:
            ts_backwards+=1
        last_event_ts=ts
        insert_cuts(ts)
        meta["groups"]+=1
        meta["last_event_ts"]=ts
        if typ=="snapshot":
            snapshots+=1
            if valid and last == latest_snapshot_id and accepted_updates_since_snapshot==0:
                duplicates+=1
                continue
            snap=new_book()
            for _,side,p,q in rows:
                apply_level(snap,side,p,q)
            if valid:
                c=compare_levels(top(book),top(snap),(1,10,20,50,100,200,400))
                self_comparisons.append({
                    "utc":format_ts(ts),"preceding_event_ts":format_ts(last_event_ts),
                    "old_sequence":current_id,"snapshot_id":last,
                    "comparisons":c,
                    "old_best":best(book),"new_best":best(snap),
                    "snapshot_depth":{s:len(snap[s]) for s in ("bid","ask")}
                })
            book=snap
            valid=True
            current_id=last
            latest_snapshot_id=last
            accepted_updates_since_snapshot=0
            meta["snapshots"]+=1
        elif typ=="update":
            if not valid:
                ignored+=1
                continue
            if current_id is None or final is None:
                gaps.append({"time":format_ts(ts),"reason":"missing_id",
                             "expected":current_id,"actual":final})
                valid=False;continue
            if final<=current_id:
                continue
            if prev==current_id:
                pass
            elif first is not None and first<=current_id+1<=final:
                bridged_without_matching_pu+=1
            else:
                if len(gaps)<100:
                    gaps.append({"time":format_ts(ts),"reason":"id_jump",
                                 "expected":current_id,"prev":prev,"first":first,"final":final})
                valid=False;continue
            current_id=final
            for _,side,p,q in rows:
                apply_level(book,side,p,q)
            accepted_groups+=1
            accepted_updates_since_snapshot+=1
        else:
            if len(gaps)<100:
                gaps.append({"time":format_ts(ts),"reason":"unknown_type","typ":typ})
    insert_cuts(END)
    summary={"hours":24,"prewindow_hours":17,"first_bootstrap_search_start":"2026-08-31T07:00:00Z","raw_bytes":sum(sizes),
             "groups":meta["groups"],"snapshots":snapshots,
             "duplicate_snapshots":duplicates,"accepted_update_groups":accepted_groups,
             "ignored_before_bootstrap_or_gap":ignored,
             "sequence_gap_examples":gaps[:30],
             "sequence_gap_count_capped":len(gaps),
             "bridging_without_prev_match":bridged_without_matching_pu,
             "event_time_reversals":ts_backwards,
             "snapshot_self_checks":len(self_comparisons),
             "snapshot_self_top_exact":{str(n):sum(item["comparisons"][str(n)]["both_sides_exact"] for item in self_comparisons) for n in TOP},
             "valid_15min_checkpoints":sum(o["valid"] for o in observations.values()),
             "total_15min_checkpoints":len(observations)}
    print("CHD_SUMMARY",json.dumps(summary),flush=True)
    return observations,summary,self_comparisons


def reconstruct_tardis():
    url=f"https://datasets.tardis.dev/v1/binance-futures/incremental_book_L2/2026/09/01/{SYMBOL}.csv.gz"
    request=Request(url,headers={"User-Agent":"quant-test-full-day-book-comparison/1.0"})
    book=new_book()
    valid=False
    obs={}
    snapshot_checks=[]
    meta={"snapshots":0,"groups":0,"last_event_ts":None}
    next_cut=0
    current_snapshot_key=None
    row_count=0
    reversals=0
    prev_ts=None
    early_rows=0
    snapshot_rows=0
    snapshot_staging=None

    def finish_snapshot():
        nonlocal book, valid, snapshot_staging
        if snapshot_staging is None:
            return
        if valid:
            # A subsequent independent Tardis snapshot is a quality check.
            snap_book=snapshot_staging["book"]
            res=compare_levels(top(book),top(snap_book))
            snapshot_checks.append({"utc":format_ts(snapshot_staging["ts"]),
                                    "old_best":best(book),"snapshot_best":best(snap_book),
                                    "comparisons":res})
        book=snapshot_staging["book"]
        valid=True
        meta["snapshots"]+=1
        snapshot_staging=None

    def insert_cuts(ts):
        nonlocal next_cut
        while next_cut<len(CUTS) and ts>=CUTS[next_cut][1]:
            # Note that a snapshot group is staged before application; cutoffs
            # should not fall inside one; this preserves group atomicity.
            label,cuto=CUTS[next_cut]
            obs[label]=capture(book,valid,meta,cuto)
            next_cut+=1

    with urlopen(request,timeout=180) as response:
        print("TARDIS_STATUS",response.status,"content-length",response.headers.get("Content-Length"),flush=True)
        with gzip.GzipFile(fileobj=response) as decoded:
            with io.TextIOWrapper(decoded,encoding="utf-8",newline="") as text:
                reader=csv.DictReader(text)
                print("TARDIS_COLUMNS",json.dumps(reader.fieldnames),flush=True)
                for r in reader:
                    ts=int(r["timestamp"])//1000
                    local_ts=int(r["local_timestamp"])//1000
                    is_snap=r["is_snapshot"].lower()=="true"
                    snapkey=(r["timestamp"],r["local_timestamp"]) if is_snap else None
                    if snapshot_staging is not None and (not is_snap or snapkey!=current_snapshot_key):
                        finish_snapshot()
                    if prev_ts is not None and ts<prev_ts:
                        reversals+=1
                    prev_ts=ts
                    insert_cuts(ts)
                    if ts>=END:
                        break
                    meta["groups"]+=1
                    meta["last_event_ts"]=ts
                    row_count+=1
                    if is_snap:
                        if snapshot_staging is None:
                            current_snapshot_key=snapkey
                            snapshot_staging={"book":new_book(),"ts":ts}
                        snapshot_rows+=1
                        apply_level(snapshot_staging["book"],r["side"].lower(),r["price"],r["amount"])
                    else:
                        if not valid:
                            early_rows+=1
                            continue
                        apply_level(book,r["side"].lower(),r["price"],r["amount"])
                    if row_count%5000000==0:
                        print("TARDIS_PROGRESS",row_count,"snapshots",meta["snapshots"],flush=True)
    finish_snapshot()
    insert_cuts(END)
    summary={"rows":row_count,"snapshot_level_rows":snapshot_rows,
             "snapshot_groups":meta["snapshots"],
             "snapshot_self_checks":len(snapshot_checks),
             "event_time_reversals":reversals,
             "early_unanchored_rows":early_rows,
             "valid_15min_checkpoints":sum(o["valid"] for o in obs.values()),
             "total_15min_checkpoints":len(obs)}
    print("TARDIS_SUMMARY",json.dumps(summary),flush=True)
    return obs,summary,snapshot_checks


def main():
    t0=perf_counter()
    with TemporaryDirectory(prefix="solusdt-sep01-books-") as folder:
        chd, chds, chd_self=reconstruct_chd(Path(folder))
        tardis, tstats, tardis_self=reconstruct_tardis()
    rows=[]
    for label,cut in CUTS:
        c=chd.get(label);t=tardis.get(label)
        if c is None or t is None:
            rows.append({"time":label,"status":"missing_checkpoint"});continue
        if not c["valid"] or not t["valid"]:
            rows.append({"time":label,"status":"unanchored_or_gap",
                         "chd_valid":c["valid"],"tardis_valid":t["valid"]})
            continue
        cmp=compare_levels(t["levels"],c["levels"])
        rows.append({"time":label,"status":"compared",
                     "tardis_best":t["best"],"chd_best":c["best"],
                     "both_best_price_equal":t["best"]==c["best"],
                     "tardis_last_event":t["event_at_or_before"],
                     "chd_last_event":c["event_at_or_before"],
                     "comparison":cmp,
                     "chd_snapshot_groups":c["snapshot_count"],
                     "tardis_snapshot_groups":t["snapshot_count"]})
        if len(rows)%12==0:
            print("CROSS_PROGRESS",label,"top20_exact",cmp["20"]["both_sides_exact"],
                  "top200_exact",cmp["200"]["both_sides_exact"],flush=True)
    compared=[r for r in rows if r["status"]=="compared"]
    aggregate={"symbol":SYMBOL,"date_utc":DAY,"market":"Binance USD-M Futures",
               "expected_cutoffs":len(CUTS),"compared_cutoffs":len(compared),
               "chd":chds,"tardis":tstats,"book_best_price_exact":sum(r["both_best_price_equal"] for r in compared),
               "top_n":{str(n):{
                   "both_sides_entire_top_n_exact":sum(r["comparison"][str(n)]["both_sides_exact"] for r in compared),
                   "matched_price_and_qty_rank_count":sum(r["comparison"][str(n)]["rank_match"] for r in compared),
                   "expected_price_rank_count":len(compared)*2*n,
                   "shared_price_qty_disagreements":sum(r["comparison"][str(n)]["shared_wrong_qty"] for r in compared),
                   "tardis_only_prices_across_cutoffs":sum(r["comparison"][str(n)]["lhs_only_prices"] for r in compared),
                   "chd_only_prices_across_cutoffs":sum(r["comparison"][str(n)]["rhs_only_prices"] for r in compared),
               } for n in TOP},
               "chd_self_snapshots_checked":len(chd_self),
               "chd_self_top_exact":{str(n):sum(r["comparisons"][str(n)]["both_sides_exact"] for r in chd_self) for n in TOP},
               "tardis_self_snapshots_checked":len(tardis_self),
               "tardis_self_top_exact":{str(n):sum(r["comparisons"][str(n)]["both_sides_exact"] for r in tardis_self) for n in TOP},
               "elapsed_seconds":round(perf_counter()-t0,1),
               "limitations":["Snapshots between collectors have different startup depth and received-time ordering",
                              "Tardis is free 2026-09-01 only; not a substitute for missing 2026-09-02 L2",
                              "Exchange timestamps may have identical values for distinct message groups",
                              "No L3 individual order position is observable"]}
    ROOT.mkdir(parents=True,exist_ok=True)
    report=ROOT/"solusdt_chd_tardis_full_day_2026-09-01.json"
    report.write_text(json.dumps({"summary":aggregate,"cutoffs":rows,
                                  "chd_snapshot_checks":chd_self,"tardis_snapshot_checks":tardis_self},
                                 indent=2,ensure_ascii=False),encoding="utf-8")
    csv_path=ROOT/"solusdt_chd_tardis_full_day_2026-09-01.csv"
    with csv_path.open("w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["utc","status","bbo_equal","top1_exact","top10_exact","top20_exact",
                    "top50_exact","top100_exact","top200_exact","top400_exact",
                    "shared_wrong_qty_top200","shared_wrong_qty_top400"])
        for r in rows:
            cmp=r.get("comparison",{})
            w.writerow([r["time"],r["status"],r.get("both_best_price_equal"),
                        *(cmp.get(str(n),{}).get("both_sides_exact") for n in TOP if n!= 1 or True),
                        cmp.get("200",{}).get("shared_wrong_qty"),cmp.get("400",{}).get("shared_wrong_qty")])
    print("FULLDAY_RESULT",json.dumps(aggregate,ensure_ascii=False),flush=True)
    print("DIFF_EXAMPLES",json.dumps([{"time":r["time"],"best":r.get("both_best_price_equal"),
         "top10":r.get("comparison",{}).get("10"),"top200":r.get("comparison",{}).get("200")}
         for r in rows if r["status"]=="compared" and
         not r["comparison"]["10"]["both_sides_exact"]][:6],ensure_ascii=False),flush=True)
    print("REPORTS",report,csv_path,flush=True)
    if len(rows)!=len(CUTS) or not compared:
        raise SystemExit("No comparable cutoffs")


if __name__=="__main__":
    main()
