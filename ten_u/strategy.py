from __future__ import annotations

from dataclasses import dataclass

from ten_u.config import StrategyConfig
from ten_u.indicators import (
    atr,
    map_higher_timeframe_ema,
    rolling_max_prior,
    rolling_median_prior,
    rolling_min_prior,
    rolling_percentile_prior,
)
from ten_u.models import Candle, Signal, SymbolRules, iso_cn, iso_utc, ms_to_utc


@dataclass(frozen=True)
class FeatureSet:
    atr: list[float | None]
    atr_threshold: list[float | None]
    prior_high: list[float | None]
    prior_low: list[float | None]
    volume_median: list[float | None]
    ema_5_fast: list[float | None]
    ema_5_slow: list[float | None]
    ema_15_fast: list[float | None]
    ema_15_slow: list[float | None]
    ha_open: list[float]
    ha_close: list[float]
    ha_body: list[float]
    ha_range_y: list[float | None]
    ha_psy: list[float | None]
    ha_deviation: list[float | None]


def prepare_features(candles: list[Candle], cfg: StrategyConfig) -> FeatureSet:
    n = len(candles)
    if cfg.signal_model == "manuscript":
        atr_values = atr(candles, cfg.atr_period)
        ha_open, ha_close = double_heikin_ashi(candles)
        ha_body = [abs(c - o) for o, c in zip(ha_open, ha_close, strict=True)]
        ha_range_y, ha_psy = manuscript_range_features(
            ha_open,
            ha_close,
            ha_body,
            cfg.ha_range_window,
        )
        return FeatureSet(
            atr=atr_values,
            atr_threshold=[None] * n,
            prior_high=[None] * n,
            prior_low=[None] * n,
            volume_median=[None] * n,
            ema_5_fast=[None] * n,
            ema_5_slow=[None] * n,
            ema_15_fast=[None] * n,
            ema_15_slow=[None] * n,
            ha_open=ha_open,
            ha_close=ha_close,
            ha_body=ha_body,
            ha_range_y=ha_range_y,
            ha_psy=ha_psy,
            ha_deviation=manuscript_deviation(ha_close, cfg.ha_deviation_window),
        )

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]
    atr_values = atr(candles, cfg.atr_period)
    atr_threshold = rolling_percentile_prior(
        atr_values,
        cfg.atr_compression_window,
        cfg.atr_compression_percentile,
    )
    ema_5_fast, ema_5_slow = map_higher_timeframe_ema(
        candles,
        5,
        cfg.trend_fast,
        cfg.trend_slow,
    )
    ema_15_fast, ema_15_slow = map_higher_timeframe_ema(
        candles,
        15,
        cfg.trend_fast,
        cfg.trend_slow,
    )
    return FeatureSet(
        atr=atr_values,
        atr_threshold=atr_threshold,
        prior_high=rolling_max_prior(highs, cfg.donchian_window),
        prior_low=rolling_min_prior(lows, cfg.donchian_window),
        volume_median=rolling_median_prior(volumes, cfg.volume_window),
        ema_5_fast=ema_5_fast,
        ema_5_slow=ema_5_slow,
        ema_15_fast=ema_15_fast,
        ema_15_slow=ema_15_slow,
        ha_open=[0.0] * n,
        ha_close=[0.0] * n,
        ha_body=[0.0] * n,
        ha_range_y=[None] * n,
        ha_psy=[None] * n,
        ha_deviation=[None] * n,
    )


class StrategyEngine:
    def __init__(
        self,
        symbol: str,
        candles: list[Candle],
        cfg: StrategyConfig,
        rules: SymbolRules | None = None,
    ) -> None:
        self.symbol = symbol
        self.candles = candles
        self.cfg = cfg
        self.rules = rules
        self.features = prepare_features(candles, cfg)

    def evaluate(self, index: int) -> Signal | None:
        return evaluate_signal(
            self.symbol,
            self.candles,
            self.features,
            index,
            self.cfg,
            self.rules,
        )


def evaluate_signal(
    symbol: str,
    candles: list[Candle],
    features: FeatureSet,
    index: int,
    cfg: StrategyConfig,
    rules: SymbolRules | None = None,
) -> Signal | None:
    if cfg.signal_model == "manuscript":
        return evaluate_manuscript_signal(symbol, candles, features, index, cfg, rules)
    if cfg.signal_model != "breakout":
        raise ValueError("signal_model must be 'manuscript' or 'breakout'")
    return evaluate_breakout_signal(symbol, candles, features, index, cfg, rules)


def evaluate_breakout_signal(
    symbol: str,
    candles: list[Candle],
    features: FeatureSet,
    index: int,
    cfg: StrategyConfig,
    rules: SymbolRules | None = None,
) -> Signal | None:
    if index <= 0 or index >= len(candles):
        return None
    candle = candles[index]
    close = candle.close
    if close <= 0:
        return None
    required = [
        features.atr[index],
        features.atr_threshold[index],
        features.prior_high[index],
        features.prior_low[index],
        features.volume_median[index],
        features.ema_5_fast[index],
        features.ema_5_slow[index],
        features.ema_15_fast[index],
        features.ema_15_slow[index],
    ]
    if any(v is None for v in required):
        return None

    current_atr = float(features.atr[index] or 0.0)
    atr_threshold = float(features.atr_threshold[index] or 0.0)
    prior_high = float(features.prior_high[index] or 0.0)
    prior_low = float(features.prior_low[index] or 0.0)
    volume_median = float(features.volume_median[index] or 0.0)
    if current_atr <= 0 or atr_threshold <= 0 or volume_median <= 0:
        return None

    trend_up = (
        float(features.ema_5_fast[index] or 0.0) > float(features.ema_5_slow[index] or 0.0)
        and float(features.ema_15_fast[index] or 0.0) > float(features.ema_15_slow[index] or 0.0)
    )
    trend_down = (
        float(features.ema_5_fast[index] or 0.0) < float(features.ema_5_slow[index] or 0.0)
        and float(features.ema_15_fast[index] or 0.0) < float(features.ema_15_slow[index] or 0.0)
    )
    volume_surge = candle.volume >= volume_median * cfg.volume_multiple
    compressed_recently = _compressed_recently(features.atr, atr_threshold, index)
    taker_ratio = candle.taker_buy_ratio

    long_parts = {
        "TREND_5M_15M_UP": trend_up,
        "DONCHIAN_BREAKOUT_UP": close > prior_high,
        "VOLUME_SURGE": volume_surge,
        "TAKER_BUY_DOMINANT": taker_ratio >= cfg.taker_ratio_long,
        "ATR_COMPRESSED": compressed_recently,
    }
    short_parts = {
        "TREND_5M_15M_DOWN": trend_down,
        "DONCHIAN_BREAKOUT_DOWN": close < prior_low,
        "VOLUME_SURGE": volume_surge,
        "TAKER_SELL_DOMINANT": taker_ratio <= cfg.taker_ratio_short,
        "ATR_COMPRESSED": compressed_recently,
    }

    long_score = _score(long_parts)
    short_score = _score(short_parts)
    side = None
    parts: dict[str, bool] | None = None
    score = 0.0
    if all(long_parts.values()) and long_score >= cfg.score_threshold:
        side = "LONG"
        parts = long_parts
        score = long_score
    if all(short_parts.values()) and short_score >= cfg.score_threshold and short_score > score:
        side = "SHORT"
        parts = short_parts
        score = short_score
    if side is None or parts is None:
        return None

    leverage = choose_leverage(close, current_atr, cfg)
    if leverage is None:
        return None
    take_profit_price, stop_price = exit_prices(close, side, leverage, cfg, cfg.max_loss_usdt)
    if rules is not None:
        take_profit_price = rules.round_price(take_profit_price)
        stop_price = rules.round_price(stop_price)
    expires_at_ms = candle.close_time + cfg.max_hold_minutes * 60_000
    return Signal(
        time_utc=iso_utc(candle.close_time),
        time_cn=iso_cn(candle.close_time),
        symbol=symbol,
        side=side,
        leverage=leverage,
        margin_usdt=cfg.margin_usdt,
        entry_reference=rules.round_price(close) if rules is not None else close,
        take_profit_price=take_profit_price,
        stop_price=stop_price,
        target_pnl=cfg.target_profit_usdt,
        max_loss=cfg.max_loss_usdt,
        expires_at=ms_to_utc(expires_at_ms).isoformat(),
        score=score,
        reason_codes=tuple(k for k, ok in parts.items() if ok),
        strategy_variant="breakout",
    )


def evaluate_manuscript_signal(
    symbol: str,
    candles: list[Candle],
    features: FeatureSet,
    index: int,
    cfg: StrategyConfig,
    rules: SymbolRules | None = None,
) -> Signal | None:
    if index <= 0 or index >= len(candles):
        return None
    candle = candles[index]
    close = candle.close
    if close <= 0:
        return None
    current_atr = features.atr[index]
    range_y = features.ha_range_y[index]
    psy = features.ha_psy[index]
    deviation = features.ha_deviation[index]
    if current_atr is None or range_y is None or psy is None or deviation is None:
        return None
    if current_atr <= 0:
        return None

    ha_bull = features.ha_close[index] >= features.ha_open[index]
    long_trend = (
        range_y >= cfg.ha_range_y_threshold
        and psy >= cfg.ha_psy_threshold
        and ha_bull
    )
    short_trend = (
        range_y <= -cfg.ha_range_y_threshold
        and psy <= 1 - cfg.ha_psy_threshold
        and not ha_bull
    )
    long_momentum = deviation >= cfg.ha_deviation_threshold
    short_momentum = deviation <= -cfg.ha_deviation_threshold

    long_parts = {
        "HA_RANGE_Y_LONG": long_trend,
        "HA_PSY_LONG": psy >= cfg.ha_psy_threshold,
        "HA_MEAN_DEVIATION_UP": long_momentum,
        "HA_CANDLE_UP": ha_bull,
    }
    short_parts = {
        "HA_RANGE_Y_SHORT": short_trend,
        "HA_PSY_SHORT": psy <= 1 - cfg.ha_psy_threshold,
        "HA_MEAN_DEVIATION_DOWN": short_momentum,
        "HA_CANDLE_DOWN": not ha_bull,
    }

    long_score = _manuscript_score(long_parts, trend_ok=long_trend, momentum_ok=long_momentum)
    short_score = _manuscript_score(short_parts, trend_ok=short_trend, momentum_ok=short_momentum)
    side = None
    parts: dict[str, bool] | None = None
    score = 0.0
    if (long_trend or long_momentum) and long_score >= cfg.ha_score_threshold:
        side = "LONG"
        parts = long_parts
        score = long_score
    if (short_trend or short_momentum) and short_score >= cfg.ha_score_threshold and short_score > score:
        side = "SHORT"
        parts = short_parts
        score = short_score
    if side is None or parts is None:
        return None

    leverage = choose_leverage(close, float(current_atr), cfg)
    if leverage is None:
        return None
    take_profit_price, stop_price = exit_prices(close, side, leverage, cfg, cfg.max_loss_usdt)
    if rules is not None:
        take_profit_price = rules.round_price(take_profit_price)
        stop_price = rules.round_price(stop_price)
    expires_at_ms = candle.close_time + cfg.max_hold_minutes * 60_000
    reason_codes = [k for k, ok in parts.items() if ok]
    reason_codes.extend(
        [
            f"RANGE_Y={range_y:.2f}",
            f"PSY={psy:.2f}",
            f"DEV={deviation:.5f}",
        ]
    )
    return Signal(
        time_utc=iso_utc(candle.close_time),
        time_cn=iso_cn(candle.close_time),
        symbol=symbol,
        side=side,
        leverage=leverage,
        margin_usdt=cfg.margin_usdt,
        entry_reference=rules.round_price(close) if rules is not None else close,
        take_profit_price=take_profit_price,
        stop_price=stop_price,
        target_pnl=cfg.target_profit_usdt,
        max_loss=cfg.max_loss_usdt,
        expires_at=ms_to_utc(expires_at_ms).isoformat(),
        score=score,
        reason_codes=tuple(reason_codes),
        strategy_variant="manuscript",
    )


def _score(parts: dict[str, bool]) -> float:
    weights = {
        "TREND_5M_15M_UP": 30.0,
        "TREND_5M_15M_DOWN": 30.0,
        "DONCHIAN_BREAKOUT_UP": 25.0,
        "DONCHIAN_BREAKOUT_DOWN": 25.0,
        "VOLUME_SURGE": 20.0,
        "TAKER_BUY_DOMINANT": 15.0,
        "TAKER_SELL_DOMINANT": 15.0,
        "ATR_COMPRESSED": 10.0,
    }
    return sum(weights[name] for name, ok in parts.items() if ok)


def _manuscript_score(parts: dict[str, bool], trend_ok: bool, momentum_ok: bool) -> float:
    score = 0.0
    if trend_ok:
        score += 60.0
    if momentum_ok:
        score += 40.0
    if parts.get("HA_CANDLE_UP") or parts.get("HA_CANDLE_DOWN"):
        score += 10.0
    if parts.get("HA_PSY_LONG") or parts.get("HA_PSY_SHORT"):
        score += 10.0
    return min(score, 100.0)


def _compressed_recently(
    atr_values: list[float | None],
    threshold: float,
    index: int,
    lookback: int = 5,
) -> bool:
    start = max(0, index - lookback)
    values = [v for v in atr_values[start:index] if v is not None]
    if not values:
        return False
    return min(values) <= threshold


def choose_leverage(price: float, current_atr: float, cfg: StrategyConfig) -> int | None:
    if price <= 0 or current_atr <= 0:
        return None
    expected_move_pct = (current_atr * (cfg.max_hold_minutes**0.5)) / price
    atr_pct = current_atr / price
    for leverage in range(cfg.min_leverage, cfg.max_leverage + 1):
        tp_pct = cfg.target_profit_usdt / (cfg.margin_usdt * leverage)
        sl_pct = cfg.max_loss_usdt / (cfg.margin_usdt * leverage)
        target_reachable = expected_move_pct >= tp_pct * cfg.min_expected_move_mult
        stop_not_noise = sl_pct >= atr_pct * cfg.min_stop_atr_mult
        if target_reachable and stop_not_noise:
            return leverage
    return None


def exit_prices(
    entry_price: float,
    side: str,
    leverage: int,
    cfg: StrategyConfig,
    max_loss_usdt: float,
) -> tuple[float, float]:
    tp_pct = cfg.target_profit_usdt / (cfg.margin_usdt * leverage)
    sl_pct = max_loss_usdt / (cfg.margin_usdt * leverage)
    if side == "LONG":
        return entry_price * (1 + tp_pct), entry_price * (1 - sl_pct)
    if side == "SHORT":
        return entry_price * (1 - tp_pct), entry_price * (1 + sl_pct)
    raise ValueError("side must be LONG or SHORT")


def double_heikin_ashi(candles: list[Candle]) -> tuple[list[float], list[float]]:
    if not candles:
        return [], []
    ha_open_1: list[float] = []
    ha_close_1: list[float] = []
    for i, candle in enumerate(candles):
        close_1 = (candle.open + candle.high + candle.low + candle.close) / 4
        if i == 0:
            open_1 = (candle.open + candle.close) / 2
        else:
            open_1 = (ha_open_1[-1] + ha_close_1[-1]) / 2
        ha_open_1.append(open_1)
        ha_close_1.append(close_1)

    ha_open_2: list[float] = []
    ha_close_2: list[float] = []
    for i, candle in enumerate(candles):
        close_2 = (ha_open_1[i] + candle.high + candle.low + ha_close_1[i]) / 4
        if i == 0:
            open_2 = (ha_open_1[i] + ha_close_1[i]) / 2
        else:
            open_2 = (ha_open_2[-1] + ha_close_2[-1]) / 2
        ha_open_2.append(open_2)
        ha_close_2.append(close_2)
    return ha_open_2, ha_close_2


def manuscript_range_features(
    ha_open: list[float],
    ha_close: list[float],
    ha_body: list[float],
    window: int,
) -> tuple[list[float | None], list[float | None]]:
    if window <= 0:
        raise ValueError("window must be positive")
    range_y: list[float | None] = [None] * len(ha_close)
    psy: list[float | None] = [None] * len(ha_close)
    up_prefix = [0.0]
    down_prefix = [0.0]
    count_prefix = [0]
    for open_, close, body in zip(ha_open, ha_close, ha_body, strict=True):
        is_up = close >= open_
        up_prefix.append(up_prefix[-1] + (body if is_up else 0.0))
        down_prefix.append(down_prefix[-1] + (0.0 if is_up else body))
        count_prefix.append(count_prefix[-1] + (1 if is_up else 0))
    for i in range(window, len(ha_close)):
        up_range = up_prefix[i] - up_prefix[i - window]
        down_range = down_prefix[i] - down_prefix[i - window]
        up_count = count_prefix[i] - count_prefix[i - window]
        total_range = up_range + down_range
        if total_range <= 0:
            range_y[i] = 0.0
        else:
            range_y[i] = (up_range - down_range) / total_range * 100
        psy[i] = up_count / window
    return range_y, psy


def manuscript_deviation(values: list[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    for i in range(window - 1, len(values)):
        avg = (prefix[i + 1] - prefix[i + 1 - window]) / window
        if avg != 0:
            out[i] = values[i] / avg - 1
    return out
