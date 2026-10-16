"""Empirically inspect Tardis raw timestamp resolution for Binance futures.

Stream only the FIRST 30k data rows from each gzipped CSV (no full-day storage).
Never publish raw market rows or trades. This is a precision test, not a
full-day quality audit.
"""
import csv
import gzip
import io
import json
from collections import Counter
from urllib.request import Request, urlopen

TESTS = [
    ("2023/04/01", "BTCUSDT", "incremental_book_L2"),
    ("2023/04/01", "BTCUSDT", "trades"),
    ("2026/09/01", "SOLUSDT", "incremental_book_L2"),
    ("2026/09/01", "SOLUSDT", "trades"),
]
LIMIT = 30000


def inspect(date, symbol, typ):
    url = f"https://datasets.tardis.dev/v1/binance-futures/{typ}/{date}/{symbol}.csv.gz"
    rec = {"date": date, "symbol": symbol, "type": typ, "limit": LIMIT}
    try:
        with urlopen(Request(url, headers={"User-Agent": "quant-test-tardis-resolution/1.0"}), timeout=70) as response:
            with gzip.GzipFile(fileobj=response) as z:
                with io.TextIOWrapper(z, encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rec["columns"] = reader.fieldnames
                    count = 0
                    et = Counter()
                    lt = Counter()
                    exch_prefix = []
                    local_prefix = []
                    unique_exchange = set()
                    unique_local = set()
                    for row in reader:
                        exch = int(row["timestamp"])
                        local = int(row["local_timestamp"])
                        if count < 5:
                            exch_prefix.append(exch)
                            local_prefix.append(local)
                        et[exch % 1000 == 0] += 1
                        lt[local % 1000 == 0] += 1
                        unique_exchange.add(exch)
                        unique_local.add(local)
                        count += 1
                        if count >= LIMIT:
                            break
        rec.update(
            rows_checked=count,
            exchange_first5_us=exch_prefix,
            local_first5_us=local_prefix,
            exchange_timestamp_all_integer_ms=(et[True] == count),
            exchange_timestamp_fraction_millisecond_aligned=round(et[True]/count, 6),
            local_timestamp_fraction_millisecond_aligned=round(lt[True]/count, 6),
            exchange_unique_timestamps=len(unique_exchange),
            local_unique_timestamps=len(unique_local),
            note="Tardis CSV timestamp fields use epoch MICROSECONDS; if timestamp%1000==0 it is only millisecond-granular, despite the microsecond container."
        )
    except Exception as e:
        rec["error"] = repr(e)
    print("TARDIS_TIMESTAMP_PRECISION " + json.dumps(rec), flush=True)
    return rec


for date, symbol, typ in TESTS:
    inspect(date, symbol, typ)
