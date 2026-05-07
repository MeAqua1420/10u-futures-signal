from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from ten_u.binance import BinanceClient
from ten_u.config import StrategyConfig
from ten_u.models import Candle, Signal, SymbolRules
from ten_u.strategy import StrategyEngine


def select_best_signal(
    symbol_candles: dict[str, list[Candle]],
    cfg: StrategyConfig,
    rules: dict[str, SymbolRules] | None = None,
    last_signal_exit_ms: int | None = None,
) -> Signal | None:
    best: Signal | None = None
    for symbol, candles in symbol_candles.items():
        if len(candles) < 400:
            continue
        engine = StrategyEngine(symbol, candles, cfg, None if rules is None else rules.get(symbol))
        signal = engine.evaluate(len(candles) - 1)
        if signal is None:
            continue
        if last_signal_exit_ms is not None and candles[-1].close_time <= last_signal_exit_ms:
            continue
        if best is None or signal.score > best.score:
            best = signal
    return best


def print_signal(signal: Signal) -> None:
    print(json.dumps(signal.as_dict(), ensure_ascii=False, separators=(",", ":")))


def rest_polling_scanner(
    client: BinanceClient,
    symbols: Iterable[str],
    cfg: StrategyConfig,
    lookback: int = 720,
    poll_seconds: int = 15,
) -> None:
    rules = client.symbol_rules()
    active_until_ms: int | None = None
    last_emitted_key: tuple[str, str, str] | None = None
    symbol_list = list(symbols)
    print(f"REST scanner started for {len(symbol_list)} symbols at {datetime.now(UTC).isoformat()}", flush=True)
    while True:
        symbol_candles: dict[str, list[Candle]] = {}
        for symbol in symbol_list:
            candles = client.klines(symbol, "1m", limit=lookback)
            if candles and candles[-1].close_time > int(time.time() * 1000):
                candles = candles[:-1]
            symbol_candles[symbol] = candles
        signal = select_best_signal(symbol_candles, cfg, rules, active_until_ms)
        if signal is not None:
            key = (signal.symbol, signal.side, signal.time_utc)
            if key != last_emitted_key:
                print_signal(signal)
                last_emitted_key = key
                active_until_ms = int(datetime.fromisoformat(signal.expires_at).timestamp() * 1000)
        time.sleep(poll_seconds)


async def websocket_scanner(
    symbols: Iterable[str],
    cfg: StrategyConfig,
    bootstrap_client: BinanceClient,
    lookback: int = 720,
) -> None:
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError:
        await asyncio.to_thread(
            _websocket_client_loop,
            list(symbols),
            cfg,
            bootstrap_client,
            lookback,
        )
        return

    symbol_list = [s.lower() for s in symbols]
    rules = bootstrap_client.symbol_rules()
    buffers: dict[str, list[Candle]] = {}
    for symbol in (s.upper() for s in symbol_list):
        buffers[symbol] = bootstrap_client.klines(symbol, "1m", limit=lookback)
    streams = "/".join(f"{s}@kline_1m" for s in symbol_list)
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    active_until_ms: int | None = None
    last_emitted_key: tuple[str, str, str] | None = None
    print(f"WebSocket scanner started for {len(symbol_list)} symbols", flush=True)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                async for message in ws:
                    payload = json.loads(message)
                    data = payload.get("data", {})
                    kline = data.get("k", {})
                    if not kline.get("x"):
                        continue
                    symbol = str(kline["s"]).upper()
                    candle = Candle(
                        open_time=int(kline["t"]),
                        open=float(kline["o"]),
                        high=float(kline["h"]),
                        low=float(kline["l"]),
                        close=float(kline["c"]),
                        volume=float(kline["v"]),
                        close_time=int(kline["T"]),
                        quote_volume=float(kline["q"]),
                        trades=int(kline["n"]),
                        taker_buy_base=float(kline["V"]),
                        taker_buy_quote=float(kline["Q"]),
                    )
                    current = buffers.setdefault(symbol, [])
                    if current and current[-1].open_time == candle.open_time:
                        current[-1] = candle
                    else:
                        current.append(candle)
                    buffers[symbol] = current[-lookback:]
                    signal = select_best_signal(buffers, cfg, rules, active_until_ms)
                    if signal is None:
                        continue
                    key = (signal.symbol, signal.side, signal.time_utc)
                    if key == last_emitted_key:
                        continue
                    print_signal(signal)
                    last_emitted_key = key
                    active_until_ms = int(datetime.fromisoformat(signal.expires_at).timestamp() * 1000)
        except Exception as exc:
            print(f"WebSocket scanner reconnecting after error: {exc}", flush=True)
            await asyncio.sleep(5)


def _websocket_client_loop(
    symbols: list[str],
    cfg: StrategyConfig,
    bootstrap_client: BinanceClient,
    lookback: int,
) -> None:
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError("websocket mode requires websocket-client or websockets") from exc

    symbol_list = [s.lower() for s in symbols]
    rules = bootstrap_client.symbol_rules()
    buffers: dict[str, list[Candle]] = {}
    for symbol in (s.upper() for s in symbol_list):
        buffers[symbol] = bootstrap_client.klines(symbol, "1m", limit=lookback)
    streams = "/".join(f"{s}@kline_1m" for s in symbol_list)
    url = f"wss://fstream.binance.com/stream?streams={streams}"
    active_until_ms: int | None = None
    last_emitted_key: tuple[str, str, str] | None = None
    print(f"WebSocket scanner started for {len(symbol_list)} symbols", flush=True)
    while True:
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=30)
            while True:
                raw = ws.recv()
                payload = json.loads(raw)
                data = payload.get("data", {})
                kline = data.get("k", {})
                if not kline.get("x"):
                    continue
                symbol = str(kline["s"]).upper()
                candle = Candle(
                    open_time=int(kline["t"]),
                    open=float(kline["o"]),
                    high=float(kline["h"]),
                    low=float(kline["l"]),
                    close=float(kline["c"]),
                    volume=float(kline["v"]),
                    close_time=int(kline["T"]),
                    quote_volume=float(kline["q"]),
                    trades=int(kline["n"]),
                    taker_buy_base=float(kline["V"]),
                    taker_buy_quote=float(kline["Q"]),
                )
                current = buffers.setdefault(symbol, [])
                if current and current[-1].open_time == candle.open_time:
                    current[-1] = candle
                else:
                    current.append(candle)
                buffers[symbol] = current[-lookback:]
                signal = select_best_signal(buffers, cfg, rules, active_until_ms)
                if signal is None:
                    continue
                key = (signal.symbol, signal.side, signal.time_utc)
                if key == last_emitted_key:
                    continue
                print_signal(signal)
                last_emitted_key = key
                active_until_ms = int(datetime.fromisoformat(signal.expires_at).timestamp() * 1000)
        except Exception as exc:
            print(f"WebSocket scanner reconnecting after error: {exc}", flush=True)
            time.sleep(5)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
