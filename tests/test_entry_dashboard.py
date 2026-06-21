import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class EntryDashboardTests(unittest.TestCase):
    def test_dashboard_shows_no_entry_opportunity_and_reason_summary(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "dry_run": True,
                "scans": [
                    {
                        "symbol": "BTC-USDT-SWAP",
                        "action": "wait",
                        "reason": "current bar is outside configured trading window",
                        "last_close": 64000.0,
                        "regime_1h": "green",
                        "rsi14": 62.0,
                        "macd_hist": 1.2,
                        "fib_level": None,
                        "fib_distance_pct": None,
                    },
                    {
                        "symbol": "LAB-USDT-SWAP",
                        "action": "skip",
                        "reason": "1h regime is red",
                        "last_close": 12.3,
                        "regime_1h": "red",
                        "rsi14": 45.0,
                        "macd_hist": -0.2,
                        "fib_level": None,
                        "fib_distance_pct": None,
                    },
                ],
                "orders": [],
                "expired_orders": [],
                "data_errors": [],
                "universe_error": None,
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn('<html lang="zh-CN">', html)
        self.assertIn("当前无进场机会", html)
        self.assertIn("current bar is outside configured trading window", html)
        self.assertIn("1h regime is red", html)
        self.assertIn("页面刷新倒计时", html)
        self.assertIn("30", html)
        self.assertIn("当前无挂单建议，因此无撤单目标。", html)
        self.assertNotIn("下一轮不再出现同一个 client_order_id", html)

    def test_dashboard_shows_complete_planned_order_ticket_and_bound_cancel_target(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "dry_run": True,
                "scans": [
                    {
                        "symbol": "BTC-USDT-SWAP",
                        "action": "enter",
                        "reason": "recent retest confirmed; resting second-pullback fib limit",
                        "last_close": 64120.0,
                        "regime_1h": "green",
                        "rsi14": 55.0,
                        "macd_hist": 0.2,
                        "macd_hist_prev": 0.1,
                        "fib_level": 64000.1,
                        "fib_distance_pct": 0.001,
                        "trigger_price": 64000.1,
                        "initial_stop": 62000.0,
                        "signal_time_ms": 1780056000000,
                        "second_pullback_wait_bars": 8,
                    }
                ],
                "orders": [
                    {
                        "symbol": "BTC-USDT-SWAP",
                        "status": "planned",
                        "reason": "recent retest confirmed; resting second-pullback fib limit",
                        "client_order_id": "OD1234567890ABCDEF12",
                        "notional_usdt": 25.0,
                        "limit_price": "64000.1",
                        "size": "0.01",
                        "initial_stop": 62000.0,
                        "signal_time_ms": 1780056000000,
                    }
                ],
                "expired_orders": [],
                "data_errors": [],
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn("发现 1 个可人工复核机会", html)
        self.assertIn("挂单建议单", html)
        self.assertIn("限价买入", html)
        self.assertIn("BTC-USDT-SWAP", html)
        self.assertIn("64000.1", html)
        self.assertIn("0.01", html)
        self.assertIn("25", html)
        self.assertIn("62000", html)
        self.assertIn("OD1234567890ABCDEF12", html)
        self.assertIn("撤单目标：BTC-USDT-SWAP / OD1234567890ABCDEF12 / 64000.1 / 0.01", html)
        self.assertIn("撤单触发点：下一轮扫描不再出现同一个 client_order_id=OD1234567890ABCDEF12", html)
        self.assertIn("时间失效点：2026-05-29 12:00 UTC + 8 根 15m K = 2026-05-29 14:00 UTC", html)
        self.assertIn("1h 失效：regime_1h == red", html)
        self.assertIn("RSI 失效：rsi14 &lt; 45.00", html)
        self.assertIn("MACD 失效：macd_hist &lt; macd_hist_prev 且 macd_hist &lt; 0", html)
        self.assertIn("数据失效：market data stale/missing/load failed", html)

    def test_dashboard_shows_submitted_demo_order_and_bound_cancel_target(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "dry_run": False,
                "scans": [
                    {
                        "symbol": "BTC-USDT-SWAP",
                        "action": "enter",
                        "reason": "recent retest confirmed; resting second-pullback fib limit",
                        "last_close": 64120.0,
                        "regime_1h": "green",
                        "rsi14": 55.0,
                        "macd_hist": 0.2,
                        "macd_hist_prev": 0.1,
                        "fib_level": 64000.1,
                        "fib_distance_pct": 0.001,
                        "trigger_price": 64000.1,
                        "initial_stop": 62000.0,
                        "signal_time_ms": 1780056000000,
                        "second_pullback_wait_bars": 8,
                    }
                ],
                "orders": [
                    {
                        "symbol": "BTC-USDT-SWAP",
                        "status": "submitted",
                        "reason": "recent retest confirmed; resting second-pullback fib limit",
                        "client_order_id": "ODSUBMITTED1234567890",
                        "notional_usdt": 25.0,
                        "limit_price": "64000.1",
                        "size": "0.01",
                        "initial_stop": 62000.0,
                        "signal_time_ms": 1780056000000,
                        "response": {
                            "code": "0",
                            "data": [{"ordId": "ORD123", "clOrdId": "ODSUBMITTED1234567890", "sCode": "0"}],
                        },
                    }
                ],
                "expired_orders": [],
                "data_errors": [],
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn("本轮已提交 1 个 demo 订单", html)
        self.assertIn("订单执行结果", html)
        self.assertIn("已提交 demo 订单", html)
        self.assertIn("ORD123", html)
        self.assertIn("ODSUBMITTED1234567890", html)
        self.assertIn("64000.1", html)
        self.assertIn("0.01", html)
        self.assertIn("撤单目标：BTC-USDT-SWAP / ORD123 / ODSUBMITTED1234567890 / 64000.1 / 0.01", html)
        self.assertNotIn("当前无挂单建议，因此无撤单目标。", html)

    def test_dashboard_shows_blocked_order_without_cancel_target(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "dry_run": False,
                "scans": [],
                "orders": [
                    {
                        "symbol": "ETH-USDT-SWAP",
                        "status": "blocked",
                        "reason": "max_open_exposure_reached",
                        "client_order_id": "ODBLOCKED1234567890AB",
                        "notional_usdt": 10.0,
                        "limit_price": "1700",
                        "size": "0.01",
                        "initial_stop": 1650.0,
                    }
                ],
                "expired_orders": [],
                "data_errors": [],
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn("本轮无可挂单建议，存在阻塞订单结果", html)
        self.assertIn("订单执行结果", html)
        self.assertIn("未下单，无撤单目标", html)
        self.assertIn("max_open_exposure_reached", html)
        self.assertNotIn("撤单触发点：下一轮扫描不再出现同一个 client_order_id=ODBLOCKED1234567890AB", html)

    def test_dashboard_warns_when_planned_order_size_is_missing(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "orders": [
                    {
                        "symbol": "ETH-USDT-SWAP",
                        "status": "planned",
                        "reason": "recent retest confirmed",
                        "client_order_id": "ODABC",
                        "notional_usdt": 10.0,
                        "limit_price": "1700",
                        "size": None,
                        "initial_stop": 1650.0,
                    }
                ],
                "scans": [],
                "expired_orders": [],
                "data_errors": [],
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn("挂单数量未完成换算，不建议直接挂单", html)
        self.assertIn("不可直接挂单", html)

    def test_dashboard_renders_runner_failure_as_not_tradeable(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "cycle_failed",
                "reason": "runner_failed",
                "error_type": "RuntimeError",
                "message": "temporary OKX timeout",
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn("扫描失败", html)
        self.assertIn("不可下单", html)
        self.assertIn("不可作为下单依据", html)
        self.assertIn("temporary OKX timeout", html)

    def test_dashboard_translates_dry_run_and_marks_symbol_source(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "universe": [
                    {"inst_id": "BTC-USDT-SWAP", "source": "top"},
                    {"inst_id": "MU-USDT-SWAP", "source": "watchlist"},
                ],
                "orders": [],
                "scans": [
                    {
                        "symbol": "MU-USDT-SWAP",
                        "source": "watchlist",
                        "action": "wait",
                        "reason": "current bar is outside configured trading window",
                        "last_close": 3.2,
                        "regime_1h": "yellow",
                        "rsi14": 50.0,
                        "macd_hist": 0.1,
                        "macd_hist_prev": 0.2,
                    }
                ],
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertIn("模拟扫描：不读取私有账户，不下单，不撤单", html)
        self.assertIn("Top 1 + 固定关注 1", html)
        self.assertIn("watchlist", html)

    def test_dashboard_escapes_log_content(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "orders": [],
                "scans": [
                    {
                        "symbol": "<script>alert(1)</script>",
                        "action": "wait",
                        "reason": "<b>bad</b>",
                        "last_close": 1.0,
                        "regime_1h": "yellow",
                        "rsi14": None,
                        "macd_hist": None,
                    }
                ],
            },
            refresh_seconds=30,
            generated_at="2026-06-20 23:00:00 CST",
        )

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<b>bad</b>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", html)

    def test_render_command_rebuilds_dashboard_from_latest_valid_jsonl(self):
        from mu_strategy.commands.render_entry_dashboard import main

        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "scan.log"
            output_path = Path(tmp) / "dashboard.html"
            log_path.write_text(
                "\n".join(
                    [
                        "{not valid json",
                        json.dumps({"mode": "dry_run", "orders": [], "scans": []}),
                        json.dumps(
                            {
                                "mode": "dry_run",
                                "orders": [
                                    {
                                        "symbol": "BTC-USDT-SWAP",
                                        "status": "planned",
                                        "reason": "recent retest confirmed",
                                        "client_order_id": "ODLATEST",
                                        "notional_usdt": 10.0,
                                        "limit_price": "64000",
                                        "size": "0.01",
                                        "initial_stop": 62000.0,
                                    }
                                ],
                                "scans": [],
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            exit_code = main(["--log", str(log_path), "--output", str(output_path)], stdout=stdout)

            self.assertEqual(0, exit_code)
            html = output_path.read_text(encoding="utf-8")
            self.assertIn("发现 1 个可人工复核机会", html)
            self.assertIn("ODLATEST", html)
            self.assertIn(str(output_path), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
