from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def iso_utc(ms: int) -> str:
    return ms_to_utc(ms).isoformat()


def iso_cn(ms: int) -> str:
    return ms_to_utc(ms).astimezone(CN_TZ).isoformat()


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trades: int
    taker_buy_base: float
    taker_buy_quote: float

    @classmethod
    def from_binance(cls, row: list[Any]) -> "Candle":
        return cls(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
            quote_volume=float(row[7]),
            trades=int(row[8]),
            taker_buy_base=float(row[9]),
            taker_buy_quote=float(row[10]),
        )

    def to_csv_row(self) -> list[str]:
        return [
            str(self.open_time),
            repr(self.open),
            repr(self.high),
            repr(self.low),
            repr(self.close),
            repr(self.volume),
            str(self.close_time),
            repr(self.quote_volume),
            str(self.trades),
            repr(self.taker_buy_base),
            repr(self.taker_buy_quote),
        ]

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Candle":
        return cls(
            open_time=int(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            close_time=int(row["close_time"]),
            quote_volume=float(row["quote_volume"]),
            trades=int(row["trades"]),
            taker_buy_base=float(row["taker_buy_base"]),
            taker_buy_quote=float(row["taker_buy_quote"]),
        )

    @property
    def taker_buy_ratio(self) -> float:
        if self.quote_volume <= 0:
            return 0.5
        ratio = self.taker_buy_quote / self.quote_volume
        return max(0.0, min(1.0, ratio))


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    price_precision: int
    quantity_precision: int
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float
    trigger_protect: float = 0.0

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        return float((d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step)

    def round_price(self, price: float) -> float:
        rounded = self._floor_to_step(price, self.tick_size)
        return round(rounded, self.price_precision)

    def round_quantity(self, quantity: float) -> float:
        rounded = self._floor_to_step(quantity, self.step_size)
        return round(rounded, self.quantity_precision)


@dataclass(frozen=True)
class Signal:
    time_utc: str
    time_cn: str
    symbol: str
    side: str
    leverage: int
    margin_usdt: float
    entry_reference: float
    take_profit_price: float
    stop_price: float
    target_pnl: float
    max_loss: float
    expires_at: str
    score: float
    reason_codes: tuple[str, ...]
    strategy_variant: str = "main"

    def as_dict(self) -> dict[str, Any]:
        return {
            "time_utc": self.time_utc,
            "time_cn": self.time_cn,
            "symbol": self.symbol,
            "side": self.side,
            "leverage": self.leverage,
            "margin_usdt": self.margin_usdt,
            "entry_reference": self.entry_reference,
            "take_profit_price": self.take_profit_price,
            "stop_price": self.stop_price,
            "target_pnl": self.target_pnl,
            "max_loss": self.max_loss,
            "expires_at": self.expires_at,
            "score": self.score,
            "reason_codes": list(self.reason_codes),
            "strategy_variant": self.strategy_variant,
        }


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    side: str
    signal_time: int
    entry_time: int
    exit_time: int
    leverage: int
    entry_price: float
    exit_price: float
    take_profit_price: float
    stop_price: float
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    outcome: str
    bars_held: int
    score: float
    reason_codes: tuple[str, ...]

    @property
    def won(self) -> bool:
        return self.net_pnl > 0
