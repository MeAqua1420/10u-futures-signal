from __future__ import annotations

import unittest

from datetime import UTC, datetime, timedelta

from ten_u.cli import (
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
                "--risk-profile",
                "standard",
                "--loop",
                "--poll-seconds",
                "30",
            ]
        )
        self.assertTrue(args.loop)
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
                "--risk-profile",
                "conservative",
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
        self.assertEqual(args.risk_profile, "conservative")
        self.assertEqual(args.pos_mode, "long-short")
        self.assertEqual(args.poll_seconds, 45)

    def test_poll_seconds_has_safe_minimum(self) -> None:
        self.assertEqual(_poll_seconds(0), 1)

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
