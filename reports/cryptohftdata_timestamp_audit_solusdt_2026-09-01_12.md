# Timestamp granularity audit: SOLUSDT, Binance Futures, CryptoHFTData

**Audit run:** https://github.com/Huhabubu/quant_test/actions/runs/38830023429  
**Source:** [scripts/audit_chd_timestamps.py](../scripts/audit_chd_timestamps.py)  
**Sample:** 2026-09-01 12:00–13:00 UTC, SOLUSDT perpetual.

The prior 7-day L2 validation checked depth-update continuity, file coverage and sampled orderbook consistency. **It did not prove sub-millisecond exchange timestamp fidelity or strategy-relevant feed latency.**

| Metric | Depth (124,453 unique updates) | Trades (80,324 rows) |
|---|---:|---:|
| Binance `event_time E - transaction/trade time T`, median | 5 ms | 0 ms |
| Collector `received_time - E`, median | 127.220 ms | 128.090 ms |
| Collector `received_time - T`, median | 132.963 ms | 129.225 ms |
| Collector `received_time - T`, p95 | 151.152 ms | 163.949 ms |
| Collector `received_time - T`, minimum | 121.358 ms | 121.355 ms |
| Negative `received_time-T` | 0 | 0 |
| Fraction of messages sharing the same `T` millisecond (1 - unique T / count) | 0.0008% | 81.48% |
| Sub-millisecond components present in collector `received_time` | >99.99% | 100% |

**Interpretation:**
1. Binance emits `E` and `T` in **milliseconds**; CHD copies them directly. Adding `× 1,000,000` to convert ms to ns does not increase original precision.
2. The separately recorded `received_time` uses the provider's **collector clock**, with ns unit and nonzero nanosecond subdivisions, but its actual clock accuracy is not documented by these values. It is not the user's exchange-adjacent live market-feed receive time.
3. HftBacktest's official Binance Futures converter uses **`T` as `exch_ts` for both depth and trades**; `E` is the exchange publish/event time and shouldn't be silently substituted. It converts `T × 1_000_000` and keeps the local receive timestamp.
4. `received_time - T` ~130ms is an **observed timestamp difference** for this vendor/collector/date; it includes exchange-side publication, network, collector clock behavior and any time sync offset. It is not verified one-way network latency and not representative of user VPS/colocation delay. Never replace a measured strategy-specific feed latency with this value without calibration.
5. Same-ms trade timestamps are frequent. Trade IDs preserve relative order *within the trade stream*. L2 `pu/u` IDs preserve relative order *within the depth stream*. Cross-stream sub-ms exchange ordering generally cannot be recovered exactly from these millisecond timestamps.
6. The prior integrity audit stands, but **does not justify a sub-millisecond queue/latency execution simulator as-is**. Before such use, test exchange/local time ordering, time sync, observed delays by period, how to handle ties between trade/depth and a sensitivity analysis for >=1ms timing uncertainty.

**Official sources**
- CryptoHFTData Futures wire mapping: https://www.cryptohftdata.com/docs/wire-format-binance-futures
- HftBacktest Binance Futures converter: https://hftbacktest.readthedocs.io/en/latest/_modules/hftbacktest/data/utils/binancefutures.html
- HftBacktest feed data timestamps and feed latency validation: https://hftbacktest.readthedocs.io/en/latest/data.html
