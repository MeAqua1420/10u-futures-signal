from __future__ import annotations

import unittest

from ten_u.config import StrategyConfig
from ten_u.models import Signal
from ten_u.okx import OKXCredentials, OKXInstrument, build_order_plan, okx_candle_from_row, okx_sign


class OKXTests(unittest.TestCase):
    def test_okx_signature_is_stable(self) -> None:
        signature = okx_sign(
            "secret",
            "2026-05-07T00:00:00.000Z",
            "POST",
            "/api/v5/trade/order",
            '{"instId":"BTC-USDT-SWAP"}',
        )
        self.assertEqual(signature, "2MBixUGdYJ1VZbxQwBBNZ6Ylwg8eCkgpCFeZK1LQhrY=")

    def test_okx_candle_conversion(self) -> None:
        candle = okx_candle_from_row(
            ["1778081220000", "100", "105", "99", "103", "12", "1.2", "1234", "1"],
            "1m",
        )
        self.assertEqual(candle.open_time, 1778081220000)
        self.assertEqual(candle.close_time, 1778081279999)
        self.assertEqual(candle.open, 100.0)
        self.assertEqual(candle.quote_volume, 1234.0)
        self.assertEqual(candle.taker_buy_ratio, 0.5)

    def test_contract_size_for_linear_swap(self) -> None:
        inst = OKXInstrument.from_api(
            {
                "instId": "BTC-USDT-SWAP",
                "baseCcy": "BTC",
                "quoteCcy": "USDT",
                "settleCcy": "USDT",
                "tickSz": "0.1",
                "lotSz": "0.01",
                "minSz": "0.01",
                "ctVal": "0.01",
                "ctValCcy": "BTC",
                "state": "live",
            }
        )
        self.assertEqual(inst.round_price(100.19), "100.1")
        self.assertEqual(inst.contracts_for_margin(100_000, 10, 10), "0.1")

    def test_build_order_plan_uses_demo_swap_shape(self) -> None:
        inst = OKXInstrument.from_api(
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
        cfg = StrategyConfig()
        signal = Signal(
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
        plan = build_order_plan(signal, inst, "long-short")
        body = plan.request_body()
        self.assertEqual(body["instId"], "ETH-USDT-SWAP")
        self.assertEqual(body["tdMode"], "isolated")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["posSide"], "long")
        self.assertEqual(body["ordType"], "market")
        self.assertEqual(body["sz"], "0.5")
        self.assertEqual(body["attachAlgoOrds"][0]["tpOrdPx"], "-1")
        self.assertEqual(body["attachAlgoOrds"][0]["slOrdPx"], "-1")


if __name__ == "__main__":
    unittest.main()
