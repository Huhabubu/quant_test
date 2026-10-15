"""Compare public Binance Futures SOLUSDT tick files for one free Tardis day.

Only aggregate statistics are printed. Raw market data is not committed or uploaded.
"""
import csv
import gzip
import io
import json
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pyarrow.parquet as pq

DATE = "2026-09-01"
HOUR = "00"
SYMBOL = "SOLUSDT"
START_MS = int(datetime.fromisoformat(DATE).replace(tzinfo=timezone.utc).timestamp() * 1000)
END_MS = START_MS + 3600_000


def numeric(value):
    s = str(value).strip()
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def download_chd(kind, directory):
    remote = f"binance_futures/{DATE}/{HOUR}/{SYMBOL}_{kind}.parquet"
    url = "https://api.cryptohftdata.com/v1/download?" + urlencode({"file": remote})
    dest = Path(directory) / (kind + ".parquet")
    request = Request(url, headers={"User-Agent": "quant-test-crosscheck/1.0"})
    with urlopen(request, timeout=90) as response, dest.open("wb") as output:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 750 * 1024 * 1024:
                raise RuntimeError(f"{kind} exceeds safe file size limit")
            output.write(chunk)
    return dest


def tardis_rows(kind):
    url = f"https://datasets.tardis.dev/v1/binance-futures/{kind}/2026/09/01/{SYMBOL}.csv.gz"
    request = Request(url, headers={"User-Agent": "quant-test-crosscheck/1.0"})
    with urlopen(request, timeout=90) as response:
        # Tardis stores a *whole day*; stop reading as soon as hour 01 begins.
        with gzip.GzipFile(fileobj=response) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                reader = csv.DictReader(text)
                print(f"TARDIS_{kind}_COLUMNS " + json.dumps(reader.fieldnames), flush=True)
                for row in reader:
                    event_ms = int(row["timestamp"]) // 1000
                    if event_ms >= END_MS:
                        return
                    if event_ms >= START_MS:
                        yield row


def check_trades(directory):
    trades = {}
    chd_count = 0
    chd_dupes = 0
    f = pq.ParquetFile(download_chd("trades", directory))
    for batch in f.iter_batches(batch_size=100_000):
        data = batch.to_pydict()
        for i, trade_id in enumerate(data["trade_id"]):
            chd_count += 1
            key = str(trade_id)
            value = (int(data["trade_time"][i]), numeric(data["price"][i]),
                     numeric(data["quantity"][i]),
                     "sell" if data["is_buyer_maker"][i] else "buy")
            if key in trades:
                chd_dupes += 1
            trades[key] = value

    tardis_count = 0
    unique = set()
    matches = 0
    id_only = 0
    mismatch_counts = Counter()
    tardis_duplicates = 0
    for row in tardis_rows("trades"):
        tardis_count += 1
        trade_id = row["id"]
        if trade_id in unique:
            tardis_duplicates += 1
        unique.add(trade_id)
        counterpart = trades.get(trade_id)
        if counterpart is None:
            continue
        id_only += 1
        sample = (int(row["timestamp"]) // 1000, numeric(row["price"]),
                  numeric(row["amount"]), row["side"].lower())
        for index, field in enumerate(("time", "price", "quantity", "aggressor_side")):
            if counterpart[index] != sample[index]:
                mismatch_counts[field] += 1
        if counterpart == sample:
            matches += 1

    return {
        "cryptohftdata_rows": chd_count,
        "tardis_rows": tardis_count,
        "common_trade_ids": id_only,
        "exact_record_matches": matches,
        "id_overlap_pct_vs_tardis": round(100 * id_only / tardis_count, 4) if tardis_count else None,
        "exact_match_pct_vs_tardis": round(100 * matches / tardis_count, 4) if tardis_count else None,
        "field_mismatches_on_shared_id": dict(mismatch_counts),
        "cryptohftdata_duplicate_ids": chd_dupes,
        "tardis_duplicate_ids": tardis_duplicates,
        "unique_chd_not_in_tardis": len(set(trades) - unique),
        "unique_tardis_not_in_chd": len(unique - set(trades)),
    }


def check_orderbook(directory):
    book = Counter()
    book_tx = Counter()
    rows = 0
    snapshots = 0
    chd = pq.ParquetFile(download_chd("orderbook", directory))
    for batch in chd.iter_batches(batch_size=100_000):
        data = batch.to_pydict()
        for i, typ in enumerate(data["event_type"]):
            if typ == "snapshot":
                snapshots += 1
                continue
            if typ != "update":
                continue
            rows += 1
            base = (data["side"][i], numeric(data["price"][i]), numeric(data["quantity"][i]))
            book[(data["event_time"][i], *base)] += 1
            if data["transaction_time"][i] is not None:
                book_tx[(data["transaction_time"][i], *base)] += 1
    tardis_counts = Counter()
    tardis_rows_count = 0
    tardis_snapshots = 0
    for row in tardis_rows("incremental_book_L2"):
        if row["is_snapshot"].lower() == "true":
            tardis_snapshots += 1
            continue
        tardis_rows_count += 1
        key = (int(row["timestamp"]) // 1000, row["side"].lower(),
               numeric(row["price"]), numeric(row["amount"]))
        tardis_counts[key] += 1
    common_event_time = sum((book & tardis_counts).values())
    common_tx_time = sum((book_tx & tardis_counts).values())
    return {
        "cryptohftdata_update_rows": rows,
        "tardis_update_rows": tardis_rows_count,
        "cryptohftdata_snapshot_rows": snapshots,
        "tardis_snapshot_rows": tardis_snapshots,
        "exact_update_level_matches_by_event_time": common_event_time,
        "exact_update_level_matches_by_transaction_time": common_tx_time,
        "event_time_match_pct_vs_tardis": round(100 * common_event_time / tardis_rows_count, 4) if tardis_rows_count else None,
        "transaction_time_match_pct_vs_tardis": round(100 * common_tx_time / tardis_rows_count, 4) if tardis_rows_count else None,
        "event_time_unique_chd": len(book),
        "event_time_unique_tardis": len(tardis_counts),
        "note": "L2 signatures use (exchange timestamp ms, side, price, absolute level quantity). Different capture feeds may have differing event aggregation.",
    }


def main():
    results = {"date": DATE, "hour_utc": HOUR, "symbol": SYMBOL,
               "exchange": "binance-futures", "data_types": ["trades", "incremental_book_L2"]}
    with tempfile.TemporaryDirectory() as temp:
        for name, fn in (("trades", check_trades), ("orderbook", check_orderbook)):
            try:
                results[name] = fn(temp)
            except Exception as error:
                results[name] = {"error": f"{type(error).__name__}: {error}"}
    print("CROSSCHECK_RESULT " + json.dumps(results, ensure_ascii=False, sort_keys=True), flush=True)
    if any("error" in results[n] for n in ("trades", "orderbook")):
        raise SystemExit("Data comparison incomplete")


if __name__ == "__main__":
    main()
