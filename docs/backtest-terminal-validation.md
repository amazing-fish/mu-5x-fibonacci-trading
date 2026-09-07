# 回测末根事件修复验证（#117）

本记录验证数据结束边界的事件处理，不评价策略盈利能力。关联 [#117](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/117)、研究质量 [#83](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/83) 和前瞻验证 [#99](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/99)；不关闭两个父 Issue。事件契约见[研究指南](research-guide.md#回测末根事件契约)。

## 基线与环境

- 2026-09-08 核对本地与 GitHub `main`：`bf564af40201d0ab8801edb5d12595cd275081f4`，包含 #116；当时唯一未合并 PR #86 为文档，无同类修复。
- 实测 Windows 11，build `10.0.26100`，Python `3.12.4`（`D:\Develop\Tool\Miniconda\python.exe`），`America/New_York` 可用。PATH 的默认 Python 是 3.10.14，本验证显式选用 3.12.4。
- 独立修复 worktree 与同 SHA 的 detached 对照 worktree；主工作区的行情、输出及运行服务未修改。生产代码仅 `mu_strategy/backtest.py` 不同，日历、registry、成本、依赖相同。

## 修复前失败，修复后通过

`tests/test_backtest_terminal.py` 导入真实 `run_backtest`。只控制 Fibonacci 候选的出现时点和 RSI/MACD 数组；信号验证、限价/止损成交、风险、加仓、止损更新、结算及循环不 mock。配置从当前 registry 的 canonical MU 构造，保留日历；仅手续费和指定场景的等待根数作显式覆盖。主要反例处于 2026-06-11 美股策略时段，非时段优先级案例单独选择时段外时间。预热由指标输入控制，无裸默认配置替代 baseline。

在实现修改前运行 14 个边界测试，得到 `FAILED (failures=12)`（包含参数化子场景），进程退出码 1。反例由真实入场函数产生：10,000 权益、5x、首仓 margin 0.2 → 100 价格、100 单位、98 初始止损；末根 OHLC 为 100/106/90/105。

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 已有持仓末根触及止损，零费率 | `end_of_data`，105，权益 10,500 | `stop`，98，权益 9,800 |
| 同一输入追加一根普通行情 | 历史退出从 105 改成 98 | 同一时间、98、`stop`，完整交易记录一致 |
| 末根开盘 95 跳空越过 98 止损 | 105 期末结算 | 95 止损成交 |
| 有效二次回踩在末根成交 | 无交易 | 真实成交后检查 `initial_stop`，或一次期末结算 |
| 末根已有持仓非时段杠杆风险 | 105 期末结算 | 80，`non_session_liquidation_risk`，优先于止损 |

其余回归覆盖已提前退出、末根存活、非零费率、三种入场方式的末根成交/初始风险、挂单到期（包含边界）/过期/限价未触及/时段关闭、末根新信号、无仓/短输入，以及先风险再加仓/收紧且不回查旧低点。非零费率 0.0005 的 100 单位止损案例，费用为 `10000*0.0005 + 9800*0.0005 = 9.9`，权益为 9,790.1。

```powershell
# 在修复 worktree 中，显式使用同一 Windows Python 3.12.4
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests -p 'test_backtest*.py' -v
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests
```

修复后聚焦 36 个测试通过，退出码 0。完整 suite 1031 个测试、77.228 秒，`OK (skipped=7)`，退出码 0。7 个跳过均来自 `test_strategy_artifact_publication` 的 symlink 场景（dangling pending/witness 两项、final artifact publish/recover/read 三项、witness collision 一项、valid sidecar 一项），因 Windows `WinError 1314` 缺少符号链接特权；并非回测跳过。单独执行该模块 46 项亦通过并确认同样 7 个跳过。`git diff --check` 通过。远端 CI 和外部审查状态以 PR 为准，本记录不替代它们。

## 同一真实 generation 对照

使用本地真实 schema-v4 generation `94d7533d3ce540f2943d2871452a4ef8`，复制原 manifest 和其引用的 21 个 segment 到隔离快照目录，逐文件校验 SHA-256。两边通过相同 `load_historical_window` 严格 reader；不使用 current 指针、不刷新、不改 manifest，不绕过覆盖/时序/hash 校验。比较后 22 个文件 hash 保持不变。

该 generation 不足完整 180 天，reader 实际返回 `HistoricalGenerationError: historical window has insufficient_coverage:15m`；因此使用能完整验证的 179 天窗口，未放宽门禁。两边均为当前 registry `baseline`，5x、market/taker 每侧 0.0005、10,000 初始权益，日历 `us_equities_2025_2028_v1`。完整配置封装为 `{strategy, strategy_config: StrategyConfigPayloadV2, starting_equity}`。

| 身份 | SHA-256 |
|---|---|
| 完整配置封装 | `43921b4616c821ca51ea27ab51599df35566703d4a14c3e382933b89e67c590c` |
| 日历内容 | `0fc82fba68f3dea507d8d20c83fb3f0a72fe700182b3d66b9004eab1a3d1b2c5` |
| 179 天选中 15m | `cd38729dd17c20a906851f531bb7c2a6750f2680392c5f86b6dd7ca5fe2a1037` |
| 179 天选中 1h | `d440a0022ccb52214197dfdb1a3bca641d7fe9b81b1f9d483eca8f03c3f0c64b` |
| 修复前 Python 源码集合 | `2a1037e2a932864a1c031a68c72627b584a742f189935916fd2c329178a16a3c` |
| 修复后 Python 源码集合 | `1d20f33400e7eb949209bcc6cb1716181a8e7c130c75545800cfeb9b2249567b` |

源码 hash 使用现有历史 provenance 的 LF 归一化算法；逐文件比较确认只有 `mu_strategy/backtest.py` 改变。以下收益均为账户收益，回撤沿用现有 equity curve 口径；交易数为已结束持仓笔数，买入 fill 数另列。

| 固定输入（UTC，结束不包含） | 交易 / 买入 fill（前→后） | 账户收益（前→后） | 最大回撤（前→后） | 总费用（前→后） |
|---|---|---|---|---|
| 179 天：03-10 13:00 至 09-05 13:00，17,184 根 | 56 / 108 → 相同 | 314.689424% → 相同 | -26.248898% → 相同 | 3361.308123 → 相同 |
| 14 天：08-22 13:00 至 09-05 13:00，1,344 根 | 4 / 8 → 相同 | 6.149860% → 相同 | -9.662887% → 相同 | 78.145151 → 相同 |
| 179 天输入前缀：止于 09-03 13:45，16,995 根 | 55 / 105 → 相同 | 278.007445% → 273.312858% | -26.248898% → 相同 | 3247.423224 → 3247.188377 |
| 上述前缀再加一根：止于 09-03 14:00，16,996 根 | 55 / 105 → 相同 | 273.312858% → 相同 | -26.248898% → 相同 | 3247.188377 → 相同 |

两个完整窗口的所有交易字段均一致；存活末根新增一条平仓前标记权益记录，因此 equity curve 不逐项相同，但本快照的最大回撤未变化。不能将“指标一致”说成所有输出字节一致。

诊断前缀的终点选择规则预先固定为“原实现 179 天结果中最后一次 `stop` 的 K 线”，用于暴露边界，不是收益评估或择优窗口。两边从同一 03-10 13:00 起点计算相同因果指标上下文，仅截断尾部。只有第 55 笔退出改变：2026-09-03 13:30 UTC（`1788442200000`），末根 OHLC 957.83/960.49/920.9/939.42，退出由 `end_of_data` 939.42 改为 `stop` 927.758944；该笔费用 37.98519959614307 → 37.75035280455043，净损益 -330.92500640179344 → -800.383742795468。账户期末权益 37800.7444931441 → 37331.28575675042。修复后追加一根，所有已发生交易记录一致；这里最终权益碰巧也一致，测试契约不依赖这个偶然结果。

重放完整窗口可在两边 worktree 各运行以下命令，将 `$snapshotDir` 设为保留原 manifest/segment 的隔离副本；14 天对照仅把两边 `--days` 同时设为 14。不要刷新旧 generation。

```powershell
& D:\Develop\Tool\Miniconda\python.exe -B -m mu_strategy.cli --generation-id 94d7533d3ce540f2943d2871452a4ef8 --data-dir $snapshotDir --days 179 --strategy baseline --fee-profile market --report reports/live/terminal-replay.md
```

诊断前缀重放：使用同一 `load_historical_window(..., days=179)`，由其 15m/1h 通过 `build_hourly_context` 计算上下文；将真实 15m 列表分别截为 `open_time_ms <= 1788442200000` 和 `<= 1788443100000`，将该列表、相同上下文、上述 registry 配置传给真实 `run_backtest`。不抽取引擎函数、不合成历史行情、不修改 reader 状态。

## 保留问题与可比性边界

- 原有加仓因果性：`backtest._maybe_add` 将当前根 `rsi_values[index]` / `hist_values[index]` 传给 `position_rules.decide_pyramid_add`，后者按当前根 high/open 与阈值成交。是否应延后一根是独立模型问题；本 PR 仅让末根沿用已有行为，不重构 RSI/MACD 时序。
- 原有费用确认：`_make_fill` 将入场费放在 Fill，`_marked_equity` 只加浮动盈亏，`_close_position` 一次计入入场和退出费。因此过程权益不是逐次成交即扣费口径；本次未改造。新增末根标记点在其他窗口可能改变回撤统计，即使期末权益相同。
- 保留现有非时段杠杆风险优先级和 OHLC 成交假设；没有新的清算模型、盘中价格路径、funding、滑点、队列或部分成交模型。
- 当前只有这个 MU generation 被量化；非 baseline 的确定性回归通过不表示所有策略/标的的真实行情影响已覆盖。未验证其他 Windows/Python 版本或用户当前服务进程，未执行邮件、订单、Demo/Production 或数据刷新。
- 旧报告必须按固定 generation、完整配置/日历、成本、窗口、依赖和代码重新计算。修复不证明全部因果性、费用时点或策略有效性问题已解决，也不构成 #99 前瞻验收。
