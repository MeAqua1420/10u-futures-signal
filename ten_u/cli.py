from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
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
from ten_u.config import BacktestConfig, CostConfig, StrategyConfig, parameter_grid
from ten_u.market_calendar import (
    is_us_market_non_workday,
    recent_us_market_non_workdays,
    us_eastern_date,
    us_eastern_day_bounds_ms,
)
from ten_u.models import CN_TZ, Signal
from ten_u.okx import OKXClient, OKXCredentials, best_okx_signal, build_order_plan, load_or_fetch_okx_candles
from ten_u.realtime import rest_polling_scanner, websocket_scanner
from ten_u.session_stats import OKXSessionStats


@dataclass
class ManagedOKXPosition:
    signal_key: str
    inst_id: str
    pos_side: str | None
    expires_at: datetime


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
    if args.command == "okx-weekend-backtest":
        return cmd_okx_weekend_backtest(args)
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
    okx_signal.add_argument("--bar", choices=["1s", "1m"], default="1m")
    okx_signal.add_argument("--strategy", choices=["manuscript", "breakout", "microburst"], default="manuscript")
    okx_signal.add_argument(
        "--risk-profile",
        choices=["balanced", "conservative", "standard", "aggressive", "scalp-1s", "weekend-1s"],
        default="balanced",
    )
    okx_signal.add_argument("--loop", action="store_true", help="keep scanning until interrupted")
    okx_signal.add_argument("--poll-seconds", type=int, default=60, help="seconds between loop scans")

    okx_demo = sub.add_parser("okx-demo", help="prepare or execute an OKX demo trading order from the best signal")
    okx_demo.add_argument("--symbols", nargs="*", default=None, help="OKX instIds, e.g. BTC-USDT-SWAP ETH-USDT-SWAP")
    okx_demo.add_argument("--top", type=int, default=20)
    okx_demo.add_argument("--lookback", type=int, default=720)
    okx_demo.add_argument("--bar", choices=["1s", "1m"], default="1m")
    okx_demo.add_argument("--strategy", choices=["manuscript", "breakout", "microburst"], default="manuscript")
    okx_demo.add_argument(
        "--risk-profile",
        choices=["balanced", "conservative", "standard", "aggressive", "scalp-1s", "weekend-1s"],
        default="balanced",
    )
    okx_demo.add_argument("--pos-mode", choices=["net", "long-short"], default="net")
    okx_demo.add_argument("--execute", action="store_true", help="actually place the order in OKX demo trading")
    okx_demo.add_argument("--loop", action="store_true", help="keep scanning until interrupted")
    okx_demo.add_argument("--poll-seconds", type=int, default=60, help="seconds between loop scans")
    okx_demo.add_argument(
        "--trade-cooldown-seconds",
        type=int,
        default=None,
        help="minimum seconds between accepted orders; scalp-1s defaults to 300",
    )
    okx_weekend = sub.add_parser("okx-weekend-backtest", help="search OKX 1s factors on US/Eastern non-workdays")
    okx_weekend.add_argument("--symbols", nargs="*", default=None)
    okx_weekend.add_argument("--top", type=int, default=20)
    okx_weekend.add_argument("--weekends", type=int, default=8)
    okx_weekend.add_argument("--non-workdays", type=int, default=None, help="exact number of recent US/Eastern non-workdays to fetch")
    okx_weekend.add_argument("--grid", choices=["quick", "full"], default="quick")
    okx_weekend.add_argument("--min-oos-trades", type=int, default=100)
    okx_weekend.add_argument("--min-quote-volume", type=float, default=50_000_000)
    okx_weekend.add_argument("--refresh", action="store_true")
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
    bar = _effective_okx_bar(args.risk_profile, args.bar)
    cfg = _okx_strategy_config(args.strategy, args.risk_profile, bar)
    inst_ids = args.symbols or client.top_usdt_swap_instruments(args.top, cfg.min_liquidity_quote_volume)
    instruments = client.instruments()
    if args.loop:
        _print_json(
            {
                "mode": "WATCH_OKX_SIGNAL",
                "message": "Scanning OKX simulated market data until interrupted.",
                "risk_profile": args.risk_profile,
                "bar": bar,
                "strategy_config": _okx_strategy_config_payload(cfg),
                "poll_seconds": _poll_seconds(args.poll_seconds),
                "symbols": inst_ids,
                **_scan_time(),
            }
        )
        _okx_signal_loop(client, inst_ids, cfg, args.lookback, args.poll_seconds, bar, instruments)
        return 0
    if _market_day_filtered(cfg):
        _print_json(_market_day_filtered_payload(inst_ids))
        return 0
    signal = best_okx_signal(client, inst_ids, cfg, args.lookback, bar, instruments=instruments)
    _print_json(_okx_signal_payload(signal, inst_ids))
    return 0


def cmd_okx_weekend_backtest(args: argparse.Namespace) -> int:
    client = OKXClient(simulated=True)
    cfg = _okx_strategy_config("microburst", "weekend-1s", "1s").with_updates(
        min_liquidity_quote_volume=args.min_quote_volume,
        pool_size=args.top,
    )
    costs = CostConfig()
    backtest_cfg = BacktestConfig(
        min_oos_trades=args.min_oos_trades,
        min_oos_win_rate=0.0,
        min_profit_factor=1.15,
        min_expectancy=0.0,
    )
    inst_ids = args.symbols or client.top_usdt_swap_instruments(args.top, cfg.min_liquidity_quote_volume)
    end = int(time.time() * 1000)
    selected_dates = recent_us_market_non_workdays(args.non_workdays, end) if args.non_workdays else None
    start = (
        min(us_eastern_day_bounds_ms(day)[0] for day in selected_dates)
        if selected_dates
        else int((datetime.now(UTC) - timedelta(days=max(21, args.weekends * 7 + 14))).timestamp() * 1000)
    )
    instruments = client.instruments()
    candle_map = {}
    print(f"Fetching OKX 1s US non-workday search data: {', '.join(inst_ids)}", file=sys.stderr)
    min_required = max(240, cfg.atr_compression_window + cfg.donchian_window + cfg.micro_momentum_slow)
    for inst_id in inst_ids:
        if inst_id not in instruments:
            print(f"Skipping {inst_id}: instrument not live", file=sys.stderr)
            continue
        if selected_dates:
            candles = []
            for day in selected_dates:
                day_start, day_end = us_eastern_day_bounds_ms(day)
                candles.extend(
                    load_or_fetch_okx_candles(
                        client,
                        inst_id,
                        "1s",
                        day_start,
                        min(day_end, end),
                        refresh=args.refresh,
                    )
                )
        else:
            candles = load_or_fetch_okx_candles(client, inst_id, "1s", start, end, refresh=args.refresh)
        if len(candles) < min_required:
            print(f"Skipping {inst_id}: not enough 1s candles ({len(candles)})", file=sys.stderr)
            continue
        candle_map[inst_id] = candles
    if not candle_map:
        _print_json({"mode": "NO_US_NONWORKDAY_EDGE", "message": "No symbols with enough OKX 1s data."})
        return 2

    all_dates = selected_dates or sorted(
        {
            us_eastern_date(candle.close_time)
            for candles in candle_map.values()
            for candle in candles
            if is_us_market_non_workday(candle.close_time)
        }
    )
    if len(all_dates) < 3:
        _print_json(
            {
                "mode": "NO_US_NONWORKDAY_EDGE",
                "message": "Not enough US/Eastern non-workday dates in the fetched OKX 1s data.",
                "non_workday_dates": [d.isoformat() for d in all_dates],
            }
        )
        return 2

    train_dates, validation_dates, oos_dates = _split_dates(all_dates)
    train_filter = _date_filter(train_dates)
    validation_filter = _date_filter(validation_dates)
    oos_filter = _date_filter(oos_dates)
    best: dict[str, Any] | None = None
    for params in parameter_grid(args.grid, "microburst"):
        test_cfg = cfg.with_updates(**params)
        if _target_net_at_max_leverage(test_cfg) < test_cfg.min_target_net_usdt:
            continue
        train_metrics = summarize(backtest_portfolio(candle_map, test_cfg, costs, signal_filter=train_filter))
        validation_metrics = summarize(backtest_portfolio(candle_map, test_cfg, costs, signal_filter=validation_filter))
        rank = _hyper_rank(validation_metrics, train_metrics)
        if best is None or rank > best["rank"]:
            oos_metrics = summarize(backtest_portfolio(candle_map, test_cfg, costs, signal_filter=oos_filter))
            best = {
                "rank": rank,
                "params": params,
                "train": train_metrics,
                "validation": validation_metrics,
                "oos": oos_metrics,
            }
    if best is None:
        _print_json({"mode": "NO_US_NONWORKDAY_EDGE", "message": "All parameter combinations failed cost filters."})
        return 2
    tuned_cfg = cfg.with_updates(**best["params"])
    full_metrics = summarize(
        backtest_portfolio(candle_map, tuned_cfg, costs, signal_filter=_date_filter(set(all_dates)))
    )
    deployable = _hyper_deployable(best["oos"], backtest_cfg)
    _print_json(
        {
            "mode": "OKX_US_NONWORKDAY_BACKTEST",
            "message": "DEPLOYABLE" if deployable else "NO_US_NONWORKDAY_EDGE",
            "strategy_model": "microburst",
            "risk_profile": "weekend-1s",
            "bar": "1s",
            "symbols": list(candle_map),
            "non_workday_timezone": "America/New_York",
            "non_workday_dates": [d.isoformat() for d in all_dates],
            "split_dates": {
                "train": [d.isoformat() for d in sorted(train_dates)],
                "validation": [d.isoformat() for d in sorted(validation_dates)],
                "out_of_sample": [d.isoformat() for d in sorted(oos_dates)],
            },
            "best_params": best["params"],
            "deployment_gate": {
                "deployable": deployable,
                "requirements": {
                    "oos_trades": f">= {backtest_cfg.min_oos_trades}",
                    "oos_expectancy": "> 0",
                    "oos_profit_factor": ">= 1.15",
                },
            },
            "split_metrics": {
                "train": _metrics_dict(best["train"]),
                "validation": _metrics_dict(best["validation"]),
                "out_of_sample": _metrics_dict(best["oos"]),
            },
            "full_non_workday_metrics": _metrics_dict(full_metrics),
            "strategy_config": _okx_strategy_config_payload(tuned_cfg),
        }
    )
    return 0


def cmd_okx_demo(args: argparse.Namespace) -> int:
    public_client = OKXClient(simulated=True)
    bar = _effective_okx_bar(args.risk_profile, args.bar)
    args.bar = bar
    cfg = _okx_strategy_config(args.strategy, args.risk_profile, bar)
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
                "bar": bar,
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
    bar: str,
    instruments: dict[str, Any] | None = None,
) -> None:
    while True:
        scan_started = time.monotonic()
        try:
            if _market_day_filtered(cfg):
                _print_json(_with_scan_duration(_market_day_filtered_payload(inst_ids), scan_started))
                if not _sleep_between_scans(max(60, poll_seconds), "OKX signal scanner"):
                    return
                continue
            signal = best_okx_signal(client, inst_ids, cfg, lookback, bar, instruments=instruments)
            _print_json(_with_scan_duration(_okx_signal_payload(signal, inst_ids), scan_started))
        except KeyboardInterrupt:
            _print_json({"mode": "STOPPED", "message": "OKX signal scanner stopped by user.", **_scan_time()})
            return
        except Exception as exc:  # pragma: no cover - depends on network/API conditions
            _print_json(_with_scan_duration({"mode": "SCAN_ERROR", "error": str(exc), **_scan_time()}, scan_started))
        if not _sleep_between_scans(poll_seconds, "OKX signal scanner"):
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
    instruments = public_client.instruments()
    cooldown_seconds = _effective_trade_cooldown_seconds(args.risk_profile, args.trade_cooldown_seconds)
    cooldown_until: datetime | None = None
    managed_positions: dict[str, ManagedOKXPosition] = {}
    while True:
        scan_started = time.monotonic()
        try:
            if _market_day_filtered(cfg):
                _print_json(_with_scan_duration(_market_day_filtered_payload(inst_ids), scan_started))
                if not _sleep_between_scans(max(60, args.poll_seconds), "OKX demo scanner", stats, private_client):
                    return
                continue
            if private_client is not None and args.risk_profile == "weekend-1s":
                _drop_closed_managed_positions(private_client, managed_positions)
                _close_expired_managed_positions(private_client, managed_positions)
                if managed_positions:
                    _print_json(
                        _with_scan_duration(
                            {
                                "mode": "POSITION_ACTIVE_SKIP_SCAN",
                                "message": "A managed weekend-1s position is still active; waiting before opening another one.",
                                "managed_positions": [
                                    {
                                        "signal_key": pos.signal_key,
                                        "inst_id": pos.inst_id,
                                        "pos_side": pos.pos_side,
                                        "expires_at": pos.expires_at.isoformat(),
                                    }
                                    for pos in managed_positions.values()
                                ],
                                **_scan_time(),
                            },
                            scan_started,
                        )
                    )
                    if not _sleep_between_scans(max(1, args.poll_seconds), "OKX demo scanner", stats, private_client):
                        return
                    continue
            if cooldown_until is not None and datetime.now(UTC) < cooldown_until:
                _print_json(
                    _with_scan_duration(
                        {
                            "mode": "TRADE_COOLDOWN_ACTIVE",
                            "message": "Skipping signal scan while the post-order cooldown is active.",
                            "cooldown_until": cooldown_until.isoformat(),
                            **_scan_time(),
                        },
                        scan_started,
                    )
                )
                if not _sleep_between_scans(max(1, min(5, int((cooldown_until - datetime.now(UTC)).total_seconds()))), "OKX demo scanner", stats, private_client):
                    return
                continue
            stats.record_scan()
            _prune_executed_signals(executed_signals)
            accepted_before = _accepted_order_count(stats)
            _okx_demo_once(
                public_client,
                inst_ids,
                cfg,
                args,
                loop_mode=True,
                executed_signals=executed_signals,
                stats=stats,
                private_client=private_client,
                scan_started=scan_started,
                instruments=instruments,
                managed_positions=managed_positions,
            )
            if _accepted_order_count(stats) > accepted_before and cooldown_seconds > 0:
                cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
        except KeyboardInterrupt:
            _print_json({"mode": "STOPPED", "message": "OKX demo scanner stopped by user.", **_scan_time()})
            _print_json(stats.summary(private_client))
            return
        except Exception as exc:  # pragma: no cover - depends on network/API conditions
            stats.record_error(exc)
            _print_json(_with_scan_duration({"mode": "SCAN_ERROR", "error": str(exc), **_scan_time()}, scan_started))
        if not _sleep_between_scans(args.poll_seconds, "OKX demo scanner", stats, private_client):
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
    scan_started: float | None = None,
    instruments: dict[str, Any] | None = None,
    managed_positions: dict[str, ManagedOKXPosition] | None = None,
) -> int | bool:
    scan_started = time.monotonic() if scan_started is None else scan_started
    if _market_day_filtered(cfg):
        _print_json(_with_scan_duration(_market_day_filtered_payload(inst_ids), scan_started))
        return False if loop_mode else 0
    instruments = public_client.instruments() if instruments is None else instruments
    signal = best_okx_signal(public_client, inst_ids, cfg, args.lookback, args.bar, instruments=instruments)
    if signal is None:
        if stats is not None:
            stats.record_no_signal()
        _print_json(_with_scan_duration(_okx_signal_payload(None, inst_ids), scan_started))
        return False if loop_mode else 0
    signal_key = _signal_trade_key(signal)
    if args.execute and loop_mode and executed_signals is not None and signal_key in executed_signals:
        if stats is not None:
            stats.record_duplicate_signal()
        payload = {
            "mode": "DUPLICATE_SIGNAL_SKIPPED",
            "message": "This symbol/side already has an executed signal in the current expiry window.",
            "signal_key": signal_key,
            "active_until": executed_signals[signal_key].isoformat(),
            "signal": signal.as_dict(),
            **_scan_time(),
        }
        _print_json(
            _with_scan_duration(payload, scan_started)
        )
        return False
    instrument = instruments.get(signal.symbol)
    if instrument is None:
        print(f"OKX instrument not found or not live: {signal.symbol}", file=sys.stderr)
        return False if loop_mode else 2
    order_plan = build_order_plan(signal, instrument, args.pos_mode)
    if not args.execute:
        if stats is not None:
            stats.record_dry_run_signal()
        payload = {
            "mode": "DRY_RUN",
            "message": "No order was sent. Add --execute to place this in OKX demo trading.",
            "order_plan": order_plan.as_dict(),
            **_scan_time(),
        }
        _print_json(_with_scan_duration(payload, scan_started))
        return False if loop_mode else 0

    private_client = private_client or OKXClient(credentials=OKXCredentials.from_env(), simulated=True)
    private_client.set_leverage(
        order_plan.inst_id,
        order_plan.leverage,
        order_plan.pos_side,
    )
    response = private_client.place_order(order_plan)
    if stats is not None:
        recorded_order = stats.record_order(order_plan, response, signal_key)
    else:
        recorded_order = None
    payload = {
        "mode": "EXECUTED_OKX_DEMO",
        "message": "One OKX demo order was sent. Loop mode will keep scanning and skip this symbol/side until expiry.",
        "signal_key": signal_key,
        "active_until": _signal_expires_at(signal).isoformat(),
        "order_plan": order_plan.as_dict(),
        "okx_response": response,
        **_scan_time(),
    }
    _print_json(_with_scan_duration(payload, scan_started))
    if loop_mode and executed_signals is not None:
        executed_signals[signal_key] = _signal_expires_at(signal)
    if (
        loop_mode
        and managed_positions is not None
        and (recorded_order is None or recorded_order.accepted)
        and cfg.signal_model == "microburst"
    ):
        managed_positions[signal_key] = ManagedOKXPosition(
            signal_key=signal_key,
            inst_id=order_plan.inst_id,
            pos_side=order_plan.pos_side,
            expires_at=_signal_expires_at(signal),
        )
    return False if loop_mode else 0


def _okx_signal_payload(signal: Signal | None, inst_ids: list[str]) -> dict[str, Any]:
    if signal is None:
        return {"message": "NO_SIGNAL", "symbols": inst_ids, **_scan_time()}
    return {"message": "SIGNAL", "signal": signal.as_dict(), **_scan_time()}


def _market_day_filtered(cfg: StrategyConfig) -> bool:
    return cfg.us_nonworkday_only and not is_us_market_non_workday(int(time.time() * 1000))


def _market_day_filtered_payload(inst_ids: list[str]) -> dict[str, Any]:
    return {
        "mode": "MARKET_DAY_FILTERED",
        "message": "US/Eastern is a NYSE workday; weekend-1s will not emit or execute signals.",
        "symbols": inst_ids,
        "non_workday_timezone": "America/New_York",
        **_scan_time(),
    }


def _split_dates(dates: list[date]) -> tuple[set[date], set[date], set[date]]:
    train_end = max(1, int(len(dates) * 0.60))
    validation_end = max(train_end + 1, int(len(dates) * 0.80))
    validation_end = min(validation_end, len(dates) - 1)
    return set(dates[:train_end]), set(dates[train_end:validation_end]), set(dates[validation_end:])


def _date_filter(allowed_dates: set[date]):
    return lambda candle: us_eastern_date(candle.close_time) in allowed_dates and is_us_market_non_workday(candle.close_time)


def _target_net_at_max_leverage(cfg: StrategyConfig) -> float:
    return cfg.target_profit_usdt - cfg.margin_usdt * cfg.max_leverage * cfg.estimated_round_trip_cost_rate


def _hyper_rank(primary: Any, secondary: Any) -> tuple[float, float, float, int]:
    enough = min(primary.trades / 50, 1.0)
    profit_factor = primary.profit_factor if primary.profit_factor != float("inf") else 999.0
    return (primary.expectancy * enough, profit_factor, primary.win_rate, secondary.trades)


def _hyper_deployable(metrics: Any, cfg: BacktestConfig) -> bool:
    return (
        metrics.trades >= cfg.min_oos_trades
        and metrics.expectancy > cfg.min_expectancy
        and metrics.profit_factor >= cfg.min_profit_factor
    )


def _poll_seconds(value: int) -> int:
    return max(0, int(value))


def _sleep_between_scans(
    poll_seconds: int,
    stopped_message: str,
    stats: OKXSessionStats | None = None,
    private_client: OKXClient | None = None,
) -> bool:
    seconds = _poll_seconds(poll_seconds)
    if seconds <= 0:
        return True
    try:
        time.sleep(seconds)
        return True
    except KeyboardInterrupt:
        _print_json({"mode": "STOPPED", "message": f"{stopped_message} stopped by user.", **_scan_time()})
        if stats is not None:
            _print_json(stats.summary(private_client))
        return False


def _with_scan_duration(payload: dict[str, Any], scan_started: float) -> dict[str, Any]:
    payload["scan_duration_seconds"] = round(time.monotonic() - scan_started, 3)
    return payload


def _okx_strategy_config(signal_model: str, risk_profile: str, bar: str = "1m") -> StrategyConfig:
    cfg = StrategyConfig(signal_model=signal_model, candle_interval_seconds=_bar_to_seconds(bar))
    if risk_profile == "weekend-1s":
        if signal_model != "microburst":
            raise ValueError("weekend-1s risk profile requires --strategy microburst")
        return cfg.with_updates(
            target_profit_usdt=1.0,
            max_loss_usdt=0.6,
            max_hold_seconds=180,
            candle_interval_seconds=1,
            min_leverage=30,
            max_leverage=55,
            donchian_window=20,
            volume_window=60,
            volume_multiple=2.0,
            score_threshold=80,
            atr_period=14,
            atr_compression_window=120,
            atr_min_percentile=35,
            atr_max_percentile=95,
            min_expected_move_mult=0.35,
            min_stop_atr_mult=0.20,
            micro_momentum_fast=3,
            micro_momentum_mid=15,
            micro_momentum_slow=30,
            micro_volume_burst_seconds=3,
            micro_body_ratio_min=0.55,
            micro_wick_ratio_max=0.35,
            min_target_net_usdt=0.05,
            estimated_round_trip_cost_rate=0.0016,
            us_nonworkday_only=True,
        )
    if risk_profile == "scalp-1s":
        if signal_model == "manuscript":
            return cfg.with_updates(
                target_profit_usdt=2.0,
                max_loss_usdt=1.0,
                max_hold_minutes=240,
                max_leverage=50,
                min_expected_move_mult=0.45,
                min_stop_atr_mult=1.25,
                atr_period=60,
                ha_range_window=300,
                ha_range_y_threshold=30,
                ha_psy_threshold=0.58,
                ha_deviation_window=60,
                ha_deviation_threshold=0.00035,
                ha_score_threshold=100,
                direction_window=300,
                min_direction_change_pct=0.0008,
            )
        return cfg.with_updates(
            target_profit_usdt=2.0,
            max_loss_usdt=1.0,
            max_hold_minutes=240,
            max_leverage=50,
            donchian_window=180,
            volume_window=60,
            volume_multiple=1.5,
            score_threshold=80,
            atr_period=60,
            min_expected_move_mult=0.45,
            min_stop_atr_mult=1.25,
            direction_window=300,
            min_direction_change_pct=0.0008,
        )
    if risk_profile == "standard":
        return cfg
    if risk_profile == "balanced":
        if signal_model == "manuscript":
            return cfg.with_updates(
                max_leverage=15,
                min_expected_move_mult=1.15,
                min_stop_atr_mult=1.35,
                ha_range_y_threshold=40,
                ha_psy_threshold=0.35,
                ha_deviation_threshold=0.0020,
                ha_score_threshold=100,
            )
        return cfg.with_updates(
            max_leverage=15,
            volume_multiple=2.0,
            score_threshold=90,
            min_expected_move_mult=1.15,
            min_stop_atr_mult=1.35,
        )
    if risk_profile == "aggressive":
        if signal_model == "manuscript":
            return cfg.with_updates(
                max_leverage=20,
                min_expected_move_mult=1.05,
                min_stop_atr_mult=1.10,
                ha_range_y_threshold=30,
                ha_psy_threshold=0.30,
                ha_deviation_threshold=0.0015,
                ha_score_threshold=90,
            )
        return cfg.with_updates(
            max_leverage=20,
            volume_multiple=1.8,
            score_threshold=80,
            min_expected_move_mult=1.05,
            min_stop_atr_mult=1.10,
        )
    if risk_profile != "conservative":
        raise ValueError(
            "risk_profile must be 'balanced', 'conservative', 'standard', 'aggressive', 'scalp-1s', or 'weekend-1s'"
        )
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
        "max_hold_minutes",
        "max_hold_seconds",
        "candle_interval_seconds",
        "min_leverage",
        "max_leverage",
        "atr_period",
        "min_expected_move_mult",
        "min_stop_atr_mult",
        "ha_range_window",
        "ha_range_y_threshold",
        "ha_psy_threshold",
        "ha_deviation_window",
        "ha_deviation_threshold",
        "ha_score_threshold",
        "direction_window",
        "min_direction_change_pct",
        "atr_min_percentile",
        "atr_max_percentile",
        "micro_momentum_fast",
        "micro_momentum_mid",
        "micro_momentum_slow",
        "micro_volume_burst_seconds",
        "micro_body_ratio_min",
        "micro_wick_ratio_max",
        "min_target_net_usdt",
        "estimated_round_trip_cost_rate",
        "us_nonworkday_only",
    ]
    return {key: getattr(cfg, key) for key in keys}


def _bar_to_seconds(bar: str) -> int:
    if bar.endswith("s"):
        return int(bar[:-1])
    if bar.endswith("m"):
        return int(bar[:-1]) * 60
    raise ValueError(f"unsupported OKX bar: {bar}")


def _effective_okx_bar(risk_profile: str, requested_bar: str) -> str:
    if risk_profile in {"scalp-1s", "weekend-1s"}:
        return "1s"
    return requested_bar


def _effective_trade_cooldown_seconds(risk_profile: str, requested: int | None) -> int:
    if requested is not None:
        return max(0, requested)
    if risk_profile == "scalp-1s":
        return 300
    return 0


def _accepted_order_count(stats: OKXSessionStats) -> int:
    return sum(1 for order in stats.orders if order.accepted)


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


def _drop_closed_managed_positions(
    private_client: OKXClient,
    managed_positions: dict[str, ManagedOKXPosition],
) -> None:
    for key, managed in list(managed_positions.items()):
        try:
            positions = private_client.positions("SWAP", managed.inst_id)
        except Exception:
            continue
        if not _managed_position_is_open(positions, managed):
            _print_json(
                {
                    "mode": "POSITION_CLOSED_DETECTED",
                    "message": "Managed position is no longer open on OKX.",
                    "signal_key": key,
                    "inst_id": managed.inst_id,
                    **_scan_time(),
                }
            )
            del managed_positions[key]


def _close_expired_managed_positions(
    private_client: OKXClient,
    managed_positions: dict[str, ManagedOKXPosition],
) -> None:
    now = datetime.now(UTC)
    for key, managed in list(managed_positions.items()):
        if managed.expires_at > now:
            continue
        response = private_client.close_position(managed.inst_id, managed.pos_side)
        _print_json(
            {
                "mode": "TIME_EXIT_CLOSE_SENT",
                "message": "Max hold seconds elapsed; sent OKX simulated close-position request.",
                "signal_key": key,
                "inst_id": managed.inst_id,
                "pos_side": managed.pos_side,
                "expires_at": managed.expires_at.isoformat(),
                "okx_response": response,
                **_scan_time(),
            }
        )
        del managed_positions[key]


def _managed_position_is_open(positions: list[dict[str, Any]], managed: ManagedOKXPosition) -> bool:
    for position in positions:
        if position.get("instId") != managed.inst_id:
            continue
        if managed.pos_side is not None and position.get("posSide") != managed.pos_side:
            continue
        try:
            if abs(float(position.get("pos") or 0.0)) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


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
