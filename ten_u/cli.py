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
from ten_u.models import CN_TZ, Signal
from ten_u.okx import OKXClient, OKXCredentials, best_okx_signal, build_order_plan
from ten_u.realtime import rest_polling_scanner, websocket_scanner
from ten_u.session_stats import OKXSessionStats


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "symbols":
        return cmd_symbols(args)
    if args.command == "backtest":
        return cmd_backtest(args)
    if args.command == "realtime":
        return cmd_realtime(args)
    if args.command == "okx-symbols":
        return cmd_okx_symbols(args)
    if args.command == "okx-signal":
        return cmd_okx_signal(args)
    if args.command == "okx-demo":
        return cmd_okx_demo(args)
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

    okx_symbols = sub.add_parser("okx-symbols", help="print liquidity-ranked OKX USDT swap instruments")
    okx_symbols.add_argument("--top", type=int, default=20)
    okx_symbols.add_argument("--min-quote-volume", type=float, default=50_000_000)
    okx_symbols.add_argument("--json", action="store_true")

    okx_signal = sub.add_parser("okx-signal", help="print the best current OKX signal without placing orders")
    okx_signal.add_argument("--symbols", nargs="*", default=None, help="OKX instIds, e.g. BTC-USDT-SWAP ETH-USDT-SWAP")
    okx_signal.add_argument("--top", type=int, default=20)
    okx_signal.add_argument("--lookback", type=int, default=720)
    okx_signal.add_argument("--strategy", choices=["manuscript", "breakout"], default="manuscript")
    okx_signal.add_argument("--risk-profile", choices=["conservative", "standard"], default="conservative")
    okx_signal.add_argument("--loop", action="store_true", help="keep scanning until interrupted")
    okx_signal.add_argument("--poll-seconds", type=int, default=60, help="seconds between loop scans")

    okx_demo = sub.add_parser("okx-demo", help="prepare or execute an OKX demo trading order from the best signal")
    okx_demo.add_argument("--symbols", nargs="*", default=None, help="OKX instIds, e.g. BTC-USDT-SWAP ETH-USDT-SWAP")
    okx_demo.add_argument("--top", type=int, default=20)
    okx_demo.add_argument("--lookback", type=int, default=720)
    okx_demo.add_argument("--strategy", choices=["manuscript", "breakout"], default="manuscript")
    okx_demo.add_argument("--risk-profile", choices=["conservative", "standard"], default="conservative")
    okx_demo.add_argument("--pos-mode", choices=["net", "long-short"], default="net")
    okx_demo.add_argument("--execute", action="store_true", help="actually place the order in OKX demo trading")
    okx_demo.add_argument("--loop", action="store_true", help="keep scanning until interrupted")
    okx_demo.add_argument("--poll-seconds", type=int, default=60, help="seconds between loop scans")
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


def cmd_okx_symbols(args: argparse.Namespace) -> int:
    client = OKXClient(simulated=True)
    inst_ids = client.top_usdt_swap_instruments(args.top, args.min_quote_volume)
    if args.json:
        print(json.dumps(inst_ids, ensure_ascii=False, indent=2))
    else:
        for i, inst_id in enumerate(inst_ids, 1):
            print(f"{i:02d}. {inst_id}")
    return 0


def cmd_okx_signal(args: argparse.Namespace) -> int:
    client = OKXClient(simulated=True)
    cfg = _okx_strategy_config(args.strategy, args.risk_profile)
    inst_ids = args.symbols or client.top_usdt_swap_instruments(args.top, cfg.min_liquidity_quote_volume)
    if args.loop:
        _print_json(
            {
                "mode": "WATCH_OKX_SIGNAL",
                "message": "Scanning OKX simulated market data until interrupted.",
                "risk_profile": args.risk_profile,
                "strategy_config": _okx_strategy_config_payload(cfg),
                "poll_seconds": _poll_seconds(args.poll_seconds),
                "symbols": inst_ids,
                **_scan_time(),
            }
        )
        _okx_signal_loop(client, inst_ids, cfg, args.lookback, args.poll_seconds)
        return 0
    signal = best_okx_signal(client, inst_ids, cfg, args.lookback)
    _print_json(_okx_signal_payload(signal, inst_ids))
    return 0


def cmd_okx_demo(args: argparse.Namespace) -> int:
    public_client = OKXClient(simulated=True)
    cfg = _okx_strategy_config(args.strategy, args.risk_profile)
    inst_ids = args.symbols or public_client.top_usdt_swap_instruments(args.top, cfg.min_liquidity_quote_volume)
    if args.loop:
        _print_json(
            {
                "mode": "WATCH_OKX_DEMO",
                "message": "Scanning OKX demo trading until interrupted. Executed symbol/side signals are skipped until expiry.",
                "execute": args.execute,
                "simulated": True,
                "pos_mode": args.pos_mode,
                "risk_profile": args.risk_profile,
                "strategy_config": _okx_strategy_config_payload(cfg),
                "poll_seconds": _poll_seconds(args.poll_seconds),
                "symbols": inst_ids,
                **_scan_time(),
            }
        )
        _okx_demo_loop(public_client, inst_ids, cfg, args)
        return 0
    return _okx_demo_once(public_client, inst_ids, cfg, args)


def _okx_signal_loop(
    client: OKXClient,
    inst_ids: list[str],
    cfg: StrategyConfig,
    lookback: int,
    poll_seconds: int,
) -> None:
    while True:
        try:
            signal = best_okx_signal(client, inst_ids, cfg, lookback)
            _print_json(_okx_signal_payload(signal, inst_ids))
        except KeyboardInterrupt:
            _print_json({"mode": "STOPPED", "message": "OKX signal scanner stopped by user.", **_scan_time()})
            return
        except Exception as exc:  # pragma: no cover - depends on network/API conditions
            _print_json({"mode": "SCAN_ERROR", "error": str(exc), **_scan_time()})
        try:
            time.sleep(_poll_seconds(poll_seconds))
        except KeyboardInterrupt:
            _print_json({"mode": "STOPPED", "message": "OKX signal scanner stopped by user.", **_scan_time()})
            return


def _okx_demo_loop(
    public_client: OKXClient,
    inst_ids: list[str],
    cfg: StrategyConfig,
    args: argparse.Namespace,
) -> None:
    executed_signals: dict[str, datetime] = {}
    stats = OKXSessionStats()
    private_client = OKXClient(credentials=OKXCredentials.from_env(), simulated=True) if args.execute else None
    while True:
        try:
            stats.record_scan()
            _prune_executed_signals(executed_signals)
            _okx_demo_once(
                public_client,
                inst_ids,
                cfg,
                args,
                loop_mode=True,
                executed_signals=executed_signals,
                stats=stats,
                private_client=private_client,
            )
        except KeyboardInterrupt:
            _print_json({"mode": "STOPPED", "message": "OKX demo scanner stopped by user.", **_scan_time()})
            _print_json(stats.summary(private_client))
            return
        except Exception as exc:  # pragma: no cover - depends on network/API conditions
            stats.record_error(exc)
            _print_json({"mode": "SCAN_ERROR", "error": str(exc), **_scan_time()})
        try:
            time.sleep(_poll_seconds(args.poll_seconds))
        except KeyboardInterrupt:
            _print_json({"mode": "STOPPED", "message": "OKX demo scanner stopped by user.", **_scan_time()})
            _print_json(stats.summary(private_client))
            return


def _okx_demo_once(
    public_client: OKXClient,
    inst_ids: list[str],
    cfg: StrategyConfig,
    args: argparse.Namespace,
    loop_mode: bool = False,
    executed_signals: dict[str, datetime] | None = None,
    stats: OKXSessionStats | None = None,
    private_client: OKXClient | None = None,
) -> int | bool:
    signal = best_okx_signal(public_client, inst_ids, cfg, args.lookback)
    if signal is None:
        if stats is not None:
            stats.record_no_signal()
        _print_json(_okx_signal_payload(None, inst_ids))
        return False if loop_mode else 0
    signal_key = _signal_trade_key(signal)
    if args.execute and loop_mode and executed_signals is not None and signal_key in executed_signals:
        if stats is not None:
            stats.record_duplicate_signal()
        _print_json(
            {
                "mode": "DUPLICATE_SIGNAL_SKIPPED",
                "message": "This symbol/side already has an executed signal in the current expiry window.",
                "signal_key": signal_key,
                "active_until": executed_signals[signal_key].isoformat(),
                "signal": signal.as_dict(),
                **_scan_time(),
            }
        )
        return False
    instruments = public_client.instruments()
    instrument = instruments.get(signal.symbol)
    if instrument is None:
        print(f"OKX instrument not found or not live: {signal.symbol}", file=sys.stderr)
        return False if loop_mode else 2
    order_plan = build_order_plan(signal, instrument, args.pos_mode)
    if not args.execute:
        if stats is not None:
            stats.record_dry_run_signal()
        _print_json(
            {
                "mode": "DRY_RUN",
                "message": "No order was sent. Add --execute to place this in OKX demo trading.",
                "order_plan": order_plan.as_dict(),
                **_scan_time(),
            }
        )
        return False if loop_mode else 0

    private_client = private_client or OKXClient(credentials=OKXCredentials.from_env(), simulated=True)
    private_client.set_leverage(
        order_plan.inst_id,
        order_plan.leverage,
        order_plan.pos_side,
    )
    response = private_client.place_order(order_plan)
    if stats is not None:
        stats.record_order(order_plan, response, signal_key)
    _print_json(
        {
            "mode": "EXECUTED_OKX_DEMO",
            "message": "One OKX demo order was sent. Loop mode will keep scanning and skip this symbol/side until expiry.",
            "signal_key": signal_key,
            "active_until": _signal_expires_at(signal).isoformat(),
            "order_plan": order_plan.as_dict(),
            "okx_response": response,
            **_scan_time(),
        }
    )
    if loop_mode and executed_signals is not None:
        executed_signals[signal_key] = _signal_expires_at(signal)
    return False if loop_mode else 0


def _okx_signal_payload(signal: Signal | None, inst_ids: list[str]) -> dict[str, Any]:
    if signal is None:
        return {"message": "NO_SIGNAL", "symbols": inst_ids, **_scan_time()}
    return {"message": "SIGNAL", "signal": signal.as_dict(), **_scan_time()}


def _poll_seconds(value: int) -> int:
    return max(1, int(value))


def _okx_strategy_config(signal_model: str, risk_profile: str) -> StrategyConfig:
    cfg = StrategyConfig(signal_model=signal_model)
    if risk_profile == "standard":
        return cfg
    if risk_profile != "conservative":
        raise ValueError("risk_profile must be 'conservative' or 'standard'")
    if signal_model == "manuscript":
        return cfg.with_updates(
            max_leverage=10,
            min_expected_move_mult=1.30,
            min_stop_atr_mult=1.60,
            ha_range_y_threshold=50,
            ha_psy_threshold=0.40,
            ha_deviation_threshold=0.0025,
            ha_score_threshold=100,
        )
    return cfg.with_updates(
        max_leverage=10,
        volume_multiple=2.2,
        score_threshold=100,
        min_expected_move_mult=1.30,
        min_stop_atr_mult=1.60,
    )


def _okx_strategy_config_payload(cfg: StrategyConfig) -> dict[str, Any]:
    keys = [
        "signal_model",
        "margin_usdt",
        "target_profit_usdt",
        "max_loss_usdt",
        "min_leverage",
        "max_leverage",
        "min_expected_move_mult",
        "min_stop_atr_mult",
        "ha_range_y_threshold",
        "ha_psy_threshold",
        "ha_deviation_threshold",
        "ha_score_threshold",
    ]
    return {key: getattr(cfg, key) for key in keys}


def _signal_trade_key(signal: Signal) -> str:
    return f"{signal.symbol}:{signal.side}"


def _signal_expires_at(signal: Signal) -> datetime:
    expires_at = signal.expires_at.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(expires_at)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _prune_executed_signals(executed_signals: dict[str, datetime]) -> None:
    now = datetime.now(UTC)
    expired = [key for key, expires_at in executed_signals.items() if expires_at <= now]
    for key in expired:
        del executed_signals[key]


def _scan_time() -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "scan_time_utc": now.isoformat(),
        "scan_time_cn": now.astimezone(CN_TZ).isoformat(),
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


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
