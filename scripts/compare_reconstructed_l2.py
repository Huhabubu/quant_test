"""Independently reconstruct Tardis and CryptoHFTData Binance Futures L2 books.

Free Tardis day: 2026-09-01. All data remains temporary on a GitHub runner.
Only aggregated statistics are written to stdout.
"""
import csv
import gzip
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import pyarrow.parquet as pq

DATE = "2026-09-01"
SYMBOL = "SOLUSDT"
HOUR = "07"
CHECKPOINT_HM = ("07:40", "07:50", "07:59")
DAY_START_MS = int(datetime.fromisoformat(DATE).replace(tzinfo=timezone.utc).timestamp() * 1000)
CUTOFFS = [(hm, DAY_START_MS + (int(hm[:2]) * 60 + int(hm[3:])) * 60_000) for hm in CHECKPOINT_HM]


def book_new():
    return {"bid": {}, "ask": {}}


def apply_level(book, side, price, amount):
    if side not in ("bid", "ask"):
        return
    p = float(price)
    q = float(amount)
    if q <= 0:
        book[side].pop(p, None)
    else:
        book[side][p] = q


def snapshot_book(book):
    return {side: dict(values) for side, values in book.items()}


def book_best(book):
    bids, asks = book["bid"], book["ask"]
    if not bids or not asks:
        return None
    return {"bid": max(bids), "ask": min(asks), "spread": round(min(asks)-max(bids), 8)}


def levels(book, side, n):
    return sorted(book[side].items(), reverse=(side == "bid"))[:n]


def compare_book(tardis, crypto):
    out = {"best_tardis": book_best(tardis), "best_crypto": book_best(crypto),
           "levels_tardis": {s: len(tardis[s]) for s in ("bid", "ask")},
           "levels_crypto": {s: len(crypto[s]) for s in ("bid", "ask")}}
    for n in (20, 100, 200, 500):
        for side in ("bid", "ask"):
            t, c = levels(tardis, side, n), levels(crypto, side, n)
            # Both sequences compare side-by-side by rank, and also by the
            # union of quoted price keys. Float quantities are exact here
            # because both datasets carry identical decimal strings.
            match_at_rank = sum(i < len(t) and i < len(c) and
                                t[i][0] == c[i][0] and abs(t[i][1] - c[i][1]) < 1e-9
                                for i in range(n))
            mismatches = [{"rank": i+1,
                           "tardis": t[i] if i<len(t) else None,
                           "cryptohftdata": c[i] if i<len(c) else None}
                          for i in range(n)
                          if not (i<len(t) and i<len(c) and t[i][0] == c[i][0] and
                                  abs(t[i][1]-c[i][1]) < 1e-9)]
            out[f"{side}_top{n}"] = {
                "exact_rank_matches": match_at_rank, "compared_ranks": n,
                "all_equal": not mismatches,
                "mismatch_count": len(mismatches), "first_mismatches": mismatches[:4]}
    return out


def get_chd(temp):
    remote=f"binance_futures/{DATE}/{HOUR}/{SYMBOL}_orderbook.parquet"
    url="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":remote})
    dest=Path(temp)/"chd_book.parquet"
    request=Request(url, headers={"User-Agent":"quant-test-independent-book-state/1.0"})
    with urlopen(request,timeout=90) as src,dest.open("wb") as out:
        while chunk:=src.read(1024*1024):
            out.write(chunk)
    return dest


def replay_chd(temp):
    path = get_chd(temp)
    cols = ("received_time", "event_time", "event_type", "first_update_id",
            "final_update_id", "prev_final_update_id", "last_update_id",
            "side", "price", "quantity")
    book=book_new()
    valid=False
    current_seq=None
    last_group=None
    observed={}
    snapshots=[]
    events=0
    gaps=[]
    ignored_updates=0
    reset_reason=None
    next_cutoff = 0
    apply_this_group = False
    meta = pq.ParquetFile(path)
    print("CHD_PARQUET",json.dumps({"rows":meta.metadata.num_rows, "row_groups":meta.metadata.num_row_groups}),flush=True)
    for batch in meta.iter_batches(batch_size=50_000, columns=list(cols)):
        data=batch.to_pydict()
        for row in zip(*(data[c] for c in cols)):
            received,ts,typ,first_id,final_id,prev_id,last_id,side,price,quantity = row
            key=(received,typ,first_id,final_id,prev_id,last_id)
            if key != last_group:
                apply_this_group = False
                # Snapshot times are minute boundaries, so record the
                # state only after all earlier event groups are applied.
                while next_cutoff < len(CUTOFFS) and ts >= CUTOFFS[next_cutoff][1]:
                    label,_ = CUTOFFS[next_cutoff]
                    observed[label]={"book": snapshot_book(book), "valid": valid,
                                     "last_seq":current_seq,"events":events}
                    next_cutoff += 1
                events+=1
                if typ=="snapshot":
                    book=book_new()
                    valid=True
                    current_seq=last_id
                    snapshots.append({"received_time":received,"event_time":ts,
                                      "update_id":last_id})
                    reset_reason="snapshot"
                    apply_this_group=True
                elif typ=="update":
                    if not valid:
                        ignored_updates+=1
                    elif current_seq is None:
                        gaps.append({"reason":"missing_sequence", "time":ts})
                        valid=False
                    elif final_id is not None and final_id <= current_seq:
                        pass
                    elif prev_id == current_seq or (
                        first_id is not None and final_id is not None and
                        first_id <= current_seq+1 <= final_id):
                        current_seq = final_id
                        apply_this_group = True
                    else:
                        if len(gaps)<10:
                            gaps.append({"received_time":received,
                                         "event_time":ts,
                                         "expected_prev":current_seq,"actual_prev":prev_id,
                                         "first":first_id,"final":final_id})
                        valid=False
                last_group=key
            if not valid or not apply_this_group or side not in ("bid","ask"):
                continue
            apply_level(book, side, price, quantity)
        if len(observed)==len(CUTOFFS):
            break
    return observed,{"snapshots_seen":snapshots[:12],"snapshot_count":len(snapshots),
                     "events_seen":events,"gaps":gaps,"ignored_updates":ignored_updates,
                     "observed_labels":list(observed)}


def replay_tardis():
    url=f"https://datasets.tardis.dev/v1/binance-futures/incremental_book_L2/2026/09/01/{SYMBOL}.csv.gz"
    request=Request(url,headers={"User-Agent":"quant-test-independent-book-state/1.0"})
    book=book_new()
    observed={}
    next_cutoff=0
    current_snapshot_key=None
    snapshots=0
    rows=0
    time_reversals=0
    last_ts=None
    early_updates=0
    with urlopen(request, timeout=180) as response:
        with gzip.GzipFile(fileobj=response) as zipped:
            with io.TextIOWrapper(zipped,encoding="utf-8",newline="") as reader_text:
                reader=csv.DictReader(reader_text)
                print("TARDIS_CSV_COLUMNS "+json.dumps(reader.fieldnames),flush=True)
                for row in reader:
                    ts=int(row["timestamp"])//1000
                    while next_cutoff < len(CUTOFFS) and ts >= CUTOFFS[next_cutoff][1]:
                        label,_=CUTOFFS[next_cutoff]
                        observed[label]={"book":snapshot_book(book),"events":rows}
                        print("TARDIS_CHECKPOINT "+json.dumps({"label":label,"rows":rows,
                            "bids":len(book["bid"]),"asks":len(book["ask"])}),flush=True)
                        next_cutoff+=1
                    if len(observed)==len(CUTOFFS):
                        break
                    if last_ts is not None and ts<last_ts:
                        time_reversals+=1
                    last_ts=ts
                    rows+=1
                    if row["is_snapshot"].lower()=="true":
                        key=(row["timestamp"],row["local_timestamp"])
                        if key!=current_snapshot_key:
                            book=book_new()
                            snapshots+=1
                            current_snapshot_key=key
                    elif snapshots==0:
                        early_updates+=1
                        continue
                    apply_level(book,row["side"].lower(),row["price"],row["amount"])
                    if rows%2_500_000==0:
                        print("TARDIS_PROGRESS "+json.dumps({"rows":rows,"exchange_time_ms":ts}),flush=True)
    return observed,{"snapshot_groups":snapshots,"processed_rows":rows,
                     "event_time_reversals":time_reversals,
                     "early_unanchored_updates":early_updates,
                     "observed_labels":list(observed)}


def main():
    with TemporaryDirectory() as td:
        chd,chd_stats=replay_chd(td)
        print("CHD_REPORT "+json.dumps(chd_stats,default=str),flush=True)
        tardis,tardis_stats=replay_tardis()
    results={"date":DATE, "venue":"binance-futures", "symbol":SYMBOL,
             "crypto":chd_stats, "tardis":tardis_stats, "observations":{}}
    for label,_ in CUTOFFS:
        if label not in chd or label not in tardis:
            results["observations"][label]={"error":"missing_source_checkpoint"}
            continue
        result=compare_book(tardis[label]["book"],chd[label]["book"])
        result["chd_sequence_valid"]=chd[label]["valid"]
        result["chd_last_sequence_id"]=chd[label]["last_seq"]
        results["observations"][label]=result
        print("BOOK_STATE_COMPARISON "+json.dumps({"time":label, **result},
             ensure_ascii=False),flush=True)
    print("BOOK_STATE_REPORT "+json.dumps(results,ensure_ascii=False),flush=True)
    if len(results["observations"]) != len(CUTOFFS) or any(
        not item.get("chd_sequence_valid") for item in results["observations"].values()):
        raise SystemExit("Incomplete or invalid CHD reconstruction")
    if tardis_stats["event_time_reversals"]:
        print("CAUTION: Tardis exchange timestamps were not monotonically ordered",flush=True)


if __name__ == "__main__":
    main()
