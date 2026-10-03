from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp


FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FUTURES_WS_BASE = "wss://fstream.binance.com"
SPOT_FX_WS = "wss://stream.binance.com:9443/ws/usdcusdt@bookTicker"


@dataclass(slots=True)
class Quote:
    symbol: str
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    exchange_event_ms: int | None
    exchange_tx_ms: int | None
    recv_wall_ns: int
    recv_mono_ns: int


@dataclass(slots=True)
class ActiveEvent:
    event_id: str
    base: str
    direction: str
    start_edge_bps: float
    max_edge_bps: float
    last_edge_bps: float
    start_snapshot: dict[str, Any]
    max_snapshot: dict[str, Any]
    last_snapshot: dict[str, Any]
    start_mono_ns: int


def utc_iso_from_ns(ns: int | None) -> str:
    if ns is None:
        return ""
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def day_from_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).strftime("%Y%m%d")


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def discover_usdt_usdc_pairs(exchange_info: dict[str, Any]) -> dict[str, dict[str, str]]:
    by_base: dict[str, dict[str, str]] = {}

    for symbol in exchange_info.get("symbols", []):
        if symbol.get("status") != "TRADING":
            continue
        if symbol.get("contractType") != "PERPETUAL":
            continue

        quote = symbol.get("quoteAsset")
        if quote not in {"USDT", "USDC"}:
            continue

        base = symbol.get("baseAsset")
        name = symbol.get("symbol")
        if not base or not name:
            continue

        by_base.setdefault(base, {})[quote] = name

    return {
        base: legs
        for base, legs in sorted(by_base.items())
        if "USDT" in legs and "USDC" in legs
    }


def calculate_edges_bps(usdt: Quote, usdc: Quote, fx: Quote) -> tuple[float, float, float]:
    """
    Return:
      usdt_rich_bps: sell AUSDT at bid, buy AUSDC at ask
      usdc_rich_bps: sell AUSDC at bid, buy AUSDT at ask
      fx_mid: USDT value of 1 USDC, from spot USDCUSDT BBO midpoint

    FX is valuation-only in v0. There is no third execution leg.
    """
    fx_mid = (fx.bid + fx.ask) / 2.0

    usdc_ask_in_usdt = usdc.ask * fx_mid
    usdc_bid_in_usdt = usdc.bid * fx_mid

    usdt_rich_bps = (usdt.bid / usdc_ask_in_usdt - 1.0) * 10_000.0
    usdc_rich_bps = (usdc_bid_in_usdt / usdt.ask - 1.0) * 10_000.0

    return usdt_rich_bps, usdc_rich_bps, fx_mid


class SignalRecorder:
    SUMMARY_FIELDS = [
        "event_id",
        "base",
        "direction",
        "start_utc",
        "end_utc",
        "start_edge_bps",
        "max_edge_bps",
        "end_edge_bps",
        "threshold_bps",
        "duration_ms",
        "start_trigger_market",
        "start_trigger_symbol",
        "start_trigger_exchange_event_ms",
        "start_trigger_exchange_tx_ms",
        "start_local_recv_ns",
        "start_local_detect_ns",
        "start_processing_us",
        "start_exchange_to_local_ms_est",
        "start_pair_tx_skew_ms",
        "start_pair_recv_skew_ms",
        "start_usdt_age_ms",
        "start_usdc_age_ms",
        "start_fx_age_ms",
        "start_usdt_bid",
        "start_usdt_ask",
        "start_usdc_bid",
        "start_usdc_ask",
        "start_fx_bid",
        "start_fx_ask",
        "max_trigger_market",
        "max_trigger_symbol",
        "max_trigger_exchange_tx_ms",
        "max_local_detect_ns",
        "end_reason",
    ]

    def __init__(self, threshold_bps: float, output_dir: Path) -> None:
        self.threshold_bps = threshold_bps
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.active: dict[tuple[str, str], ActiveEvent] = {}
        self.started_count = 0
        self.closed_count = 0

    def update(
        self,
        *,
        base: str,
        direction: str,
        edge_bps: float,
        snapshot: dict[str, Any],
    ) -> None:
        key = (base, direction)
        current = self.active.get(key)

        if edge_bps >= self.threshold_bps:
            if current is None:
                self._start(base, direction, edge_bps, snapshot)
                return

            current.last_edge_bps = edge_bps
            current.last_snapshot = snapshot
            if edge_bps > current.max_edge_bps:
                current.max_edge_bps = edge_bps
                current.max_snapshot = snapshot
            return

        if current is not None:
            current.last_edge_bps = edge_bps
            current.last_snapshot = snapshot
            self._finish(key, current, snapshot, "BELOW_THRESHOLD")

    def abort_all(self, reason: str) -> None:
        if not self.active:
            return

        now_wall_ns = time.time_ns()
        now_mono_ns = time.monotonic_ns()

        for key, event in list(self.active.items()):
            snapshot = dict(event.last_snapshot)
            snapshot["trigger_market"] = "SYSTEM"
            snapshot["trigger_symbol"] = ""
            snapshot["trigger_exchange_event_ms"] = None
            snapshot["trigger_exchange_tx_ms"] = None
            snapshot["local_recv_wall_ns"] = now_wall_ns
            snapshot["local_recv_mono_ns"] = now_mono_ns
            snapshot["local_detect_wall_ns"] = now_wall_ns
            snapshot["local_detect_mono_ns"] = now_mono_ns
            snapshot["processing_us"] = 0.0
            snapshot["exchange_to_local_ms_est"] = None
            self._finish(key, event, snapshot, reason)

    def _start(
        self,
        base: str,
        direction: str,
        edge_bps: float,
        snapshot: dict[str, Any],
    ) -> None:
        start_wall_ns = int(snapshot["local_detect_wall_ns"])
        start_mono_ns = int(snapshot["local_detect_mono_ns"])
        event_id = f"{base}-{direction}-{start_wall_ns}"

        event = ActiveEvent(
            event_id=event_id,
            base=base,
            direction=direction,
            start_edge_bps=edge_bps,
            max_edge_bps=edge_bps,
            last_edge_bps=edge_bps,
            start_snapshot=snapshot,
            max_snapshot=snapshot,
            last_snapshot=snapshot,
            start_mono_ns=start_mono_ns,
        )
        self.active[(base, direction)] = event
        self.started_count += 1

        self._append_transition("START", event, snapshot, edge_bps, "")
        print(
            f"[SIGNAL START] {base:<12} {direction:<10} "
            f"edge={edge_bps:8.3f} bps "
            f"trigger={snapshot['trigger_symbol']} "
            f"T={snapshot.get('trigger_exchange_tx_ms')} "
            f"recv={utc_iso_from_ns(snapshot['local_recv_wall_ns'])}"
        )

    def _finish(
        self,
        key: tuple[str, str],
        event: ActiveEvent,
        end_snapshot: dict[str, Any],
        reason: str,
    ) -> None:
        end_wall_ns = int(end_snapshot["local_detect_wall_ns"])
        end_mono_ns = int(end_snapshot["local_detect_mono_ns"])
        duration_ms = max(0.0, (end_mono_ns - event.start_mono_ns) / 1_000_000.0)

        self._append_transition(
            "END",
            event,
            end_snapshot,
            event.last_edge_bps,
            reason,
        )
        self._append_summary(event, end_snapshot, reason, duration_ms)

        print(
            f"[SIGNAL END]   {event.base:<12} {event.direction:<10} "
            f"start={event.start_edge_bps:8.3f} "
            f"max={event.max_edge_bps:8.3f} "
            f"end={event.last_edge_bps:8.3f} bps "
            f"duration={duration_ms:9.3f} ms "
            f"reason={reason}"
        )

        self.closed_count += 1
        self.active.pop(key, None)

    def _append_transition(
        self,
        kind: str,
        event: ActiveEvent,
        snapshot: dict[str, Any],
        edge_bps: float,
        reason: str,
    ) -> None:
        wall_ns = int(snapshot["local_detect_wall_ns"])
        path = self.output_dir / f"signal_transitions_{day_from_ns(wall_ns)}.jsonl"
        row = {
            "kind": kind,
            "event_id": event.event_id,
            "base": event.base,
            "direction": event.direction,
            "edge_bps": edge_bps,
            "threshold_bps": self.threshold_bps,
            "reason": reason,
            "snapshot": snapshot,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _append_summary(
        self,
        event: ActiveEvent,
        end_snapshot: dict[str, Any],
        reason: str,
        duration_ms: float,
    ) -> None:
        start = event.start_snapshot
        maximum = event.max_snapshot
        end_wall_ns = int(end_snapshot["local_detect_wall_ns"])
        path = self.output_dir / f"signal_events_{day_from_ns(end_wall_ns)}.csv"

        row = {
            "event_id": event.event_id,
            "base": event.base,
            "direction": event.direction,
            "start_utc": utc_iso_from_ns(start["local_detect_wall_ns"]),
            "end_utc": utc_iso_from_ns(end_wall_ns),
            "start_edge_bps": f"{event.start_edge_bps:.9f}",
            "max_edge_bps": f"{event.max_edge_bps:.9f}",
            "end_edge_bps": f"{event.last_edge_bps:.9f}",
            "threshold_bps": f"{self.threshold_bps:.9f}",
            "duration_ms": f"{duration_ms:.6f}",
            "start_trigger_market": start.get("trigger_market", ""),
            "start_trigger_symbol": start.get("trigger_symbol", ""),
            "start_trigger_exchange_event_ms": start.get("trigger_exchange_event_ms"),
            "start_trigger_exchange_tx_ms": start.get("trigger_exchange_tx_ms"),
            "start_local_recv_ns": start.get("local_recv_wall_ns"),
            "start_local_detect_ns": start.get("local_detect_wall_ns"),
            "start_processing_us": start.get("processing_us"),
            "start_exchange_to_local_ms_est": start.get("exchange_to_local_ms_est"),
            "start_pair_tx_skew_ms": start.get("pair_tx_skew_ms"),
            "start_pair_recv_skew_ms": start.get("pair_recv_skew_ms"),
            "start_usdt_age_ms": start.get("usdt_age_ms"),
            "start_usdc_age_ms": start.get("usdc_age_ms"),
            "start_fx_age_ms": start.get("fx_age_ms"),
            "start_usdt_bid": start.get("usdt_bid"),
            "start_usdt_ask": start.get("usdt_ask"),
            "start_usdc_bid": start.get("usdc_bid"),
            "start_usdc_ask": start.get("usdc_ask"),
            "start_fx_bid": start.get("fx_bid"),
            "start_fx_ask": start.get("fx_ask"),
            "max_trigger_market": maximum.get("trigger_market", ""),
            "max_trigger_symbol": maximum.get("trigger_symbol", ""),
            "max_trigger_exchange_tx_ms": maximum.get("trigger_exchange_tx_ms"),
            "max_local_detect_ns": maximum.get("local_detect_wall_ns"),
            "end_reason": reason,
        }

        new_file = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.SUMMARY_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)


class BasisMonitor:
    def __init__(
        self,
        pairs: dict[str, dict[str, str]],
        threshold_bps: float,
        output_dir: Path,
    ) -> None:
        self.pairs = pairs
        self.output_dir = output_dir
        self.recorder = SignalRecorder(threshold_bps, output_dir)

        self.symbol_meta: dict[str, tuple[str, str]] = {}
        for base, legs in pairs.items():
            self.symbol_meta[legs["USDT"]] = (base, "USDT")
            self.symbol_meta[legs["USDC"]] = (base, "USDC")

        self.quotes: dict[str, Quote] = {}
        self.fx_quote: Quote | None = None

    def write_run_metadata(self) -> Path:
        now_ns = time.time_ns()
        path = self.output_dir / f"run_metadata_{day_from_ns(now_ns)}_{now_ns}.json"
        payload = {
            "started_utc": utc_iso_from_ns(now_ns),
            "threshold_bps": self.recorder.threshold_bps,
            "pair_count": len(self.pairs),
            "pairs": self.pairs,
            "futures_exchange_info": FUTURES_EXCHANGE_INFO,
            "futures_ws_base": FUTURES_WS_BASE,
            "fx_stream": SPOT_FX_WS,
            "fx_semantics": (
                "USDCUSDT spot BBO midpoint is used only to normalize USDC contract "
                "prices into USDT units. v0 has no executable FX third leg."
            ),
            "edge_formulas": {
                "USDT_RICH": "(AUSDT_bid / (AUSDC_ask * fx_mid) - 1) * 10000",
                "USDC_RICH": "((AUSDC_bid * fx_mid) / AUSDT_ask - 1) * 10000",
            },
            "time_semantics": {
                "exchange_event_ms": "Binance futures bookTicker E: event generation time.",
                "exchange_tx_ms": "Binance futures bookTicker T: matching-engine transaction time.",
                "local_recv_wall_ns": (
                    "time.time_ns() captured immediately after the websocket message is yielded, "
                    "before JSON decoding."
                ),
                "local_recv_mono_ns": "time.monotonic_ns() captured with local receive time.",
                "local_detect_wall_ns": "time.time_ns() captured after edge calculation.",
                "processing_us": "local_detect_mono_ns - local_recv_mono_ns for the trigger message.",
                "exchange_to_local_ms_est": (
                    "local_recv_wall_ms - trigger exchange_tx_ms. This is only an estimate and "
                    "includes clock offset; interpret as one-way latency only on a synchronized host."
                ),
                "signal_event_time": (
                    "For a threshold crossing, the trigger message's Binance T/E are the signal "
                    "exchange times. If an FX bookTicker update causes the crossing, Binance Spot "
                    "bookTicker has no T/E here, so only local receive/detect timestamps are recorded."
                ),
            },
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    async def run_futures_ws(self, session: aiohttp.ClientSession) -> None:
        symbols = sorted(self.symbol_meta)
        streams = "/".join(f"{symbol.lower()}@bookTicker" for symbol in symbols)
        url = f"{FUTURES_WS_BASE}/stream?streams={streams}"

        backoff = 1.0
        connected_once = False

        while True:
            try:
                if connected_once:
                    self.recorder.abort_all("FUTURES_RECONNECT")
                    self.quotes.clear()

                async with session.ws_connect(url, heartbeat=30, receive_timeout=90) as ws:
                    connected_once = True
                    backoff = 1.0
                    print(f"[WS] futures connected: {len(symbols)} bookTicker streams")

                    async for msg in ws:
                        recv_wall_ns = time.time_ns()
                        recv_mono_ns = time.monotonic_ns()

                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in {
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                            continue

                        payload = json.loads(msg.data)
                        data = payload.get("data", payload)
                        symbol = data.get("s")
                        meta = self.symbol_meta.get(symbol)
                        if meta is None:
                            continue

                        quote = Quote(
                            symbol=symbol,
                            bid=safe_float(data.get("b")),
                            bid_qty=safe_float(data.get("B")),
                            ask=safe_float(data.get("a")),
                            ask_qty=safe_float(data.get("A")),
                            exchange_event_ms=int(data["E"]) if data.get("E") is not None else None,
                            exchange_tx_ms=int(data["T"]) if data.get("T") is not None else None,
                            recv_wall_ns=recv_wall_ns,
                            recv_mono_ns=recv_mono_ns,
                        )
                        if not self._valid_quote(quote):
                            continue

                        self.quotes[symbol] = quote
                        base, _ = meta
                        self.evaluate_base(base, "FUTURES", quote)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[WS] futures error: {exc!r}; reconnect in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def run_fx_ws(self, session: aiohttp.ClientSession) -> None:
        backoff = 1.0
        connected_once = False

        while True:
            try:
                if connected_once:
                    self.recorder.abort_all("FX_RECONNECT")
                    self.fx_quote = None

                async with session.ws_connect(SPOT_FX_WS, heartbeat=30, receive_timeout=90) as ws:
                    connected_once = True
                    backoff = 1.0
                    print("[WS] spot FX connected: USDCUSDT bookTicker")

                    async for msg in ws:
                        recv_wall_ns = time.time_ns()
                        recv_mono_ns = time.monotonic_ns()

                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in {
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                            continue

                        data = json.loads(msg.data)
                        quote = Quote(
                            symbol=data.get("s", "USDCUSDT"),
                            bid=safe_float(data.get("b")),
                            bid_qty=safe_float(data.get("B")),
                            ask=safe_float(data.get("a")),
                            ask_qty=safe_float(data.get("A")),
                            exchange_event_ms=None,
                            exchange_tx_ms=None,
                            recv_wall_ns=recv_wall_ns,
                            recv_mono_ns=recv_mono_ns,
                        )
                        if not self._valid_quote(quote):
                            continue

                        self.fx_quote = quote
                        for base in self.pairs:
                            self.evaluate_base(base, "SPOT_FX", quote)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[WS] spot FX error: {exc!r}; reconnect in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(60)
            initialized = 0
            for legs in self.pairs.values():
                if legs["USDT"] in self.quotes and legs["USDC"] in self.quotes:
                    initialized += 1

            print(
                f"[HEARTBEAT] initialized={initialized}/{len(self.pairs)} "
                f"fx={'yes' if self.fx_quote else 'no'} "
                f"active={len(self.recorder.active)} "
                f"started={self.recorder.started_count} "
                f"closed={self.recorder.closed_count}"
            )

    def evaluate_base(self, base: str, trigger_market: str, trigger_quote: Quote) -> None:
        fx = self.fx_quote
        if fx is None:
            return

        legs = self.pairs[base]
        usdt = self.quotes.get(legs["USDT"])
        usdc = self.quotes.get(legs["USDC"])
        if usdt is None or usdc is None:
            return

        detect_mono_ns = time.monotonic_ns()
        usdt_rich_bps, usdc_rich_bps, fx_mid = calculate_edges_bps(usdt, usdc, fx)
        detect_wall_ns = time.time_ns()

        snapshot = self._snapshot(
            trigger_market=trigger_market,
            trigger_quote=trigger_quote,
            usdt=usdt,
            usdc=usdc,
            fx=fx,
            fx_mid=fx_mid,
            detect_wall_ns=detect_wall_ns,
            detect_mono_ns=detect_mono_ns,
        )

        self.recorder.update(
            base=base,
            direction="USDT_RICH",
            edge_bps=usdt_rich_bps,
            snapshot=snapshot,
        )
        self.recorder.update(
            base=base,
            direction="USDC_RICH",
            edge_bps=usdc_rich_bps,
            snapshot=snapshot,
        )

    @staticmethod
    def _valid_quote(quote: Quote) -> bool:
        return (
            math.isfinite(quote.bid)
            and math.isfinite(quote.ask)
            and quote.bid > 0.0
            and quote.ask > 0.0
            and quote.ask >= quote.bid
        )

    @staticmethod
    def _snapshot(
        *,
        trigger_market: str,
        trigger_quote: Quote,
        usdt: Quote,
        usdc: Quote,
        fx: Quote,
        fx_mid: float,
        detect_wall_ns: int,
        detect_mono_ns: int,
    ) -> dict[str, Any]:
        pair_tx_skew_ms = None
        if usdt.exchange_tx_ms is not None and usdc.exchange_tx_ms is not None:
            pair_tx_skew_ms = abs(usdt.exchange_tx_ms - usdc.exchange_tx_ms)

        pair_recv_skew_ms = abs(usdt.recv_wall_ns - usdc.recv_wall_ns) / 1_000_000.0

        processing_us = max(
            0.0,
            (detect_mono_ns - trigger_quote.recv_mono_ns) / 1_000.0,
        )

        exchange_to_local_ms_est = None
        if trigger_quote.exchange_tx_ms is not None:
            exchange_to_local_ms_est = (
                trigger_quote.recv_wall_ns / 1_000_000.0 - trigger_quote.exchange_tx_ms
            )

        def age_ms(q: Quote) -> float:
            return max(0.0, (trigger_quote.recv_mono_ns - q.recv_mono_ns) / 1_000_000.0)

        return {
            "trigger_market": trigger_market,
            "trigger_symbol": trigger_quote.symbol,
            "trigger_exchange_event_ms": trigger_quote.exchange_event_ms,
            "trigger_exchange_tx_ms": trigger_quote.exchange_tx_ms,
            "local_recv_wall_ns": trigger_quote.recv_wall_ns,
            "local_recv_mono_ns": trigger_quote.recv_mono_ns,
            "local_detect_wall_ns": detect_wall_ns,
            "local_detect_mono_ns": detect_mono_ns,
            "processing_us": processing_us,
            "exchange_to_local_ms_est": exchange_to_local_ms_est,
            "pair_tx_skew_ms": pair_tx_skew_ms,
            "pair_recv_skew_ms": pair_recv_skew_ms,
            "usdt_age_ms": age_ms(usdt),
            "usdc_age_ms": age_ms(usdc),
            "fx_age_ms": age_ms(fx),
            "usdt_symbol": usdt.symbol,
            "usdt_bid": usdt.bid,
            "usdt_bid_qty": usdt.bid_qty,
            "usdt_ask": usdt.ask,
            "usdt_ask_qty": usdt.ask_qty,
            "usdt_exchange_event_ms": usdt.exchange_event_ms,
            "usdt_exchange_tx_ms": usdt.exchange_tx_ms,
            "usdt_recv_wall_ns": usdt.recv_wall_ns,
            "usdc_symbol": usdc.symbol,
            "usdc_bid": usdc.bid,
            "usdc_bid_qty": usdc.bid_qty,
            "usdc_ask": usdc.ask,
            "usdc_ask_qty": usdc.ask_qty,
            "usdc_exchange_event_ms": usdc.exchange_event_ms,
            "usdc_exchange_tx_ms": usdc.exchange_tx_ms,
            "usdc_recv_wall_ns": usdc.recv_wall_ns,
            "fx_symbol": fx.symbol,
            "fx_bid": fx.bid,
            "fx_bid_qty": fx.bid_qty,
            "fx_ask": fx.ask,
            "fx_ask_qty": fx.ask_qty,
            "fx_mid": fx_mid,
            "fx_recv_wall_ns": fx.recv_wall_ns,
        }


async def fetch_exchange_info(session: aiohttp.ClientSession) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with session.get(FUTURES_EXCHANGE_INFO, timeout=timeout) as response:
        response.raise_for_status()
        return await response.json()


async def async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
    headers = {"User-Agent": "quant-test-basis-monitor/0.1"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        exchange_info = await fetch_exchange_info(session)
        pairs = discover_usdt_usdc_pairs(exchange_info)

        if not pairs:
            raise RuntimeError("No active USDT/USDC perpetual pairs were discovered.")

        print(f"[DISCOVERY] active USDT/USDC perpetual pairs: {len(pairs)}")
        for base, legs in pairs.items():
            print(f"  {base:<12} {legs['USDT']:<20} {legs['USDC']}")

        if args.list_pairs:
            return

        monitor = BasisMonitor(
            pairs=pairs,
            threshold_bps=args.threshold_bps,
            output_dir=output_dir,
        )
        metadata_path = monitor.write_run_metadata()
        print(f"[RUN] threshold={args.threshold_bps:.3f} bps")
        print(f"[RUN] metadata={metadata_path}")
        print("[RUN] monitoring only; no orders are sent")

        try:
            await asyncio.gather(
                monitor.run_futures_ws(session),
                monitor.run_fx_ws(session),
                monitor.heartbeat(),
            )
        finally:
            monitor.recorder.abort_all("SHUTDOWN")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor executable BBO dislocations between Binance AUSDT and AUSDC "
            "USDⓈ-M perpetual contracts."
        )
    )
    parser.add_argument(
        "--threshold-bps",
        type=float,
        default=18.0,
        help="Signal threshold in basis points. Default: 18.0",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Directory for event CSV/JSONL files. Default: data",
    )
    parser.add_argument(
        "--list-pairs",
        action="store_true",
        help="Discover and print active paired contracts, then exit.",
    )
    args = parser.parse_args()

    if args.threshold_bps <= 0:
        parser.error("--threshold-bps must be > 0")

    return args


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n[STOP] interrupted by user")


if __name__ == "__main__":
    main()
