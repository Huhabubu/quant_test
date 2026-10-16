# Daily CryptoHFTData archive benchmark — SOLUSDT vs BTCUSDT

**Status:** successful empirical A/B1/B2/C size benchmarks on two real Binance USDⓈ-M Futures UTC days, 2026-09-01. Tested all 48 original hourly L2 + Trades files per symbol. Source market data stayed on disposable GitHub-hosted runner disks, never pushed to repository or artifacts.

- [SOL run](https://github.com/Huhabubu/quant_test/actions/runs/38837392598)
- [BTC run](https://github.com/Huhabubu/quant_test/actions/runs/38837836788)
- [Source code](../scripts/chd_daily_archive_benchmark.py)

## Real measured bytes (decimal MB)

| Data | SOLUSDT | BTCUSDT |
|---|---:|---:|
| Raw L2+Trades source files | 303,513,933 B | 585,469,862 B |
| A: ZIP_STORED retaining all 48 original files + manifest | 303,528,392 B (303.53 MB) | 585,484,335 B (585.48 MB) |
| B1: 2 daily Parquet/ZSTD-6 files, unchanged original schema | 300,586,675 B (300.59 MB) | 594,636,324 B (594.64 MB) |
| B2: 2 daily Parquet/ZSTD-6 files, price/quantity converted to scaled int64 | 326,512,499 B (326.51 MB) | 628,884,587 B (628.88 MB) |
| C: NPZ 64-byte Event layout **size-only proxy**, not usable for backtest | 262,815,983 B (262.82 MB) | 1,013,084,450 B (1013.08 MB) |
| B1 size difference from A | **-0.97%** | **+1.56%** |
| B2 size difference from A | **+7.57%** | **+7.41%** |

## Detailed metrics

| Measurement | SOLUSDT | BTCUSDT |
|---|---:|---:|
| Depth rows | 27,674,013 | 134,482,964 |
| Trade rows | 2,095,407 | 3,638,927 |
| C event-like rows (excluding NA trades) | 29,760,104 | 138,105,687 |
| `NA` non-trade rows excluded only in C | 9,316 | 16,204 |
| Vendor hourly files | 48 | 48 |
| Outer zstd wrappers | 0 | 0 |
| ZIP overhead incl manifest | 14,459 B | 14,473 B |
| Download duration | 24.69 s | 29.40 s |
| ZIP packing duration | 0.45 s | 0.85 s |
| Entire test duration | 126.56 s | 362.06 s |
| Peak process RSS (includes memmapped NPZ computation) | 2,172.6 MB | **8,818.6 MB** |

**Validation:** B1 full-row/full-column/full-order Arrow comparisons passed for the entire source on both symbols. B2 scaled-integer numeric equality and all other field/order comparisons passed, but B2 does not preserve raw lexical representations of price/quantity (for example, original decimal trailing zeroes). C intentionally uses provisional source-stream ordering, does not correct EXCH/LOCAL chronology or snapshot clearing, and must not be used as a valid hftbacktest data file.

**Practical finding:** BTC C's 138 million event-like records generated a `np.memmap` Event proxy over 8 GB RSS at peak. Kaggle conversion has to use controlled streaming/chunking and monitor RAM; this is *not* the memory footprint of the intended raw ZIP creator.

## Decision

**Use alternative A, original hourly Parquet files packed as ZIP_STORED.** Re-encoding B1 saved less than one percent on SOL and increased size on BTC. B2 increased size for both and has extra semantic complexity. No demonstrated compression advantage meets the previously proposed >=25% threshold. No need to reorganize raw vendor files for long-term storage.

### Not yet tested (do not claim finished)

1. `snapshot_initial` / `snapshot_final` reconstruction across adjacent days, including gap state machine and provider checkpoint semantics.
2. Full manifest completeness, price-band/coverage quality, update ID gap and trade ID gap audits.
3. Real hftbacktest-ready NPZ conversion, event order validation, actual Kaggle replay correctness.
4. Google Drive resumable upload and readback checksum.
5. Older archives with outer Zstd wrapper.

**Recommendations:** Keep A ZIP format, add verified initial/final snapshots and manifest, test the cross-day book state chain separately; avoid multi-GB NPZ generation on a small runner just for long-term archival; use these two samples as hard evidence rather than extrapolating a universal codec compression percentage.
