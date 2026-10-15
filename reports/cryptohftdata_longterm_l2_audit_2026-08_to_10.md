# CryptoHFTData Binance SOLUSDT L2: multi-day and cross-month audit

**Run date:** 2026-10-15 (UTC).  
**Venue:** Binance USDⓈ-M futures. **Symbol:** SOLUSDT.  
**Purpose:** Determine availability, event-chain continuity, checkpoint quality and L2 replay fitness, **not** backtest GLFT profitability.
**Branch:** `data-check/tardis-crosscheck`; `main` unchanged.  
**Data handling:** Only hourly market data temporarily downloaded into Actions runners; no market data committed or exported. Workflow output contains aggregate metadata only.

## A. Seven consecutive days: 2026-09-01 through 2026-09-07 inclusive

| Item | Observed |
|---|---:|
| Expected orderbook hours | 168 |
| Downloaded and readable | **168/168** |
| Missing/unreadable hours | **0** |
| Total L2 level rows | **175,397,705** |
| Depth-update event groups | **20,555,173** |
| Raw Binance `pu -> preceding u` chain discontinuities | **0** |
| Sequence gaps detected after valid snapshot bootstrap | **0** |
| Genuine snapshot/checkpoint groups (duplicate halves combined) | **36** |
| One-per-minute best-bid/ask samples after bootstrap | **9,024** |
| Crossed quotes in sampled states | **0** |
| Same-sequence checkpoint comparisons on bid/ask top-20 | **24 passed / 24 compared** |

The seven days were partitioned into 3 **chronological** jobs without resetting at intra-job hour boundaries:

| Chunk | Files | Level rows | Update groups | Independent bootstrap first valid |
|---|---:|---:|---:|---|
| Sep 01 → Sep 03 | 48/48 | 54,106,411 | 5,903,756 | Sep 01 07:34:21.481580 UTC |
| Sep 03 → Sep 05 | 48/48 | 55,419,599 | 5,890,072 | Sep 03 01:34:02.728745 UTC |
| Sep 05 → Sep 08 | 72/72 | 65,871,695 | 8,761,345 | Sep 05 08:21:47.980761 UTC |

**Inter-job boundary reconciliation:**

- Sep 02 terminal `u=11459447232569` = Sep 03 first `pu=11459447232569`.
- Sep 04 terminal `u=11478412411040` = Sep 05 first `pu=11478412411040`.

Each chunk has zero raw update-sequence discontinuities. Therefore treating the post-snapshot periods of separately computed chunks as one continuous sequence is supported by exact update-ID handoffs. The per-chunk independently initialized percentages are **lower bounds**, not actual missing-data fractions.

### Pre-window snapshot bootstrap

To avoid treating the first 7.5 hours of Sep 01 as missing, a fourth job replayed **2026-08-31 00:00 through 2026-09-01 08:00 UTC**.

- 32/32 hourly files downloaded; **36,681,164 L2 rows**.
- First usable prior snapshot: **Aug 31 07:44:20.057286 UTC**.
- **0** raw chain gaps; **0** post-bootstrap gaps; **0** sampled crossed quotes.
- The job's reconstructed book was valid across the 2026-09-01 midnight boundary and through Sep 01 08:00.

Combined with the exact ID chain checks in the seven-day audit, a verified snapshot **pre-dating the seven-day window** exists and the entire Sep 01–Sep 07 interval is sequence-connected to it.

**Important qualification:** The seven-day week was not replayed in a single uninterrupted process; the state is joined using explicit previous-update ID equality at both job boundaries. This validates replay *feasibility and continuity*, not every historical depth level against Binance ground truth at every millisecond.

## B. Two cross-month sample days

| Date (UTC) | Archive format | Files | L2 rows | Update groups | Sequence gaps | Crossed minute samples |
|---|---|---:|---:|---:|---:|---:|
| **Aug 01 2026** | Zstandard outer wrapper, Parquet inside | 24/24 | 16,680,846 | 2,934,968 | 0 | 0 |
| **Oct 01 2026** | Direct Parquet | 24/24 | 30,736,429 | 2,969,463 | 0 | 0 |

- **August 1:** Direct PyArrow initially failed for all 24 files. The response inspection confirmed HTTP **200** and content type `application/zstd`, magic bytes `28b52ffd`, containing Parquet after decompression. The corrected pipeline decoded all 24 files and replayed successfully. The earliest independent snapshot *within that day* was **Aug 01 11:26:53.632072 UTC**, so book-state evaluation of earlier minutes requires a prior-day snapshot; however sequence IDs were continuous in the entire day.
- **October 1:** 24/24 files passed, with an hour-start checkpoint at 00:00, **100% internally initialized** throughout this day. There were **23/23** same-update-ID top-20 checkpoint matches and no sampled crossed books.

**Do not label the initial August Parquet parse failure a provider data outage.** Older files have an outer compression layer; a consumer should sniff the magic and decode the wrapper before giving the bytes to PyArrow.

## C. Independent vendor comparison already completed

On **2026-09-01**, both CryptoHFTData and Tardis normalized Binance Futures feeds were downloaded:
- After filtering 317 `NA` zero-price/zero-quantity pseudo-trade events, **52,508 / 52,508** Tardis true trades matched CHD on exchange trade ID, price, quantity, aggressor side and trade time.
- After normalizing hour boundaries, the common interval's **861,629 L2 update level records** matched.
- Separately reconstructed historical bid and ask **top 200** matched **1,200 / 1,200** across three observation cutoffs (07:40, 07:50 and 07:59 UTC); deeper top-500 differences related to price-depth coverage and did not contradict quantities at prices represented by both vendors.

Source report: [Independent reconstructed depth comparison](reconstructed_book_solusdt_2026-09-01.md).

## D. Overall verdict and limitations

**Verdict for Binance Futures SOLUSDT L2 research/backtesting:** **PASS for the tested periods, subject to ingestion controls.**

Confirmed: hourly availability in the selected dates, sequence continuity across the continuous week, availability of prior checkpoint to anchor the week, many checkpoint-to-replay top-20 matches, no crossed quotes at the **once-per-minute sampled** observation times, and full agreement with Tardis in the earlier tested near-touch window.

Not proved:
- No gap on **every historical date**; this covered 7 continuous days plus two noncontiguous full-day samples only.
- Intraday **trade** completeness across seven days (this audit is specifically L2; vendor trades were compared separately for one hour).
- Every order-book level at every message (crosses sampled once per minute; no L3 ordering).
- All snapshot states are independently equal to Binance ground truth; same-update-ID top-20 comparison is one practical proxy.
- The August 1 early 00:00–11:26 session can be reconstructed **without** a preceding July 31 snapshot; this prior anchor has not been tested.

**Ingestion rules:** inspect file compression magic (`PAR1` vs outer Zstd); preserve archived event order; combine duplicate halves of the same Binance REST snapshot using event time and last update ID; treat hour-start checkpoints as full-book replacement states; verify Binance `pu=previous.u`; reset to the next valid snapshot on broken sequence; apply absolute quantities with quantity 0 meaning deletion; drop `side=noop` as a level change but keep its sequence IDs; never assume every hourly file contains a snapshot.

## Evidence and reproducibility

- [Seven-day 3-job matrix, all passed](https://github.com/Huhabubu/quant_test/actions/runs/38718329747)
- [August 31 pre-window snapshot bootstrap, passed](https://github.com/Huhabubu/quant_test/actions/runs/38719041501)
- [Cross-month original runs (August direct-Parquet attempt fails; October passes)](https://github.com/Huhabubu/quant_test/actions/runs/38719369021)
- [Older-file HTTP response/magic diagnostics, passed](https://github.com/Huhabubu/quant_test/actions/runs/38719655918)
- [August Zstandard-aware replay and audit, passed](https://github.com/Huhabubu/quant_test/actions/runs/38719824898)
- [Seven-day chronological audit source](../scripts/audit_cryptohftdata_7d.py)
- [Legacy Zstandard-aware version](../scripts/audit_cryptohftdata_legacy.py)

Official provider references:
- https://www.cryptohftdata.com/docs/wire-format-binance-futures
- https://www.cryptohftdata.com/docs/rest-orderbook
- https://www.cryptohftdata.com/docs/data-gaps-and-corrections
