from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ten_u.backtest import (
    backtest_portfolio,
    is_deployable,
    optimize,
    random_baseline,
    summarize,
    walk_forward,
)
from ten_u.binance import BinanceClient, load_or_fetch_klines
from ten_u.config import BacktestConfig, CostConfig, StrategyConfig
from ten_u.realtime import rest_polling_scanner, websocket_scanner


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "symbols":
        return cmd_symbols(args)
    if args.command == "backtest":
        return cmd_backtest(args)
    if args.command == "realtime":
        return cmd_realtime(args)
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="10U Binance USD-M perpetual signal system")
    sub = parser.add_subparsers(dest="command")

    symbols = sub.add_parser("symbols", help="print liquidity-ranked Binance USDT perpetual symbols")
    symbols.add_argument("--top", type=int, default=60)
    symbols.add_argument("--min-quote-volume", type=float, default=50_000_000)
    symbols.add_argument("--json", action="store_true")

    backtest = sub.add_parser("backtest", help="fetch data, optimize, and run strategy comparisons")
    backtest.add_argument("--symbols", nargs="*", default=None, help="explicit symbols, e.g. BTCUSDT ETHUSDT")
    backtest.add_argument("--top", type=int, default=10, help="top liquidity symbols when --symbols is omitted")
    backtest.add_argument("--days", type=int, default=30)
    backtest.add_argument("--grid", choices=["quick", "full"], default="quick")
    backtest.add_argument("--strategy", choices=["manuscript", "breakout"], default="manuscript")
    backtest.add_argument("--refresh", action="store_true", help="ignore local CSV cache")
    backtest.add_argument("--walk-forward", action="store_true")
    backtest.add_argument("--min-oos-trades", type=int, default=100)

    realtime = sub.add_parser("realtime", help="print realtime terminal signals")
    realtime.add_argument("--symbols", nargs="*", default=None)
    realtime.add_argument("--top", type=int, default=60)
    realtime.add_argument("--lookback", type=int, default=720)
    realtime.add_argument("--poll-seconds", type=int, default=15)
    realtime.add_argument("--mode", choices=["rest", "ws"], default="rest")
    realtime.add_argument("--strategy", choices=["manuscript", "breakout"], default="manuscript")
    return parser


def cmd_symbols(args: argparse.Namespace) -> int:
    cfg = StrategyConfig(min_liquidity_quote_volume=args.min_quote_volume, pool_size=args.top)
    client = BinanceClient()
    symbols = client.top_usdt_perpetual_symbols(args.top, cfg.min_liquidity_quote_volume)
    if args.json:
        print(json.dumps(symbols, ensure_ascii=False, indent=2))
    else:
        for i, symbol in enumerate(symbols, 1):
            print(f"{i:02d}. {symbol}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    client = BinanceClient()
    strategy_cfg = StrategyConfig(pool_size=args.top, signal_model=args.strategy)
    backtest_cfg = BacktestConfig(min_oos_trades=args.min_oos_trades)
    costs = CostConfig()
    symbols = args.symbols or client.top_usdt_perpetual_symbols(
        args.top,
        strategy_cfg.min_liquidity_quote_volume,
    )
    if not symbols:
        print("No symbols found.", file=sys.stderr)
        return 2
    end = int(time.time() * 1000)
    start = int((datetime.now(UTC) - timedelta(days=args.days)).timestamp() * 1000)
    rules = client.symbol_rules()
    candle_map = {}
    print(f"Fetching {args.days}d x {len(symbols)} symbols: {', '.join(symbols)}", file=sys.stderr)
    for symbol in symbols:
        candles = load_or_fetch_klines(client, symbol, "1m", start, end, refresh=args.refresh)
        if args.days >= 180 and len(candles) < 180 * 24 * 60 * 0.95:
            print(f"Skipping {symbol}: less than 180d usable 1m history", file=sys.stderr)
            continue
        if len(candles) >= 400:
            candle_map[symbol] = candles
        else:
            print(f"Skipping {symbol}: not enough candles ({len(candles)})", file=sys.stderr)
    if not candle_map:
        print("No symbols with enough candle data.", file=sys.stderr)
        return 2

    result = optimize(candle_map, strategy_cfg, backtest_cfg, costs, rules, args.grid)
    tuned_cfg = strategy_cfg.with_updates(**result.params)
    main_trades = backtest_portfolio(candle_map, tuned_cfg, costs, rules, "main")
    legacy_trades = backtest_portfolio(candle_map, tuned_cfg, costs, rules, "legacy")
    reference_cfg = StrategyConfig(pool_size=args.top, signal_model="breakout" if args.strategy == "manuscript" else "manuscript")
    reference_trades = backtest_portfolio(candle_map, reference_cfg, costs, rules, "main")
    baseline_trades = random_baseline(candle_map, tuned_cfg, costs, seed=backtest_cfg.random_seed)
    report: dict[str, Any] = {
        "strategy_model": args.strategy,
        "best_params": result.params,
        "deployment_gate": {
            "deployable": result.deployable,
            "message": "DEPLOYABLE" if result.deployable else "NO_DEPLOYABLE_SIGNAL_RULESET",
            "requirements": {
                "oos_win_rate": ">= 0.60",
                "oos_trades": f">= {backtest_cfg.min_oos_trades}",
                "expectancy": "> 0",
                "profit_factor": ">= 1.20",
            },
        },
        "split_metrics": {
            "train": _metrics_dict(result.train),
            "validation": _metrics_dict(result.validation),
            "out_of_sample": _metrics_dict(result.oos),
            "out_of_sample_deployable_recheck": is_deployable(result.oos, backtest_cfg),
        },
        "comparison_full_period": {
            f"{args.strategy}_plus5_minus2": _metrics_dict(summarize(main_trades)),
            f"{args.strategy}_plus5_minus10": _metrics_dict(summarize(legacy_trades)),
            f"{reference_cfg.signal_model}_reference_plus5_minus2": _metrics_dict(summarize(reference_trades)),
            "random_baseline": _metrics_dict(summarize(baseline_trades)),
        },
    }
    if args.walk_forward:
        wf = walk_forward(candle_map, strategy_cfg, backtest_cfg, costs, rules, args.grid)
        report["walk_forward_90d_train_30d_test"] = [
            {
                "params": w.params,
                "train": _metrics_dict(w.train),
                "test": _metrics_dict(w.test),
            }
            for w in wf
        ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_realtime(args: argparse.Namespace) -> int:
    client = BinanceClient()
    strategy_cfg = StrategyConfig(pool_size=args.top, signal_model=args.strategy)
    symbols = args.symbols or client.top_usdt_perpetual_symbols(
        args.top,
        strategy_cfg.min_liquidity_quote_volume,
    )
    if args.mode == "ws":
        asyncio.run(websocket_scanner(symbols, strategy_cfg, client, args.lookback))
    else:
        rest_polling_scanner(client, symbols, strategy_cfg, args.lookback, args.poll_seconds)
    return 0


def _metrics_dict(metrics: Any) -> dict[str, Any]:
    data = asdict(metrics)
    data["win_rate_pct"] = round(metrics.win_rate * 100, 2)
    data["net_pnl"] = round(metrics.net_pnl, 6)
    data["expectancy"] = round(metrics.expectancy, 6)
    if data["profit_factor"] == float("inf"):
        data["profit_factor"] = "inf"
    else:
        data["profit_factor"] = round(metrics.profit_factor, 4)
    data["max_drawdown"] = round(metrics.max_drawdown, 6)
    return data


if __name__ == "__main__":
    raise SystemExit(main())
