"""Audit uninterrupted Binance Futures L2 recordings from CryptoHFTData.

Read only temporary parquet files. No original market data is retained or uploaded.
Each CLI invocation scans a contiguous multi-day chunk without resetting at hour/day boundaries.
"""
import argparse
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pyarrow.parquet as pq


def ns_iso(ns):
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat() if ns is not None else None


class Audit:
    def __init__(self, start, end):
        self.start_ns = int(start.timestamp() * 1_000_000_000)
        self.end_ns = int(end.timestamp() * 1_000_000_000)
        self.bid, self.ask = {}, {}
        self.valid = False
        self.valid_since = None
        self.valid_ns = 0
        self.prev_id = None
        self.raw_prev_id = None
        self.group_key = None
        self.group = None
        self.stats = Counter()
        self.hourly = []
        self.gaps = []
        self.snapshot_times = []
        self.crossed = []
        self.first_update = None
        self.last_update = None
        self.first_good_ns = None
        self.last_good_ns = None
        self.last_quote_check_minute = None
        self.same_version_snapshots = 0
        self.same_version_mismatches = 0
        self.last_snapshot_identity = None
        self.unverifiable_spans = []

    def switch_valid(self, is_valid, recv_ns, reason):
        recv_ns = max(self.start_ns, min(self.end_ns, int(recv_ns)))
        if is_valid == self.valid:
            return
        if is_valid:
            self.valid_since = recv_ns
            if self.first_good_ns is None:
                self.first_good_ns = recv_ns
        else:
            if self.valid_since is not None:
                self.valid_ns += max(0, recv_ns - self.valid_since)
                self.unverifiable_spans.append({
                    "from": ns_iso(recv_ns), "reason": reason
                })
            self.valid_since = None
        self.valid = is_valid

    def quote(self, recv_ns):
        if not self.valid or not self.bid or not self.ask:
            return
        best_bid = max(self.bid)
        best_ask = min(self.ask)
        self.stats["minute_quote_samples"] += 1
        self.last_good_ns = recv_ns
        if best_bid >= best_ask:
            self.stats["crossed_sampled_quotes"] += 1
            if len(self.crossed) < 8:
                self.crossed.append({
                    "at": ns_iso(recv_ns),
                    "bid": best_bid, "ask": best_ask
                })

    def finish_group(self):
        g = self.group
        if g is None:
            return
        recv = g["last_received"]
        if g["type"] == "snapshot":
            self.stats["snapshot_groups"] += 1
            if len(self.snapshot_times) < 20:
                self.snapshot_times.append({
                    "at": ns_iso(recv),
                    "last_update_id": g["last_id"],
                    "bids": len(self.bid),
                    "asks": len(self.ask)
                })
            good = bool(self.bid and self.ask and max(self.bid) < min(self.ask))
            if not good:
                self.stats["invalid_snapshots"] += 1
            self.switch_valid(good, recv, "invalid_snapshot")
            if good and g.get("prior_top") is not None and g["prior_id"] == g["last_id"]:
                self.same_version_snapshots += 1
                new_top = self.top_levels(20)
                if new_top != g["prior_top"]:
                    self.same_version_mismatches += 1
        elif g["type"] == "update":
            self.stats["update_groups"] += 1
            if g.get("noop"):
                self.stats["noop_update_groups"] += 1
            if g.get("skipped_old"):
                self.stats["old_update_groups"] += 1
            if g.get("gap"):
                self.stats["chain_gaps"] += 1
                if len(self.gaps) < 25:
                    self.gaps.append({
                        "received": ns_iso(recv),
                        "event_time_ms": g["event_ms"],
                        "prev_expected": g.get("expected"),
                        "prev_observed": g["pu"],
                        "U": g["U"], "u": g["u"]
                    })
            minute = recv // (60 * 1_000_000_000)
            if minute != self.last_quote_check_minute:
                self.last_quote_check_minute = minute
                self.quote(recv)
        self.group = None

    def top_levels(self, n):
        return (
            sorted(self.bid.items(), reverse=True)[:n],
            sorted(self.ask.items())[:n]
        )

    def feed(self, recv_ns, event_ms, kind, U, u, pu, last_id, side, price, quantity):
        # A historical REST snapshot can appear in two consecutive halves with
        # DIFFERENT receive timestamps: join on exchange event time + update ID.
        if kind == "snapshot":
            identifier = ("snapshot", event_ms, last_id)
        else:
            identifier = ("update", recv_ns, event_ms, U, u, pu)
        if identifier != self.group_key:
            self.finish_group()
            self.group_key = identifier
            group = {"type": kind, "last_received": recv_ns,
                     "event_ms": event_ms, "U": U, "u": u, "pu": pu,
                     "last_id": last_id, "apply": False}

            if kind == "snapshot":
                group["prior_top"] = self.top_levels(20) if self.valid else None
                group["prior_id"] = self.prev_id
                self.bid.clear()
                self.ask.clear()
                self.prev_id = last_id if last_id is not None else u
                self.raw_prev_id = None
                group["apply"] = True
                self.switch_valid(False, recv_ns, "snapshot_reset")
            elif kind == "update":
                self.stats["all_update_groups"] += 1
                if self.first_update is None:
                    self.first_update = {"at": ns_iso(recv_ns), "U": U,
                                         "u": u, "pu": pu}
                self.last_update = {"at": ns_iso(recv_ns), "U": U,
                                    "u": u, "pu": pu}
                # Check original wire continuity even before bootstrap.
                if self.raw_prev_id is not None and pu is not None:
                    if pu != self.raw_prev_id:
                        self.stats["raw_chain_discontinuities"] += 1
                self.raw_prev_id = u

                if self.valid:
                    if u is not None and self.prev_id is not None and u <= self.prev_id:
                        group["skipped_old"] = True
                    elif self.prev_id is not None and (
                        pu == self.prev_id or
                        (U is not None and u is not None and
                         U <= self.prev_id + 1 <= u)
                    ):
                        group["apply"] = True
                        self.prev_id = u
                    else:
                        group["gap"] = True
                        group["expected"] = self.prev_id
                        self.switch_valid(False, recv_ns, "sequence_gap")
                else:
                    self.stats["unanchored_update_groups"] += 1
            else:
                self.stats["unknown_event_groups"] += 1
            self.group = group
        else:
            self.group["last_received"] = max(recv_ns, self.group["last_received"])

        if not self.group["apply"]:
            return
        if side not in ("bid", "ask"):
            if side == "noop":
                self.group["noop"] = True
            return
        self.stats["applied_level_rows"] += 1
        target = self.bid if side == "bid" else self.ask
        p = float(price)
        q = float(quantity)
        if q <= 0:
            target.pop(p, None)
        else:
            target[p] = q

    def finish(self):
        self.finish_group()
        if self.valid and self.valid_since is not None:
            self.valid_ns += max(0, self.end_ns - self.valid_since)
        return {
            "stats": dict(self.stats),
            "coverage_pct_lower_bound": round(100 * self.valid_ns /
                  (self.end_ns - self.start_ns), 4),
            "first_valid_at": ns_iso(self.first_good_ns),
            "last_quote_at": ns_iso(self.last_good_ns),
            "first_update": self.first_update,
            "last_update": self.last_update,
            "final_seq": self.prev_id,
            "final_valid": self.valid,
            "snapshot_samples": self.snapshot_times,
            "sequence_gap_examples": self.gaps,
            "crossed_quote_examples": self.crossed,
            "same_version_snapshot_comparisons": self.same_version_snapshots,
            "same_version_snapshot_top20_mismatches": self.same_version_mismatches,
            "interval_end": ns_iso(self.end_ns)
        }


def scan_hour(audit, date, hour, temp):
    remote = f"binance_futures/{date}/{hour:02d}/SOLUSDT_orderbook.parquet"
    url = "https://api.cryptohftdata.com/v1/download?" + urlencode({"file": remote})
    target = Path(temp) / "hour.parquet"
    out = {"date": date, "hour": hour, "status": "ok"}
    request = Request(url, headers={"User-Agent": "quant-test-seven-day-book-audit/1.0"})
    try:
        with urlopen(request, timeout=95) as resp, target.open("wb") as fd:
            size = 0
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > 700 * 1024 * 1024:
                    raise ValueError("single hourly file >700 MiB")
                fd.write(chunk)
        parquet = pq.ParquetFile(target)
        out["bytes"] = size
        out["rows"] = parquet.metadata.num_rows
        fields = ["received_time", "event_time", "event_type", "first_update_id",
                  "final_update_id", "prev_final_update_id", "last_update_id",
                  "side", "price", "quantity"]
        if not set(fields).issubset(set(parquet.schema_arrow.names)):
            raise ValueError("missing required fields")
        for batch in parquet.iter_batches(batch_size=160_000, columns=fields):
            d = batch.to_pydict()
            for values in zip(*(d[col] for col in fields)):
                audit.feed(*values)
        audit.finish_group()
    except HTTPError as e:
        out["status"] = f"http_{e.code}"
        audit.stats["missing_or_failed_hours"] += 1
        audit.switch_valid(False, int(datetime.fromisoformat(date).replace(
            tzinfo=timezone.utc).timestamp() * 1e9) + hour * 3600 * 1_000_000_000,
            "unavailable_hour")
        audit.prev_id = None
        audit.raw_prev_id = None
    except (URLError, OSError, ValueError, RuntimeError, Exception) as e:
        out["status"] = "error"
        out["error"] = str(e)[:250]
        audit.stats["missing_or_failed_hours"] += 1
        audit.switch_valid(False, int(datetime.fromisoformat(date).replace(
            tzinfo=timezone.utc).timestamp() * 1e9) + hour * 3600 * 1_000_000_000,
            "unreadable_hour")
        audit.prev_id = None
        audit.raw_prev_id = None
    finally:
        target.unlink(missing_ok=True)
    if out["status"] == "ok":
        audit.stats["ok_hours"] += 1
    print("HOURLY_AUDIT " + json.dumps({
        **out, "snapshot_groups_total": audit.stats["snapshot_groups"],
        "chain_gaps_total": audit.stats["chain_gaps"],
        "raw_chain_discontinuities_total": audit.stats["raw_chain_discontinuities"],
        "valid_after_hour": audit.valid
    }), flush=True)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    audit = Audit(start, end)
    hours = []
    with TemporaryDirectory() as temp:
        current = start
        while current < end:
            entry = scan_hour(audit, current.date().isoformat(), current.hour, temp)
            hours.append(entry)
            current += timedelta(hours=1)
            time.sleep(0.25)
    result = {
        "from": args.start, "to": args.end,
        "expected_hours": len(hours),
        "ok_hours": sum(h["status"] == "ok" for h in hours),
        "failed_hours": [h for h in hours if h["status"] != "ok"],
        "total_bytes": sum(h.get("bytes",0) for h in hours),
        "total_level_rows": sum(h.get("rows",0) for h in hours),
        "audit": audit.finish()
    }
    print("CHUNK_AUDIT_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    # Do not automatically fail on a genuine data-quality problem: report it.
    # Fail if files are unusable, in which case a rerun may be needed.
    if result["ok_hours"] == 0:
        raise SystemExit("No usable hourly files")


if __name__ == "__main__":
    main()
