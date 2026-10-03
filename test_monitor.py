import math
import tempfile
import unittest
from pathlib import Path

from monitor import Quote, SignalRecorder, calculate_edges_bps, discover_usdt_usdc_pairs


def q(
    symbol: str,
    bid: float,
    ask: float,
    *,
    event_ms: int | None = 1000,
    tx_ms: int | None = 999,
    recv_wall_ns: int = 2_000_000_000,
    recv_mono_ns: int = 1_000_000_000,
) -> Quote:
    return Quote(
        symbol=symbol,
        bid=bid,
        bid_qty=10.0,
        ask=ask,
        ask_qty=10.0,
        exchange_event_ms=event_ms,
        exchange_tx_ms=tx_ms,
        recv_wall_ns=recv_wall_ns,
        recv_mono_ns=recv_mono_ns,
    )


class DiscoveryTests(unittest.TestCase):
    def test_discovers_only_active_shared_perpetual_bases(self) -> None:
        info = {
            "symbols": [
                {
                    "symbol": "SOLUSDT",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                },
                {
                    "symbol": "SOLUSDC",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDC",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                },
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                },
                {
                    "symbol": "ETHUSDC",
                    "baseAsset": "ETH",
                    "quoteAsset": "USDC",
                    "contractType": "PERPETUAL",
                    "status": "BREAK",
                },
                {
                    "symbol": "SOLUSDT_261225",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDT",
                    "contractType": "CURRENT_QUARTER",
                    "status": "TRADING",
                },
            ]
        }

        pairs = discover_usdt_usdc_pairs(info)

        self.assertEqual(
            pairs,
            {"SOL": {"USDT": "SOLUSDT", "USDC": "SOLUSDC"}},
        )


class EdgeTests(unittest.TestCase):
    def test_usdt_rich_uses_executable_bid_vs_ask(self) -> None:
        usdt = q("SOLUSDT", bid=100.30, ask=100.31)
        usdc = q("SOLUSDC", bid=99.99, ask=100.00)
        fx = q(
            "USDCUSDT",
            bid=1.0,
            ask=1.0,
            event_ms=None,
            tx_ms=None,
        )

        usdt_rich, usdc_rich, fx_mid = calculate_edges_bps(usdt, usdc, fx)

        self.assertAlmostEqual(fx_mid, 1.0)
        self.assertAlmostEqual(usdt_rich, 30.0, places=8)
        self.assertLess(usdc_rich, 0.0)

    def test_usdc_rich_uses_fx_mid_normalization(self) -> None:
        usdt = q("SOLUSDT", bid=99.99, ask=100.00)
        usdc = q("SOLUSDC", bid=100.25, ask=100.26)
        fx = q(
            "USDCUSDT",
            bid=0.9999,
            ask=1.0001,
            event_ms=None,
            tx_ms=None,
        )

        usdt_rich, usdc_rich, fx_mid = calculate_edges_bps(usdt, usdc, fx)

        self.assertAlmostEqual(fx_mid, 1.0)
        self.assertAlmostEqual(usdc_rich, 25.0, places=8)
        self.assertLess(usdt_rich, 0.0)


class RecorderTests(unittest.TestCase):
    def test_event_is_one_start_and_one_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = SignalRecorder(18.0, Path(tmp))
            snapshot = {
                "trigger_market": "FUTURES",
                "trigger_symbol": "SOLUSDT",
                "trigger_exchange_event_ms": 1000,
                "trigger_exchange_tx_ms": 999,
                "local_recv_wall_ns": 2_000_000_000,
                "local_recv_mono_ns": 1_000_000_000,
                "local_detect_wall_ns": 2_000_010_000,
                "local_detect_mono_ns": 1_000_010_000,
                "processing_us": 10.0,
                "exchange_to_local_ms_est": 1001.0,
                "pair_tx_skew_ms": 1,
                "pair_recv_skew_ms": 0.1,
                "usdt_age_ms": 0.0,
                "usdc_age_ms": 0.1,
                "fx_age_ms": 1.0,
                "usdt_bid": 100.2,
                "usdt_ask": 100.3,
                "usdc_bid": 99.9,
                "usdc_ask": 100.0,
                "fx_bid": 1.0,
                "fx_ask": 1.0,
            }

            recorder.update(
                base="SOL",
                direction="USDT_RICH",
                edge_bps=19.0,
                snapshot=snapshot,
            )
            recorder.update(
                base="SOL",
                direction="USDT_RICH",
                edge_bps=22.0,
                snapshot={**snapshot, "local_detect_mono_ns": 1_001_000_000},
            )
            recorder.update(
                base="SOL",
                direction="USDT_RICH",
                edge_bps=17.5,
                snapshot={
                    **snapshot,
                    "local_recv_wall_ns": 2_002_000_000,
                    "local_recv_mono_ns": 1_002_000_000,
                    "local_detect_wall_ns": 2_002_010_000,
                    "local_detect_mono_ns": 1_002_010_000,
                },
            )

            self.assertEqual(recorder.started_count, 1)
            self.assertEqual(recorder.closed_count, 1)
            self.assertEqual(recorder.active, {})

            summaries = list(Path(tmp).glob("signal_events_*.csv"))
            transitions = list(Path(tmp).glob("signal_transitions_*.jsonl"))
            self.assertEqual(len(summaries), 1)
            self.assertEqual(len(transitions), 1)

            lines = transitions[0].read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
