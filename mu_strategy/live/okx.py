from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from mu_strategy.execution.instruments import OKXInstrumentSpec
from mu_strategy.market_data.http_pool import ProxyTarget, resolve_proxy


OKX_BASE_URL = "https://www.okx.com"
OKX_PLACE_ORDER_PATH = "/api/v5/trade/order"
OKX_CANCEL_ORDER_PATH = "/api/v5/trade/cancel-order"
OKX_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")
_RETRYABLE_GET_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 5.0
_SSL_CONTEXT_LOCK = threading.Lock()
_DEFAULT_SSL_CONTEXT: ssl.SSLContext | None = None


def _default_ssl_context() -> ssl.SSLContext:
    """Cache the default TLS context once per process (avoids multi-second Windows cert-store reloads)."""
    global _DEFAULT_SSL_CONTEXT
    context = _DEFAULT_SSL_CONTEXT
    if context is None:
        with _SSL_CONTEXT_LOCK:
            context = _DEFAULT_SSL_CONTEXT
            if context is None:
                context = ssl.create_default_context()
                _DEFAULT_SSL_CONTEXT = context
    return context


class OKXHTTPError(RuntimeError):
    """Structured HTTP error preserving OKX JSON code/msg payload when present."""

    def __init__(
        self,
        status: int,
        *,
        code: str | None = None,
        msg: str = "",
        payload: dict[str, Any] | None = None,
        body: str = "",
        url: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.msg = msg
        self.payload = payload
        self.body = body
        self.url = url
        detail = f"code={code} msg={msg}" if code is not None else (msg or body[:200] or "empty response")
        super().__init__(f"OKX HTTP {status}: {detail}")


def _decode_http_error(status: int, raw_body: bytes, *, url: str = "") -> OKXHTTPError:
    text = raw_body.decode("utf-8", errors="replace")
    payload: dict[str, Any] | None = None
    code: str | None = None
    msg = ""
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            msg = text.strip()[:200]
        else:
            if isinstance(parsed, dict):
                payload = parsed
                raw_code = parsed.get("code")
                code = str(raw_code) if raw_code is not None else None
                msg = str(parsed.get("msg") or "")
            else:
                msg = text.strip()[:200]
    return OKXHTTPError(status, code=code, msg=msg, payload=payload, body=text, url=url)


def _parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        delay = float(header_value.strip())
    except ValueError:
        return None
    if delay < 0:
        return None
    return min(delay, _MAX_RETRY_AFTER_SECONDS)


class _PooledHTTPTransport:
    """Thread-safe persistent HTTP/HTTPS keep-alive transport with bounded GET retries."""

    def __init__(
        self,
        *,
        max_get_attempts: int = 3,
        backoff_seconds: float = 0.2,
        sleeper: Callable[[float], None] = time.sleep,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if max_get_attempts < 1:
            raise ValueError("max_get_attempts must be at least 1")
        self.max_get_attempts = max_get_attempts
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper
        self.ssl_context = ssl_context
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_connections: set[http.client.HTTPConnection] = set()

    def _pool(self) -> dict[tuple[Any, ...], http.client.HTTPConnection]:
        pool = getattr(self._local, "connections", None)
        if pool is None:
            pool = {}
            self._local.connections = pool
        return pool

    def _acquire(self, key: tuple[Any, ...]) -> tuple[http.client.HTTPConnection, bool]:
        pool = self._pool()
        conn = pool.get(key)
        if conn is not None:
            if conn.sock is not None:
                return conn, True
            self._evict(key, conn)
        if len(key) == 4:
            scheme, host, port, timeout = key
            proxy = resolve_proxy(scheme, host, port)
        else:
            scheme, host, port, timeout, proxy = key
        if scheme == "https":
            context = self.ssl_context if self.ssl_context is not None else _default_ssl_context()
            if isinstance(proxy, ProxyTarget):
                conn = http.client.HTTPSConnection(proxy.host, proxy.port, timeout=timeout, context=context)
                tunnel_headers = {"Proxy-Authorization": proxy.auth_header} if proxy.auth_header else None
                conn.set_tunnel(host, port=port, headers=tunnel_headers)
            else:
                conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
        elif scheme == "http":
            if isinstance(proxy, ProxyTarget):
                if proxy.scheme == "https":
                    context = self.ssl_context if self.ssl_context is not None else _default_ssl_context()
                    conn = http.client.HTTPSConnection(proxy.host, proxy.port, timeout=timeout, context=context)
                else:
                    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
        else:
            raise ValueError(f"unsupported URL scheme: {scheme}")
        pool[key] = conn
        with self._lock:
            self._all_connections.add(conn)
        return conn, False

    def _evict(self, key: tuple[Any, ...], conn: http.client.HTTPConnection) -> None:
        pool = self._pool()
        if pool.get(key) is conn:
            del pool[key]
        with self._lock:
            self._all_connections.discard(conn)
        try:
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            connections = list(self._all_connections)
            self._all_connections.clear()
        pool = getattr(self._local, "connections", None)
        if pool is not None:
            pool.clear()
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: str | None,
        timeout: int,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if not host:
            raise ValueError(f"invalid URL: {url}")
        port = parsed.port or (443 if scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        method_upper = method.upper()
        idempotent = method_upper == "GET"
        max_attempts = self.max_get_attempts if idempotent else 1
        encoded_body = body.encode("utf-8") if body is not None else None
        request_headers = dict(headers)
        if not any(key.lower() == "connection" for key in request_headers):
            request_headers["Connection"] = "keep-alive"

        proxy = resolve_proxy(scheme, host, parsed.port)
        if proxy is not None and proxy.scheme not in {"http", "https"}:
            ssl_context = (
                self.ssl_context
                if self.ssl_context is not None
                else (_default_ssl_context() if scheme == "https" else None)
            )
            for attempt in range(1, max_attempts + 1):
                req = urllib.request.Request(url, data=encoded_body, headers=request_headers, method=method_upper)
                try:
                    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    raw_body = exc.read() if exc.fp is not None else b""
                    retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                    error = _decode_http_error(exc.code, raw_body, url=url)
                    if idempotent and (exc.code in _RETRYABLE_GET_STATUSES or 500 <= exc.code < 600) and attempt < max_attempts:
                        delay = _parse_retry_after(retry_after)
                        self.sleeper(delay if delay is not None else self.backoff_seconds * attempt)
                        continue
                    raise error from exc
                except (OSError, http.client.HTTPException, urllib.error.URLError):
                    if not idempotent or attempt >= max_attempts:
                        raise
                    self.sleeper(self.backoff_seconds * attempt)
                    continue
            raise RuntimeError("unreachable")

        if scheme == "http" and proxy is not None:
            host_part = f"[{host}]" if ":" in host else host
            netloc = f"{host_part}:{parsed.port}" if parsed.port is not None else host_part
            request_target = f"http://{netloc}{target}"
            if proxy.auth_header and not any(k.lower() == "proxy-authorization" for k in request_headers):
                request_headers["Proxy-Authorization"] = proxy.auth_header
        else:
            request_target = target

        key: tuple[Any, ...] = (scheme, host, port, timeout, proxy)
        for attempt in range(1, max_attempts + 1):
            conn, reused = self._acquire(key)
            try:
                conn.request(method_upper, request_target, body=encoded_body, headers=request_headers)
                response = conn.getresponse()
                raw_body = response.read()
                status = response.status
                retry_after = response.getheader("Retry-After")
                will_close = response.will_close
            except (OSError, http.client.HTTPException):
                self._evict(key, conn)
                if not idempotent or attempt >= max_attempts:
                    raise
                if not (reused and attempt == 1):
                    self.sleeper(self.backoff_seconds * attempt)
                continue

            if will_close:
                self._evict(key, conn)

            if status >= 400:
                error = _decode_http_error(status, raw_body, url=url)
                if idempotent and (status in _RETRYABLE_GET_STATUSES or 500 <= status < 600) and attempt < max_attempts:
                    delay = _parse_retry_after(retry_after)
                    self.sleeper(delay if delay is not None else self.backoff_seconds * attempt)
                    continue
                raise error

            return json.loads(raw_body.decode("utf-8"))

        raise RuntimeError("unreachable")


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
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.credentials = credentials
        self.demo = demo
        self.base_url = base_url.rstrip("/")
        self._owned_transport = _PooledHTTPTransport(ssl_context=ssl_context) if transport is None else None
        self.transport = transport if transport is not None else self._owned_transport
        self.timeout = timeout
        self.timestamp_factory = timestamp_factory or _utc_timestamp

    def close(self) -> None:
        if self._owned_transport is not None:
            self._owned_transport.close()

    def __enter__(self) -> "OKXRestClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

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
        if not self.demo:
            raise PermissionError("demo leverage setup requires a demo client")
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
        return self._dispatch(prepared)

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

    def cancel_order(
        self,
        *,
        inst_id: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
        confirm_demo_order: bool,
    ) -> dict[str, Any]:
        if not self.demo:
            raise PermissionError("demo order cancellation requires a demo client")
        if not confirm_demo_order:
            raise PermissionError("confirm_demo_order=True is required before canceling a demo order")
        if order_id is None and client_order_id is None:
            raise ValueError("order_id or client_order_id is required")
        body = {"instId": inst_id}
        if order_id is not None:
            body["ordId"] = order_id
        if client_order_id is not None:
            if not OKX_CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id):
                raise ValueError("client_order_id must be 1-32 ASCII alphanumeric characters")
            body["clOrdId"] = client_order_id
        return self._request("POST", OKX_CANCEL_ORDER_PATH, body=body, private=True)

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
        return self._dispatch(prepared)

    def _dispatch(self, prepared: PreparedRequest) -> dict[str, Any]:
        url = f"{self.base_url}{prepared.path}"
        try:
            return self.transport(
                prepared.method,
                url,
                headers=prepared.headers,
                body=prepared.body,
                timeout=self.timeout,
            )
        except urllib.error.HTTPError as exc:
            raw_body = exc.read() if exc.fp is not None else b""
            raise _decode_http_error(exc.code, raw_body, url=url) from exc

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
        process_values = _process_credential_values(prefix)
        if _credential_values_are_complete(prefix, process_values):
            return process_values
        if os.name == "nt":
            for scope in ("user", "machine"):
                values = _read_windows_environment(prefix, scope)
                if _credential_values_are_complete(prefix, values):
                    return values
        return process_values
    if resolved_source == "process":
        return _process_credential_values(prefix)
    if resolved_source in {"user", "machine"}:
        return _read_windows_environment(prefix, resolved_source)
    raise ValueError(f"unsupported {prefix}_ENV_SOURCE: {resolved_source}")


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
