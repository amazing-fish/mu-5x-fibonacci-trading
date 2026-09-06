# 策略与研究指南

研究要回答规则是否值得继续验证；输出报告、候选排名或 `baseline` 名称都不授予交易资格。当前模块建设见[架构总览](architecture.md)，样本外与前瞻工作归 [#99](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/99)。

## 固定策略与规则来源

当前 MU baseline 为 `baseline`：1h 结构过滤、15m Fibonacci 二次回踩限价入场、RSI/MACD 确认、美股现金盘窗口及 5x 分阶段金字塔仓位。MU 的 `fib_lookback=8`，对应 2h；未记录标的沿用 32 根 15m 的默认窗口。

- 名称、规则身份、默认集合、可选状态和兼容别名唯一来自 [registry](../mu_strategy/strategies/registry.py)；`second_pullback_limit_8` 是 baseline 的兼容别名。
- [strategy.py](../mu_strategy/strategy.py) 持有配置与基础规则；[position_rules.py](../mu_strategy/strategies/position_rules.py) 持有共享加仓和止损判断。
- 查看当前可选策略使用 `python -B -m mu_strategy.cli --help`，避免在多处维护容易失真的完整名单。
- 1h regime 只能在该 K 线收盘后被 15m 消费。普通 walk-forward/Fibonacci 分区使用完整输入的因果指标上下文，不能把报告分区当指标重置点；release cold-start 协议另有定义。
- [Fibonacci 参数记录](fibonacci-preferred-parameters.md) 是历史实验来源，当前可执行值以配置为准；早期研究文字不覆盖现行 baseline。

## 当前快照上的回测与实验

先按[行情指南](data-guide.md)完成独立刷新。以下入口都只读可信缓存，不接受旧 `--refresh` / `--source` / `--trusted-data` 参数，不联网补齐数据。

```powershell
python -B -m mu_strategy.cli --days 180 --strategy baseline --report reports/live/mu_okx_backtest.md
python -B -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports/live/mu_okx_backtest.html
python -B -m mu_strategy.walk_forward --window-days 14 --windows 2 --report reports/live/wf.md --html-report reports/live/wf.html
python -B -m mu_strategy.experiments.fibonacci_pullback --days 60 --report reports/live/fib.md
python -B -m mu_strategy.experiments.strategy_ladder --window-days 14 --windows 2 --report reports/live/ladder.md --html-report reports/live/ladder.html --conclusion-index reports/live/ladder.json
```

Fibonacci 扫描和 walk-forward 是重新研究参数、比较策略的工具，不等于已经完成当前固定策略的标准验收。Fibonacci 非 OKX 的 `AssetSpec.source` 会被拒绝。独立 walk-forward 窗口不能拼接重置后的权益曲线计算总回撤。

## 固定历史 generation 回放

普通回测、HTML 可视化和候选 ladder 支持显式 `--generation-id`。先将下列变量设置为**本地真实保留且可验证的 generation ID**，不要直接使用别人某次报告里的 ID。

```powershell
$generationId = '<existing_generation_id>'
python -B -m mu_strategy.cli --generation-id $generationId --days 14 --report reports/live/replay.md
python -B -m mu_strategy.visualize --generation-id $generationId --days 14 --output reports/live/replay.html
python -B -m mu_strategy.experiments.strategy_ladder --generation-id $generationId --window-days 14 --windows 2 --report reports/live/ladder-replay.md --html-report reports/live/ladder-replay.html --conclusion-index reports/live/ladder-replay.json
```

历史模式经 [research.historical_data](../mu_strategy/research/historical_data.py) 唯一 reader 读取指定快照，不读 `current.json`，不按本轮时钟判断历史过期。未知 ID、路径、manifest/hash、时序、未收盘数据或覆盖错误仍失败。schema-v3 flat 与 v4 segmented 都保留严格验证；不会联网补数据或回退 current。

窗口结束于有效周期共同覆盖的最后一个完整小时，采用结束时间不包含的区间。`--days` 须被完整覆盖；ladder 另需 8 天输入历史供最长 169 小时动量预热。因此历史窗口终点可能早于 current 模式的最后一根 15m K 线，不能据此推断规则发生变化。

`historical_replay` provenance 记录 generation、原始与选中数据的 hash、发布时间、历史 freshness、窗口和完整配置。`code_sha256` 绑定当前 `mu_strategy/**/*.py` 内容（路径稳定、换行统一 LF），不是 Git commit ID；`configuration_sha256` 绑定完整配置。相同输入的产物不含运行时钟和本机目录，可作字节重放比较。

普通 walk-forward/Fibonacci CLI 尚未提供这个历史参数；release 实验已有另一套明确的 cold-start 协议，见[发布溯源](strategy-release-provenance.md)。Demo/scanner 不接受历史回放参数，历史结论不代表当前行情可交易。

## 收益、成本和比较口径

| 指标 / 场景 | 含义与限制 |
|---|---|
| 账户收益 | 窗口权益变化；不能与单笔保证金收益混用 |
| `Trade.return_pct` | 已投入保证金口径的单笔收益；HTML/walk-forward 的部分标签仍待 #88 修正 |
| 1x buy-and-hold / 杠杆 price-only diagnostic | 价格对照，不含费用、funding、清算及路径依赖，不能当作可实现杠杆收益 |
| top-5 集中度、剔除净利、stage 分布 | 用于解释结果依赖的少数交易和加仓阶段；见 [robustness](../mu_strategy/research/robustness.py) |
| 候选 ladder | 默认两个独立 14 天窗口，展示实际配置杠杆；local candidates 为 1x，registry baseline 为 5x。原始账户收益排序未做风险归一化，candidate 不表示优势已证实 |

ladder 的 `Account return` 是各窗口期末权益之和除以期初权益之和再减一，不是窗口收益复利拼接。配置杠杆也不等于持续实际敞口。完整 registry 的同快照稳健性比较归 #83，不能用现有三个候选族替代。

默认采用 `market/taker` 每侧 0.0500% 手续费。回测虽使用限价式入场信号，未建模挂单队列、盘口价差、部分成交或错失成交；`limit/maker` 每侧 0.0200% 只作成本敏感性：

```powershell
python -B -m mu_strategy.cli --days 180 --strategy baseline --fee-profile limit --report reports/live/mu_okx_backtest_limit.md
```

早于 1h 收盘可见性修复（[#50](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/50)）的普通报告不能直接与修复后结果比较。先固定数据、代码、配置、成本和窗口，再比较结果；不将旧报告的收益数字复制为“当前表现”。

## 研究与执行之间

严格候选、SCM 审批快照和 exact-ID release resolver 已实现，但当前仓库未保存已批准策略 release。第一阶段研究无需靠发布 release 才能开展；第二阶段执行前置由 [#100](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/100) 承接，详见[产品路线](product-roadmap.md)。不得用 mock release、自动挑最高收益或宽松 resolver 接通执行。
