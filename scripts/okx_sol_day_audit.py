"""Stream one UTC day of OKX SOL-USDT-SWAP L2; scan adjacent trades.
No credentials, exchange orders, persistent raw data, or third-party libraries.
"""
import csv
from datetime import datetime, timezone
import io
import json
import tarfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile

from okx_sol_archive_probe import API, ms

SYMBOL = "SOL-USDT-SWAP"
DAY = "2026-09-02"

def files(module, start, end):
    args = dict(module=str(module), instType="SWAP", dateAggrType="daily",
                begin=ms(start), end=ms(end), instFamilyList="SOL-USDT")
    with urlopen(Request(API + "?" + urlencode(args), headers={"User-Agent": "okx-archive-audit/1.0"}), timeout=40) as resp:
        result = json.load(resp)
    if result.get("code") != "0":
        raise RuntimeError("OKX API error: " + str(result.get("code")) + ": " + str(result.get("msg")))
    return [f for batch in result.get("data", [])
            for group in batch.get("details", []) if group.get("instFamily") == "SOL-USDT"
            for f in group.get("groupDetails", []) if SYMBOL in f.get("filename", "")]

def new_stats():
    return dict(n=0, first=None, last=None, biggest_gap=0, gap_at=None, over_1s=0,
                over_10s=0, backwards=0, same_time=0)

def record(stats, ts):
    if stats["first"] is None:
        stats["first"] = ts
    if stats["last"] is not None:
        diff = ts - stats["last"]
        if diff < 0: stats["backwards"] += 1
        if diff == 0: stats["same_time"] += 1
        if diff > 1000: stats["over_1s"] += 1
        if diff > 10000: stats["over_10s"] += 1
        if diff > stats["biggest_gap"]:
            stats["biggest_gap"], stats["gap_at"] = diff, (stats["last"], ts)
    stats["last"] = ts
    stats["n"] += 1

def audit_book(file):
    print("\n=== FULL L2 SCAN ===", file["filename"], flush=True)
    stats = new_stats()
    actions = {}
    keys = set()
    req = Request(file["url"], headers={"User-Agent": "okx-archive-audit/1.0"})
    with urlopen(req, timeout=90) as response:
        with tarfile.open(fileobj=response, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile(): continue
                print("TAR member:", member.name, "uncompressed_bytes:", member.size, flush=True)
                stream = tar.extractfile(member)
                if stream is None: continue
                for line in stream:
                    event = json.loads(line)
                    if stats["n"] == 0:
                        keys = set(event)
                    record(stats, int(event["ts"]))
                    action = event.get("action", "?")
                    actions[action] = actions.get(action, 0) + 1
                    if stats["n"] % 1000000 == 0:
                        print("processed:", stats["n"], flush=True)
    print("L2 messages:", stats["n"], "actions:", actions, "fields(first):", sorted(keys))
    print("L2 timing:", stats, flush=True)
    return stats

def audit_trades(file):
    print("\n=== FULL TRADES SCAN ===", file["filename"], flush=True)
    stats = new_stats()
    gaps = 0
    skipped = 0
    negative_ids = 0
    previous_id = None
    url = file["url"]
    with urlopen(Request(url, headers={"User-Agent": "okx-archive-audit/1.0"}), timeout=90) as resp:
        # The two trade ZIPs are about 7 MB each; enforce a bounded download.
        data = resp.read(30000001)
    if len(data) > 30000000:
        raise RuntimeError("Unexpected large trade ZIP")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.namelist():
            if not member.endswith(".csv"): continue
            with archive.open(member) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                print("Trade columns:", reader.fieldnames)
                for row in reader:
                    tid = int(row["trade_id"])
                    record(stats, int(row["created_time"]))
                    if previous_id is not None:
                        if tid < previous_id: negative_ids += 1
                        elif tid > previous_id + 1:
                            gaps += 1
                            skipped += tid - previous_id - 1
                    previous_id = tid
    print("Trades:", stats["n"], "id_gaps:", gaps, "skipped_ids:", skipped, "reversed_ids:", negative_ids)
    print("Trade timing:", stats, flush=True)
    return stats

if __name__ == "__main__":
    book_files = files(4, DAY, DAY)
    trade_files = files(1, DAY, "2026-09-03")
    print("L2 archive files:", [f["filename"] for f in book_files])
    print("Trade archive files:", [f["filename"] for f in trade_files])
    if len(book_files) != 1 or len(trade_files) < 2:
        raise RuntimeError("Requested archive files missing")
    audit_book(book_files[0])
    for f in sorted(trade_files, key=lambda x: x["filename"]):
        audit_trades(f)
    print("\nFULL ARCHIVE SCAN COMPLETED. No seqId/prevSeqId in observed L2 event fields; time gaps do not prove packet continuity.")
