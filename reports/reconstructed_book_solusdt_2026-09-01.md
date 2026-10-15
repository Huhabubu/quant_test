# Independent L2 order-book state comparison: Tardis vs CryptoHFTData

**Executed:** 2026-10-15. **Venue:** Binance USDⓈ-M Futures. **Symbol:** SOLUSDT.
**Market date:** 2026-09-01 (Tardis free day).
**Evidence:** [final successful GitHub Actions run](https://github.com/Huhabubu/quant_test/actions/runs/38715825960).
**Reproduction:** [scripts/compare_reconstructed_l2.py](../scripts/compare_reconstructed_l2.py).
**Branch:** `data-check/tardis-crosscheck` (`main` unchanged).

## Independent reconstruction method

1. **Tardis:** fetch keyless `incremental_book_L2` CSV.gz for the free day; reset the book on each genuine snapshot and apply absolute price-level quantities to reconstruct the book from midnight through 07:59 UTC. Approx. **7,344,453** rows consumed by 07:59; **2** snapshot groups. Snapshot rows are interpreted as replacement states.
2. **CryptoHFTData:** fetch public hourly `binance_futures/2026-09-01/07/SOLUSDT_orderbook.parquet`; discard unanchored early events until the first valid REST snapshot at **07:34:21.342 UTC**; apply all accepted depth-update groups in source order, checking Binance `pu/u` continuity. The archive contains **2 consecutive copies of the same snapshot with identical exchange update ID**, consistent with documented older-file behavior. **0** sequence gaps detected after bootstrap.
3. Freeze independent book states immediately before the first event group at each exchange-event-time cutoff: **07:40, 07:50, 07:59 UTC**. Both streams showed monotone event timestamps in the examined window (Tardis: 0 reversals).
4. For each cutoff, compare both sides' ranked level prices and absolute quantities. Also compare prices common to each top-N window individually; a missing depth level is distinct from a conflicting quantity for the same price.

**Result:** no shared-price quantity contradiction among the compared top 500 levels at any of the three cutoffs.

## Top-of-book and depth results

| UTC cutoff | Both BBO equal | Bid top 20 | Ask top 20 | Bid top 100 | Ask top 100 | Bid top 200 | Ask top 200 | Bid top 500 | Ask top 500 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 07:40 | 102.85 / 102.86 | 20/20 | 20/20 | 100/100 | 100/100 | 200/200 | 200/200 | 474/500 | 500/500 |
| 07:50 | 103.10 / 103.11 | 20/20 | 20/20 | 100/100 | 100/100 | 200/200 | 200/200 | 499/500 | 500/500 |
| 07:59 | 103.04 / 103.05 | 20/20 | 20/20 | 100/100 | 100/100 | 200/200 | 200/200 | 493/500 | 500/500 |

Thus the combined **top-200 comparison is 1,200/1,200 matched** (3 cutoffs × 2 sides × 200 levels). The combined **top-500 comparison is 2,966/3,000 matched** by rank.

## Why are there top-500 bid differences?

Top-500 level differences are **differences in which deep prices are present**, not disagreements in quantity when a price is shared:

| UTC cutoff | Shared bid prices within top 500 | Shared-price quantity differences | Tardis-only bid prices | CHD-only bid prices |
|---|---:|---:|---:|---:|
| 07:40 | 493 | **0** | 7 | 7 |
| 07:50 | 499 | **0** | 1 | 1 |
| 07:59 | 497 | **0** | 3 | 3 |

At 07:40, the closest Tardis-only bid price is 98.11 vs contemporaneous best bid 102.85, a deep ~4.6% distance from top of book. CHD archives its REST initial snapshot with **500 levels per side**, whereas Tardis starts at midnight and accrues deeper price levels from hours of historical updates. A missing deep price in CHD can be due to initial snapshot depth truncation, not necessarily a missed incremental update. This explanation is consistent with the source schemas and observed depth, but the exact update history of each affected deep price has not been individually traced.

**Depth counts at these observation cutoffs** (Tardis vs CHD):
- 07:40: bids 3161 vs 891; asks 2229 vs 792.
- 07:50: bids 3243 vs 1107; asks 2245 vs 932.
- 07:59: bids 3290 vs 1244; asks 2254 vs 998.

These are accumulated known-price maps from different bootstrap depths and history lengths, **not the exchange's complete live book**, and should not be used as proof of venue-wide depth availability.

## Scope and caveats

- Strong evidence of accurate reconstruction of **near-touch L2 book state** for these three instants, independently of the previous direct row-level comparison.
- This is **not** a verification of every millisecond across the full day, **not** a 7-day availability audit, **not** a full-depth match and **not** proof that either vendor precisely reconstructs every order at L3 granularity.
- Any future backtest ingestion should verify snapshots with Binance update IDs, preserve data source order, reject gap periods, and only compare levels inside the truly initialized coverage.
- Historical data remains ephemeral in GitHub Actions runner; no raw market data has been committed or uploaded to artifacts.

## Source semantics

- CHD Binance Futures raw-depth mapping and snapshot `lastUpdateId`: https://www.cryptohftdata.com/docs/wire-format-binance-futures
- CHD archives and older duplicate snapshot notes: https://www.cryptohftdata.com/docs/rest-orderbook
- Tardis downloadable CSV format: https://docs.tardis.dev/downloadable-csv-files
