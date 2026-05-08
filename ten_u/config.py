from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from typing import Iterable


@dataclass(frozen=True)
class CostConfig:
    taker_fee_rate: float = 0.0005
    slippage_rate: float = 0.0003


@dataclass(frozen=True)
class StrategyConfig:
    signal_model: str = "manuscript"
    margin_usdt: float = 10.0
    target_profit_usdt: float = 5.0
    max_loss_usdt: float = 2.0
    legacy_max_loss_usdt: float = 10.0
    max_hold_minutes: int = 240
    max_hold_seconds: int = 0
    candle_interval_seconds: int = 60
    min_leverage: int = 3
    max_leverage: int = 15
    donchian_window: int = 30
    volume_window: int = 20
    volume_multiple: float = 1.8
    taker_ratio_long: float = 0.58
    taker_ratio_short: float = 0.42
    score_threshold: float = 80.0
    atr_period: int = 14
    atr_compression_window: int = 120
    atr_compression_percentile: float = 40.0
    trend_fast: int = 12
    trend_slow: int = 36
    min_expected_move_mult: float = 1.05
    min_stop_atr_mult: float = 1.20
    min_liquidity_quote_volume: float = 50_000_000.0
    pool_size: int = 60
    ha_range_window: int = 30
    ha_range_y_threshold: float = 30.0
    ha_psy_threshold: float = 0.30
    ha_deviation_window: int = 5
    ha_deviation_threshold: float = 0.0015
    ha_score_threshold: float = 100.0
    direction_window: int = 0
    min_direction_change_pct: float = 0.0
    atr_min_percentile: float = 35.0
    atr_max_percentile: float = 95.0
    micro_momentum_fast: int = 3
    micro_momentum_mid: int = 15
    micro_momentum_slow: int = 30
    micro_volume_burst_seconds: int = 3
    micro_body_ratio_min: float = 0.55
    micro_wick_ratio_max: float = 0.35
    min_target_net_usdt: float = 0.05
    estimated_round_trip_cost_rate: float = 0.0016
    us_nonworkday_only: bool = False

    def with_updates(self, **kwargs: float | int | str) -> "StrategyConfig":
        return replace(self, **kwargs)


@dataclass(frozen=True)
class BacktestConfig:
    interval: str = "1m"
    train_ratio: float = 0.60
    validation_ratio: float = 0.20
    oos_ratio: float = 0.20
    min_oos_trades: int = 100
    min_oos_win_rate: float = 0.60
    min_profit_factor: float = 1.20
    min_expectancy: float = 0.0
    random_seed: int = 42


def parameter_grid(mode: str = "quick", signal_model: str = "breakout") -> Iterable[dict[str, float | int]]:
    """Yield the approved optimization grid.

    The quick grid is intentionally tiny for smoke tests. The full grid follows
    the plan exactly and can be expensive on 12 months x 60 symbols of 1m data.
    """
    if signal_model == "microburst":
        if mode == "quick":
            keys = [
                "donchian_window",
                "volume_multiple",
                "score_threshold",
                "max_leverage",
                "max_hold_seconds",
            ]
            values = [
                [10, 20, 30],
                [1.5, 2.0],
                [70, 80],
                [40, 50],
                [60, 180],
            ]
            for combo in product(*values):
                yield dict(zip(keys, combo, strict=True))
            return
        if mode != "full":
            raise ValueError("grid mode must be 'quick' or 'full'")
        keys = [
            "donchian_window",
            "volume_multiple",
            "atr_min_percentile",
            "atr_max_percentile",
            "score_threshold",
            "max_leverage",
            "max_hold_seconds",
        ]
        values = [
            [10, 20, 30],
            [1.5, 2.0, 3.0],
            [35, 50],
            [85, 95],
            [70, 80, 90],
            [40, 50, 55],
            [30, 60, 180],
        ]
        for combo in product(*values):
            yield dict(zip(keys, combo, strict=True))
        return

    if signal_model == "manuscript":
        if mode == "quick":
            yield {
                "max_loss_usdt": 2.0,
                "ha_range_window": 30,
                "ha_range_y_threshold": 30,
                "ha_psy_threshold": 0.30,
                "ha_deviation_threshold": 0.0015,
                "ha_score_threshold": 100,
                "max_leverage": 15,
            }
            return
        if mode != "full":
            raise ValueError("grid mode must be 'quick' or 'full'")
        keys = [
            "max_loss_usdt",
            "ha_range_window",
            "ha_range_y_threshold",
            "ha_psy_threshold",
            "ha_deviation_threshold",
            "ha_score_threshold",
            "max_leverage",
        ]
        values = [
            [1.0, 2.0],
            [30, 45],
            [30, 50],
            [0.30, 0.40],
            [0.0015, 0.0025],
            [80, 100],
            [15],
        ]
        for combo in product(*values):
            yield dict(zip(keys, combo, strict=True))
        return

    if mode == "quick":
        yield {
            "donchian_window": 30,
            "volume_multiple": 1.8,
            "score_threshold": 80,
            "atr_compression_percentile": 40,
            "max_leverage": 20,
        }
        return
    if mode != "full":
        raise ValueError("grid mode must be 'quick' or 'full'")
    keys = [
        "donchian_window",
        "volume_multiple",
        "score_threshold",
        "atr_compression_percentile",
        "max_leverage",
    ]
    values = [
        [20, 30, 45],
        [1.5, 1.8, 2.2],
        [75, 80, 85],
        [30, 40, 50],
        [15, 20],
    ]
    for combo in product(*values):
        yield dict(zip(keys, combo, strict=True))
