import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from mu_strategy.market_data.providers.okx import fetch_okx_listing_time
from mu_strategy.market_data.trusted_data.refresh import OKXMarketDataProvider


SYMBOL = "MU-USDT-SWAP"
LISTING = 1772608500000


class OkxListingTimeTests(unittest.TestCase):
    def fetch_payload(self, payload):
        response = io.BytesIO(json.dumps(payload).encode())
        with patch("mu_strategy.market_data.providers.okx.urllib.request.urlopen", return_value=response) as request:
            result = fetch_okx_listing_time(SYMBOL)
        return result, request

    def test_reads_matching_instrument_from_public_endpoint(self):
        result, request = self.fetch_payload({"code": "0", "data": [{"instId": SYMBOL, "listTime": str(LISTING)}]})
        self.assertEqual(LISTING, result)
        url = urlsplit(request.call_args.args[0].full_url)
        self.assertEqual(("https", "www.okx.com", "/api/v5/public/instruments"), (url.scheme, url.netloc, url.path))
        self.assertEqual({"instType": ["SWAP"], "instId": [SYMBOL]}, parse_qs(url.query))
        self.assertEqual(20, request.call_args.kwargs["timeout"])

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
        with patch("mu_strategy.market_data.providers.okx.urllib.request.urlopen", side_effect=TimeoutError), self.assertRaises(TimeoutError):
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
