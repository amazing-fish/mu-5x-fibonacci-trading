from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable


OKX_BASE_URL = "https://www.okx.com"
OKX_PLACE_ORDER_PATH = "/api/v5/trade/order"
OKX_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")


@dataclass(frozen=True)
class OKXCredentials:
    api_key: str
    secret_key: str
    passphrase: str

    @classmethod
    def from_env(cls, prefix: str = "OKX", source: str | None = None) -> "OKXCredentials":
        values = _credential_values_from_source(prefix, source)
        api_key = values.get(f"{prefix}_API_KEY", "")
        secret_key = values.get(f"{prefix}_SECRET_KEY", "")
        passphrase = values.get(f"{prefix}_PASSPHRASE", "")
        if not api_key or not secret_key or not passphrase:
            raise RuntimeError(
                f"Missing {prefix}_API_KEY, {prefix}_SECRET_KEY, or {prefix}_PASSPHRASE"
            )
        return cls(api_key=api_key, secret_key=secret_key, passphrase=passphrase)


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    path: str
    body: str | None
    headers: dict[str, str]

    def sanitized(self) -> dict[str, Any]:
        headers = {
            key: ("<redacted>" if key in {"OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-PASSPHRASE"} else value)
            for key, value in self.headers.items()
        }
        return {"method": self.method, "path": self.path, "body": self.body, "headers": headers}


@dataclass(frozen=True)
class DemoOrderRequest:
    inst_id: str
    side: str
    size: str
    order_type: str = "market"
    price: str | None = None
    td_mode: str = "isolated"
    client_order_id: str | None = None
    pos_side: str | None = None
    reduce_only: bool | None = None

    def to_body(self) -> dict[str, Any]:
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.order_type not in {"market", "limit", "post_only", "fok", "ioc"}:
            raise ValueError("unsupported order_type")
        if self.order_type == "market" and self.price is not None:
            raise ValueError("price is not allowed for market orders")
        if self.order_type in {"limit", "post_only", "fok", "ioc"} and self.price is None:
            raise ValueError("price is required for non-market orders")
        body: dict[str, Any] = {
            "instId": self.inst_id,
            "tdMode": self.td_mode,
            "side": self.side,
            "ordType": self.order_type,
            "sz": self.size,
        }
        if self.price is not None:
            body["px"] = self.price
        if self.client_order_id is not None:
            if not OKX_CLIENT_ORDER_ID_PATTERN.fullmatch(self.client_order_id):
                raise ValueError("client_order_id must be 1-32 ASCII alphanumeric characters")
            body["clOrdId"] = self.client_order_id
        if self.pos_side is not None:
            body["posSide"] = self.pos_side
        if self.reduce_only is not None:
            body["reduceOnly"] = self.reduce_only
        return body


@dataclass(frozen=True)
class OKXInstrumentSpec:
    inst_id: str
    tick_size: Decimal
    lot_size: Decimal
    contract_value: Decimal

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "OKXInstrumentSpec":
        return cls(
            inst_id=str(row["instId"]),
            tick_size=Decimal(str(row["tickSz"])),
            lot_size=Decimal(str(row["lotSz"])),
            contract_value=Decimal(str(row.get("ctVal", "1"))),
        )

    def price_to_string(self, price: float | str | Decimal) -> str:
        return _decimal_to_string(_floor_to_step(Decimal(str(price)), self.tick_size))

    def size_to_string(self, size: float | str | Decimal) -> str:
        return _decimal_to_string(_floor_to_step(Decimal(str(size)), self.lot_size))

    def size_for_notional(self, notional_usdt: float | str | Decimal, *, price: float | str | Decimal) -> str:
        price_value = Decimal(str(price))
        if price_value <= 0:
            raise ValueError("price must be positive")
        if self.contract_value <= 0:
            raise ValueError("contract_value must be positive")
        raw_size = Decimal(str(notional_usdt)) / (price_value * self.contract_value)
        return self.size_to_string(raw_size)


Transport = Callable[[str, str], dict[str, Any]]


class OKXRestClient:
    def __init__(
        self,
        *,
        credentials: OKXCredentials | None,
        demo: bool,
        base_url: str = OKX_BASE_URL,
        transport: Callable[..., dict[str, Any]] | None = None,
        timeout: int = 20,
        timestamp_factory: Callable[[], str] | None = None,
    ) -> None:
        self.credentials = credentials
        self.demo = demo
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _urllib_transport
        self.timeout = timeout
        self.timestamp_factory = timestamp_factory or _utc_timestamp

    def get_balance(self, *, ccy: str | None = None) -> dict[str, Any]:
        query = {"ccy": ccy} if ccy else None
        return self._request("GET", "/api/v5/account/balance", query=query, private=True)

    def get_positions(self, *, inst_type: str | None = None, inst_id: str | None = None) -> dict[str, Any]:
        query = {}
        if inst_type:
            query["instType"] = inst_type
        if inst_id:
            query["instId"] = inst_id
        return self._request("GET", "/api/v5/account/positions", query=query or None, private=True)

    def get_open_orders(self, *, inst_type: str | None = None, inst_id: str | None = None) -> dict[str, Any]:
        query = {}
        if inst_type:
            query["instType"] = inst_type
        if inst_id:
            query["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-pending", query=query or None, private=True)

    def get_instruments(self, *, inst_type: str, inst_id: str | None = None) -> dict[str, Any]:
        query = {"instType": inst_type}
        if inst_id:
            query["instId"] = inst_id
        return self._request("GET", "/api/v5/public/instruments", query=query, private=False)

    def set_leverage(self, *, inst_id: str, lever: float | int | str, margin_mode: str = "isolated") -> dict[str, Any]:
        body = {
            "instId": inst_id,
            "lever": _decimal_to_string(Decimal(str(lever))),
            "mgnMode": margin_mode,
        }
        return self._request("POST", "/api/v5/account/set-leverage", body=body, private=True)

    def prepare_demo_order(self, request: DemoOrderRequest) -> PreparedRequest:
        if not self.demo:
            raise PermissionError("demo order preparation requires a demo client")
        body = _compact_json(request.to_body())
        return self._prepare_request("POST", OKX_PLACE_ORDER_PATH, body=body, private=True)

    def place_demo_order(self, request: DemoOrderRequest, *, confirm_demo_order: bool) -> dict[str, Any]:
        if not confirm_demo_order:
            raise PermissionError("confirm_demo_order=True is required before sending a demo order")
        prepared = self.prepare_demo_order(request)
        return self.transport(
            prepared.method,
            f"{self.base_url}{prepared.path}",
            headers=prepared.headers,
            body=prepared.body,
            timeout=self.timeout,
        )

    def place_limit_buy(
        self,
        *,
        inst_id: str,
        size: str,
        price: str,
        client_order_id: str,
        confirm_demo_order: bool,
        td_mode: str = "isolated",
        pos_side: str | None = None,
    ) -> dict[str, Any]:
        return self.place_demo_order(
            DemoOrderRequest(
                inst_id=inst_id,
                side="buy",
                size=size,
                order_type="limit",
                price=price,
                td_mode=td_mode,
                client_order_id=client_order_id,
                pos_side=pos_side,
            ),
            confirm_demo_order=confirm_demo_order,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        private: bool,
    ) -> dict[str, Any]:
        request_path = _request_path(path, query)
        body_text = _compact_json(body) if body is not None else None
        prepared = self._prepare_request(method, request_path, body=body_text, private=private)
        return self.transport(
            method,
            f"{self.base_url}{prepared.path}",
            headers=prepared.headers,
            body=prepared.body,
            timeout=self.timeout,
        )

    def _prepare_request(self, method: str, path: str, *, body: str | None, private: bool) -> PreparedRequest:
        method = method.upper()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        if private:
            if self.credentials is None:
                raise RuntimeError("OKX credentials are required for private requests")
            timestamp = self.timestamp_factory()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.credentials.api_key,
                    "OK-ACCESS-SIGN": _sign(
                        timestamp=timestamp,
                        method=method,
                        request_path=path,
                        body=body or "",
                        secret_key=self.credentials.secret_key,
                    ),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
                }
            )
        return PreparedRequest(method=method, path=path, body=body, headers=headers)


@dataclass(frozen=True)
class ShadowExecutionEvent:
    event_id: str
    symbol: str
    action: str
    plan_price: float
    observed_price: float | None
    quantity: float
    status: str
    reason: str
    timestamp_ms: int

    @property
    def slippage_bps(self) -> float | None:
        if self.observed_price is None or self.plan_price == 0:
            return None
        direction = 1 if self.action == "buy" else -1
        return direction * ((self.observed_price / self.plan_price) - 1) * 10_000


@dataclass(frozen=True)
class ShadowExecutionMetrics:
    total_events: int
    filled_events: int
    missed_events: int
    fill_rate: float
    average_slippage_bps: float


class ShadowExecutionLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: ShadowExecutionEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_compact_json(asdict(event)) + "\n")

    def read_events(self) -> list[ShadowExecutionEvent]:
        if not self.path.exists():
            return []
        events = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(ShadowExecutionEvent(**json.loads(line)))
        return events

    def metrics(self) -> ShadowExecutionMetrics:
        events = self.read_events()
        filled = [event for event in events if event.status == "filled"]
        missed = [event for event in events if event.status == "missed"]
        slippages = [event.slippage_bps for event in filled if event.slippage_bps is not None]
        return ShadowExecutionMetrics(
            total_events=len(events),
            filled_events=len(filled),
            missed_events=len(missed),
            fill_rate=(len(filled) / len(events)) if events else 0.0,
            average_slippage_bps=(sum(slippages) / len(slippages)) if slippages else 0.0,
        )


def build_shadow_event(
    *,
    event_id: str,
    symbol: str,
    action: str,
    plan_price: float,
    observed_price: float | None,
    quantity: float,
    status: str,
    reason: str,
    timestamp_ms: int,
) -> ShadowExecutionEvent:
    return ShadowExecutionEvent(
        event_id=event_id,
        symbol=symbol,
        action=action,
        plan_price=plan_price,
        observed_price=observed_price,
        quantity=quantity,
        status=status,
        reason=reason,
        timestamp_ms=timestamp_ms,
    )


def _request_path(path: str, query: dict[str, str] | None) -> str:
    if not query:
        return path
    return f"{path}?{urllib.parse.urlencode(query)}"


def _credential_values_from_source(prefix: str, source: str | None) -> dict[str, str]:
    resolved_source = (source or os.environ.get(f"{prefix}_ENV_SOURCE") or "auto").strip().lower()
    if resolved_source == "auto":
        for values in _auto_credential_sources(prefix):
            if _credential_values_are_complete(prefix, values):
                return values
        return _process_credential_values(prefix)
    if resolved_source == "process":
        return _process_credential_values(prefix)
    if resolved_source in {"user", "machine"}:
        return _read_windows_environment(prefix, resolved_source)
    raise ValueError(f"unsupported {prefix}_ENV_SOURCE: {resolved_source}")


def _auto_credential_sources(prefix: str) -> list[dict[str, str]]:
    sources = []
    if os.name == "nt":
        sources.extend(
            [
                _read_windows_environment(prefix, "user"),
                _read_windows_environment(prefix, "machine"),
            ]
        )
    sources.append(_process_credential_values(prefix))
    return sources


def _process_credential_values(prefix: str) -> dict[str, str]:
    return {
        name: os.environ.get(name, "")
        for name in _credential_env_names(prefix)
    }


def _read_windows_environment(prefix: str, scope: str) -> dict[str, str]:
    if os.name != "nt":
        return {}
    try:
        import winreg
    except ImportError:
        return {}

    if scope == "user":
        root = winreg.HKEY_CURRENT_USER
        subkey = "Environment"
    elif scope == "machine":
        root = winreg.HKEY_LOCAL_MACHINE
        subkey = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    else:
        raise ValueError(f"unsupported Windows environment scope: {scope}")

    values = {}
    try:
        with winreg.OpenKey(root, subkey) as key:
            for name in _credential_env_names(prefix):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    value = ""
                values[name] = str(value) if value is not None else ""
    except OSError:
        return {}
    return values


def _credential_env_names(prefix: str) -> tuple[str, str, str]:
    return (
        f"{prefix}_API_KEY",
        f"{prefix}_SECRET_KEY",
        f"{prefix}_PASSPHRASE",
    )


def _credential_values_are_complete(prefix: str, values: Mapping[str, str]) -> bool:
    return all(values.get(name) for name in _credential_env_names(prefix))


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _decimal_to_string(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _sign(*, timestamp: str, method: str, request_path: str, body: str, secret_key: str) -> str:
    message = f"{timestamp}{method.upper()}{request_path}{body}".encode("utf-8")
    digest = hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _urllib_transport(method: str, url: str, *, headers: dict[str, str], body: str | None, timeout: int) -> dict[str, Any]:
    data = body.encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
