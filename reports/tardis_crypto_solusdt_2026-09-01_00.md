# Tardis vs CryptoHFTData: Binance Futures SOLUSDT

- Comparison executed: 2026-10-15
- Shared market: Binance USDT-margined perpetual, SOLUSDT
- Shared UTC window: 2026-09-01 00:00:00–00:59:59.999
- Tardis: keyless first-day-of-month daily CSV.gz, streamed and filtered to the hour.
- CryptoHFTData: anonymous hourly Parquet downloads.
- Execution: [GitHub Actions run 38711084512](https://github.com/Huhabubu/quant_test/actions/runs/38711084512).
- Reproducible script: [scripts/compare_tardis_crypto.py](../scripts/compare_tardis_crypto.py).
- No raw historical data stored in the repository or workflow artifacts.

## Trade-by-trade comparison

| Metric | Result |
|---|---:|
| Tardis rows | 52,508 |
| CryptoHFTData rows | 52,825 |
| Identical trade IDs in both | 52,508 |
| Full matches on shared IDs (time/price/size/aggressor) | 52,508 (100%) |
| Tardis-only IDs | 0 |
| CryptoHFTData-only IDs | 317 |
| Duplicated IDs | 0 in both |

The 317 CryptoHFTData-only trades are distributed from minute 01 through minute 59; they cannot be explained solely by an hour-boundary cutoff. Their authenticity has not been independently established from Binance raw data.

## Order-book L2 level update comparison

A signature is `(event timestamp milliseconds, bid/ask, price, absolute quantity)`.
Records marked as initial snapshot are excluded from the update comparison because the providers can snapshot at different points.

| Metric | Result |
|---|---:|
| Tardis incremental update rows | 861,663 |
| CryptoHFTData incremental update rows | 861,925 |
| Identical signatures | 861,629 |
| Fraction of Tardis update rows matched | 99.9961% |
| Tardis-only updates | 34 (minute 59) |
| CryptoHFTData-only updates | 296 (23 before hour, 273 in minute 00) |
| Tardis snapshot level rows | 2,001 |
| CryptoHFTData snapshot level rows in this file | 0 |

Unmatched L2 records are concentrated at the hour boundaries, consistent with providers using different timestamp and file partitioning rules. This is an inference, not yet independently proven.

## Assessment

The main data categories needed for GLFT / hftbacktest research—trades and incremental L2 changes—exist on both services and show excellent direct overlap in the tested hour. However, snapshot placement and file boundaries differ; full-depth book replay still requires a preceding valid checkpoint.

This comparison validates **type compatibility and high overlap in this one-hour sample**, not day-level completeness, Binance-ground-truth correctness, or fill-model realism. Further research should investigate the 317 unmatched trades against the original Binance data feed and test more dates and volatile hours.
