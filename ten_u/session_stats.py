from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ten_u.okx import OKXClient, OKXOrderPlan


@dataclass(frozen=True)
class SessionOrder:
    submitted_at: datetime
    inst_id: str
    signal_key: str
    side: str
    pos_side: str | None
    leverage: int
    size_contracts: str
    client_order_id: str
    order_id: str
    accepted: bool
    s_code: str
    s_msg: str


@dataclass
class OKXSessionStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    scan_count: int = 0
    no_signal_count: int = 0
    dry_run_signal_count: int = 0
    duplicate_signal_count: int = 0
    scan_error_count: int = 0
    errors: list[str] = field(default_factory=list)
    orders: list[SessionOrder] = field(default_factory=list)

    def record_scan(self) -> None:
        self.scan_count += 1

    def record_no_signal(self) -> None:
        self.no_signal_count += 1

    def record_dry_run_signal(self) -> None:
        self.dry_run_signal_count += 1

    def record_duplicate_signal(self) -> None:
        self.duplicate_signal_count += 1

    def record_error(self, error: Exception | str) -> None:
        self.scan_error_count += 1
        self.errors.append(str(error))

    def record_order(
        self,
        order_plan: OKXOrderPlan,
        response: dict[str, Any],
        signal_key: str,
        submitted_at: datetime | None = None,
    ) -> SessionOrder:
        rows = response.get("data", []) if isinstance(response, dict) else []
        row = rows[0] if rows else {}
        s_code = str(row.get("sCode", response.get("code", "")) if isinstance(response, dict) else "")
        order = SessionOrder(
            submitted_at=submitted_at or datetime.now(UTC),
            inst_id=order_plan.inst_id,
            signal_key=signal_key,
            side=order_plan.side,
            pos_side=order_plan.pos_side,
            leverage=order_plan.leverage,
            size_contracts=order_plan.size_contracts,
            client_order_id=order_plan.client_order_id,
            order_id=str(row.get("ordId", "")),
            accepted=isinstance(response, dict) and response.get("code") == "0" and s_code == "0",
            s_code=s_code,
            s_msg=str(row.get("sMsg", "")),
        )
        self.orders.append(order)
        return order

    def summary(self, client: OKXClient | None = None) -> dict[str, Any]:
        now = datetime.now(UTC)
        accepted_orders = [order for order in self.orders if order.accepted]
        rejected_orders = [order for order in self.orders if not order.accepted]
        payload: dict[str, Any] = {
            "mode": "SESSION_SUMMARY",
            "started_at_utc": self.started_at.isoformat(),
            "ended_at_utc": now.isoformat(),
            "runtime_seconds": round((now - self.started_at).total_seconds(), 3),
            "scans": self.scan_count,
            "no_signal_scans": self.no_signal_count,
            "dry_run_signals": self.dry_run_signal_count,
            "duplicate_signals_skipped": self.duplicate_signal_count,
            "scan_errors": self.scan_error_count,
            "orders_sent": len(self.orders),
            "orders_accepted": len(accepted_orders),
            "orders_rejected": len(rejected_orders),
            "accepted_order_ids": [order.order_id for order in accepted_orders if order.order_id],
            "rejected_orders": [
                {
                    "client_order_id": order.client_order_id,
                    "inst_id": order.inst_id,
                    "s_code": order.s_code,
                    "s_msg": order.s_msg,
                }
                for order in rejected_orders
            ],
            "recent_errors": self.errors[-5:],
        }
        if client is not None and accepted_orders:
            payload["realized"] = self._realized_summary(client)
            payload["open_positions"] = self._position_summary(client)
        else:
            payload["realized"] = {
                "message": "No accepted orders or no OKX private client; realized PnL was not queried.",
            }
        return payload

    def _realized_summary(self, client: OKXClient) -> dict[str, Any]:
        try:
            fills = self._load_session_fills(client)
        except Exception as exc:  # pragma: no cover - depends on OKX account/API state
            return {"error": str(exc)}
        gross_pnl = 0.0
        fees = 0.0
        exit_orders: dict[str, dict[str, float]] = {}
        for fill in fills:
            fill_pnl = _float_field(fill, "fillPnl")
            fee = _float_field(fill, "fee")
            gross_pnl += fill_pnl
            fees += fee
            if fill.get("fillPnl") in ("", None):
                continue
            key = str(fill.get("ordId") or fill.get("tradeId") or len(exit_orders))
            if key not in exit_orders:
                exit_orders[key] = {"pnl": 0.0, "fee": 0.0}
            exit_orders[key]["pnl"] += fill_pnl
            exit_orders[key]["fee"] += fee
        closed_trade_nets = [row["pnl"] + row["fee"] for row in exit_orders.values() if row["pnl"] != 0.0]
        wins = sum(1 for value in closed_trade_nets if value > 0)
        losses = sum(1 for value in closed_trade_nets if value <= 0)
        total_closed = wins + losses
        net_pnl = gross_pnl + fees
        return {
            "fill_scope": "Accepted session instruments since process start; avoid manual trades on the same instruments during a run.",
            "fills": len(fills),
            "closed_trades": total_closed,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / total_closed * 100, 2) if total_closed else 0.0,
            "gross_realized_pnl_usdt": round(gross_pnl, 6),
            "fees_usdt": round(fees, 6),
            "net_realized_pnl_usdt": round(net_pnl, 6),
            "expectancy_usdt": round(net_pnl / total_closed, 6) if total_closed else 0.0,
        }

    def _position_summary(self, client: OKXClient) -> dict[str, Any]:
        inst_ids = sorted({order.inst_id for order in self.orders if order.accepted})
        positions: list[dict[str, Any]] = []
        errors: list[str] = []
        for inst_id in inst_ids:
            try:
                positions.extend(client.positions("SWAP", inst_id))
            except Exception as exc:  # pragma: no cover - depends on OKX account/API state
                errors.append(f"{inst_id}: {exc}")
        open_positions = []
        unrealized = 0.0
        for pos in positions:
            if _float_value(pos.get("pos")) == 0.0:
                continue
            upl = _float_value(pos.get("upl"))
            unrealized += upl
            open_positions.append(
                {
                    "inst_id": pos.get("instId"),
                    "pos_side": pos.get("posSide"),
                    "pos": pos.get("pos"),
                    "avg_px": pos.get("avgPx"),
                    "upl": pos.get("upl"),
                    "lever": pos.get("lever"),
                }
            )
        return {
            "open_position_count": len(open_positions),
            "unrealized_pnl_usdt": round(unrealized, 6),
            "positions": open_positions,
            "errors": errors,
        }

    def _load_session_fills(self, client: OKXClient) -> list[dict[str, Any]]:
        inst_ids = sorted({order.inst_id for order in self.orders if order.accepted})
        begin_ms = int(self.started_at.timestamp() * 1000)
        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        fills: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for inst_id in inst_ids:
            for fill in client.fills_history("SWAP", inst_id=inst_id, begin=begin_ms, end=end_ms, limit=100):
                key = (
                    str(fill.get("instId", "")),
                    str(fill.get("ordId", "")),
                    str(fill.get("tradeId", "")),
                    str(fill.get("fillTime", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                fills.append(fill)
        return fills


def _float_field(row: dict[str, Any], key: str) -> float:
    return _float_value(row.get(key))


def _float_value(value: Any) -> float:
    if value in ("", None):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
