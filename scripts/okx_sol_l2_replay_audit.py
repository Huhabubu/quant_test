"""Full-day OKX L2 replay vs 96 periodic snapshots.

Reconstruct by SETTING each price level to the new absolute size (zero removes),
compare BEFORE resetting the book to each subsequent snapshot.
- No exchange credentials, no orders, no raw market data committed.
- Checks top-N price+quantity and order-count, plus overlap within snapshot range.
- The first snapshot initializes the state; 95 subsequent snapshots are checkpoints.
- Historical archives do not include seqId/prevSeqId or collector receive times.
"""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tarfile
from urllib.request import Request, urlopen
from okx_sol_day_audit import files, DAY

LEVELS = (1, 10, 50, 100, 400)
REPORT_DIR = Path("reports")
SYMBOL = "SOL-USDT-SWAP"


def parsed_levels(levels):
    result = {}
    for row in levels:
        price = float(row[0])
        qty = float(row[1])
        orders = int(row[2]) if len(row) > 2 and row[2] != "" else None
        if qty > 0:
            result[price] = (qty, orders)
    return result


def sorted_side(side, is_bid):
    return sorted(side.items(), key=lambda p: p[0], reverse=is_bid)


def side_check(pred, obs, is_bid):
    """Compare only visible snapshot price range, plus ranked top levels."""
    pred_sorted = sorted_side(pred, is_bid)
    obs_sorted = sorted_side(obs, is_bid)
    obs_prices = set(obs)
    if not obs_prices:
        raise ValueError("snapshot has empty side")
    lo, hi = min(obs_prices), max(obs_prices)
    visible_pred = {p: v for p, v in pred.items() if lo <= p <= hi}
    common = obs_prices.intersection(visible_pred)
    missing = obs_prices - set(visible_pred)
    extra = set(visible_pred) - obs_prices
    wrong_qty = sum(pred[p][0] != obs[p][0] for p in common)
    wrong_orders = sum(pred[p][1] != obs[p][1] for p in common)
    per_depth = {}
    for n in LEVELS:
        r, s = pred_sorted[:n], obs_sorted[:n]
        both = min(len(r), len(s))
        price_diff = sum(r[i][0] != s[i][0] for i in range(both)) + abs(len(r) - len(s))
        qty_diff = sum(r[i] != s[i] for i in range(both) if r[i][0] == s[i][0])
        full_match = len(r) == len(s) and all(r[i] == s[i] for i in range(len(r)))
        # In particular top 1 must have one level on each side.
        per_depth[str(n)] = {
            "price_rank_differences": price_diff,
            "quantity_or_order_differences": qty_diff,
            "exact": full_match,
            "pred_count": len(r),
            "snapshot_count": len(s),
        }
    return {
        "snapshot_levels": len(obs),
        "reconstructed_levels": len(pred),
        "within_visible_depth_missing": len(missing),
        "within_visible_depth_extra": len(extra),
        "within_visible_depth_wrong_qty": wrong_qty,
        "within_visible_depth_wrong_order_count": wrong_orders,
        "first_missing": sorted(missing, reverse=is_bid)[:3],
        "first_extra": sorted(extra, reverse=is_bid)[:3],
        "top": per_depth,
        "snapshot_best": obs_sorted[0],
        "reconstructed_best": pred_sorted[0] if pred_sorted else None,
    }


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def audit():
    candidates = files(4, DAY, DAY)
    selected = [x for x in candidates if x["filename"].endswith(f"{DAY}.tar.gz") and "400lv" in x["filename"]]
    if len(selected) != 1:
        raise RuntimeError(f"Expected one SOL L2 file; got: {[x['filename'] for x in candidates]}")
    archive = selected[0]
    print("Reading", archive["filename"], "sizeMB", archive.get("sizeMB"), flush=True)
    books = {"bids": {}, "asks": {}}
    checks = []
    snapshots = 0
    updates = 0
    update_levels = 0
    unknown_actions = 0
    prior_ts = None
    violations = 0
    req = Request(archive["url"], headers={"User-Agent": "OKX-SOL-L2-replay-audit/1.0"})
    with urlopen(req, timeout=90) as stream:
        with tarfile.open(fileobj=stream, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if not member.name.endswith(".data"):
                    raise RuntimeError(f"Unexpected file in archive: {member.name}")
                content = tar.extractfile(member)
                if content is None:
                    raise RuntimeError("Unable to read tar member")
                print("Uncompressed bytes:", member.size, flush=True)
                for line in content:
                    event = json.loads(line)
                    if event.get("instId") != SYMBOL:
                        raise RuntimeError(f"Unexpected symbol {event.get('instId')}")
                    ts = int(event["ts"])
                    if prior_ts is not None and ts < prior_ts:
                        violations += 1
                    previous_message_ts = prior_ts
                    prior_ts = ts
                    action = event["action"]
                    if action == "snapshot":
                        if snapshots:
                            result = {
                                "checkpoint": snapshots,
                                "timestamp": ts,
                                "utc": iso(ts),
                                "updates_since_previous": updates - last_updates,
                                "ms_since_previous_message": ts - previous_message_ts if previous_message_ts is not None else None,
                                "bids": side_check(books["bids"], parsed_levels(event["bids"]), True),
                                "asks": side_check(books["asks"], parsed_levels(event["asks"]), False),
                            }
                            result["top"] = {
                                str(n): result["bids"]["top"][str(n)]["exact"] and result["asks"]["top"][str(n)]["exact"]
                                for n in LEVELS
                            }
                            result["visible_exact"] = all(
                                all(result[side][key] == 0 for key in
                                    ("within_visible_depth_missing", "within_visible_depth_extra",
                                     "within_visible_depth_wrong_qty", "within_visible_depth_wrong_order_count"))
                                for side in ("bids", "asks")
                            )
                            checks.append(result)
                            if snapshots % 10 == 0:
                                print("checkpoint", snapshots, result["utc"],
                                      "top1", result["top"]["1"],
                                      "top10", result["top"]["10"],
                                      "top400", result["top"]["400"],
                                      "visible_exact", result["visible_exact"], flush=True)
                        # Reset to authoritative snapshot AFTER comparison.
                        books["bids"] = parsed_levels(event["bids"])
                        books["asks"] = parsed_levels(event["asks"])
                        snapshots += 1
                        last_updates = updates
                    elif action == "update":
                        updates += 1
                        for side in ("bids", "asks"):
                            book = books[side]
                            for row in event[side]:
                                price = float(row[0])
                                qty = float(row[1])
                                orders = int(row[2]) if len(row) > 2 and row[2] != "" else None
                                if qty <= 0:
                                    book.pop(price, None)
                                else:
                                    book[price] = (qty, orders)
                                update_levels += 1
                        if updates % 1000000 == 0:
                            print("updates processed:", updates, flush=True)
                    else:
                        unknown_actions += 1
                        if unknown_actions <= 3:
                            print("UNKNOWN action:", action)
    if snapshots != 96:
        print("WARNING: expected 96 snapshots, actually", snapshots)
    counts = {str(n): sum(r["top"][str(n)] for r in checks) for n in LEVELS}
    visible_pass = sum(r["visible_exact"] for r in checks)
    examples = [r for r in checks if not r["top"]["1"]][:5]
    summary = {
        "symbol": SYMBOL,
        "date_utc": DAY,
        "file": archive["filename"],
        "message_count": snapshots + updates + unknown_actions,
        "snapshot_count": snapshots,
        "update_count": updates,
        "changed_levels": update_levels,
        "checkpoint_count": len(checks),
        "exact_both_sides_topN": counts,
        "visible_range_exact": visible_pass,
        "timestamps_backwards": violations,
        "unknown_actions": unknown_actions,
        "first_top1_mismatches": examples,
        "limits": [
            "No sequence IDs preserved; exact feed completeness unprovable",
            "Snapshots reset rolling reconstruction after each check",
            "Depth-truncated feed may omit out-of-range levels; topN metrics quantify drift",
            "Comparisons require exact quantity and level order-count equality",
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"okx_sol_l2_replay_{DAY}.json"
    csv_path = REPORT_DIR / f"okx_sol_l2_checkpoints_{DAY}.csv"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "checks": checks}, f, indent=2)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["checkpoint", "timestamp_utc", "updates_since_previous",
                         "top1_exact", "top10_exact", "top50_exact", "top100_exact", "top400_exact",
                         "visible_exact", "bid_missing", "bid_extra", "bid_wrong_qty",
                         "ask_missing", "ask_extra", "ask_wrong_qty"])
        for r in checks:
            writer.writerow([r["checkpoint"], r["utc"], r["updates_since_previous"],
                             *(int(r["top"][str(n)]) for n in LEVELS), int(r["visible_exact"]),
                             *[r["bids"][k] for k in ("within_visible_depth_missing",
                                                       "within_visible_depth_extra",
                                                       "within_visible_depth_wrong_qty")],
                             *[r["asks"][k] for k in ("within_visible_depth_missing",
                                                       "within_visible_depth_extra",
                                                       "within_visible_depth_wrong_qty")]])
    print("\n=== SNAPSHOT REPLAY AUDIT SUMMARY ===")
    print(json.dumps({k:v for k,v in summary.items() if k!="first_top1_mismatches"}, indent=2))
    print("First top1 mismatches:", json.dumps(examples, ensure_ascii=False)[:5000])
    print("Reports:", json_path, csv_path, flush=True)
    if snapshots != 96 or len(checks) != 95 or unknown_actions or violations:
        raise SystemExit("Basic replay validation failed")


if __name__ == "__main__":
    audit()
