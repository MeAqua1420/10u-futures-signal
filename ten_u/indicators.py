from __future__ import annotations

from bisect import bisect_right
from statistics import median

from ten_u.models import Candle


MaybeFloat = float | None


def ema(values: list[float], period: int) -> list[MaybeFloat]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[MaybeFloat] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


def true_ranges(candles: list[Candle]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for candle in candles:
        if prev_close is None:
            out.append(candle.high - candle.low)
        else:
            out.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - prev_close),
                    abs(candle.low - prev_close),
                )
            )
        prev_close = candle.close
    return out


def atr(candles: list[Candle], period: int) -> list[MaybeFloat]:
    tr = true_ranges(candles)
    return ema(tr, period)


def rolling_median_prior(values: list[float], window: int) -> list[MaybeFloat]:
    out: list[MaybeFloat] = [None] * len(values)
    for i in range(window, len(values)):
        out[i] = median(values[i - window : i])
    return out


def rolling_max_prior(values: list[float], window: int) -> list[MaybeFloat]:
    out: list[MaybeFloat] = [None] * len(values)
    for i in range(window, len(values)):
        out[i] = max(values[i - window : i])
    return out


def rolling_min_prior(values: list[float], window: int) -> list[MaybeFloat]:
    out: list[MaybeFloat] = [None] * len(values)
    for i in range(window, len(values)):
        out[i] = min(values[i - window : i])
    return out


def percentile(values: list[float], pct: float) -> MaybeFloat:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if pct <= 0:
        return clean[0]
    if pct >= 100:
        return clean[-1]
    rank = (len(clean) - 1) * (pct / 100)
    lower = int(rank)
    upper = min(lower + 1, len(clean) - 1)
    frac = rank - lower
    return clean[lower] * (1 - frac) + clean[upper] * frac


def rolling_percentile_prior(values: list[MaybeFloat], window: int, pct: float) -> list[MaybeFloat]:
    out: list[MaybeFloat] = [None] * len(values)
    for i in range(window, len(values)):
        window_values = [v for v in values[i - window : i] if v is not None]
        out[i] = percentile(window_values, pct)
    return out


def resample_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    bucket_ms = minutes * 60_000
    buckets: list[Candle] = []
    current_key: int | None = None
    current: dict[str, float | int] | None = None
    for candle in candles:
        key = (candle.open_time // bucket_ms) * bucket_ms
        if current_key is None or key != current_key:
            if current is not None:
                buckets.append(
                    Candle(
                        open_time=int(current["open_time"]),
                        open=float(current["open"]),
                        high=float(current["high"]),
                        low=float(current["low"]),
                        close=float(current["close"]),
                        volume=float(current["volume"]),
                        close_time=int(current["close_time"]),
                        quote_volume=float(current["quote_volume"]),
                        trades=int(current["trades"]),
                        taker_buy_base=float(current["taker_buy_base"]),
                        taker_buy_quote=float(current["taker_buy_quote"]),
                    )
                )
            current_key = key
            current = {
                "open_time": candle.open_time,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "close_time": candle.close_time,
                "quote_volume": candle.quote_volume,
                "trades": candle.trades,
                "taker_buy_base": candle.taker_buy_base,
                "taker_buy_quote": candle.taker_buy_quote,
            }
            continue
        assert current is not None
        current["high"] = max(float(current["high"]), candle.high)
        current["low"] = min(float(current["low"]), candle.low)
        current["close"] = candle.close
        current["volume"] = float(current["volume"]) + candle.volume
        current["close_time"] = candle.close_time
        current["quote_volume"] = float(current["quote_volume"]) + candle.quote_volume
        current["trades"] = int(current["trades"]) + candle.trades
        current["taker_buy_base"] = float(current["taker_buy_base"]) + candle.taker_buy_base
        current["taker_buy_quote"] = float(current["taker_buy_quote"]) + candle.taker_buy_quote
    if current is not None:
        buckets.append(
            Candle(
                open_time=int(current["open_time"]),
                open=float(current["open"]),
                high=float(current["high"]),
                low=float(current["low"]),
                close=float(current["close"]),
                volume=float(current["volume"]),
                close_time=int(current["close_time"]),
                quote_volume=float(current["quote_volume"]),
                trades=int(current["trades"]),
                taker_buy_base=float(current["taker_buy_base"]),
                taker_buy_quote=float(current["taker_buy_quote"]),
            )
        )
    return buckets


def map_higher_timeframe_ema(
    candles: list[Candle],
    minutes: int,
    fast_period: int,
    slow_period: int,
) -> tuple[list[MaybeFloat], list[MaybeFloat]]:
    higher = resample_candles(candles, minutes)
    closes = [c.close for c in higher]
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    higher_close_times = [c.close_time for c in higher]
    mapped_fast: list[MaybeFloat] = [None] * len(candles)
    mapped_slow: list[MaybeFloat] = [None] * len(candles)
    for i, candle in enumerate(candles):
        pos = bisect_right(higher_close_times, candle.close_time) - 1
        if pos >= 0:
            mapped_fast[i] = fast[pos]
            mapped_slow[i] = slow[pos]
    return mapped_fast, mapped_slow
