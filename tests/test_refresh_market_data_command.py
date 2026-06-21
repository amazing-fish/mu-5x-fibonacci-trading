import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class RefreshMarketDataCommandTests(unittest.TestCase):
    def test_explicit_interval_overrides_default_intervals(self):
        from mu_strategy.commands.refresh_market_data import main

        captured = {}

        def fake_refresh(**kwargs):
            captured.update(kwargs)
            return {"status": "ok", "symbols": {}}

        with TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            html_path = Path(tmp) / "health.html"
            with patch("mu_strategy.commands.refresh_market_data.refresh_market_data_once", side_effect=fake_refresh):
                with patch("mu_strategy.commands.refresh_market_data.write_data_health_dashboard") as write_dashboard:
                    exit_code = main(["--interval", "5m", "--html-output", str(html_path)], stdout=stdout)

        self.assertEqual(0, exit_code)
        self.assertEqual(("5m",), captured["intervals"])
        self.assertEqual({"status": "ok", "symbols": 0}, json.loads(stdout.getvalue()))
        write_dashboard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
