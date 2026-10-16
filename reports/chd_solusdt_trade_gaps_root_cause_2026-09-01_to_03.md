# SOLUSDT 2026-09-01–03 missing trades ROOT CAUSE investigation

**Executed:** 2026-10-16 UTC. All original raw market data stayed on ephemeral GitHub Actions runner.  
**Market:** Binance USDⓈ-M Futures, SOLUSDT.  
**CryptoHFTData:** 72 full hourly trade files; include `order_type=NA` in ID checks.  
**Independent evidence:** Binance FIRST-PARTY daily raw futures trades, plus Tardis 2026-09-01 free original trades.  
**Code:** [../scripts/chd_trade_gap_root_cause.py](../scripts/chd_trade_gap_root_cause.py), [../scripts/chd_l2_during_trade_gaps.py](../scripts/chd_l2_during_trade_gaps.py), [../scripts/chd_l2_max_gap.py](../scripts/chd_l2_max_gap.py).  
**Runs:** [trade gaps, Binance + Tardis](https://github.com/Huhabubu/quant_test/actions/runs/38841551683) · [L2 events in trade gaps](https://github.com/Huhabubu/quant_test/actions/runs/38841926726) · [L2 exact quiet spans](https://github.com/Huhabubu/quant_test/actions/runs/38842241431).

## Independent-confirmed loss of real trades

| UTC trade gap | CHD skipped trade IDs | Official Binance raw trades **present** among skipped IDs | Duration between surrounding trades |
|---|---:|---:|---:|
| 2026-09-01 15:32:53.082 → 15:32:55.735 | 8 | **8** | 2.653 s |
| 2026-09-02 15:22:57.642 → 15:23:00.119 | 47 | **47** | 2.477 s |
| 2026-09-02 16:38:28.135 → 16:40:30.005 | 2558 | **2541** | 121.870 s |
| 2026-09-02 22:01:59.755 → 22:02:03.625 | 27 | **27** | 3.870 s |
| 2026-09-03 08:38:47.089 → 08:38:53.049 | 1 | **1** | 5.960 s |
| 2026-09-03 21:52:05.613 → 21:52:08.079 | 22 | **22** | 2.466 s |
| **TOTAL** | **2663** | **2646** | — |

### Independent source evidence

- Binance primary source URL pattern: `https://data.binance.vision/data/futures/um/daily/trades/SOLUSDT/SOLUSDT-trades-YYYY-MM-DD.zip`; all three official daily files were successfully downloaded and fully scanned.
- Full-day official raw trade rows: **2,086,101** Sep 1, **1,940,886** Sep 2, **2,334,937** Sep 3.
- **2646** skipped CHD IDs occur as trade IDs with market price/quantity in Binance's official raw daily trades files. Thus these are actual missed executions, not only ID holes.
- The remaining **17** skipped IDs all lie in Sep 2's two-minute gap and are absent from Binance's first-party raw trades archive. They could be `NA` / nonmarket messages, but this is **not proven**.
- Tardis Sep 1 independently contains all 8 missing trades with IDs `3498856889` through `3498856896`. For example the first missing trade ID `3498856889` occurred at 2026-09-01 15:32:54.098 UTC with price 101.75, size 1.36 SOL.
- Vendor recorded `order_type=NA` counts in those days: Sep1 9316, Sep2 8953, Sep3 10663; the original gap detection **did not exclude NA**. No null trade IDs or nonmonotone IDs were found in the three days.

## Cross-source L2 activity at the same times

| UTC trade gap | CHD unique L2 update groups with exchange E inside gap | Interpretation |
|---|---:|---|
| Sep 1 15:32 | 89 | L2 active, trade stream misses execution events |
| Sep 2 15:22 | 84 | L2 active |
| Sep 2 16:38–16:40 | **14**, all before ~16:38:28.536 UTC | L2 stream **also stopped** |
| Sep 2 22:01 | 133 | L2 active |
| Sep 3 08:38 | **6**, all before ~08:38:47.228 UTC | L2 stream interrupted, followed by new snapshots |
| Sep 3 21:52 | 85 | L2 active |

### Sep 2 16:38 L2: precise hard gap

L2 last normal incremental:
- received_time 2026-09-02 **16:38:28.657** UTC, `final_update_id=11456961289726`
- following L2 record: **16:42:47.014** UTC, type **snapshot**, `last_update_id=11456991526868`
- silent span: **258.356 seconds** (4min18.356s) without any published L2 updates.
- Supplier emits two source snapshots with same update ID at received times 16:42:47.014 and 16:42:47.166.
- The price-level chain cannot be reconstructed through the silent interval; the book can be re-anchored at the later snapshot only.
- Trades resume at 16:40:30, ~2min17sec **before** L2 resumes.

### Sep 3 08:38 L2: repeat reconnect / snapshots

- Last normal incremental received at **08:38:47.355** UTC, `u=11462636884943`.
- Next published snapshot at **08:39:01.674** UTC, with `last_update_id=11462641695324` and exchange `event_time=08:39:39.976` UTC. (This synthetic snapshot's event time is **later than** its received_time; do **not** treat as ordinary market-feed latency. Preserve source.)
- Later snapshot rows at 08:39:39.976, 08:39:53.188, 08:39:53.779, 08:40:56.458, 08:40:56.589.
- Largest adjacent silent span among all recorded L2 groups was **62.679 s** (08:39:53.779 → 08:40:56.458); these snapshots are *not* substitute for missing incremental history.
- This explains why a prior audit could have returned '0 chain gaps' while still having missing L2 time ranges. Snapshots reset the state and re-anchor update IDs.

## Root cause grading

**CONFIRMED:** CHD's historical SOLUSDT Trades omit at least **2646** real market trades that Binance's own archive contains (and Tardis independently confirms 8 on Sep 1). These omissions are **not** caused by the test's NA filtering.

**CONFIRMED:** at least two trade gaps coincide with missing or interrupted CHD L2 incremental message history. Sep 2 has an unequivocal 258.356 sec L2 blackout before fresh snapshot; Sep 3 has repeated snapshot-only recovery periods.

**CONSISTENT WITH VENDOR'S DOCUMENTED FAILURE MODE:** CryptoHFTData's [Known Gaps & Corrections](https://www.cryptohftdata.com/docs/data-gaps-and-corrections) states that Binance collector WebSocket reconnections, typically around daily rotation or network errors, can drop Trades and cause ID gaps. The observed multi-second trade outages and L2 snapshot restarts are consistent with reconnection(s).

**NOT PROVEN:** exact failure trigger (scheduled reconnect versus network exception, process restart, upstream routing, collector shard). This would require vendor connection logs. The 17 skipped IDs absent from Binance's first-party trade archive cannot be assigned to real market execution or NA message type without more evidence.

## Implications for hftbacktest and archive design

1. **The earlier 7-day '0 update-ID gaps' metric alone did NOT imply complete temporal L2 coverage.** Do not label a day fully verified based just on valid update chains. Add a time-silence and snapshot-reset coverage audit, or compare independent L2 references.
2. Reconstructable book state *after* a new snapshot does **not recover** missing in-between book events, queue transitions, cancels, trades, or adverse selection. Strict queue/fill replay must explicitly mask or split these intervals and reinitialize state, not connect through them.
3. Binance official raw trades can repair the **trade content** (ids/prices/qty/trade timestamps) for at least 2646 of the misses. It does **not** provide the original CHD collector `received_time` or Binance WebSocket `E` for missing messages; these fields must be modeled/flagged unknown rather than faked.
4. Keep raw hourly CHD data in one daily ZIP, with separately tagged optional Binance repair trades if chosen. The daily manifest must report `L2_coverage_intervals`, gap windows, bootstrap snapshots, `trades_missing_count`, vendor archive version/hashes and source provenance.
5. In the specific sample, all three days are **partial for strict full-day queue replay** (all have at least one real missing execution); Sep 2/3 include substantive L2 blackout/snapshot-only intervals. It is valid to replay separate independently anchored segments with explicit exclusion windows, not claim entire days represent continuous ground truth.
