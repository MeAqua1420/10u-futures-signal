from __future__ import annotations

import unittest

from datetime import UTC, datetime

from ten_u.config import StrategyConfig
from ten_u.models import Signal
from ten_u.okx import OKXInstrument, build_order_plan
from ten_u.session_stats import OKXSessionStats


class SessionStatsTests(unittest.TestCase):
    def test_session_summary_counts_orders_and_realized_fills(self) -> None:
        stats = OKXSessionStats(started_at=datetime(2026, 5, 7, tzinfo=UTC))
        plan = build_order_plan(_sample_signal(), _sample_instrument(), "long-short")
        stats.record_scan()
        stats.record_no_signal()
        stats.record_order(
            plan,
            {"code": "0", "data": [{"ordId": "1", "sCode": "0", "sMsg": ""}]},
            "ETH-USDT-SWAP:LONG",
        )
        summary = stats.summary(_FakeOKXClient())
        self.assertEqual(summary["scans"], 1)
        self.assertEqual(summary["no_signal_scans"], 1)
        self.assertEqual(summary["orders_sent"], 1)
        self.assertEqual(summary["orders_accepted"], 1)
        self.assertEqual(summary["realized"]["closed_trades"], 1)
        self.assertEqual(summary["realized"]["wins"], 1)
        self.assertEqual(summary["realized"]["net_realized_pnl_usdt"], 4.9)
        self.assertEqual(summary["open_positions"]["open_position_count"], 1)
        self.assertEqual(summary["open_positions"]["unrealized_pnl_usdt"], -0.25)


class _FakeOKXClient:
    def fills_history(self, *args, **kwargs):
        return [
            {"instId": "ETH-USDT-SWAP", "ordId": "entry", "tradeId": "e1", "fillTime": "1", "fillPnl": "", "fee": "-0.05"},
            {"instId": "ETH-USDT-SWAP", "ordId": "exit", "tradeId": "x1", "fillTime": "2", "fillPnl": "5", "fee": "-0.05"},
        ]

    def positions(self, *args, **kwargs):
        return [
            {
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "pos": "0.1",
                "avgPx": "2000",
                "upl": "-0.25",
                "lever": "10",
            }
        ]


def _sample_signal() -> Signal:
    cfg = StrategyConfig()
    return Signal(
        time_utc="2026-05-07T00:00:00+00:00",
        time_cn="2026-05-07T08:00:00+08:00",
        symbol="ETH-USDT-SWAP",
        side="LONG",
        leverage=10,
        margin_usdt=cfg.margin_usdt,
        entry_reference=2000.0,
        take_profit_price=2100.0,
        stop_price=1960.0,
        target_pnl=cfg.target_profit_usdt,
        max_loss=cfg.max_loss_usdt,
        expires_at="2026-05-07T04:00:00+00:00",
        score=100,
        reason_codes=("TEST",),
    )


def _sample_instrument() -> OKXInstrument:
    return OKXInstrument.from_api(
        {
            "instId": "ETH-USDT-SWAP",
            "baseCcy": "ETH",
            "quoteCcy": "USDT",
            "settleCcy": "USDT",
            "tickSz": "0.01",
            "lotSz": "0.1",
            "minSz": "0.1",
            "ctVal": "0.1",
            "ctValCcy": "ETH",
            "state": "live",
        }
    )


if __name__ == "__main__":
    unittest.main()
