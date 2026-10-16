# CryptoHFTData three-day L2 / Trades continuity and throughput audit — SOLUSDT

**Completed successful run:** https://github.com/Huhabubu/quant_test/actions/runs/38839058079  
**Code:** [../scripts/chd_cross_day_chain_and_trades.py](../scripts/chd_cross_day_chain_and_trades.py)  
**Market:** Binance USDⓈ-M Futures, SOLUSDT  
**Period:** 2026-09-01 through 2026-09-03 UTC  
**Authentication:** Anonymous REST API (deliberate >=1.18 second spacing between requests, honoring <=60 requests per 60 sec).  
**Storage:** ephemeral GitHub Actions runner; only summary in logs/artifact, no raw market data committed/uploaded.

## Performance (entire 48-file L2 + trades/day pipeline)

| Date | Size (MB decimal) | Day wall (s) | L2 download+replay (s) | Trade download (s) | Trade parse (s) | Trades rows |
|---|---:|---:|---:|---:|---:|---:|
| Sep 1 | 303.51 | **124.36** | 91.24 | 31.49 | 0.56 | 2,095,407 |
| Sep 2 | 294.46 | **121.37** | 88.54 | 31.71 | 0.56 | 1,947,225 |
| Sep 3 | 317.03 | **135.74** | 101.16 | 33.62 | 0.66 | 2,345,578 |
| **Total** | **915.01** | **381.49** | — | — | — | **6,388,210** |

- Files downloaded and scanned: 72 L2 + 72 trades = **144**, all present.
- Peak Python RSS: **484.3 MB**.
- L2 scanning covers every price-level row; trade scan covers every trade ID, including `order_type='NA'` rows.
- Prior downloads without replay using 3 concurrent streams required **24.69s for 303.51MB SOL** and **29.40s for 585.47MB BTC** for their full 48-hourly-file day. That is **download only**, and may exceed sustainable anonymous API request cadence if repeated rapidly across days.
- This three-day run supports **~2.1 minutes per SOLUSDT symbol-day** for download+L2 validation/trades-id scan. It excludes creating/serializing actual initial/final snapshot Parquet, building ZIP/manifest, Google Drive upload and cloud checksums.

## L2 book chain and state handoff

| Day | Initial state | Final state | Final update ID `u` | Bids / asks at final | L2 chain gaps |
|---|---|---|---:|---:|---:|
| Sep 1 | Unanchored (no valid 00:00 seed in this test) | Valid after vendor snapshot | 11450218025900 | 4,572 / 4,284 | 0 |
| Sep 2 | Valid from Sep 1 final | Valid | 11459447232569 | 3,716 / 2,229 | 0 |
| Sep 3 | Valid from Sep 2 final | Valid | 11469319900744 | 5,089 / 3,762 | 0 |

- Sep 1 starts without a valid seed; approximately **929,957 update message groups** occurred before a source snapshot anchored the book. This is why a production Sep 1 archive must replay a suitable earlier day (previous audit identified an Aug 31 valid source snapshot).
- Sep 2 began from Sep 1 final exact in-memory book + update ID; Sep 3 began from Sep 2 final. Both remained in the book valid state. No sequence-chain gaps were recorded in these two days.
- Extra supplier snapshots: 1/2/5 snapshot groups across Sep 1/2/3. Proper snapshot resets and duplicate handling are necessary. Existing replay uses the experimental prior Audit implementation, and snapshot digests are **floating-point replay-state diagnostics**, not yet full-precision production snapshot Parquet artifacts.
- Cross-day handoff identity is a deterministic program-state check; the stronger evidence is replaying all subsequent messages with no detected sequence discontinuity. This is not a complete exchange full-depth orderbook audit beyond observed depth coverage.

## Trade continuity (CRITICAL: all days have missing trade IDs)

| Day | NA rows | Positive trade-ID gap groups | Missing IDs implied by gaps | Hours affected |
|---|---:|---:|---:|---|
| Sep 1 | 9,316 | 1 | **8** | 15 UTC |
| Sep 2 | 8,953 | 3 | **2,632** | 15, 16, 22 UTC |
| Sep 3 | 10,663 | 2 | **23** | 08, 21 UTC |
| **Total** | **28,932** | **6** | **2,663** | — |

- The gap counter INCLUDED all `order_type=NA` rows. It did not filter them out.
- No nonincreasing IDs were reported (0).
- Missing IDs are *suspected* trade-message losses until exchange rules and/or a second vendor independently confirms them. They should not be silently filled.
- Classification for strict L2+trades-dependent market-making replay: **Sep 1 partial** (missing 00:00 book seed + trade gap); **Sep 2 partial** (verified L2 chain, trade gaps); **Sep 3 partial** (verified L2 chain, trade gaps).
- An initial output field called `anchor_status` only described the L2 book chain. It returned 'verified' for Sep 2/3, which MUST NOT be interpreted as an overall archive status. Script was amended to add `archive_status`, downgrading any day with trade ID gaps to `partial`. The raw successful-run metrics above are from the initial run.

## Conclusions and next verification

1. **No need for API key yet.** Anonymous REST allows 60 requests/60 seconds; intentionally paced batch test fetched 144 files without authentication. For bulk multi-symbol years, authenticated limits and direct S3 may help; use GitHub Actions secret/JWT, never put raw key in logs or URLs.
2. Realistic daily throughput includes replay and is currently ~2–2.5 min per **SOLUSDT** day under anonymous pacing, rather than the ~25 sec download-only benchmark. Do NOT extrapolate to BTC without running the full BTC replay (134.5m L2 rows on sample BTC day vs ~28m for SOL).
3. Long-term format recommendation unchanged: preserve the 48 vendor files (byte-for-byte) in `ZIP_STORED`, plus initial/final snapshot and manifest. Production snapshot serialization and manifest completeness remain unimplemented.
4. Before calling a day `verified` for strict hftbacktest fill/queue testing, **locate and validate trade ID gaps** and mark affected periods as unsafe for queue replay; test against independent trade source where available. A fully valid L2 chain does not imply Trades are complete.
5. Confirm production snapshot serialization with exact decimal strings, initial/final update IDs and session boundaries; handle hour checkpoints separately from true market message times.
