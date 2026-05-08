from __future__ import annotations

import unittest

from datetime import UTC, datetime, timedelta

from ten_u.cli import (
    _bar_to_seconds,
    _effective_okx_bar,
    _effective_trade_cooldown_seconds,
    _okx_strategy_config,
    _poll_seconds,
    _prune_executed_signals,
    _signal_trade_key,
    build_parser,
)
from ten_u.models import Signal


class CLITests(unittest.TestCase):
    def test_okx_signal_loop_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "okx-signal",
                "--top",
                "5",
                "--strategy",
                "manuscript",
                "--bar",
                "1s",
                "--risk-profile",
                "standard",
                "--loop",
                "--poll-seconds",
                "30",
            ]
        )
        self.assertTrue(args.loop)
        self.assertEqual(args.bar, "1s")
        self.assertEqual(args.risk_profile, "standard")
        self.assertEqual(args.poll_seconds, 30)

    def test_okx_demo_loop_execute_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "okx-demo",
                "--top",
                "5",
                "--strategy",
                "manuscript",
                "--bar",
                "1s",
                "--risk-profile",
                "aggressive",
                "--pos-mode",
                "long-short",
                "--loop",
                "--poll-seconds",
                "45",
                "--execute",
            ]
        )
        self.assertTrue(args.loop)
        self.assertTrue(args.execute)
        self.assertEqual(args.bar, "1s")
        self.assertEqual(args.risk_profile, "aggressive")
        self.assertEqual(args.pos_mode, "long-short")
        self.assertEqual(args.poll_seconds, 45)

    def test_poll_seconds_has_safe_minimum(self) -> None:
        self.assertEqual(_poll_seconds(0), 0)
        self.assertEqual(_poll_seconds(-1), 0)

    def test_signal_trade_key_deduplicates_symbol_side(self) -> None:
        signal = _sample_signal()
        self.assertEqual(_signal_trade_key(signal), "ETH-USDT-SWAP:LONG")

    def test_prune_executed_signals_removes_expired_keys(self) -> None:
        active = {
            "ETH-USDT-SWAP:LONG": datetime.now(UTC) + timedelta(minutes=5),
            "BTC-USDT-SWAP:SHORT": datetime.now(UTC) - timedelta(seconds=1),
        }
        _prune_executed_signals(active)
        self.assertEqual(list(active.keys()), ["ETH-USDT-SWAP:LONG"])

    def test_conservative_okx_strategy_is_stricter_than_standard(self) -> None:
        standard = _okx_strategy_config("manuscript", "standard")
        conservative = _okx_strategy_config("manuscript", "conservative")
        self.assertLess(conservative.max_leverage, standard.max_leverage)
        self.assertGreater(conservative.ha_range_y_threshold, standard.ha_range_y_threshold)
        self.assertGreater(conservative.ha_deviation_threshold, standard.ha_deviation_threshold)
        self.assertGreater(conservative.min_stop_atr_mult, standard.min_stop_atr_mult)

    def test_balanced_okx_strategy_relaxes_leverage_from_conservative(self) -> None:
        balanced = _okx_strategy_config("manuscript", "balanced")
        conservative = _okx_strategy_config("manuscript", "conservative")
        self.assertGreater(balanced.max_leverage, conservative.max_leverage)
        self.assertLess(balanced.ha_range_y_threshold, conservative.ha_range_y_threshold)

    def test_aggressive_okx_strategy_uses_higher_max_leverage(self) -> None:
        aggressive = _okx_strategy_config("manuscript", "aggressive", "1s")
        self.assertEqual(aggressive.max_leverage, 20)
        self.assertEqual(aggressive.candle_interval_seconds, 1)

    def test_scalp_1s_profile_forces_one_second_bar_and_plus2_minus1(self) -> None:
        cfg = _okx_strategy_config("manuscript", "scalp-1s", "1s")
        self.assertEqual(_effective_okx_bar("scalp-1s", "1m"), "1s")
        self.assertEqual(cfg.target_profit_usdt, 2.0)
        self.assertEqual(cfg.max_loss_usdt, 1.0)
        self.assertEqual(cfg.candle_interval_seconds, 1)
        self.assertEqual(cfg.max_leverage, 50)
        self.assertEqual(cfg.ha_range_window, 300)
        self.assertEqual(cfg.ha_deviation_window, 60)
        self.assertEqual(cfg.direction_window, 300)
        self.assertEqual(cfg.min_direction_change_pct, 0.0008)

    def test_scalp_1s_default_trade_cooldown(self) -> None:
        self.assertEqual(_effective_trade_cooldown_seconds("scalp-1s", None), 300)
        self.assertEqual(_effective_trade_cooldown_seconds("scalp-1s", 0), 0)
        self.assertEqual(_effective_trade_cooldown_seconds("balanced", None), 0)

    def test_weekend_1s_profile_forces_microburst_and_one_second_bar(self) -> None:
        args = build_parser().parse_args(
            [
                "okx-demo",
                "--top",
                "5",
                "--strategy",
                "microburst",
                "--risk-profile",
                "weekend-1s",
            ]
        )
        self.assertEqual(args.strategy, "microburst")
        self.assertEqual(_effective_okx_bar("weekend-1s", "1m"), "1s")
        cfg = _okx_strategy_config("microburst", "weekend-1s", "1s")
        self.assertEqual(cfg.target_profit_usdt, 1.0)
        self.assertEqual(cfg.max_loss_usdt, 0.6)
        self.assertEqual(cfg.max_hold_seconds, 180)
        self.assertEqual(cfg.min_leverage, 30)
        self.assertEqual(cfg.max_leverage, 55)
        self.assertTrue(cfg.us_nonworkday_only)

    def test_okx_weekend_backtest_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "okx-weekend-backtest",
                "--top",
                "3",
                "--weekends",
                "8",
                "--grid",
                "full",
                "--min-oos-trades",
                "25",
            ]
        )
        self.assertEqual(args.command, "okx-weekend-backtest")
        self.assertEqual(args.top, 3)
        self.assertEqual(args.weekends, 8)
        self.assertEqual(args.grid, "full")
        self.assertEqual(args.min_oos_trades, 25)

    def test_bar_to_seconds(self) -> None:
        self.assertEqual(_bar_to_seconds("1s"), 1)
        self.assertEqual(_bar_to_seconds("1m"), 60)


def _sample_signal() -> Signal:
    return Signal(
        time_utc="2026-05-07T00:00:00+00:00",
        time_cn="2026-05-07T08:00:00+08:00",
        symbol="ETH-USDT-SWAP",
        side="LONG",
        leverage=10,
        margin_usdt=10,
        entry_reference=2000.0,
        take_profit_price=2100.0,
        stop_price=1960.0,
        target_pnl=5,
        max_loss=2,
        expires_at="2026-05-07T04:00:00+00:00",
        score=100,
        reason_codes=("TEST",),
    )


if __name__ == "__main__":
    unittest.main()
