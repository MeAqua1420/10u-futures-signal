from __future__ import annotations

import unittest

from ten_u.config import StrategyConfig
from ten_u.models import Signal
from ten_u.okx import (
    OKXCredentials,
    OKXClient,
    OKXInstrument,
    build_order_plan,
    closed_okx_candles,
    dedupe_okx_candles,
    okx_candle_from_row,
    okx_sign,
)


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

    def test_okx_one_second_candle_conversion(self) -> None:
        candle = okx_candle_from_row(
            ["1778081220000", "100", "105", "99", "103", "12", "1.2", "1234", "1"],
            "1s",
        )
        self.assertEqual(candle.close_time, 1778081220999)

    def test_closed_okx_candles_filters_unconfirmed_latest_bar(self) -> None:
        rows = [
            ["1778081220000", "100", "105", "99", "103", "12", "1.2", "1234", "1"],
            ["1778081280000", "103", "106", "102", "104", "9", "0.9", "936", "0"],
        ]
        candles = closed_okx_candles(rows, "1m", now_ms=1778081330000)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].open_time, 1778081220000)

    def test_closed_okx_candles_uses_time_when_confirm_flag_missing(self) -> None:
        rows = [
            ["1778081220000", "100", "105", "99", "103", "12", "1.2", "1234"],
            ["1778081280000", "103", "106", "102", "104", "9", "0.9", "936"],
        ]
        candles = closed_okx_candles(rows, "1m", now_ms=1778081281000)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].open_time, 1778081220000)

    def test_dedupe_okx_candles_keeps_sorted_unique_open_times(self) -> None:
        first = okx_candle_from_row(["1000", "100", "101", "99", "100", "1", "1", "100", "1"], "1s")
        replacement = okx_candle_from_row(["1000", "100", "102", "99", "101", "1", "1", "100", "1"], "1s")
        second = okx_candle_from_row(["2000", "101", "102", "100", "101", "1", "1", "100", "1"], "1s")
        candles = dedupe_okx_candles([second, first, replacement])
        self.assertEqual([c.open_time for c in candles], [1000, 2000])
        self.assertEqual(candles[0].close, 101.0)

    def test_history_candles_uses_okx_history_endpoint(self) -> None:
        client = _FakeOKXClient()
        candles = client.history_candles("BTC-USDT-SWAP", "1s", after=2000, limit=300)
        self.assertEqual(len(candles), 1)
        self.assertEqual(client.last_path, "/api/v5/market/history-candles")
        self.assertEqual(client.last_params["after"], "2000")
        self.assertEqual(client.last_params["limit"], "300")

    def test_contract_size_for_linear_swap(self) -> None:
        inst = OKXInstrument.from_api(
            {
                "instId": "BTC-USDT-SWAP",
                "baseCcy": "BTC",
                "quoteCcy": "USDT",
                "settleCcy": "USDT",
                "instCategory": "1",
                "tickSz": "0.1",
                "lotSz": "0.01",
                "minSz": "0.01",
                "ctVal": "0.01",
                "ctValCcy": "BTC",
                "state": "live",
                "lever": "100",
            }
        )
        self.assertEqual(inst.max_leverage, 100)
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

    def test_close_position_uses_okx_close_endpoint(self) -> None:
        client = _FakeOKXClient()
        response = client.close_position("ETH-USDT-SWAP", "long")
        self.assertEqual(response["code"], "0")
        self.assertEqual(client.last_path, "/api/v5/trade/close-position")
        self.assertEqual(client.last_body["instId"], "ETH-USDT-SWAP")
        self.assertEqual(client.last_body["posSide"], "long")
        self.assertEqual(client.last_body["mgnMode"], "isolated")


class _FakeOKXClient(OKXClient):
    def __init__(self) -> None:
        self.last_path = ""
        self.last_params = {}
        self.last_body = {}

    def public_get(self, path, params=None):
        self.last_path = path
        self.last_params = params or {}
        return {
            "code": "0",
            "data": [["1000", "100", "101", "99", "100", "1", "1", "100", "1"]],
        }

    def private_post(self, path, body):
        self.last_path = path
        self.last_body = body
        return {"code": "0", "data": [{"sCode": "0"}]}


if __name__ == "__main__":
    unittest.main()
