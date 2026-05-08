from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable

from ten_u.config import BacktestConfig, CostConfig, StrategyConfig, parameter_grid
from ten_u.models import Candle, SymbolRules, TradeResult
from ten_u.strategy import StrategyEngine, exit_prices, strategy_hold_seconds


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    max_consecutive_losses: int
    average_bars_held: float

    @property
    def deployable(self) -> bool:
        return False


@dataclass(frozen=True)
class OptimizationResult:
    params: dict[str, float | int]
    train: Metrics
    validation: Metrics
    oos: Metrics
    deployable: bool


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    params: dict[str, float | int]
    train: Metrics
    test: Metrics


def calculate_trade_pnl(
    side: str,
    entry_price: float,
    exit_price: float,
    margin_usdt: float,
    leverage: int,
    costs: CostConfig,
) -> tuple[float, float, float, float]:
    notional = margin_usdt * leverage
    if side == "LONG":
        gross = notional * (exit_price / entry_price - 1)
    elif side == "SHORT":
        gross = notional * (1 - exit_price / entry_price)
    else:
        raise ValueError("side must be LONG or SHORT")
    fee_cost = notional * costs.taker_fee_rate * 2
    slip_cost = notional * costs.slippage_rate * 2
    net = gross - fee_cost - slip_cost
    return gross, fee_cost, slip_cost, net


def simulate_exit(
    candles: list[Candle],
    entry_index: int,
    side: str,
    leverage: int,
    cfg: StrategyConfig,
    costs: CostConfig,
    max_loss_usdt: float,
) -> tuple[int, float, float, float, str, int]:
    entry = candles[entry_index]
    entry_price = entry.open
    tp, stop = exit_prices(entry_price, side, leverage, cfg, max_loss_usdt)
    if cfg.max_hold_seconds > 0:
        hold_bars = max(1, math.ceil(strategy_hold_seconds(cfg) / max(1, cfg.candle_interval_seconds)))
        max_exit_index = min(len(candles) - 1, entry_index + hold_bars - 1)
    else:
        max_exit_index = min(len(candles) - 1, entry_index + cfg.max_hold_minutes)
    exit_index = max_exit_index
    exit_price = candles[max_exit_index].close
    outcome = "TIME_EXIT"

    for i in range(entry_index, max_exit_index + 1):
        candle = candles[i]
        if side == "LONG":
            hit_stop = candle.low <= stop
            hit_tp = candle.high >= tp
        else:
            hit_stop = candle.high >= stop
            hit_tp = candle.low <= tp
        if hit_stop:
            exit_index = i
            exit_price = stop
            outcome = "STOP"
            break
        if hit_tp:
            exit_index = i
            exit_price = tp
            outcome = "TAKE_PROFIT"
            break
    bars_held = max(1, exit_index - entry_index + 1)
    gross, fees, slippage, net = calculate_trade_pnl(
        side,
        entry_price,
        exit_price,
        cfg.margin_usdt,
        leverage,
        costs,
    )
    return exit_index, exit_price, gross, net, outcome, bars_held


def backtest_symbol(
    symbol: str,
    candles: list[Candle],
    cfg: StrategyConfig,
    costs: CostConfig,
    rules: SymbolRules | None = None,
    variant: str = "main",
    start_index: int | None = None,
    end_index: int | None = None,
    signal_filter: Callable[[Candle], bool] | None = None,
) -> list[TradeResult]:
    if len(candles) < 400:
        return []
    engine = StrategyEngine(symbol, candles, cfg, rules)
    trades: list[TradeResult] = []
    i = max(start_index if start_index is not None else 0, 1)
    end = min(end_index if end_index is not None else len(candles) - 1, len(candles) - 2)
    while i <= end:
        if signal_filter is not None and not signal_filter(candles[i]):
            i += 1
            continue
        signal = engine.evaluate(i)
        if signal is None:
            i += 1
            continue
        entry_index = i + 1
        max_loss = cfg.max_loss_usdt if variant == "main" else cfg.legacy_max_loss_usdt
        exit_index, exit_price, gross, net, outcome, bars_held = simulate_exit(
            candles,
            entry_index,
            signal.side,
            signal.leverage,
            cfg,
            costs,
            max_loss,
        )
        entry_price = candles[entry_index].open
        tp, stop = exit_prices(entry_price, signal.side, signal.leverage, cfg, max_loss)
        _, fees, slippage, net = calculate_trade_pnl(
            signal.side,
            entry_price,
            exit_price,
            cfg.margin_usdt,
            signal.leverage,
            costs,
        )
        trades.append(
            TradeResult(
                symbol=symbol,
                side=signal.side,
                signal_time=candles[i].close_time,
                entry_time=candles[entry_index].open_time,
                exit_time=candles[exit_index].close_time,
                leverage=signal.leverage,
                entry_price=entry_price,
                exit_price=exit_price,
                take_profit_price=tp,
                stop_price=stop,
                gross_pnl=gross,
                fees=fees,
                slippage=slippage,
                net_pnl=net,
                outcome=outcome,
                bars_held=bars_held,
                score=signal.score,
                reason_codes=signal.reason_codes,
            )
        )
        i = exit_index + 1
    return trades


def backtest_portfolio(
    candle_map: dict[str, list[Candle]],
    cfg: StrategyConfig,
    costs: CostConfig,
    rules: dict[str, SymbolRules] | None = None,
    variant: str = "main",
    start_index: int | None = None,
    end_index: int | None = None,
    signal_filter: Callable[[Candle], bool] | None = None,
) -> list[TradeResult]:
    all_trades: list[TradeResult] = []
    for symbol, candles in candle_map.items():
        all_trades.extend(
            backtest_symbol(
                symbol,
                candles,
                cfg,
                costs,
                None if rules is None else rules.get(symbol),
                variant,
                start_index,
                end_index,
                signal_filter,
            )
        )
    all_trades.sort(key=lambda t: t.signal_time)
    filtered: list[TradeResult] = []
    active_until = -1
    for trade in all_trades:
        if trade.entry_time <= active_until:
            continue
        filtered.append(trade)
        active_until = trade.exit_time
    return filtered


def summarize(trades: list[TradeResult]) -> Metrics:
    if not trades:
        return Metrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, math.inf, 0.0, 0, 0.0)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = len(trades) - wins
    gross_pnl = sum(t.gross_pnl for t in trades)
    fees = sum(t.fees for t in trades)
    slippage = sum(t.slippage for t in trades)
    net_pnl = sum(t.net_pnl for t in trades)
    gains = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    pains = -sum(t.net_pnl for t in trades if t.net_pnl <= 0)
    profit_factor = gains / pains if pains > 0 else math.inf
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    current_loss_streak = 0
    max_loss_streak = 0
    for trade in trades:
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if trade.net_pnl <= 0:
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        else:
            current_loss_streak = 0
    return Metrics(
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=wins / len(trades),
        gross_pnl=gross_pnl,
        fees=fees,
        slippage=slippage,
        net_pnl=net_pnl,
        expectancy=net_pnl / len(trades),
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        max_consecutive_losses=max_loss_streak,
        average_bars_held=sum(t.bars_held for t in trades) / len(trades),
    )


def split_indexes(length: int, cfg: BacktestConfig) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    train_end = int(length * cfg.train_ratio)
    validation_end = int(length * (cfg.train_ratio + cfg.validation_ratio))
    return (0, train_end), (train_end, validation_end), (validation_end, length - 2)


def optimize(
    candle_map: dict[str, list[Candle]],
    base_cfg: StrategyConfig,
    backtest_cfg: BacktestConfig,
    costs: CostConfig,
    rules: dict[str, SymbolRules] | None = None,
    grid_mode: str = "quick",
) -> OptimizationResult:
    if not candle_map:
        raise ValueError("candle_map is empty")
    min_len = min(len(c) for c in candle_map.values())
    train_range, val_range, oos_range = split_indexes(min_len, backtest_cfg)
    best: OptimizationResult | None = None
    for params in parameter_grid(grid_mode, base_cfg.signal_model):
        cfg = base_cfg.with_updates(**params)
        train_trades = backtest_portfolio(
            candle_map,
            cfg,
            costs,
            rules,
            "main",
            train_range[0],
            train_range[1],
        )
        val_trades = backtest_portfolio(
            candle_map,
            cfg,
            costs,
            rules,
            "main",
            val_range[0],
            val_range[1],
        )
        train_metrics = summarize(train_trades)
        val_metrics = summarize(val_trades)
        rank = _rank_metrics(val_metrics, train_metrics)
        if best is None or rank > _rank_metrics(best.validation, best.train):
            oos_trades = backtest_portfolio(
                candle_map,
                cfg,
                costs,
                rules,
                "main",
                oos_range[0],
                oos_range[1],
            )
            oos_metrics = summarize(oos_trades)
            deployable = is_deployable(oos_metrics, backtest_cfg)
            best = OptimizationResult(params, train_metrics, val_metrics, oos_metrics, deployable)
    assert best is not None
    return best


def walk_forward(
    candle_map: dict[str, list[Candle]],
    base_cfg: StrategyConfig,
    backtest_cfg: BacktestConfig,
    costs: CostConfig,
    rules: dict[str, SymbolRules] | None = None,
    grid_mode: str = "quick",
    train_minutes: int = 90 * 24 * 60,
    test_minutes: int = 30 * 24 * 60,
) -> list[WalkForwardWindow]:
    if not candle_map:
        return []
    min_len = min(len(c) for c in candle_map.values())
    windows: list[WalkForwardWindow] = []
    start = 0
    while start + train_minutes + test_minutes < min_len - 2:
        train_start = start
        train_end = start + train_minutes
        test_start = train_end
        test_end = test_start + test_minutes
        best_params: dict[str, float | int] | None = None
        best_train: Metrics | None = None
        for params in parameter_grid(grid_mode, base_cfg.signal_model):
            cfg = base_cfg.with_updates(**params)
            train_metrics = summarize(
                backtest_portfolio(
                    candle_map,
                    cfg,
                    costs,
                    rules,
                    "main",
                    train_start,
                    train_end,
                )
            )
            if best_train is None or _rank_metrics(train_metrics, train_metrics) > _rank_metrics(best_train, best_train):
                best_params = params
                best_train = train_metrics
        if best_params is None or best_train is None:
            break
        test_cfg = base_cfg.with_updates(**best_params)
        test_metrics = summarize(
            backtest_portfolio(
                candle_map,
                test_cfg,
                costs,
                rules,
                "main",
                test_start,
                test_end,
            )
        )
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                params=best_params,
                train=best_train,
                test=test_metrics,
            )
        )
        start += test_minutes
    return windows


def is_deployable(metrics: Metrics, cfg: BacktestConfig) -> bool:
    return (
        metrics.trades >= cfg.min_oos_trades
        and metrics.win_rate >= cfg.min_oos_win_rate
        and metrics.expectancy > cfg.min_expectancy
        and metrics.profit_factor >= cfg.min_profit_factor
    )


def _rank_metrics(primary: Metrics, secondary: Metrics) -> tuple[float, float, float, int]:
    enough = min(primary.trades / 50, 1.0)
    return (
        primary.expectancy * enough,
        primary.profit_factor if math.isfinite(primary.profit_factor) else 999.0,
        primary.win_rate,
        secondary.trades,
    )


def random_baseline(
    candle_map: dict[str, list[Candle]],
    cfg: StrategyConfig,
    costs: CostConfig,
    seed: int = 42,
    target_trades: int = 200,
) -> list[TradeResult]:
    rng = random.Random(seed)
    trades: list[TradeResult] = []
    symbols = list(candle_map)
    if not symbols:
        return trades
    attempts = 0
    while len(trades) < target_trades and attempts < target_trades * 30:
        attempts += 1
        symbol = rng.choice(symbols)
        candles = candle_map[symbol]
        if len(candles) <= cfg.max_hold_minutes + 2:
            continue
        i = rng.randint(1, len(candles) - cfg.max_hold_minutes - 2)
        side = rng.choice(["LONG", "SHORT"])
        leverage = rng.randint(cfg.min_leverage, cfg.max_leverage)
        exit_index, exit_price, gross, net, outcome, bars_held = simulate_exit(
            candles,
            i + 1,
            side,
            leverage,
            cfg,
            costs,
            cfg.max_loss_usdt,
        )
        entry_price = candles[i + 1].open
        tp, stop = exit_prices(entry_price, side, leverage, cfg, cfg.max_loss_usdt)
        _, fees, slippage, net = calculate_trade_pnl(side, entry_price, exit_price, cfg.margin_usdt, leverage, costs)
        trades.append(
            TradeResult(
                symbol=symbol,
                side=side,
                signal_time=candles[i].close_time,
                entry_time=candles[i + 1].open_time,
                exit_time=candles[exit_index].close_time,
                leverage=leverage,
                entry_price=entry_price,
                exit_price=exit_price,
                take_profit_price=tp,
                stop_price=stop,
                gross_pnl=gross,
                fees=fees,
                slippage=slippage,
                net_pnl=net,
                outcome=outcome,
                bars_held=bars_held,
                score=0.0,
                reason_codes=("RANDOM_BASELINE",),
            )
        )
    trades.sort(key=lambda t: t.signal_time)
    return trades
