# 加仓因果性配对验证

修复前收益 **341.269126%**，修复后 **193.995593%**，降低 **147.273533 个百分点**。本次验收是时间因果性；没有为恢复旧收益选择执行语义、删交易或调参。关联 #119，Refs #83 / #99，不关闭父 Issue。执行语义见[时间契约](pyramid-execution-timing.md)。

## 基线与数据身份

开始时重新核对远端 `main` 为 `21545f860bc5ad06f8b6fd119f0180896899228f`，包含已合并 #116 与 #118，无同类开放修复。主仓本地 `main` 为 `bf564af` 且有行情改动，未切换或覆盖；实现位于独立 worktree。

固定行情来自 [PR118 isolated backtest audit，run 34148910346](https://github.com/amazing-fish/mu-5x-fibonacci-trading/actions/runs/34148910346)，审计分支 `audit/pr118-replay-20260908`，workflow HEAD `76377ae3cbb01c6ca287cc3ee4002d7fbff59a98`。下载 artifact `pr118-replay-evidence-21545f86` 内的 `market.zip`，核对 SHA-256 后解压到隔离目录。没有使用主仓旧 generation，没有重新刷新行情。

- `market.zip` SHA-256：`343caaaf9e82f3d3f5406be7e5d06842d90aa313731942b5be05a31810b4bba5`。
- generation：`f694d1d9a3fd46daa8d0dc12cb12aa0b`；schema-v4 manifest、21 个 CSV segment；两边使用相同 `load_historical_window` 严格 reader 校验内容、时序与覆盖。
- UTC `[2026-03-12 17:00, 2026-09-07 17:00)`，179 个完整 24 小时自然日，非交易日计数；15m 17,184 根、1h 4,296 根；时间戳 `1773334800000` 至 `1788800400000`，结束不包含。
- registry `baseline` / `second_pullback`、5x、20/20/20/40% margin、每侧 market/taker 0.0005、起始权益 10,000；完整配置与成本两边完全相同。

| 身份 | SHA-256 |
|---|---|
| 完整 CLI 配置封装（含 days、初始权益与未建模成本声明） | `196bf7e4e3477eb6fe30d20f5982ffbac2e37c67533467e840fe3dc7256d4866` |
| 日历 `us_equities_2025_2028_v1` | `0fc82fba68f3dea507d8d20c83fb3f0a72fe700182b3d66b9004eab1a3d1b2c5` |
| 窗口 15m | `b09c28bb787a3166ff379cfe59f528014981cf8e7bc4c8f3031be801cdc6d65d` |
| 窗口 1h | `474a0c4359a04c8bc13c02bab5ece9c53eee43f3d802dfddb0f482a60b20197b` |
| 修复前 Python 源码集合（LF 归一化） | `1d20f33400e7eb949209bcc6cb1716181a8e7c130c75545800cfeb9b2249567b` |
| 修复后 Python 源码集合（LF 归一化） | `2733a3df4ab13c964a25ac7c8814e710f05623d3a8615f4bca100e5e914e97cb` |

完整 configuration、generation 三种周期的 content hash、所有交易/fill、权益曲线、候选/执行 trace 和源数据逐文件 hash 保存在 worktree 的 ignored `reports/live/before.json` / `after.json`；27 个解压文件的前后 hash 完全相同。正式报告由两边真实 CLI 另行运行，结果与 trace runner 一致。没有生产数据或台账写入。

## 收益与结构

账户收益、最大回撤沿用现有 `BacktestResult` 口径；总费用为 `sum(Trade.fees)`，已经包含在净损益中，不再扣一次。买入 fill 与平仓交易数分别披露。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 账户收益 | 341.269126% | 193.995593% |
| 最大回撤 | -26.248898% | -24.185950% |
| 期末权益 | 44126.912636 | 29399.559325 |
| 交易数 | 57 | 57 |
| 买入 fill | 111 | 106 |
| 加仓 fill | 54 | 49 |
| 总费用 | 3382.616083 | 2507.357833 |
| 盈利 / 亏损笔数 | 13 / 44 | 11 / 46 |
| top-5 净利润占总净利润 | 113.689611% | 146.501301% |
| 剔除 top-5 后净利润 | -4671.841747 | -9021.047498 |

两边交易数恰好相同，但仅 56/57 个首仓时间相同、48/57 个退出时间相同，不能解释为只删去 5 个 fill。独立审计的“54 次加仓有 16 次不满足上一根指标”仍只是诊断；本次是重新执行完整状态与权益路径，没有按该诊断筛除交易。

| max stage | 前：笔数 / 胜数 / 净损益 | 后：笔数 / 胜数 / 净损益 |
|---|---:|---:|
| 1 | 31 / 0 / -20864.619496 | 33 / 0 / -17273.227563 |
| 2 | 10 / 0 / -6537.536054 | 10 / 0 / -5605.624315 |
| 3 | 4 / 1 / 6646.026489 | 3 / 1 / 4269.265105 |
| 4 | 12 / 12 / 54883.041697 | 11 / 10 / 38009.146099 |

top-5 按净盈利降序；序号仅标识各自结果中的交易。除末笔 stage 3 外，表内其余均 stage 4。

| 排名 | 修复前：交易号 / 首仓 UTC / 净盈利 | 修复后：交易号 / 首仓 UTC / 净盈利 |
|---|---|---|
| 1 | 42 / 07-17 15:00 / 11264.135714 | 14 / 05-01 15:00 / 8005.770080 |
| 2 | 14 / 05-01 15:00 / 8622.034093 | 42 / 07-17 15:00 / 7355.284140 |
| 3 | 57 / 09-03 19:15 / 6907.620736 | 57 / 09-03 19:15 / 4371.763145 |
| 4 | 28 / 06-11 14:15 / 6485.737500 | 28 / 06-11 14:15 / 4353.138203 |
| 5 | 31 / 06-17 19:30 / 5519.226340 | 5 / 03-31 15:00 / 4334.651255 |

## 首个分歧的完整时间线

以下均为 2026 年 UTC。首仓在 **03-12 19:45** 按 405.72014 成交，两边完全相同：notional 10,000、24.647531670476106 单位、费用 5、初始止损 397.6057372。stage 2 原阈值 413.8345428，stage 3 原阈值 421.9489456，均未修改。

1. **03-13 13:30–13:45**：虽然行情 high 已越过 stage 2 阈值，但该根开盘 09:30 ET 不在原优选窗口，不产生加仓候选。
2. **13:45 开盘**：424.13。当时 13:45–14:00 这根的收盘 RSI/MACD 尚不可用。旧实现后来拿该根 RSI 89.02071692、MACD histogram 1.60494919（前值 1.08159040），把 stage 2 fill 回填到此开盘价，这是首个时间倒置。
3. **14:00 收盘信息可用/决策/下一根开盘**：前一根 OHLC 为 424.13/428.25/423.30/427.70。共享规则通过，参考价 424.13，计划阈值仍是 413.8345428。执行根开盘 427.37、已有止损未触发，最新可见 regime 为 green，于 **14:00** 按 427.37 模拟 stage 2 加仓，23.398928329082526 单位、费用 5。没有按参考价成交。
4. **14:00–14:15**：OHLC 427.37/429.47/426.05/426.05；该根 RSI 82.54557957、hist 1.72157653 ≥ 1.60494919。**14:15** 才可生成 stage 3 候选，收紧止损至首仓 405.72014，从下一根生效。旧实现的 stage 3 已在 14:00 按 427.37 回填。
5. **14:15 开盘**：425.60，高于 stage 3 阈值 421.9489456；仍 green，已有止损未触发。修复后按 425.60 加仓，23.496240601503757 单位、费用 5。该根后来 RSI 76.01382582、hist 降至 1.56302403，不能倒过来撤销已经获准的开盘成交；它会影响后续候选。到 **14:30 收盘决策** 时 stop 收紧至 blended cost 419.3299910117526，之后生效。
6. **14:30–14:45**：OHLC 424.13/425.73/422.29/422.29，低点仍高于两边止损，没有退出。
7. **14:45–15:00**：OHLC 421.94/422.67/416.74/418.28，触及已有止损。旧引擎按 418.8532162611369，修复后按 419.3299910117526 退出。Fill/Trade 时间戳标识执行根；触价具体盘中时刻未知。两边该笔都恰好回到 blended cost、总费用 30、净损益约 -30、期末权益约 9970，因此首个成交分歧没有立即产生实质账户损益差异。

首个实质损益分歧在第 2 笔：相同首仓（03-13 15:15，421.07）、起始权益 9970，stage 2 从 03-16 13:45 的 452.18 改至 14:00 的 451.74；stage 3 从 03-17 14:15 的 448.92 改至 14:30 的 454.37；stage 4 从 14:30 的 454.37 改至 14:45 的 456.19。退出时间同为 03-17 16:00、价格相同，净盈利由 828.454682 降至 638.014579。第 3 笔起始权益随之从 10798.454682 变为 10608.014579。

notional 继续严格按当时权益 × 原 margin fraction × 5 计算，因此后续数量、费用和绝对损益都会随复利路径变化。第 4 笔入场/加仓价格虽相同，起始权益已为 10565.229235 对 10365.386075，亏损绝对额相应为 -228.188235 对 -223.872015。到末笔首仓可用权益为 37219.291899 对 25027.796180。整体差额不能简单归因于删除若干原交易，也不能将不同权益下的绝对损益差当作独立信号效果。

## 末根风险与结算

两边最后一笔均在 09-03 19:15 以 950.53 首仓，最终 stage 3。旧加仓为 09-04 13:45 的 989.07、14:00 的 1000.29；修复后为 14:00 的 1000.29、14:15 的 999.44。blended cost 为 979.494121464 对 982.860535093。

最后执行根是 09-07 **16:45–17:00**，收盘 1041.10；风险处理后两边均存活，唯一一次 `end_of_data` 按 1041.10 结算。末笔净盈利为 6907.620736 对 4371.763145，费用 115.169271 对 77.307924。末根标记权益 44242.081906 对 29476.867249，平仓结算后 44126.912636 对 29399.559325；差额就是各自末笔费用，没有重复扣费。保留了 #118 的末根先风险、再处理有效计划、最后一次结算。

## 测试与执行环境

实际本次环境：Windows 11 build 26100，`D:\Develop\Tool\Miniconda\python.exe`，CPython 3.12.4 / MSC v.1929 AMD64。原 artifact 的 Linux/Python 3.12.14 环境只是来源说明；本次 before/after 均在上述同一 Windows 环境重算，修复前精确复现用户给出的指标。

先写 11 个 `run_backtest` 测试，只控制 Fibonacci 候选与指标输入；实际资格、日历、成交、风险、止损、费用、结算未 mock。修复前 `FAILED (failures=15)`、exit 1（含子场景）；修复后 11 项全部通过、exit 0。

覆盖同根收盘不能倒推开盘/触价、真实下一根与末根候选、信号参考价与下一根跳空/触价价格、未触价到期、当前收盘不能否决早前成交、前根资格不能被后根修复、执行时新 regime 失效、时段关闭/缺失连续根、止损与非时段风险优先、收紧止损不回查旧低点。#118 原 14 个测试全部保留；仅其末根加仓用例补入前一根候选，保留全部风险、价格、费用及前缀断言。旧黄金样例更新两次加仓的时间戳，价格/单位/净损益断言全部保持。

```powershell
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests -p 'test_backtest*.py'
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests -p test_position_rules.py
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests
```

聚焦回测 47 项、共享规则 14 项通过；完整 unittest **1042 项，90.977 秒，`OK (skipped=7)`，exit 0**。7 项为既有 artifact-publication symlink 场景，Windows 缺少符号链接权限；不是回测测试跳过。日历、1h 可见性、trusted-data 严格 gate、人工持仓只读以及 shadow exit 回归均包含在完整 suite 中。远端 CI/机器人 review 以 PR 最终状态为准，不由本地通过推断。

## 复现与边界

下载并校验 artifact 后，把 `market.zip` 解压根目录作为 `$dataDir`。在基线 `21545f860bc5ad06f8b6fd119f0180896899228f` 和 PR HEAD 的两个独立 checkout 中分别运行，输出文件名区分 before/after：

```powershell
gh run download 34148910346 --repo amazing-fish/mu-5x-fibonacci-trading --name pr118-replay-evidence-21545f86 --dir reports/live/audit-artifact
Get-FileHash reports/live/audit-artifact/market.zip -Algorithm SHA256
Expand-Archive reports/live/audit-artifact/market.zip -DestinationPath reports/live/fixed-market
$dataDir = (Resolve-Path reports/live/fixed-market).Path
& D:\Develop\Tool\Miniconda\python.exe -B -m mu_strategy.cli --generation-id f694d1d9a3fd46daa8d0dc12cb12aa0b --data-dir $dataDir --days 179 --strategy baseline --fee-profile market --report reports/live/causality-baseline.md
```

细粒度复现：对同一 `load_historical_window` 返回的 15m/1h 使用 `build_hourly_context`，再将 registry baseline 配置传给真实 `run_backtest`；导出 `dataclasses.asdict(result)`，用既有 `trade_concentration` / `stage_distribution` 汇总。时序 trace 仅包装 `_plan_pyramid_add` / `_execute_pyramid_add`（旧版 `_maybe_add`）记录输入/输出，未替换决策或成交。

未验证项：其他真实 generation/标的/策略族的量化变化；Windows symlink 特权场景；真实盘口顺序、网络延迟、滑点、funding、部分成交、maker queue；用户运行服务/邮件/订单/Demo/生产。没有修改费用记账（仍在平仓时确认 entry+exit fees，标记权益不即时扣入场费），也不声称其余入场时序或整个执行模型都已完成审计。旧结果必须按固定来源重跑；本次不构成 #99 样本外或前瞻表现验收。
