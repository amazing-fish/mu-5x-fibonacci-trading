import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mu_strategy.live.okx import OKXCredentials
from mu_strategy.live.okx_cli import main


class OKXExecutionCLITests(unittest.TestCase):
    def test_read_only_command_reports_position_business_code_as_warning(self):
        class StubClient:
            def __init__(self, *, credentials, demo):
                self.credentials = credentials
                self.demo = demo

            def get_instruments(self, *, inst_type, inst_id):
                return {"code": "0", "data": [{"instId": inst_id, "instType": inst_type}], "msg": ""}

            def get_balance(self, *, ccy):
                return {"code": "0", "data": [{"details": [{"ccy": ccy}]}], "msg": ""}

            def get_positions(self, *, inst_type, inst_id):
                return {
                    "code": "51001",
                    "data": [],
                    "msg": "Instrument ID, Instrument ID code, or Spread ID doesn't exist.",
                }

        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            with patch("mu_strategy.live.okx_cli.OKXRestClient", StubClient):
                exit_code = main(
                    [
                        "read-only",
                        "--demo",
                        "--inst-type",
                        "SWAP",
                        "--inst-id",
                        "MU-USDT-SWAP",
                        "--ccy",
                        "USDT",
                    ],
                    stdout=stdout,
                )

        self.assertEqual(0, exit_code)
        output = json.loads(stdout.getvalue())
        self.assertEqual("warning", output["status"])
        self.assertEqual("51001", output["positions"]["code"])
        self.assertEqual(
            [
                {
                    "component": "positions",
                    "code": "51001",
                    "msg": "Instrument ID, Instrument ID code, or Spread ID doesn't exist.",
                }
            ],
            output["warnings"],
        )

    def test_read_only_command_passes_explicit_credential_source(self):
        class StubClient:
            def __init__(self, *, credentials, demo):
                self.credentials = credentials
                self.demo = demo

            def get_instruments(self, *, inst_type, inst_id):
                return {"code": "0", "data": [], "msg": ""}

            def get_balance(self, *, ccy):
                return {"code": "0", "data": [], "msg": ""}

            def get_positions(self, *, inst_type, inst_id):
                return {"code": "0", "data": [], "msg": ""}

        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            with patch("mu_strategy.live.okx_cli.OKXRestClient", StubClient):
                main(
                    [
                        "read-only",
                        "--demo",
                        "--credential-source",
                        "process",
                    ],
                    stdout=stdout,
                )

        from_env.assert_called_once_with(source="process")

    def test_read_only_command_omitted_credential_source_honors_environment_default(self):
        class StubClient:
            def __init__(self, *, credentials, demo):
                self.credentials = credentials
                self.demo = demo

            def get_instruments(self, *, inst_type, inst_id):
                return {"code": "0", "data": [], "msg": ""}

            def get_balance(self, *, ccy):
                return {"code": "0", "data": [], "msg": ""}

            def get_positions(self, *, inst_type, inst_id):
                return {"code": "0", "data": [], "msg": ""}

        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            with patch("mu_strategy.live.okx_cli.OKXRestClient", StubClient):
                exit_code = main(["read-only", "--demo"], stdout=stdout)

        self.assertEqual(0, exit_code)
        from_env.assert_called_once_with(source=None)

    def test_shadow_record_command_appends_event_and_prints_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            exit_code = main(
                [
                    "shadow-record",
                    "--ledger",
                    str(Path(tmp) / "shadow.jsonl"),
                    "--event-id",
                    "evt-1",
                    "--symbol",
                    "MU-USDT-SWAP",
                    "--action",
                    "buy",
                    "--plan-price",
                    "100",
                    "--observed-price",
                    "100.5",
                    "--quantity",
                    "1",
                    "--status",
                    "filled",
                    "--reason",
                    "signal",
                    "--timestamp-ms",
                    "123",
                ],
                stdout=stdout,
            )

            self.assertEqual(0, exit_code)
            output = json.loads(stdout.getvalue())
            self.assertEqual(1, output["metrics"]["total_events"])
            self.assertEqual(1.0, output["metrics"]["fill_rate"])
            self.assertEqual("evt-1", output["event"]["event_id"])

    def test_demo_order_command_defaults_to_sanitized_dry_run(self):
        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            exit_code = main(
                [
                    "demo-order",
                    "--inst-id",
                    "MU-USDT-SWAP",
                    "--side",
                    "buy",
                    "--size",
                    "1",
                    "--order-type",
                    "limit",
                    "--price",
                    "100",
                    "--client-order-id",
                    "demo-1",
                ],
                stdout=stdout,
            )

        self.assertEqual(0, exit_code)
        output = json.loads(stdout.getvalue())
        self.assertEqual("dry_run", output["mode"])
        self.assertEqual("/api/v5/trade/order", output["request"]["path"])
        self.assertEqual("<redacted>", output["request"]["headers"]["OK-ACCESS-KEY"])
        self.assertNotIn("secret", stdout.getvalue())

    def test_demo_order_command_requires_confirm_flag_to_send(self):
        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            with patch("mu_strategy.live.okx_cli.OKXRestClient.place_demo_order") as place_demo_order:
                place_demo_order.return_value = {"code": "0", "data": [{"ordId": "1"}], "msg": ""}
                exit_code = main(
                    [
                        "demo-order",
                        "--inst-id",
                        "MU-USDT-SWAP",
                        "--side",
                        "buy",
                        "--size",
                        "1",
                        "--confirm-demo-order",
                    ],
                    stdout=stdout,
                )

        self.assertEqual(0, exit_code)
        place_demo_order.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertEqual("sent_demo_order", output["mode"])
        self.assertEqual("0", output["response"]["code"])

    def test_demo_order_command_passes_explicit_credential_source(self):
        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            exit_code = main(
                [
                    "demo-order",
                    "--inst-id",
                    "MU-USDT-SWAP",
                    "--side",
                    "buy",
                    "--size",
                    "1",
                    "--credential-source",
                    "process",
                ],
                stdout=stdout,
            )

        self.assertEqual(0, exit_code)
        from_env.assert_called_once_with(source="process")

    def test_demo_order_command_omitted_credential_source_honors_environment_default(self):
        stdout = io.StringIO()

        with patch("mu_strategy.live.okx_cli.OKXCredentials.from_env") as from_env:
            from_env.return_value = OKXCredentials("key", "secret", "passphrase")
            exit_code = main(
                [
                    "demo-order",
                    "--inst-id",
                    "MU-USDT-SWAP",
                    "--side",
                    "buy",
                    "--size",
                    "1",
                ],
                stdout=stdout,
            )

        self.assertEqual(0, exit_code)
        from_env.assert_called_once_with(source=None)


if __name__ == "__main__":
    unittest.main()
