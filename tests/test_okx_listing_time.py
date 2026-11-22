import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from mu_strategy.market_data.http_pool import close_pooled_connections
from mu_strategy.market_data.providers.okx import fetch_okx_listing_time
from mu_strategy.market_data.trusted_data.refresh import (
    OKXMarketDataProvider,
    RefreshTrustedMarketData,
    RefreshTrustedMarketDataRequest,
)
from mu_strategy.market_data.trusted_data.store import SegmentCorrectionError, TrustedDataStore
from mu_strategy.models import Candle


SYMBOL = "MU-USDT-SWAP"
LISTING = 1772608500000


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200
        self.reason = "OK"
        self.will_close = False
        self.headers = {}

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    def __init__(self, scheme, host, port, timeout, payload_bytes: bytes | BaseException):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.timeout = timeout
        self._payload = payload_bytes
        self.target: str | None = None
        self.headers: dict[str, str] = {}

    def request(self, method: str, target: str, headers: dict[str, str] | None = None):
        self.target = target
        self.headers = dict(headers or {})

    def getresponse(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return _FakeResponse(self._payload)

    def close(self):
        pass


class OkxListingTimeTests(unittest.TestCase):
    def tearDown(self):
        close_pooled_connections()

    def fetch_payload(self, payload):
        close_pooled_connections()
        captured: list[_FakeConnection] = []

        def fake_conn(scheme, host, port, timeout):
            conn = _FakeConnection(scheme, host, port, timeout, json.dumps(payload).encode())
            captured.append(conn)
            return conn

        with patch("mu_strategy.market_data.http_pool._new_connection", side_effect=fake_conn):
            result = fetch_okx_listing_time(SYMBOL)
        return result, captured[0]

    def test_reads_matching_instrument_from_public_endpoint(self):
        result, conn = self.fetch_payload({"code": "0", "data": [{"instId": SYMBOL, "listTime": str(LISTING)}]})
        self.assertEqual(LISTING, result)
        url = urlsplit(f"{conn.scheme}://{conn.host}{conn.target}")
        self.assertEqual(("https", "www.okx.com", "/api/v5/public/instruments"), (url.scheme, url.netloc, url.path))
        self.assertEqual({"instType": ["SWAP"], "instId": [SYMBOL]}, parse_qs(url.query))
        self.assertEqual(20.0, conn.timeout)

    def test_missing_ambiguous_wrong_or_malformed_metadata_is_rejected(self):
        valid = {"instId": SYMBOL, "listTime": str(LISTING)}
        payloads = [None, [], {"code": "500", "data": [valid]}, {"code": "0"},
                    {"code": "0", "data": []}, {"code": "0", "data": [valid, valid]},
                    {"code": "0", "data": [None]},
                    {"code": "0", "data": [{**valid, "instId": "BTC-USDT-SWAP"}]}]
        payloads.extend({"code": "0", "data": [{**valid, "listTime": value}]}
                        for value in (None, True, LISTING, "", "0", "-1", "1.5", "１２３", " 123"))
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.fetch_payload(payload)

    def test_transport_failure_is_not_interpreted_as_an_available_start(self):
        def fake_conn(scheme, host, port, timeout):
            return _FakeConnection(scheme, host, port, timeout, TimeoutError("timeout"))

        with patch("mu_strategy.market_data.http_pool._new_connection", side_effect=fake_conn), self.assertRaises(TimeoutError):
            fetch_okx_listing_time(SYMBOL)

    def test_custom_history_requires_explicit_matching_boundary_provider(self):
        with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_listing_time", side_effect=AssertionError("no network")):
            custom = OKXMarketDataProvider(history_fetcher=Mock())
            self.assertIsNone(custom.fetch_listing_time(SYMBOL))
            boundary = Mock(return_value=LISTING)
            custom = OKXMarketDataProvider(history_fetcher=Mock(), listing_time_fetcher=boundary)
            self.assertEqual(LISTING, custom.fetch_listing_time(SYMBOL))
            boundary.assert_called_once_with(SYMBOL)

    def test_default_provider_uses_exchange_metadata(self):
        with patch("mu_strategy.market_data.trusted_data.refresh.fetch_okx_listing_time", return_value=LISTING) as fetch:
            self.assertEqual(LISTING, OKXMarketDataProvider().fetch_listing_time(SYMBOL))
            fetch.assert_called_once_with(SYMBOL)

    def test_invalid_listing_payload_preserves_existing_publication(self):
        now_ms = int(datetime(2026, 3, 10, tzinfo=timezone.utc).timestamp() * 1000)
        rows = [Candle(at, 10, 11, 9, 10, 1) for at in range(LISTING, now_ms, 300_000)]
        payloads = [b'{"code":"0","data":[]}',
                    b'{"code":"0","data":[{"instId":"BTC-USDT-SWAP","listTime":"1772608500000"}]}',
                    b'{"code":"500","data":[]}', b'{broken json']
        for payload in payloads:
            with self.subTest(payload=payload), TemporaryDirectory() as tmp:
                close_pooled_connections()
                store = TrustedDataStore(data_dir=Path(tmp))
                initial = OKXMarketDataProvider(history_fetcher=lambda *a, **k: rows)
                RefreshTrustedMarketData(store, initial).execute(
                    RefreshTrustedMarketDataRequest(requested_intervals=("5m",), symbols=("BTC-USDT-SWAP",),
                                                    days=180, now_ms=now_ms, run_id="a" * 32)
                )
                pointer = store.current_path.read_bytes()
                provider = OKXMarketDataProvider(history_fetcher=lambda *a, **k: rows,
                                                 listing_time_fetcher=fetch_okx_listing_time)
                with patch(
                    "mu_strategy.market_data.http_pool._new_connection",
                    side_effect=lambda s, h, p, t, raw=payload: _FakeConnection(s, h, p, t, raw),
                ):
                    with self.assertRaises(SegmentCorrectionError):
                        RefreshTrustedMarketData(store, provider).execute(
                            RefreshTrustedMarketDataRequest(requested_intervals=("5m",), symbols=(SYMBOL,),
                                                            days=1, now_ms=now_ms, run_id="b" * 32)
                        )
                self.assertEqual(pointer, store.current_path.read_bytes())
