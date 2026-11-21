from __future__ import annotations

import base64
import http.client
import io
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


_DEFAULT_TIMEOUT = 20.0
_STALE_SOCKET_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.CannotSendRequest,
    http.client.ResponseNotReady,
    http.client.IncompleteRead,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
_TRANSIENT_ERRORS = (
    http.client.HTTPException,
    urllib.error.URLError,
    TimeoutError,
    OSError,
)

_THREAD_LOCAL = threading.local()
_SSL_CONTEXT_LOCK = threading.Lock()
_DEFAULT_SSL_CONTEXT: ssl.SSLContext | None = None


@dataclass(frozen=True)
class ProxyTarget:
    scheme: str
    host: str
    port: int
    auth_header: str | None = None


def resolve_proxy(scheme: str, host: str, port: int | None = None) -> ProxyTarget | None:
    """Resolve configured HTTP(S) proxy for a destination, honoring NO_PROXY/system bypass."""
    normalized_scheme = scheme.lower()
    proxies = urllib.request.getproxies()
    raw_proxy = proxies.get(normalized_scheme) or proxies.get("all")
    if not raw_proxy:
        return None
    host_part = f"[{host}]" if ":" in host else host
    bypass_target = f"{host_part}:{port}" if port is not None else host
    if urllib.request.proxy_bypass(bypass_target):
        return None
    raw = raw_proxy.strip()
    if not raw:
        return None
    parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
    proxy_host = parsed.hostname
    if not proxy_host:
        return None
    proxy_scheme = (parsed.scheme or "http").lower()
    proxy_port = parsed.port if parsed.port is not None else (443 if proxy_scheme == "https" else 80)
    auth_header: str | None = None
    if parsed.username is not None:
        user = urllib.parse.unquote(parsed.username)
        password = urllib.parse.unquote(parsed.password or "")
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        auth_header = f"Basic {token}"
    return ProxyTarget(
        scheme=proxy_scheme,
        host=proxy_host,
        port=proxy_port,
        auth_header=auth_header,
    )


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


def _pool() -> dict[tuple[Any, ...], http.client.HTTPConnection]:
    connections = getattr(_THREAD_LOCAL, "connections", None)
    if connections is None:
        connections = {}
        _THREAD_LOCAL.connections = connections
    return connections


def _create_connection(
    scheme: str,
    host: str,
    port: int | None,
    timeout: float,
    *,
    context: ssl.SSLContext | None = None,
    proxy: ProxyTarget | None = None,
) -> http.client.HTTPConnection:
    if scheme == "https":
        ssl_context = context if context is not None else _default_ssl_context()
        if proxy is not None:
            conn = http.client.HTTPSConnection(
                proxy.host,
                port=proxy.port,
                timeout=timeout,
                context=ssl_context,
            )
            tunnel_headers = (
                {"Proxy-Authorization": proxy.auth_header}
                if proxy.auth_header
                else None
            )
            conn.set_tunnel(host, port=port or 443, headers=tunnel_headers)
            return conn
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=ssl_context)
    if scheme == "http":
        if proxy is not None:
            if proxy.scheme == "https":
                ssl_context = context if context is not None else _default_ssl_context()
                return http.client.HTTPSConnection(
                    proxy.host,
                    port=proxy.port,
                    timeout=timeout,
                    context=ssl_context,
                )
            return http.client.HTTPConnection(
                proxy.host,
                port=proxy.port,
                timeout=timeout,
            )
        return http.client.HTTPConnection(host, port=port, timeout=timeout)
    raise ValueError(f"unsupported URL scheme: {scheme}")


def _new_connection(
    scheme: str,
    host: str,
    port: int | None,
    timeout: float,
    *,
    context: ssl.SSLContext | None = None,
) -> http.client.HTTPConnection:
    return _create_connection(
        scheme,
        host,
        port,
        timeout,
        context=context,
        proxy=resolve_proxy(scheme, host, port),
    )


_DEFAULT_NEW_CONNECTION = _new_connection


def _close_connection(
    pool: dict[tuple[Any, ...], http.client.HTTPConnection],
    key: tuple[Any, ...],
) -> None:
    conn = pool.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def close_pooled_connections() -> None:
    """Close and clear all keep-alive HTTP(S) connections for the current thread."""
    pool = getattr(_THREAD_LOCAL, "connections", None)
    if not pool:
        return
    for conn in list(pool.values()):
        try:
            conn.close()
        except OSError:
            pass
    pool.clear()


def _request_once(
    url: str,
    *,
    scheme: str,
    host: str,
    port: int | None,
    target: str,
    headers: dict[str, str],
    timeout: float,
    context: ssl.SSLContext | None = None,
) -> bytes:
    proxy = resolve_proxy(scheme, host, port)
    if proxy is not None and proxy.scheme not in {"http", "https"}:
        ssl_context = (
            context
            if context is not None
            else (_default_ssl_context() if scheme == "https" else None)
        )
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            return resp.read()

    if scheme == "http" and proxy is not None:
        host_part = f"[{host}]" if ":" in host else host
        netloc = f"{host_part}:{port}" if port is not None else host_part
        request_target = f"http://{netloc}{target}"
        if proxy.auth_header and not any(k.lower() == "proxy-authorization" for k in headers):
            send_headers = {**headers, "Proxy-Authorization": proxy.auth_header}
        else:
            send_headers = headers
    else:
        request_target = target
        send_headers = headers

    pool = _pool()
    key: tuple[Any, ...] = (
        (scheme, host, port, timeout, proxy)
        if context is None
        else (scheme, host, port, timeout, proxy, id(context))
    )
    def _open_conn() -> http.client.HTTPConnection:
        if _new_connection is _DEFAULT_NEW_CONNECTION:
            return _create_connection(scheme, host, port, timeout, context=context, proxy=proxy)
        return (
            _new_connection(scheme, host, port, timeout)
            if context is None
            else _new_connection(scheme, host, port, timeout, context=context)
        )

    reused = key in pool
    conn = pool.get(key)
    if conn is None:
        conn = _open_conn()
        pool[key] = conn

    try:
        conn.request("GET", request_target, headers=send_headers)
        response = conn.getresponse()
        body = response.read()
    except _STALE_SOCKET_ERRORS:
        _close_connection(pool, key)
        if not reused:
            raise
        # Transparently reconnect once when a pooled keep-alive socket was closed by peer.
        conn = _open_conn()
        pool[key] = conn
        try:
            conn.request("GET", request_target, headers=send_headers)
            response = conn.getresponse()
            body = response.read()
        except Exception:
            _close_connection(pool, key)
            raise
    except Exception:
        _close_connection(pool, key)
        raise

    if response.will_close:
        _close_connection(pool, key)
    if response.status < 200 or response.status >= 300:
        _close_connection(pool, key)
        raise urllib.error.HTTPError(
            url,
            response.status,
            response.reason,
            response.headers,
            io.BytesIO(body),
        )
    return body


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    retries: int = 1,
    retry_backoff_base: float = 0.25,
    retry_backoff_cap: float = 2.0,
    context: ssl.SSLContext | None = None,
) -> Any:
    """Perform an HTTP(S) GET using a per-thread keep-alive connection pool and decode JSON."""
    if retries < 1:
        raise ValueError("retries must be at least 1")
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL is missing host: {url}")
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    request_headers = {"Connection": "keep-alive"}
    if headers:
        request_headers.update(headers)

    for attempt in range(1, retries + 1):
        try:
            body = _request_once(
                url,
                scheme=scheme,
                host=host,
                port=parsed.port,
                target=target,
                headers=request_headers,
                timeout=timeout,
                context=context,
            )
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except _TRANSIENT_ERRORS:
            if attempt >= retries:
                raise
            delay = min(retry_backoff_base * (2 ** (attempt - 1)), retry_backoff_cap)
            time.sleep(delay)
    raise RuntimeError(f"HTTP GET failed after {retries} attempts: {url}")
