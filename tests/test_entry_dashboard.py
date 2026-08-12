import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def _degraded_exit_observation(
    *,
    triggered=False,
    exit_reason=None,
    trigger_basis="none",
    assumptions=None,
):
    return {
        "symbol": "MU-USDT-SWAP",
        "decision_status": "unknown",
        "state_quality": "degraded",
        "position_size": 2.0,
        "average_entry_price": 100.0,
        "known_fields": ["instId", "pos", "avgPx"],
        "unknown_fields": ["fills", "stop_price", "max_stage"],
        "assumptions": ["max_stage=1", "leverage=5"] if assumptions is None else assumptions,
        "unavailable_reason": None,
        "assumption_evaluation": {
            "candle_open_time_ms": 1780056000000,
            "latest_close": 97.0,
            "stop_before_candle": 98.0,
            "stop_after_candle_if_open": 99.0,
            "exit_triggered": triggered,
            "exit_reason": exit_reason,
            "trigger_basis": trigger_basis,
            "latest_close_at_or_below_tightened_stop": True,
            "transition_fill_count": 0,
            "transition_start": 98.0,
        },
    }


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

    def test_exit_observation_classifier_has_four_stable_tiers(self):
        from mu_strategy.viz.entry_dashboard import (
            EXIT_OBSERVATION_TIER_NONE,
            EXIT_OBSERVATION_TIER_UNAVAILABLE,
            EXIT_OBSERVATION_TIER_UNKNOWN,
            EXIT_OBSERVATION_TIER_WARNING,
            classify_exit_observation,
        )

        cases = [
            (None, EXIT_OBSERVATION_TIER_NONE),
            (
                _degraded_exit_observation(triggered=True, exit_reason="stop"),
                EXIT_OBSERVATION_TIER_WARNING,
            ),
            (_degraded_exit_observation(triggered=True), EXIT_OBSERVATION_TIER_UNAVAILABLE),
            (
                _degraded_exit_observation(triggered=False, exit_reason="stop"),
                EXIT_OBSERVATION_TIER_UNAVAILABLE,
            ),
            (_degraded_exit_observation(triggered=False), EXIT_OBSERVATION_TIER_UNKNOWN),
            (
                {"state_quality": "unavailable", "assumption_evaluation": {"exit_triggered": True}},
                EXIT_OBSERVATION_TIER_UNAVAILABLE,
            ),
        ]

        for observation, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, classify_exit_observation(observation))

    def test_dashboard_distinguishes_dry_run_without_positions_from_confirmed_empty_positions(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        dry_run_html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "exit_observations": [],
                "exit_observation_status": {
                    "status": "unavailable",
                    "reason": "dry_run_has_no_position_source",
                    "position_count": 0,
                    "observation_count": 0,
                },
            }
        )
        confirmed_empty_html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "exit_observations": [],
                "exit_observation_status": {
                    "status": "available",
                    "reason": None,
                    "position_count": 0,
                    "observation_count": 0,
                },
            }
        )

        self.assertIn("持仓出场观测 · 降级估计", dry_run_html)
        self.assertIn("Dry-run 未读取持仓", dry_run_html)
        self.assertIn("无法确认当前是否有持仓", dry_run_html)
        self.assertNotIn("本轮返回 0 个持仓", dry_run_html)
        self.assertIn("已读取交易所持仓", confirmed_empty_html)
        self.assertIn("本轮返回 0 个持仓", confirmed_empty_html)

    def test_dashboard_does_not_confirm_empty_positions_from_non_list_observations(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "exit_observations": {"broken": 1},
                "exit_observation_status": {
                    "status": "available",
                    "reason": None,
                    "position_count": 0,
                    "observation_count": 1,
                },
            }
        )

        self.assertNotIn("本轮返回 0 个持仓", html)
        self.assertIn("无法确认当前是否有持仓", html)

    def test_dashboard_does_not_confirm_empty_positions_when_observation_count_disagrees(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "exit_observations": [],
                "exit_observation_status": {
                    "status": "available",
                    "reason": None,
                    "position_count": 0,
                    "observation_count": 1,
                },
            }
        )

        self.assertNotIn("本轮返回 0 个持仓", html)
        self.assertIn("无法确认当前是否有持仓", html)

    def test_dashboard_does_not_confirm_empty_positions_from_non_integer_counts(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        for field in ("position_count", "observation_count"):
            for malformed_count in (False, 0.0, 0.5, -0.2):
                with self.subTest(field=field, malformed_count=malformed_count):
                    status = {
                        "status": "available",
                        "reason": None,
                        "position_count": 0,
                        "observation_count": 0,
                    }
                    status[field] = malformed_count

                    html = render_entry_dashboard(
                        {
                            "mode": "live_demo",
                            "exit_observations": [],
                            "exit_observation_status": status,
                        }
                    )

                    self.assertNotIn("本轮返回 0 个持仓", html)
                    self.assertIn("无法确认当前是否有持仓", html)

    def test_dashboard_preserves_confirmed_empty_and_valid_stop_warning_paths(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        confirmed_empty_html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "exit_observations": [],
                "exit_observation_status": {
                    "status": "available",
                    "reason": None,
                    "position_count": 0,
                    "observation_count": 0,
                },
            }
        )
        stop_warning_html = render_entry_dashboard(
            {
                "mode": "live_demo",
                "exit_observations": [
                    _degraded_exit_observation(triggered=True, exit_reason="stop")
                ],
            }
        )

        self.assertIn("本轮返回 0 个持仓", confirmed_empty_html)
        self.assertIn("发现 1 个持仓出场警示", stop_warning_html)
        self.assertIn("⚠️ 出场警示（降级估计）", stop_warning_html)

    def test_dashboard_exit_warning_renders_degraded_stop_semantics_next_to_values(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        observation = _degraded_exit_observation(
            triggered=True,
            exit_reason="stop",
            trigger_basis="candle_low_at_or_below_stop_before_candle",
        )

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "exit_observations": [observation],
                "exit_observation_status": {"status": "available", "position_count": 1, "observation_count": 1},
            }
        )

        self.assertIn("⚠️ 出场警示（降级估计）", html)
        self.assertIn("没有权威的实时出场判断", html)
        self.assertIn("估计值", html)
        self.assertIn("本根触发线", html)
        self.assertIn("candle_low_at_or_below_stop_before_candle", html)
        self.assertIn("若存续，下一根携带值", html)
        self.assertIn("不是本根的触发线", html)
        self.assertIn("仅供诊断", html)
        self.assertIn("不是本根的出场判断", html)
        self.assertIn("会漏报", html)
        self.assertIn("仅限于", html)
        self.assertIn("仍可能误报", html)

    def test_dashboard_liquidation_risk_uses_same_warning_tier_and_current_coverage_copy(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "exit_observations": [
                    _degraded_exit_observation(
                        triggered=True,
                        exit_reason="non_session_liquidation_risk",
                        trigger_basis="candle_low_at_or_below_non_session_liquidation_risk_price",
                        assumptions=["max_stage=1", "leverage=5"],
                    )
                ],
                "exit_observation_status": {"status": "available", "position_count": 1, "observation_count": 1},
            }
        )

        self.assertIn("⚠️ 出场警示（降级估计）", html)
        self.assertIn("non_session_liquidation_risk", html)
        self.assertIn("强平风险已纳入估计", html)
        self.assertIn("优先于普通止损", html)
        self.assertIn("估计使用杠杆 5", html)
        self.assertIn("若与你交易所", html)
        self.assertIn("实际杠杆不符", html)
        self.assertIn("估计质量高于止损路径", html)
        self.assertNotIn("未覆盖", html)
        self.assertNotIn("未建模", html)
        self.assertNotIn("来源", html)
        self.assertNotIn("payload", html)
        self.assertNotIn("配置默认值", html)

    def test_dashboard_exit_warning_precedes_planned_entry_headline(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "orders": [{"status": "planned", "symbol": "BTC-USDT-SWAP"}],
                "exit_observations": [_degraded_exit_observation(triggered=True, exit_reason="stop")],
            }
        )

        self.assertIn("发现 1 个持仓出场警示", html)
        self.assertNotIn("发现 1 个可人工复核机会", html)

    def test_dashboard_failure_precedes_exit_warning_headline(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "cycle_failed",
                "reason": "runner_failed",
                "exit_observations": [_degraded_exit_observation(triggered=True, exit_reason="stop")],
            }
        )

        self.assertIn("<h1>扫描失败</h1>", html)
        self.assertNotIn("发现 1 个持仓出场警示", html)
        self.assertIn("⚠️ 出场警示（降级估计）", html)

    def test_dashboard_unavailable_position_shows_reason_without_stop_values(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "exit_observations": [
                    {
                        "symbol": "MU-USDT-SWAP",
                        "state_quality": "unavailable",
                        "position_size": 2,
                        "average_entry_price": 100,
                        "unavailable_reason": "no_closed_15m_candles",
                        "assumption_evaluation": {
                            "exit_triggered": False,
                            "stop_before_candle": 91.234567,
                            "stop_after_candle_if_open": 92.345678,
                        },
                    }
                ],
            }
        )

        self.assertIn("持仓不可评估", html)
        self.assertIn("no_closed_15m_candles", html)
        self.assertNotIn("stop_before_candle", html)
        self.assertNotIn("stop_after_candle_if_open", html)
        self.assertNotIn("91.234567", html)
        self.assertNotIn("92.345678", html)

    def test_dashboard_tolerates_missing_none_and_malformed_assumption_evaluation(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        for evaluation in (
            None,
            "invalid",
            [],
            {"exit_triggered": True},
            {"exit_triggered": True, "exit_reason": "unexpected"},
            {"exit_triggered": False, "exit_reason": "stop"},
        ):
            with self.subTest(evaluation=evaluation):
                observation = _degraded_exit_observation()
                observation["assumption_evaluation"] = evaluation

                html = render_entry_dashboard(
                    {
                        "mode": "dry_run",
                        "exit_observations": [observation, "not-a-dict"],
                    }
                )

                self.assertIn("持仓出场观测 · 降级估计", html)
                self.assertNotIn("发现 1 个持仓出场警示", html)
                if isinstance(evaluation, dict):
                    self.assertIn("持仓不可评估", html)
                    self.assertIn("assumption_evaluation 字段矛盾", html)
                    self.assertNotIn("无法生成假设评估：-", html)

    def test_dashboard_unknown_leverage_degrades_liquidation_copy_without_guessing(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        html = render_entry_dashboard(
            {
                "mode": "dry_run",
                "exit_observations": [
                    _degraded_exit_observation(
                        triggered=True,
                        exit_reason="non_session_liquidation_risk",
                        assumptions=["max_stage=1"],
                    )
                ],
            }
        )

        self.assertIn("杠杆未知，强平线无法估计", html)
        self.assertNotIn("估计使用杠杆", html)
        self.assertNotIn("来源", html)
        self.assertNotIn("payload", html)
        self.assertNotIn("配置默认值", html)

    def test_dashboard_escapes_exit_observation_payload_text(self):
        from mu_strategy.viz.entry_dashboard import render_entry_dashboard

        observation = _degraded_exit_observation(
            triggered=True,
            exit_reason="<b>stop</b>",
            trigger_basis="<script>basis()</script>",
        )
        observation["symbol"] = "<img src=x onerror=alert(1)>"
        observation["position_size"] = "<svg onload=alert(2)>"

        html = render_entry_dashboard({"mode": "dry_run", "exit_observations": [observation]})

        self.assertNotIn("<b>stop</b>", html)
        self.assertNotIn("<script>basis()</script>", html)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertNotIn("<svg onload=alert(2)>", html)
        self.assertIn("&lt;b&gt;stop&lt;/b&gt;", html)
        self.assertIn("&lt;script&gt;basis()&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertIn("&lt;svg onload=alert(2)&gt;", html)

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
