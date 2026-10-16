"""Diagnose OKX 400-level snapshot/advance alignment in 2026-09-02 L2 archive.

Replay real L2 updates; for each 15-min snapshot, compare the preceding
reconstructed order book and candidates from +/-250 ms of update history.
The new snapshot is applied only *after* evaluating candidates; buffered later
updates are then replayed to resume a fresh authoritative book.
This is diagnostic, not proof of packet completeness. No trades/orders/keys.
"""
from collections import Counter, deque
from datetime import datetime, timezone
import json
from pathlib import Path
import tarfile
from urllib.request import Request, urlopen
from okx_sol_day_audit import files, DAY

SYMBOL = "SOL-USDT-SWAP"
WINDOW = 250
OUT = Path("reports") / f"okx_sol_l2_alignment_{DAY}.json"
SIDES = ("bids", "asks")


def parse_book(event):
    book = {}
    for side in SIDES:
        book[side] = {
            float(x[0]): (float(x[1]), int(x[2]))
            for x in event[side] if float(x[1]) > 0
        }
    return book


def apply_update(book, event):
    undo = []
    for side in SIDES:
        levels = book[side]
        for p, q, n in event[side]:
            price, qty = float(p), float(q)
            old = levels.get(price)
            undo.append((side, price, old))
            if qty == 0.0:
                levels.pop(price, None)
            else:
                levels[price] = (qty, int(n))
    return undo


def reverse_update(book, undo):
    for side, price, old in reversed(undo):
        if old is None:
            book[side].pop(price, None)
        else:
            book[side][price] = old


def side_metric(book, snap, side):
    pred = book[side]
    obs = snap[side]
    # L2 archives are depth-limited. Restrict extra/missing levels to the
    # current snapshot's displayed price range.
    lo, hi = min(obs), max(obs)
    pred_visible = {p: v for p, v in pred.items() if lo <= p <= hi}
    missing = len(obs.keys() - pred_visible.keys())
    extra = len(pred_visible.keys() - obs.keys())
    shared = obs.keys() & pred_visible.keys()
    wrong_qty = sum(obs[p][0] != pred_visible[p][0] for p in shared)
    wrong_orders = sum(obs[p][1] != pred_visible[p][1] for p in shared)
    bid = side == "bids"
    pred_rank = sorted(pred.items(), reverse=bid)
    obs_rank = sorted(obs.items(), reverse=bid)
    top = {}
    for depth in (1, 10, 50, 400):
        top[str(depth)] = (pred_rank[:depth] == obs_rank[:depth])
    best_price = (pred_rank[0][0] == obs_rank[0][0]) if pred_rank else False
    return dict(missing=missing, extra=extra, wrong_qty=wrong_qty,
                wrong_orders=wrong_orders, top=top, best_price=best_price)


def compare(book, snap, lag_ms, phase):
    bids, asks = [side_metric(book, snap, side) for side in SIDES]
    total = sum(x[k] for x in (bids, asks)
                for k in ("missing", "extra", "wrong_qty", "wrong_orders"))
    return dict(lag_ms=lag_ms, phase=phase, total_errors=total,
                qty_errors=bids["wrong_qty"] + asks["wrong_qty"],
                order_errors=bids["wrong_orders"] + asks["wrong_orders"],
                presence_errors=sum(x[k] for x in (bids, asks)
                                    for k in ("missing", "extra")),
                both_best_price=bids["best_price"] and asks["best_price"],
                both_top={str(n): bids["top"][str(n)] and asks["top"][str(n)]
                          for n in (1, 10, 50, 400)},
                bids=bids, asks=asks)


def choose(candidates):
    # First prefer no missing/extra prices, then fewer quantity differences;
    # avoid automatically choosing a large shift when tied with zero-lag.
    return min(candidates, key=lambda x: (
        x["presence_errors"], x["qty_errors"], x["order_errors"],
        abs(x["lag_ms"]), x["phase"] != "pre"))


def audit():
    archives = [a for a in files(4, DAY, DAY)
                if a["filename"].endswith(DAY + ".tar.gz")
                and "400lv" in a["filename"]]
    if len(archives) != 1:
        raise RuntimeError(f"Expected one L2 archive; found {len(archives)}")
    url = archives[0]["url"]
    snap_count = 0
    update_count = 0
    history = deque()
    book = None
    pending = None
    checks = []
    last_ts = None

    def complete_pending(p):
        nonlocal book, history
        candidates = p["candidates"]
        base = p["baseline"]
        best = choose(candidates)
        past = choose([x for x in candidates if x["phase"] in ("pre", "past")])
        future = choose([x for x in candidates if x["phase"] in ("pre", "future")])
        checks.append(dict(
            index=p["index"], utc=datetime.fromtimestamp(
                p["ts"] / 1000, tz=timezone.utc).isoformat(timespec="milliseconds"),
            baseline=base, best=best, best_past=past, best_future=future,
            candidate_count=len(candidates),
            checked_lags_ms=[x["lag_ms"] for x in candidates],
            candidate_metrics=[dict(lag_ms=x["lag_ms"],
                                    phase=x["phase"],
                                    total_errors=x["total_errors"],
                                    qty_errors=x["qty_errors"],
                                    presence_errors=x["presence_errors"],
                                    both_top=x["both_top"])
                               for x in candidates],
        ))
        if p["index"] % 10 == 0:
            print("checkpoint", p["index"],
                  "baseline_qty_errors", base["qty_errors"],
                  "best_qty_errors", best["qty_errors"],
                  "best_lag_ms", best["lag_ms"],
                  "baseline_top10", base["both_top"]["10"],
                  "best_top10", best["both_top"]["10"], flush=True)
        # Now reset to independent snapshot and fast-forward buffered later
        # updates. This ensures every subsequent comparison starts from a
        # reliable snapshot instead of a misaligned history state.
        book = {side: val.copy() for side, val in p["snap"].items()}
        for event in p["after_events"]:
            apply_update(book, event)
        history.clear()

    request = Request(url, headers={"User-Agent": "okx-l2-snapshot-alignment-audit/1.0"})
    with urlopen(request, timeout=120) as response:
        with tarfile.open(fileobj=response, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if not member.name.endswith(".data"):
                    raise ValueError(member.name)
                file = tar.extractfile(member)
                for raw in file:
                    evt = json.loads(raw)
                    ts = int(evt["ts"])
                    if evt.get("instId") != SYMBOL:
                        raise ValueError("Unexpected symbol")
                    if last_ts is not None and ts < last_ts:
                        raise ValueError("Time reversal")
                    last_ts = ts
                    if pending is not None and ts > pending["ts"] + WINDOW:
                        complete_pending(pending)
                        pending = None
                    action = evt["action"]
                    if action == "snapshot":
                        snap_count += 1
                        snap = parse_book(evt)
                        if book is None:
                            book = snap
                            continue
                        if pending is not None:
                            raise RuntimeError("Unexpected consecutive snapshots")
                        base = compare(book, snap, 0, "pre")
                        candidates = [base]
                        working = {side: x.copy() for side, x in book.items()}
                        for prior_ts, undo in reversed(history):
                            if ts - prior_ts > WINDOW:
                                break
                            reverse_update(working, undo)
                            candidates.append(compare(
                                working, snap, prior_ts - ts, "past"))
                        pending = dict(index=snap_count - 1, ts=ts, snap=snap,
                                       candidates=candidates, baseline=base,
                                       after_events=[])
                    elif action == "update":
                        update_count += 1
                        undo = apply_update(book, evt)
                        if pending is not None:
                            pending["after_events"].append(evt)
                            pending["candidates"].append(
                                compare(book, pending["snap"],
                                        ts - pending["ts"], "future"))
                        else:
                            history.append((ts, undo))
                            while history and history[0][0] < ts - WINDOW:
                                history.popleft()
                    else:
                        raise RuntimeError(f"Unexpected action {action}")
                    if update_count % 1000000 == 0:
                        print("processed", update_count, "updates", flush=True)
    if pending is not None:
        complete_pending(pending)

    def pass_count(x, key):
        return sum(r[x]["both_top"][str(key)] for r in checks)
    summary = dict(
        date=DAY, symbol=SYMBOL, snapshots=snap_count,
        updates=update_count, comparisons=len(checks),
        test_window_ms=WINDOW,
        exact_top_n={str(n): {
            "unshifted": pass_count("baseline", n),
            "best_within_window": pass_count("best", n)
        } for n in (1, 10, 50, 400)},
        visible_price_presence_all_baseline=sum(
            r["baseline"]["presence_errors"] == 0 for r in checks),
        visible_price_presence_all_best=sum(
            r["best"]["presence_errors"] == 0 for r in checks),
        baseline_qty_error_sum=sum(r["baseline"]["qty_errors"] for r in checks),
        best_qty_error_sum=sum(r["best"]["qty_errors"] for r in checks),
        exact_all_levels_unshifted=sum(
            r["baseline"]["total_errors"] == 0 for r in checks),
        exact_all_levels_best=sum(
            r["best"]["total_errors"] == 0 for r in checks),
        best_lags=dict(Counter(str(r["best"]["lag_ms"]) for r in checks)),
        best_positive=sum(r["best"]["lag_ms"] > 0 for r in checks),
        best_negative=sum(r["best"]["lag_ms"] < 0 for r in checks),
        best_zero=sum(r["best"]["lag_ms"] == 0 for r in checks),
        best_qty_zero=sum(r["best"]["qty_errors"] == 0 for r in checks),
        not_proof_of_feed_completeness=True,
    )
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"summary":summary,"checks":checks},indent=2),encoding="utf-8")
    print("\n=== ALIGNMENT DIAGNOSTIC SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print("First 12 improved checkpoints:",
          json.dumps([{
              "utc":r["utc"], "base_qty":r["baseline"]["qty_errors"],
              "best_qty":r["best"]["qty_errors"], "lag_ms":r["best"]["lag_ms"],
              "base_top10":r["baseline"]["both_top"]["10"],
              "best_top10":r["best"]["both_top"]["10"],
          } for r in checks if r["best"]["qty_errors"] < r["baseline"]["qty_errors"]][:12], indent=2))
    print("Report:", OUT, flush=True)
    if snap_count != 96 or len(checks) != 95:
        raise RuntimeError("Snapshot/checkpoint count differs from expected")


if __name__ == "__main__":
    audit()
