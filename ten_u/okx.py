from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ten_u.config import StrategyConfig
from ten_u.market_calendar import is_us_market_non_workday
from ten_u.models import Candle, Signal
from ten_u.strategy import StrategyEngine


OKX_BASE_URL = "https://www.okx.com"
OKX_CACHE_DIR = Path("data/cache/okx")


@dataclass(frozen=True)
class OKXCredentials:
    api_key: str
    api_secret: str
    passphrase: str

    @classmethod
    def from_env(cls) -> "OKXCredentials":
        missing = [
            name
            for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(f"Missing OKX credential env vars: {', '.join(missing)}")
        return cls(
            api_key=os.environ["OKX_API_KEY"],
            api_secret=os.environ["OKX_API_SECRET"],
            passphrase=os.environ["OKX_API_PASSPHRASE"],
        )


@dataclass(frozen=True)
class OKXInstrument:
    inst_id: str
    base_ccy: str
    quote_ccy: str
    settle_ccy: str
    inst_category: str
    tick_sz: Decimal
    lot_sz: Decimal
    min_sz: Decimal
    ct_val: Decimal
    ct_val_ccy: str
    state: str
    max_leverage: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "OKXInstrument":
        inst_id = str(raw["instId"])
        derived_base, derived_quote = _derive_pair(inst_id)
        return cls(
            inst_id=inst_id,
            base_ccy=str(raw.get("baseCcy") or derived_base),
            quote_ccy=str(raw.get("quoteCcy") or derived_quote),
            settle_ccy=str(raw.get("settleCcy", "")),
            inst_category=str(raw.get("instCategory", "")),
            tick_sz=_decimal_field(raw, "tickSz", "0"),
            lot_sz=_decimal_field(raw, "lotSz", "1"),
            min_sz=_decimal_field(raw, "minSz", "1"),
            ct_val=_decimal_field(raw, "ctVal", "1"),
            ct_val_ccy=str(raw.get("ctValCcy", "")),
            state=str(raw.get("state", "")),
            max_leverage=int(float(raw.get("lever") or 0)),
        )

    def round_price(self, price: float) -> str:
        return _floor_decimal(Decimal(str(price)), self.tick_sz)

    def round_contracts(self, contracts: Decimal) -> str:
        rounded = Decimal(_floor_decimal(contracts, self.lot_sz))
        if rounded < self.min_sz:
            raise ValueError(f"{self.inst_id} size {rounded} is below minSz {self.min_sz}")
        return _decimal_to_str(rounded)

    def contracts_for_margin(self, price: float, margin_usdt: float, leverage: int) -> str:
        notional = Decimal(str(margin_usdt)) * Decimal(str(leverage))
        px = Decimal(str(price))
        if px <= 0:
            raise ValueError("price must be positive")
        if self.ct_val <= 0:
            raise ValueError(f"{self.inst_id} ctVal must be positive")
        if self.ct_val_ccy == self.quote_ccy:
            contract_notional = self.ct_val
        else:
            contract_notional = self.ct_val * px
        if contract_notional <= 0:
            raise ValueError("contract notional must be positive")
        return self.round_contracts(notional / contract_notional)


@dataclass(frozen=True)
class OKXOrderPlan:
    signal: Signal
    inst_id: str
    td_mode: str
    pos_mode: str
    side: str
    pos_side: str | None
    ord_type: str
    size_contracts: str
    leverage: int
    take_profit_price: str
    stop_price: str
    client_order_id: str
    attach_algo_client_id: str

    def request_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": self.inst_id,
            "tdMode": self.td_mode,
            "clOrdId": self.client_order_id,
            "side": self.side,
            "ordType": self.ord_type,
            "sz": self.size_contracts,
            "attachAlgoOrds": [
                {
                    "attachAlgoClOrdId": self.attach_algo_client_id,
                    "tpTriggerPx": self.take_profit_price,
                    "tpTriggerPxType": "last",
                    "tpOrdPx": "-1",
                    "slTriggerPx": self.stop_price,
                    "slTriggerPxType": "last",
                    "slOrdPx": "-1",
                }
            ],
        }
        if self.pos_side is not None:
            body["posSide"] = self.pos_side
        return body

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal.as_dict(),
            "inst_id": self.inst_id,
            "td_mode": self.td_mode,
            "pos_mode": self.pos_mode,
            "side": self.side,
            "pos_side": self.pos_side,
            "ord_type": self.ord_type,
            "size_contracts": self.size_contracts,
            "leverage": self.leverage,
            "take_profit_price": self.take_profit_price,
            "stop_price": self.stop_price,
            "client_order_id": self.client_order_id,
            "attach_algo_client_id": self.attach_algo_client_id,
            "request_body": self.request_body(),
        }


class OKXClient:
    def __init__(
        self,
        credentials: OKXCredentials | None = None,
        base_url: str = OKX_BASE_URL,
        simulated: bool = True,
        timeout: int = 20,
        retries: int = 4,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.simulated = simulated
        self.timeout = timeout
        self.retries = retries

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params, auth=False)

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params, auth=True)

    def private_post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body=body, auth=True)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        request_path = f"{path}{query}"
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ten-u-okx-demo/0.1",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        if auth:
            if self.credentials is None:
                raise RuntimeError("OKX credentials are required for private endpoints")
            timestamp = okx_timestamp()
            headers.update(
                okx_auth_headers(
                    self.credentials,
                    timestamp,
                    method,
                    request_path,
                    body_text,
                )
            )
        data = body_text.encode() if body is not None else None
        req = Request(
            f"{self.base_url}{request_path}",
            data=data,
            method=method,
            headers=headers,
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("code") not in (None, "0"):
                    raise RuntimeError(f"OKX API error {payload.get('code')}: {payload.get('msg')} {payload.get('data')}")
                return payload
            except HTTPError as exc:
                last_error = exc
                if exc.code < 500 and exc.code not in {429}:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"OKX HTTP {exc.code}: {detail}") from exc
            except (URLError, RuntimeError) as exc:
                last_error = exc
                if isinstance(exc, RuntimeError) and "OKX API error" in str(exc):
                    raise
            if attempt < self.retries:
                time.sleep(min(2.0, 0.25 * (2**attempt)))
        raise RuntimeError(f"OKX request failed: {last_error}") from last_error

    def instruments(self) -> dict[str, OKXInstrument]:
        payload = self.public_get("/api/v5/public/instruments", {"instType": "SWAP"})
        instruments = {}
        for raw in payload.get("data", []):
            inst = OKXInstrument.from_api(raw)
            if (
                inst.quote_ccy == "USDT"
                and inst.settle_ccy == "USDT"
                and inst.inst_category == "1"
                and not inst.inst_id.startswith("TEST")
                and inst.state == "live"
                and inst.tick_sz > 0
                and inst.lot_sz > 0
                and inst.ct_val > 0
            ):
                instruments[inst.inst_id] = inst
        return instruments

    def tickers(self) -> list[dict[str, Any]]:
        payload = self.public_get("/api/v5/market/tickers", {"instType": "SWAP"})
        return list(payload.get("data", []))

    def top_usdt_swap_instruments(self, top: int = 20, min_quote_volume: float = 50_000_000) -> list[str]:
        instruments = self.instruments()
        rows = []
        for ticker in self.tickers():
            inst_id = ticker.get("instId")
            if inst_id not in instruments:
                continue
            quote_vol = _ticker_quote_volume(ticker)
            if quote_vol < min_quote_volume:
                continue
            rows.append((quote_vol, inst_id))
        rows.sort(reverse=True)
        return [inst_id for _, inst_id in rows[:top]]

    def candles(self, inst_id: str, bar: str = "1m", limit: int = 720) -> list[Candle]:
        payload = self.public_get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": str(limit)},
        )
        return closed_okx_candles(payload.get("data", []), bar)

    def history_candles(
        self,
        inst_id: str,
        bar: str = "1m",
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
    ) -> list[Candle]:
        params: dict[str, Any] = {
            "instId": inst_id,
            "bar": bar,
            "limit": str(min(limit, 100)),
        }
        if after is not None:
            params["after"] = str(after)
        if before is not None:
            params["before"] = str(before)
        payload = self.public_get("/api/v5/market/history-candles", params)
        return closed_okx_candles(payload.get("data", []), bar)

    def fetch_candles_range(
        self,
        inst_id: str,
        bar: str,
        start_time: int,
        end_time: int,
        sleep_seconds: float = 0.05,
    ) -> list[Candle]:
        out: list[Candle] = []
        cursor = end_time + _bar_to_ms(bar)
        while cursor > start_time:
            batch = self.history_candles(inst_id, bar, after=cursor, limit=100)
            if not batch:
                break
            out.extend(c for c in batch if start_time <= c.open_time <= end_time)
            oldest = min(c.open_time for c in batch)
            if oldest <= start_time or oldest >= cursor:
                break
            cursor = oldest
            time.sleep(sleep_seconds)
        return dedupe_okx_candles(out)

    def set_leverage(self, inst_id: str, leverage: int, pos_side: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": "isolated",
        }
        if pos_side is not None:
            body["posSide"] = pos_side
        return self.private_post("/api/v5/account/set-leverage", body)

    def place_order(self, order: OKXOrderPlan) -> dict[str, Any]:
        return self.private_post("/api/v5/trade/order", order.request_body())

    def close_position(self, inst_id: str, pos_side: str | None = None, mgn_mode: str = "isolated") -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "mgnMode": mgn_mode,
            "autoCxl": True,
        }
        if pos_side is not None:
            body["posSide"] = pos_side
        return self.private_post("/api/v5/trade/close-position", body)

    def fills_history(
        self,
        inst_type: str = "SWAP",
        inst_id: str | None = None,
        begin: int | None = None,
        end: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "instType": inst_type,
            "limit": str(limit),
        }
        if inst_id is not None:
            params["instId"] = inst_id
        if begin is not None:
            params["begin"] = str(begin)
        if end is not None:
            params["end"] = str(end)
        payload = self.private_get("/api/v5/trade/fills-history", params)
        return list(payload.get("data", []))

    def positions(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"instType": inst_type}
        if inst_id is not None:
            params["instId"] = inst_id
        payload = self.private_get("/api/v5/account/positions", params)
        return list(payload.get("data", []))


def okx_timestamp() -> str:
    now = datetime.now(UTC)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    payload = f"{timestamp}{method.upper()}{request_path}{body}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def okx_auth_headers(
    credentials: OKXCredentials,
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> dict[str, str]:
    return {
        "OK-ACCESS-KEY": credentials.api_key,
        "OK-ACCESS-SIGN": okx_sign(credentials.api_secret, timestamp, method, request_path, body),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": credentials.passphrase,
    }


def okx_candle_from_row(row: list[Any], bar: str = "1m") -> Candle:
    open_time = int(row[0])
    interval_ms = _bar_to_ms(bar)
    quote_volume = float(row[7]) if len(row) > 7 and row[7] not in ("", None) else 0.0
    return Candle(
        open_time=open_time,
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=open_time + interval_ms - 1,
        quote_volume=quote_volume,
        trades=0,
        taker_buy_base=0.0,
        taker_buy_quote=quote_volume / 2,
    )


def closed_okx_candles(
    rows: list[list[Any]],
    bar: str = "1m",
    now_ms: int | None = None,
) -> list[Candle]:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    candles = []
    for row in rows:
        candle = okx_candle_from_row(row, bar)
        if _okx_row_is_closed(row, candle, now_ms):
            candles.append(candle)
    candles.sort(key=lambda c: c.open_time)
    return candles


def dedupe_okx_candles(candles: list[Candle]) -> list[Candle]:
    by_time = {c.open_time: c for c in candles}
    return [by_time[t] for t in sorted(by_time)]


OKX_CSV_FIELDS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
]


def okx_cache_path(inst_id: str, bar: str, start_time: int, end_time: int) -> Path:
    safe_inst_id = inst_id.replace("/", "_")
    return OKX_CACHE_DIR / f"{safe_inst_id}_{bar}_{start_time}_{end_time}.csv"


def read_cached_okx_candles(inst_id: str, bar: str, start_time: int, end_time: int) -> list[Candle]:
    path = okx_cache_path(inst_id, bar, start_time, end_time)
    if not path.exists():
        return []
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        return [Candle.from_csv_row(row) for row in reader]


def write_cached_okx_candles(inst_id: str, bar: str, start_time: int, end_time: int, candles: list[Candle]) -> None:
    path = okx_cache_path(inst_id, bar, start_time, end_time)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OKX_CSV_FIELDS)
        for candle in candles:
            writer.writerow(candle.to_csv_row())


def load_or_fetch_okx_candles(
    client: OKXClient,
    inst_id: str,
    bar: str,
    start_time: int,
    end_time: int,
    refresh: bool = False,
) -> list[Candle]:
    if not refresh:
        cached = read_cached_okx_candles(inst_id, bar, start_time, end_time)
        if cached:
            return cached
    candles = client.fetch_candles_range(inst_id, bar, start_time, end_time)
    write_cached_okx_candles(inst_id, bar, start_time, end_time, candles)
    return candles


def best_okx_signal(
    client: OKXClient,
    inst_ids: list[str],
    cfg: StrategyConfig,
    lookback: int = 720,
    bar: str = "1m",
    max_closed_candle_age_ms: int = 180_000,
    instruments: dict[str, OKXInstrument] | None = None,
) -> Signal | None:
    best: Signal | None = None
    now_ms = int(time.time() * 1000)
    for inst_id in inst_ids:
        symbol_cfg = cfg
        instrument = instruments.get(inst_id) if instruments is not None else None
        if instrument is not None and instrument.max_leverage > 0:
            max_leverage = min(cfg.max_leverage, instrument.max_leverage)
            if max_leverage < cfg.min_leverage:
                continue
            symbol_cfg = cfg.with_updates(max_leverage=max_leverage)
        candles = client.candles(inst_id, bar, lookback)
        if len(candles) < max(120, symbol_cfg.ha_range_window + symbol_cfg.ha_deviation_window + symbol_cfg.atr_period):
            continue
        if now_ms - candles[-1].close_time > max_closed_candle_age_ms:
            continue
        if symbol_cfg.us_nonworkday_only and not is_us_market_non_workday(candles[-1].close_time):
            continue
        signal = StrategyEngine(inst_id, candles, symbol_cfg).evaluate(len(candles) - 1)
        if signal is None:
            continue
        if best is None or signal.score > best.score:
            best = signal
    return best


def build_order_plan(
    signal: Signal,
    instrument: OKXInstrument,
    pos_mode: str = "net",
    td_mode: str = "isolated",
) -> OKXOrderPlan:
    side = "buy" if signal.side == "LONG" else "sell"
    pos_side = None
    if pos_mode == "long-short":
        pos_side = "long" if signal.side == "LONG" else "short"
    elif pos_mode != "net":
        raise ValueError("pos_mode must be 'net' or 'long-short'")
    client_order_id = _client_id("tu")
    attach_algo_client_id = _client_id("tualgo")
    return OKXOrderPlan(
        signal=signal,
        inst_id=instrument.inst_id,
        td_mode=td_mode,
        pos_mode=pos_mode,
        side=side,
        pos_side=pos_side,
        ord_type="market",
        size_contracts=instrument.contracts_for_margin(
            signal.entry_reference,
            signal.margin_usdt,
            signal.leverage,
        ),
        leverage=signal.leverage,
        take_profit_price=instrument.round_price(signal.take_profit_price),
        stop_price=instrument.round_price(signal.stop_price),
        client_order_id=client_order_id,
        attach_algo_client_id=attach_algo_client_id,
    )


def _floor_decimal(value: Decimal, step: Decimal) -> str:
    if step <= 0:
        return _decimal_to_str(value)
    return _decimal_to_str((value / step).to_integral_value(rounding=ROUND_DOWN) * step)


def _decimal_to_str(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _bar_to_ms(bar: str) -> int:
    if bar.endswith("s"):
        return int(bar[:-1]) * 1_000
    if bar.endswith("m"):
        return int(bar[:-1]) * 60_000
    if bar.endswith("H"):
        return int(bar[:-1]) * 60 * 60_000
    if bar.endswith("D"):
        return int(bar[:-1]) * 24 * 60 * 60_000
    raise ValueError(f"unsupported OKX bar: {bar}")


def _client_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%y%m%d%H%M%S")
    suffix = str(int(time.time() * 1000))[-6:]
    return f"{prefix}{stamp}{suffix}"[:32]


def _okx_row_is_closed(row: list[Any], candle: Candle, now_ms: int) -> bool:
    if len(row) > 8 and row[8] not in ("", None):
        return str(row[8]) == "1"
    return candle.close_time < now_ms


def _decimal_field(raw: dict[str, Any], key: str, default: str) -> Decimal:
    value = raw.get(key, default)
    if value in ("", None):
        value = default
    return Decimal(str(value))


def _derive_pair(inst_id: str) -> tuple[str, str]:
    parts = inst_id.split("-")
    if len(parts) >= 3:
        quote = parts[1]
        if quote.endswith("_UM"):
            quote = quote[:-3]
        return parts[0], quote
    return "", ""


def _ticker_quote_volume(ticker: dict[str, Any]) -> float:
    if ticker.get("volCcyQuote24h") not in ("", None):
        return float(ticker.get("volCcyQuote24h") or 0.0)
    vol_ccy = float(ticker.get("volCcy24h") or 0.0)
    last = float(ticker.get("last") or 0.0)
    return vol_ccy * last
