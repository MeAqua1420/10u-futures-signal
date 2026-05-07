from __future__ import annotations

import unittest

from ten_u.config import CostConfig, StrategyConfig
from ten_u.models import Candle, SymbolRules
from ten_u.strategy import choose_leverage, double_heikin_ashi, exit_prices, manuscript_deviation, manuscript_range_features
from ten_u.backtest import calculate_trade_pnl, simulate_exit


def candle(i: int, open_: float, high: float, low: float, close: float) -> Candle:
    start = i * 60_000
    return Candle(
        open_time=start,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        close_time=start + 59_999,
        quote_volume=10_000.0,
        trades=100,
        taker_buy_base=50.0,
        taker_buy_quote=5_000.0,
    )


class StrategyMathTests(unittest.TestCase):
    def test_exit_prices_long_and_short(self) -> None:
        cfg = StrategyConfig()
        long_tp, long_stop = exit_prices(100.0, "LONG", 10, cfg, cfg.max_loss_usdt)
        short_tp, short_stop = exit_prices(100.0, "SHORT", 10, cfg, cfg.max_loss_usdt)
        self.assertAlmostEqual(long_tp, 105.0)
        self.assertAlmostEqual(long_stop, 98.0)
        self.assertAlmostEqual(short_tp, 95.0)
        self.assertAlmostEqual(short_stop, 102.0)

    def test_pnl_includes_costs(self) -> None:
        gross, fees, slippage, net = calculate_trade_pnl(
            "LONG",
            100.0,
            105.0,
            10.0,
            10,
            CostConfig(taker_fee_rate=0.0005, slippage_rate=0.0003),
        )
        self.assertAlmostEqual(gross, 5.0)
        self.assertAlmostEqual(fees, 0.1)
        self.assertAlmostEqual(slippage, 0.06)
        self.assertAlmostEqual(net, 4.84)

    def test_short_pnl_uses_entry_notional(self) -> None:
        gross, fees, slippage, net = calculate_trade_pnl(
            "SHORT",
            100.0,
            95.0,
            10.0,
            10,
            CostConfig(taker_fee_rate=0.0, slippage_rate=0.0),
        )
        self.assertAlmostEqual(gross, 5.0)
        self.assertAlmostEqual(fees, 0.0)
        self.assertAlmostEqual(slippage, 0.0)
        self.assertAlmostEqual(net, 5.0)

    def test_stop_wins_when_same_candle_touches_both(self) -> None:
        cfg = StrategyConfig(max_hold_minutes=4)
        candles = [
            candle(0, 100, 100, 100, 100),
            candle(1, 100, 106, 97, 103),
            candle(2, 103, 104, 102, 103),
        ]
        exit_index, exit_price, gross, net, outcome, bars = simulate_exit(
            candles,
            1,
            "LONG",
            10,
            cfg,
            CostConfig(taker_fee_rate=0, slippage_rate=0),
            cfg.max_loss_usdt,
        )
        self.assertEqual(exit_index, 1)
        self.assertEqual(outcome, "STOP")
        self.assertAlmostEqual(exit_price, 98.0)
        self.assertAlmostEqual(gross, -2.0)
        self.assertAlmostEqual(net, -2.0)
        self.assertEqual(bars, 1)

    def test_choose_leverage_rejects_noisy_stop(self) -> None:
        cfg = StrategyConfig(max_leverage=20)
        self.assertIsNone(choose_leverage(100.0, 6.0, cfg))
        self.assertIsNotNone(choose_leverage(100.0, 0.5, cfg))

    def test_symbol_rounding(self) -> None:
        rules = SymbolRules(
            symbol="TESTUSDT",
            price_precision=2,
            quantity_precision=3,
            tick_size=0.01,
            step_size=0.001,
            min_qty=0.001,
            min_notional=5.0,
        )
        self.assertEqual(rules.round_price(100.129), 100.12)
        self.assertEqual(rules.round_quantity(1.23456), 1.234)

    def test_manuscript_features_are_prior_windowed(self) -> None:
        ha_open = [1.0, 1.0, 2.0, 3.0]
        ha_close = [2.0, 0.5, 3.0, 2.0]
        ha_body = [abs(c - o) for o, c in zip(ha_open, ha_close, strict=True)]
        range_y, psy = manuscript_range_features(ha_open, ha_close, ha_body, 2)
        self.assertIsNone(range_y[1])
        self.assertAlmostEqual(range_y[2], 1 / 1.5 * 100 - 0.5 / 1.5 * 100)
        self.assertAlmostEqual(psy[2], 0.5)

    def test_double_heikin_ashi_and_deviation_shape(self) -> None:
        candles = [
            candle(0, 100, 102, 99, 101),
            candle(1, 101, 103, 100, 102),
            candle(2, 102, 104, 101, 103),
        ]
        ha_open, ha_close = double_heikin_ashi(candles)
        dev = manuscript_deviation(ha_close, 2)
        self.assertEqual(len(ha_open), 3)
        self.assertEqual(len(ha_close), 3)
        self.assertIsNone(dev[0])
        self.assertIsNotNone(dev[2])


if __name__ == "__main__":
    unittest.main()
