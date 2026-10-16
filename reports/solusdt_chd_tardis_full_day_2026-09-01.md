# Full-day independent order-book reconstruction — Binance USDⓈ-M SOLUSDT, 2026-09-01 (UTC)

**Completed:** 2026-10-16.  
**Action:** https://github.com/Huhabubu/quant_test/actions/runs/38906022088  
**Artifact:** https://github.com/Huhabubu/quant_test/actions/runs/38906022088/artifacts/12048461728  
**Source:** [scripts/compare_chd_tardis_full_day_20260901.py](../scripts/compare_chd_tardis_full_day_20260901.py)

## Data and protocol

- Tardis: public first-of-month `binance-futures/incremental_book_L2` 2026-09-01, **27,641,383** total CSV rows, including **9 full snapshot groups** and **18,222 snapshot-level rows**.
- CryptoHFTData (CHD): 24 source hourly Parquet L2 files for Sep 1, **27,674,013 level rows** (independently measured by prior daily archive benchmark), plus **17 hours from 2026-08-31 07:00 onward** for independent pre-midnight bootstrap; total downloaded in the successful run **480,553,171 bytes** (both dates).
- For both sources: preserve source order, use absolute size replacement at a quoted price, remove price level if quantity is zero, reset on real snapshot groups, never treat multiple price levels from a single update as independent book messages.
- CHD additionally requires `pu/u` update ID sequence validation; verified **0 accepted-chain gaps** in the tested replay. The Aug 31 bootstrap is needed because Sep 1's native initial snapshot arrives only at about 07:34 UTC. Beginning without an Aug 31 seed yields only **65 valid** cutoffs, starting after 07:34; with the seed, **95 valid** cutoffs.
- Freeze each independently reconstructed price map immediately before 15-minute cutoffs `00:15, 00:30, ..., 23:45` UTC; compare *rank and price-level quantity* on bid and ask at 1,10,20,50,100,200,400 levels.

## Full-day cross-provider result

| Depth | Both bid+ask exact checkpoints | Rank+quantity matches |
|---|---:|---:|
| 1 | 95/95 | 190/190 |
| 10 | 95/95 | 1,900/1,900 |
| 20 | 95/95 | 3,800/3,800 |
| 50 | 95/95 | 9,500/9,500 |
| 100 | 95/95 | 19,000/19,000 |
| 200 | 95/95 | 38,000/38,000 |
| **400** | **95/95** | **76,000/76,000** |

Across all 95 cross-provider checkpoints, **0 quantity contradictions** were observed among shared prices within the tested depths; no tested top-400 price was exclusive to either feed.

- CHD: **41 total hourly files** (17 for prior Aug 31 bootstrap, 24 for Sep 1); post-bootstrap **4,995,269 accepted update groups** over both dates; **0 accepted sequence gap examples**; 2 snapshot resets plus duplicate snapshot halves (4 snapshot groups observed, 2 duplicate).
- Tardis: 27,641,383 rows in day file; 9 snapshot groups; 0 exchange timestamp reversals within day.
- **Important:** the source reconstruction code reports the accepted update group count spanning prior-day bootstrap + target day; it should not be compared as a Sep 1 only statistic.

## Source-internal snapshot boundary checks are NOT a clean quality pass

A separate diagnostic compared the rolling pre-snapshot state to a new snapshot *before replacing that state*, without synchronizing them to the **same exchange update ID / exchange book version**. It returned **0/1 CHD** and **0/8 Tardis** exact at top-1 through top-400.

That is a comparison of potentially different moments, e.g. a later REST snapshot vs an earlier incremental state or a reconnecting feed. It is **not** a valid proof of missing price updates, and it is **not** comparable directly with OKX periodic snapshot checks. Proper source-internal reconciliation requires pairing each snapshot with the matching exchange state version or a validated time/sequence window, rather than simply comparing the immediately preceding streamed state.

## Market-making relevance and caveats

- **Strong cross-source evidence** of near-touch L2 depth accuracy for this date at 15-minute cutoff intervals. Significantly stronger than comparing within only one feed.
- **Not** proof that every 10ms/100ms interval or every L2 message is complete; two collectors could miss similar events or differ inside intervals while agreeing at the cutoffs.
- **Not** a verified queue position model or maker fill quality guarantee: Binance exposes aggregated depth (MBP), not each individual order; queue position, latency, partial fills and maker fees still need modeling.
- CHD previously had a verified **eight-trade omission on Sep 1** in a specific trade-ID gap; the current experiment audits **L2 only** and does not repair Trades. Independent Tardis/Binance Trades can be used for a separate trade backfill audit.
- No raw paid/vendor market data was committed to the repository. Only summary JSON/CSV are in the GitHub Actions artifact; `main` remains unchanged.

**Conclusion:** The complete Sep 1 CryptoHFTData vs Tardis cross-provider *book-state* checkpoint comparison **passes 95/95 at 400 levels on both sides**. It supports using CHD continuous L2 for experimental hftbacktest work, subject to trade-gap filtering/backfill, queue and latency sensitivity, and ongoing per-day feed integrity audit.
