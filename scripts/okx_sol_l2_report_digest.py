"""Summarize replay-vs-snapshot comparisons for high-frequency maker research."""
from collections import Counter
from pathlib import Path
from statistics import median, mean
import json
from okx_sol_day_audit import DAY

path = Path(f"reports/okx_sol_l2_replay_{DAY}.json")
data = json.loads(path.read_text(encoding="utf-8"))
checks = data["checks"]
print("\n=== CHECKPOINT DIAGNOSTICS ===")
print("Comparisons:", len(checks))
for level in (1, 10, 50, 100, 400):
    rows = [r for r in checks if r["top"][str(level)]]
    print(f"Top {level} exact:", len(rows), "/", len(checks))
for side in ("bids", "asks"):
    checks_with_price_diff = sum(
        r[side]["top"]["400"]["price_rank_differences"] > 0
        for r in checks
    )
    checks_with_qty_diff = sum(r[side]["within_visible_depth_wrong_qty"] > 0 for r in checks)
    checks_with_order_diff = sum(r[side]["within_visible_depth_wrong_order_count"] > 0 for r in checks)
    totals = {
        key: sum(r[side][key] for r in checks)
        for key in ("within_visible_depth_missing", "within_visible_depth_extra",
                    "within_visible_depth_wrong_qty", "within_visible_depth_wrong_order_count")
    }
    qty_errors = [r[side]["within_visible_depth_wrong_qty"] for r in checks]
    print(side, "price-rank mismatches:", checks_with_price_diff,
          "snapshots with qty diff:", checks_with_qty_diff,
          "snapshots with order-count diff:", checks_with_order_diff,
          "total wrong entries:", totals,
          "median wrong qty:", median(qty_errors), "max:", max(qty_errors))
price_best_match = sum(
    r["bids"]["snapshot_best"][0] == r["bids"]["reconstructed_best"][0]
    and r["asks"]["snapshot_best"][0] == r["asks"]["reconstructed_best"][0]
    for r in checks
)
qty_best_match = sum(
    r["bids"]["snapshot_best"][1][0] == r["bids"]["reconstructed_best"][1][0]
    and r["asks"]["snapshot_best"][1][0] == r["asks"]["reconstructed_best"][1][0]
    for r in checks
)
print("Both sides best-price match:", price_best_match, "/", len(checks))
print("Both sides best-quantity match:", qty_best_match, "/", len(checks))
offs = [r["ms_since_previous_message"] for r in checks if r.get("ms_since_previous_message") is not None]
print("Snapshot offset from preceding message ms: min/median/max:", min(offs), median(offs), max(offs))
print("Snapshot offsets distribution:", dict(sorted(Counter(offs).items())))
worst = sorted(checks, key=lambda r: sum(r[s]["within_visible_depth_wrong_qty"] for s in ("bids", "asks")), reverse=True)[:8]
for r in worst:
    print("Largest qty mismatch:", r["utc"], "bid wrong:", r["bids"]["within_visible_depth_wrong_qty"],
          "ask wrong:", r["asks"]["within_visible_depth_wrong_qty"],
          "prior msg ms:", r.get("ms_since_previous_message"),
          "best bid snapshot/pred:", r["bids"]["snapshot_best"], r["bids"]["reconstructed_best"],
          "best ask snapshot/pred:", r["asks"]["snapshot_best"], r["asks"]["reconstructed_best"])
print("First failing top1 times:",
      [r["utc"] for r in checks if not r["top"]["1"]][:20])
print("First failing top400 times:",
      [r["utc"] for r in checks if not r["top"]["400"]][:10])
print("Diagnostics completed", flush=True)
