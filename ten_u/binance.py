from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ten_u.models import Candle, SymbolRules


BASE_URL = "https://fapi.binance.com"
CACHE_DIR = Path("data/cache")


class BinanceClient:
    def __init__(self, base_url: str = BASE_URL, timeout: int = 20, retries: int = 4) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        req = Request(url, headers={"User-Agent": "ten-u-signal/0.1"})
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in {418, 429} or 500 <= exc.code < 600:
                    last_error = exc
                else:
                    body = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"Binance HTTP {exc.code}: {body}") from exc
            except URLError as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(min(2.0, 0.25 * (2**attempt)))
        if isinstance(last_error, HTTPError):
            body = last_error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance HTTP {last_error.code}: {body}") from last_error
        if isinstance(last_error, URLError):
            raise RuntimeError(f"Binance request failed: {last_error.reason}") from last_error
        raise RuntimeError("Binance request failed")

    def exchange_info(self) -> dict[str, Any]:
        return self.request_json("/fapi/v1/exchangeInfo")

    def ticker_24hr(self) -> list[dict[str, Any]]:
        data = self.request_json("/fapi/v1/ticker/24hr")
        if not isinstance(data, list):
            raise RuntimeError("unexpected 24hr ticker response")
        return data

    def klines(
        self,
        symbol: str,
        interval: str = "1m",
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1500,
    ) -> list[Candle]:
        params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        rows = self.request_json("/fapi/v1/klines", params)
        if not isinstance(rows, list):
            raise RuntimeError("unexpected kline response")
        return [Candle.from_binance(row) for row in rows]

    def fetch_klines_range(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        sleep_seconds: float = 0.08,
    ) -> list[Candle]:
        out: list[Candle] = []
        cursor = start_time
        while cursor < end_time:
            batch = self.klines(symbol, interval, start_time=cursor, end_time=end_time, limit=1500)
            if not batch:
                break
            out.extend(batch)
            next_cursor = batch[-1].open_time + interval_to_ms(interval)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1500:
                break
            time.sleep(sleep_seconds)
        return dedupe_candles(out)

    def symbol_rules(self) -> dict[str, SymbolRules]:
        info = self.exchange_info()
        rules: dict[str, SymbolRules] = {}
        for raw in info.get("symbols", []):
            if raw.get("contractType") != "PERPETUAL" or raw.get("quoteAsset") != "USDT":
                continue
            filters = {f.get("filterType"): f for f in raw.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            price = filters.get("PRICE_FILTER", {})
            min_notional_filter = filters.get("MIN_NOTIONAL", {})
            min_notional = float(min_notional_filter.get("notional", 0.0) or 0.0)
            symbol = raw["symbol"]
            rules[symbol] = SymbolRules(
                symbol=symbol,
                price_precision=int(raw.get("pricePrecision", 8)),
                quantity_precision=int(raw.get("quantityPrecision", 8)),
                tick_size=float(price.get("tickSize", 0.0) or 0.0),
                step_size=float(lot.get("stepSize", 0.0) or 0.0),
                min_qty=float(lot.get("minQty", 0.0) or 0.0),
                min_notional=min_notional,
                trigger_protect=float(raw.get("triggerProtect", 0.0) or 0.0),
            )
        return rules

    def top_usdt_perpetual_symbols(
        self,
        top: int = 60,
        min_quote_volume: float = 50_000_000,
    ) -> list[str]:
        info = self.exchange_info()
        tradable = {
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
        }
        tickers = self.ticker_24hr()
        ranked = []
        for ticker in tickers:
            symbol = ticker.get("symbol")
            if symbol not in tradable:
                continue
            quote_volume = float(ticker.get("quoteVolume", 0.0) or 0.0)
            if quote_volume < min_quote_volume:
                continue
            ranked.append((quote_volume, symbol))
        ranked.sort(reverse=True)
        return [symbol for _, symbol in ranked[:top]]


def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    if unit == "m":
        return value * 60_000
    if unit == "h":
        return value * 60 * 60_000
    if unit == "d":
        return value * 24 * 60 * 60_000
    raise ValueError(f"unsupported interval: {interval}")


def dedupe_candles(candles: list[Candle]) -> list[Candle]:
    by_time = {c.open_time: c for c in candles}
    return [by_time[t] for t in sorted(by_time)]


def cache_path(symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{interval}.csv"


CSV_FIELDS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
]


def read_cached_klines(symbol: str, interval: str = "1m") -> list[Candle]:
    path = cache_path(symbol, interval)
    if not path.exists():
        return []
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        return [Candle.from_csv_row(row) for row in reader]


def write_cached_klines(symbol: str, interval: str, candles: list[Candle]) -> None:
    path = cache_path(symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_FIELDS)
        for candle in candles:
            writer.writerow(candle.to_csv_row())


def load_or_fetch_klines(
    client: BinanceClient,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    refresh: bool = False,
) -> list[Candle]:
    cached = [] if refresh else read_cached_klines(symbol, interval)
    interval_ms = interval_to_ms(interval)
    needed: list[Candle] = []
    if cached:
        cached = [c for c in cached if start_time <= c.open_time <= end_time]
        if cached and cached[0].open_time <= start_time + interval_ms and cached[-1].open_time >= end_time - interval_ms:
            return cached
        needed.extend(cached)
    fetched: list[Candle] = []
    if not cached:
        fetched.extend(client.fetch_klines_range(symbol, interval, start_time, end_time))
    else:
        first_open = cached[0].open_time
        last_open = cached[-1].open_time
        if first_open > start_time + interval_ms:
            fetched.extend(client.fetch_klines_range(symbol, interval, start_time, first_open - interval_ms))
        if last_open < end_time - interval_ms:
            fetched.extend(client.fetch_klines_range(symbol, interval, last_open + interval_ms, end_time))
    merged = dedupe_candles(needed + fetched)
    write_cached_klines(symbol, interval, merged)
    return [c for c in merged if start_time <= c.open_time <= end_time]
