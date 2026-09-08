# 净权益、费用确认与配对验收

本次修复只改变净权益测量和采样。固定快照下，全部 57 个 Trade、106 个买入 Fill 的所有字段及期末权益 **精确不变**；净权益每个采样点均能用实际成交和已发生手续费对账。Refs [#121](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/121)、#83 / #99，不关闭父 Issue。

## 三个不同概念

| 概念 | 当前契约 |
|---|---|
| 仓位规模计算基准 / trade budget | `run_backtest` 的 `equity` 是已结算权益；开仓及加仓仍用它乘原保证金比例与杠杆计算 notional。持仓期间不从此预算减费或加入浮盈亏 |
| 账户净权益 | `_marked_equity` 返回已结算权益 + 当前浮盈亏 − 当前持仓已发生入场/加仓费；已关闭 Trade 的净损益已包含在已结算权益内 |
| 可用资金 / 保证金 | 当前没有完整费用预留、可用资金门禁或交易所保证金模型。本次不新增，也不以净权益冒充该能力 |

当前不含 funding 等额外现金流的模型，每个采样点满足：

`net equity = initial equity + cumulative realized gross PnL + unrealized PnL - cumulative incurred fees`

保证金占用不是费用，不从账户净权益重复扣除。尚未发生的退出费不预扣。`_close_position` 仍用原算法一次性计算该笔净损益、更新已结算权益；`Trade.pnl` 已经是净额，不能再减 `Trade.fees`。本次没有修改 `_make_fill`、`_close_position`、风险规则或候选执行规则。

人工反例：初始 10,000，100 买入 100 单位，每侧 0.0005，成交后同价净权益 9,995。同价平仓总费 10，期末 9,990；102 平仓毛利 200、入场费 5、退出费 5.1、净利 189.9，期末 10,189.9。未成交的候选不收费。原实现只在平仓一次性体现所有费用，持仓标记净值高估，并非没有扣手续费。

## 采样和时间含义

`BacktestResult.equity_curve` 保持 `(bar_open_time_ms, net_equity)` 类型。时间戳是 **15m K 线标签**：收盘值在该根结束后才可知，触价发生的精确盘中时刻未知。不得把这些点当作开盘时已可见的数据，或推断 OHLC 内部路径。列表顺序是测量契约的一部分，同标签的点不得去重成一个点。

1. 第一根保留初始权益点；该根不执行策略，空仓初始值也等于其收盘净权益。少于 4 根沿用原空结果契约。
2. 每次实际首仓/加仓后，以该 fill 的实际模拟成交价标记全部持仓，并计入截至此刻已发生的费用。候选、失效、未触价或日历门禁不产生 fill 点和手续费。
3. 实际退出后保留净结算点。已有止损/非时段风险仍先于加仓；同根入场后立即止损时保留成交后、退出后两个点。
4. 每根已输入 K 线保留一个收盘净权益点，包含首仓根、候选等待/失效、空仓、退出后的空仓以及末根。首仓 `continue` 不再漏掉本根收盘。退出后收盘的值与退出后结算相同，保留其采样意义。
5. `direct_next_open` / `break_high` 在信号根控制流中提前创建下一根成交。先记录信号根空仓收盘，才创建未来 fill；未来入场及可能立即发生的退出都不能污染信号根。下一轮再记录执行根收盘，保留原有入场方式的管理差异。
6. 最后一根先处理原有风险、有效加仓和止损更新，再记录收盘净权益；若仍有仓位，唯一一次 `end_of_data` 结算最后发生。此时收盘净权益与最终权益的差额只含尚未发生的退出费，当前持仓入场费早已计入。

最大回撤按上述有序净权益样本计算，包含实际模拟成交价格上的测量与收盘测量；不是精确盘中极值、清算模型或策略有效性证明。保留 #118 末根风险优先、#120 加仓因果性、#116 日历与 1h 收盘可见性。

## 当前基线与数据身份

开始时远端 main 为 `d3d050f34d6f26a830a57dce4abacab723b9e77e`，包含已合并 #116 / #118 / #120；未发现同类开放 Issue/PR，另建 #121。主仓本地 main 为 `bf564af` 且有未提交数据及产物，本次未切换、回滚或覆盖，实现和对照使用独立 worktree。

- 数据来自 [Actions run 34148910346](https://github.com/amazing-fish/mu-5x-fibonacci-trading/actions/runs/34148910346)，artifact `pr118-replay-evidence-21545f86` 内的 `market.zip`。
- ZIP SHA-256：`343caaaf9e82f3d3f5406be7e5d06842d90aa313731942b5be05a31810b4bba5`。复用 #120 的隔离解压副本，根目录包含 `generations/`、`segments/`，没有使用主仓 tracked 旧数据。
- generation：`f694d1d9a3fd46daa8d0dc12cb12aa0b`，schema-v4；严格 reader 校验 21 个 CSV segment；27 个数据文件在配对前后逐文件 hash 一致。未刷新、替换或改写。
- UTC `[2026-03-12 17:00, 2026-09-07 17:00)`，179 个完整 24 小时自然日；15m 17,184 根，1h 4,296 根。
- registry `baseline` / `second_pullback`，5x，margin steps 20/20/20/40%，market/taker 每侧 0.0005，初始权益 10,000。两边完整配置、日历、窗口和依赖相同。

| 身份 | SHA-256 |
|---|---|
| 完整 CLI 配置封装 | `196bf7e4e3477eb6fe30d20f5982ffbac2e37c67533467e840fe3dc7256d4866` |
| 日历 `us_equities_2025_2028_v1` | `0fc82fba68f3dea507d8d20c83fb3f0a72fe700182b3d66b9004eab1a3d1b2c5` |
| 窗口 15m | `b09c28bb787a3166ff379cfe59f528014981cf8e7bc4c8f3031be801cdc6d65d` |
| 窗口 1h | `474a0c4359a04c8bc13c02bab5ece9c53eee43f3d802dfddb0f482a60b20197b` |
| 修复前 `mu_strategy` Python 内容集合 | `2733a3df4ab13c964a25ac7c8814e710f05623d3a8615f4bca100e5e914e97cb` |
| 修复后 `mu_strategy` Python 内容集合 | `23ea9a472f46729fc0fdee8f2fdfc4dd587480407a3270f7f92521e720addeb9` |

## 三路配对结果

独立进程运行：原引擎、原采样仅扣当前持仓入场费的诊断、完整修复。诊断只用于分解影响，不作为必须匹配的目标；三路均走真实 `load_historical_window`、`build_hourly_context`、registry 和 `run_backtest`。实际 before/after CLI 另行运行，汇总与细粒度导出一致。

预设数值容差：`abs_tol=1e-9`、`rel_tol=1e-12`；时点、stage、退出原因等离散字段要求完全一致。实际所有 Trade/Fill 字段（时点、价格、数量、notional、margin fraction、费用、stage、退出原因、净 PnL、单笔保证金收益）以及期末权益 **精确相等**，没有动用容差接受交易变化。

| 指标 | 原引擎 | 仅费用诊断 | 完整修复 |
|---|---:|---:|---:|
| 账户收益 % | 193.99559325279338 | 相同 | 相同 |
| 期末权益 | 29399.55932527934 | 相同 | 相同 |
| 最大回撤 % | -24.185950262931833 | -24.188368164054207 | -24.188368164054207 |
| Trade / 买入 fill | 57 / 106 | 相同 | 相同 |
| 总手续费 | 2507.3578334614494 | 相同 | 相同 |
| 胜率 % | 19.298245614035086 | 相同 | 相同 |
| top-5 净利占比 % | 146.50130112007116 | 相同 | 相同 |
| 剔除 top-5 后净利 | -9021.047497814998 | 相同 | 相同 |
| 净权益点数 | 6099 | 6099 | 17347 |

原有 6,099 个采样全部保留，其中 6,041 个持仓收盘点按已发生费用修正；退出结算没有再扣费。新增 11,248 点 = 106 个成交后点 + 11,142 个收盘点；新增收盘中 57 个为持仓首仓根，其余 11,085 个为空仓/退出后收盘。最大回撤减少 `0.0024179011223757207` 个百分点；本快照中新增采样没有进一步改变最大值，这不意味着新增点没有意义或其他输入也不会变化。

完整修复的 **全部 17,347 点** 都通过独立账式核算：只观察真实 fill/exit，累计毛损益和费用，计算浮盈亏，不 mock 掉成交、费率、净值或结算。最大浮点对账残差 `2.1827872842550278e-11`。原采样未确认的持仓入场费最大为 `64.82516588436512`。

首次序列差异为新增空仓收盘，标签 **2026-03-12 17:15 UTC**，净值 10,000。首次真实成交在 **19:45**，价格 405.72014，24.647531670476106 单位，fee 5；新增成交后点 9,995，新增该根收盘点 9,998.200728462729。首次保留点的费用差异在 **20:00** 根收盘：原值 9,977.320820208728，净值 9,972.320820208728，差额恰为已发生入场费 5。上述收盘值仅在各根结束后可知。

末根标签 **2026-09-07 16:45 UTC**：结算前净权益 29,439.325555083957，当前持仓入场费 37.54169426991165 已体现；随后退出费 39.766229804617 后，唯一一次结算得到 29,399.55932527934。累计已实现毛利 21,906.917158740805 减累计实际费用约 2,507.35783346145，加初始 10,000，与最终权益在上述舍入范围内一致。

完整记录以规范 JSON（排序 key、紧凑分隔符）计算 SHA-256，可由配对脚本重建：

| 内容 | SHA-256 |
|---|---|
| 三路全部 Trade/Fill（完全相同） | `5ff2b75c6fcec5b8c30335380dcddc1e773c282fc9a7c083fa2bd6bac78336f9` |
| 原净值序列 | `27d0d68d15668e6236a024cfef37d5aa07952ae0435bf2188269606fd169e422` |
| 仅费用诊断序列 | `f99a34841c4bad31acd7ca699813a868cc3f02caa8c0236072190a038b26b9ff` |
| 完整修复净值序列 | `1e853d61937a960f4f4f86ea23ba76e32d3d92b0c7a1d0995ea2694e2e561bbe` |

## 复现与测试

环境为 Windows 11 build 26100，`D:\Develop\Tool\Miniconda\python.exe`，CPython 3.12.4 / MSC v.1929 AMD64，标准库。GitHub CI 的 Ubuntu / Python 3.12 结果单独记录，不能替代 Windows 验收。

先写首批 12 个真实入口测试，未修改引擎时 22 个失败子场景（exit 1），修改后通过。最终扩展为 16 项，在原基线运行出现 26 个失败子场景（exit 1），在修复后全部通过；覆盖手算净值峰谷/回撤、零费率、首仓与后续复利、四阶段预算/累计费用、未触价/失效、所有现有入场方式、同根止损、跳空、普通退出、非时段风险优先和末根结算。#118/#120 原测试未改，63 项回测回归通过（exit 0）。

完整 `python -B -m unittest discover -s tests`：**1,058 项，51.596 秒，`OK (skipped=7)`，exit 0**。7 个跳过为既有 artifact-publication 的 Windows symlink 权限场景，没有回测测试跳过。`git diff --check` 通过；AST 对照确认 backtest.py 只有 `run_backtest` 和 `_marked_equity` 两个符号变化。

首轮沙箱内全量为 1,058 项 / 101.648 秒 / exit 1，两个既有 localhost HTTP 防跨域用例报 `WinError 10053`，另 7 项跳过：`test_position_management.PositionManagementTests.test_http_origin_versions_error_input_retention_and_static_read_only`、`test_signal_feedback.SignalFeedbackTests.test_bad_requests_and_other_event_kinds_cannot_write_feedback`。两个用例在原基线及沙箱外修复分支聚焦复跑均通过，随后沙箱外完整 suite 通过。未修改相关服务/测试，未确认连接中止根因，不以猜测归因本次净权益改动或具体系统组件。GitHub CI 和机器人 review 以 PR 最终状态为准。

下载并核验上述 `market.zip`；解压根目录为 `$dataDir`，不是其 `generations/<id>` 子目录。复用隔离副本时仍核验其文件与固定 archive 一致。不要刷新数据。

```powershell
git worktree add --detach reports/live/before-source d3d050f34d6f26a830a57dce4abacab723b9e77e
$dataDir = '<absolute extraction root containing generations and segments>'
& D:\Develop\Tool\Miniconda\python.exe -B tests/replay_net_equity.py pair --before-source reports/live/before-source --after-source . --data-dir $dataDir --output-dir reports/live/equity-pair
& D:\Develop\Tool\Miniconda\python.exe -B -m mu_strategy.cli --generation-id f694d1d9a3fd46daa8d0dc12cb12aa0b --data-dir $dataDir --days 179 --strategy baseline --fee-profile market --report reports/live/equity-after.md
# 在 before-source 中重复真实 CLI，report 使用独立 before 文件名。
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests -p 'test_backtest*.py'
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests
```

[配对脚本](../tests/replay_net_equity.py) 导出 `before.json`、`fee-only.json`、`after.json`、`comparison.json`，包含全部 Trade/Fill、净值序列、每点账式分解、完整配置/数据/源码身份、首个差异和分类。写入 ignored `reports/live/`，不覆盖历史报告；不同采样版本不能仅用点数或相同时间戳逐行相减。先按 K 线标签、事件类别和同类序号对齐保留点，再比较新增点。

固定快照是回归基准，未参与样本外验收。本次未量化其他真实 generation/标的/策略族，也未验证所有费用/清算模型、盘口、滑点、funding、部分成交、maker queue 或策略有效性；没有修改服务、邮件、订单、Demo、正式台账，不合并、不部署。不扩大为成本敏感性、公平风险对照或前瞻研究。
