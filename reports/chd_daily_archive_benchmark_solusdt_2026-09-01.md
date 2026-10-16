# CryptoHFTData day archive A/B1/B2/C benchmark: Binance Futures SOLUSDT

- **Run:** https://github.com/Huhabubu/quant_test/actions/runs/38837392598
- **Test code:** [../scripts/chd_daily_archive_benchmark.py](../scripts/chd_daily_archive_benchmark.py)
- **Market:** Binance USDⓈ-M Futures (`binance_futures`), SOLUSDT
- **Day:** 2026-09-01 UTC
- **Scope:** exactly 24 hourly `orderbook` and 24 hourly `trades` files.
- **Data handling:** ephemeral GitHub Actions runner; raw data not published to GitHub or as artifact.

## Verified empirical metrics

| Alternative | Stored bytes | MB (decimal) | Versus A |
|---|---:|---:|---:|
| A: daily ZIP_STORED with 48 unchanged vendor hourly files and manifest | 303,528,392 | 303.528 | baseline |
| B1: 2 type-specific daily Parquet/ZSTD-6 files, all columns and row order retained | 300,586,675 | 300.587 | **0.97% smaller** |
| B2: 2 type-specific Parquet/ZSTD-6 files, price and quantity encoded as fixed-scale int64 (1e8) | 326,512,499 | 326.512 | **7.57% larger** |
| C: NPZ Event layout, size-only proxy | 262,815,983 | 262.816 | **13.41% smaller** |

### Source composition and audit

- Downloaded original vendor file payloads: **303,513,933 bytes**.
- ZIP_STORED package overhead including manifest and ZIP directory: **14,459 bytes**.
- Depth: **27,674,013 rows**.
- Trades: **2,095,407 rows**.
- Total source records: **29,769,420 rows**.
- Outer Zstandard wrappers: 0/48 for this date.
- B1 stream-versus-source comparison: **every row, every column and original row order passed** (depth 436 checked batches, trades 41 batches).
- B2 stream-versus-source comparison: **every row and all original non-price/quantity fields matched; scaled integer numeric equivalence passed**. However B2 is *not byte- or lexical-lossless*: original strings such as `1.2500` are not preserved as text.
- C skipped **9,316** `order_type='NA'` trades with no executable price/quantity, resulting in **29,760,104** event-like rows. C contains **a provisional source-ordered representation only**: it is not output from `correct_event_order()`, does not correctly implement multi-snapshot clear/reset handling, and is **not a market-replay-validated hftbacktest dataset**.
- B1 B2 comparison packages are Parquet payload bytes only; A contains a small manifest; initial/final snapshots are **not yet included in this compression benchmark**.

### Performance

- 48 vendor files download with three concurrent workers: **24.69 s**.
- ZIP_STORED assembly: **0.45 s**.
- B1 per-stream Parquet writer time: orderbook 14.71 s + trades 1.06 s.
- B2 scale transform: orderbook 4.49 s + trades 0.33 s (in addition to B2 write time).
- C NPZ size-only conversion/compression: 52.46 s.
- Full test elapsed: **126.56 s**.
- Max process RSS: **2,172.6 MB** (full test with NPZ; not attribution to A/B1).
- This download run observed an average of approximately 12.3 MB/s; it does **not** imply a fixed GitHub Actions ingress bandwidth entitlement.

### Conclusions and pending validation

1. **Keep A as the default long-term archive format.** With just 0.97% space saving, B1 does not justify discarding byte-for-byte originals for this SOLUSDT date. B2 is a regression in compressed size.
2. **C is not a ready-to-backtest file.** hftbacktest adaptor, event order correction, snapshot handling, and latency model must be implemented and tested separately.
3. The planned **initial and final verified book snapshots**, cross-day replay, update-id gap classification, checksum manifest, and Google Drive upload are **not yet validated by this compression-only test**. Do not claim they are complete.
4. Repeat at least one representative BTCUSDT/high-load day (compression behavior may vary) before treating the ~1% saving as universal.
5. GitHub Actions has resource, execution, abuse and artifact limits. No published fixed general inbound GB quota was identified; data origin service rate-limits requests. Keep artifacts summary-only.

**Current recommended design:** one symbol per day ZIP_STORED containing original 48 hourly files (including legacy outer zstd as delivered), plus to-be-implemented initial/final snapshots and manifest; use temporary NPZ conversion on Kaggle later.
