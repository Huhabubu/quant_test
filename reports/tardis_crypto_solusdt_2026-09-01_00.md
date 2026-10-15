# Tardis vs CryptoHFTData: SOLUSDT discrepancy root-cause audit

- Date checked: 2026-10-15
- Venue: Binance USDⓈ-M futures, symbol SOLUSDT
- Window: 2026-09-01 00:00–01:00 UTC
- Inputs: Tardis free first-of-month CSV.gz; CryptoHFTData public hourly Parquet, including the next hour for boundary verification.
- Final diagnostic run: https://github.com/Huhabubu/quant_test/actions/runs/38712273476
- Reproduction script: [scripts/compare_tardis_crypto.py](../scripts/compare_tardis_crypto.py)
- Original market data is neither committed nor uploaded as an artifact.

## Finding 1 — The 317 extra CryptoHFTData "trades" have zero executable size

| Check | Count |
|---|---:|
| Tardis normalized trade rows | 52,508 |
| CryptoHFTData trade rows | 52,825 |
| Shared trade IDs, all fields identical | 52,508 |
| CryptoHFTData-only trade IDs | 317 |
| CryptoHFTData-only with `order_type=NA` | **317** |
| CryptoHFTData-only with both price and quantity zero | **317** |
| Nonzero CHD-only trade quantity | **0** |
| Common-record differences in execution timestamp, price, quantity or aggressor | **0** |

The extra rows are Binance `@trade` channel non-market messages (type `X=NA` with `p=q=0`). CryptoHFTData explicitly documents that it preserves these messages; Tardis's normalized trade CSV excludes them. Thus, filtering out these zero-volume messages makes the two providers agree exactly on trade IDs and economically relevant fields within this hour.

Source: https://www.cryptohftdata.com/docs/wire-format-binance-futures

**Preprocessing rule for GLFT:** only count records with positive trade price, positive trade quantity and valid aggressor direction as trades. Reject zero-size/non-market events before estimating order-flow intensity and queue depletion. Keep the original `trade_id` for deduplication.

## Finding 2 — All 330 unmatched L2 update rows are at the hour's initialization/partition boundaries

L2 comparison key: `(exchange event time in ms, side, price, absolute quantity)`.
Snapshots are a separate dataset event type, deliberately excluded from incremental-update matching.

| Check | Count |
|---|---:|
| Tardis incremental rows in the selected hour | 861,663 |
| CryptoHFTData incremental rows in file `00` | 861,925 |
| Direct exact matches | **861,629** |
| CHD file `00` only | 296 |
| Tardis hour `00` only | 34 |
| Tardis-only recovered *exactly* in CHD hour `01` | **34 / 34** |
| CHD-only before Tardis's first incremental event | **296 / 296** |
| CHD-only timestamped no later than Tardis initial snapshot | 182 |
| CHD-only timestamped after snapshot's exchange time but before first incremental | 114 |
| Unmatched shared-window event after first Tardis incremental | **0** |

**Observed opening timeline (2026-09-01 UTC):**

- CHD hour `00` holds 23 book-level updates with exchange event time in the final second of 2026-08-31; another 273 records from the very first second of 2026-09-01.
- Tardis snapshot exchange timestamp: `00:00:00.535`; local receipt timestamp: **`00:00:00.831`**. 2,001 snapshot-level rows.
- Tardis first subsequent incremental exchange timestamp: `00:00:00.836`; local receipt timestamp: `00:00:00.839`.
- There are **182** CHD-only rows at or before snapshot's exchange timestamp and **114** CHD-only rows between snapshot's exchange timestamp and first incremental's exchange timestamp.
- Tardis-only 34 final-minute updates occur in CHD's **next hour** file (`01`); all 34 signatures were located exactly.

Tardis documents that its CSV files are partitioned by **local receipt time**, and that initial book snapshots may incorporate buffered updates during exchange reconnects, including cases where snapshot and update exchange timestamps are not monotonic. CHD files are partitioned by collector receipt hour. These mechanics explain the edge-only row differences.

Official references:
- https://docs.tardis.dev/downloadable-csv-files
- https://docs.tardis.dev/faq/data
- https://www.cryptohftdata.com/docs/rest-orderbook

**Important qualification:** This test matches *individual update rows* after the initial snapshot, but does not prove that every price level of each provider's reconstructed **initial** order book is identical. In particular, the 114 updates between snapshot exchange time and first incremental event may be buffered into Tardis's snapshot (whose local receipt time is later); this requires a snapshot-to-snapshot book-state comparison for an exhaustive bootstrap audit.

## Assessment for GLFT / hftbacktest

- On this single observed hour, valid trades match exactly (52,508).
- After accounting for first-snapshot startup and next-hour partitioning, the 861,629 directly comparable L2 updates also match exactly.
- Therefore, the previously reported 0.60% "extra trades" do **not** indicate additional market executions, and the 330 unmatched L2 level rows do **not** reveal an interior-hour delta discrepancy.
- Continue to validate each historical day's initial checkpoint, incremental sequence continuity and any recording gaps; one matched hour does not establish full-history completeness.
- For historical execution simulation, ingest the previous checkpoint if a CHD hourly file begins with updates and **never** treat every delta level as a separate market event when reconstructing the book.

This investigation did not need Binance public trade archives to identify the 317 unmatched records: their `order_type` and zero price/quantity are directly verifiable in the source dataset and consistent with the documented Binance WebSocket wire mapping.
