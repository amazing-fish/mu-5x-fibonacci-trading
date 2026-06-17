# MU 5x Fibonacci 策略研究

本仓库用于研究 MU 多头策略，当前默认数据源为 OKX `MU-USDT-SWAP`。项目目标是把主观交易想法拆成可回测、可对比、可持续扩展的规则和工具，不提供投资建议。

当前 baseline 使用：

- `1h` 市场结构过滤，判断是否允许做多。
- `15m` Fibonacci 回踩入场；MU 当前 baseline 使用 2h 回看窗口，即 `fib_lookback=8`。
- RSI 和 MACD 作为确认过滤。
- 美股现金盘时间窗口。
- `5x` 分阶段金字塔加仓。
- 回测默认按 `market/taker` 市价吃单成本扣手续费，费率 `0.0500%`；`limit/maker` 万二只作为成本敏感性对照。
- 按入场、加减仓、出场、过滤等维度拆分策略组。
- OKX 增量缓存，只使用已确认 K 线，并按请求的 `days` 窗口裁剪缓存。

执行规划模块只输出规划决策，不会下单，也不会调用 broker/order API。
OKX API 相关工具独立放在 `mu_strategy.live` 下，当前只覆盖只读检查、shadow execution 本地审计、以及需要显式确认的 OKX demo trading 订单。

## 架构

当前包结构见 [docs/architecture.md](docs/architecture.md)。

- `mu_strategy.market_data`：OKX/Binance 数据提供方与缓存策略。
- `mu_strategy.strategies`：策略组注册表与组件元数据。
- `mu_strategy.experiments`：walk-forward 与消融实验。
- `mu_strategy.viz`：HTML 回测报告渲染。
- `mu_strategy.research`：当前研究结论入口。
- `mu_strategy.selection`：固定策略下的候选标的排序。
- `mu_strategy.execution`：非交易的入场与风险规划。
- `mu_strategy.live`：OKX API 执行准备工具；默认只读或 dry-run，不接入回测主流程。

兼容入口仍然保留，例如 `mu_strategy.data`、`mu_strategy.walk_forward`、`mu_strategy.visualize`、`mu_strategy.cli`。

## 常用命令

运行测试：

```powershell
python -m unittest discover -s tests
```

运行当前 OKX baseline 回测：

```powershell
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\mu_okx_backtest.md
```

运行 MU 最新一周 Fibonacci 窗口敏感性回测：

```powershell
python -m mu_strategy.experiments.fibonacci_pullback --asset MU --days 7 --min-hour 1 --max-hour 12 --strategy baseline --fee-profile market --refresh --multi-report reports\mu_fibonacci_pullback_1h_12h_7d.md
```

运行多标的 Fibonacci 优选参数对照：

```powershell
python -m mu_strategy.experiments.fibonacci_pullback --asset MU,SPACEX,META,BTC --days 180 --min-hour 1 --max-hour 12 --strategy baseline --fee-profile market --refresh --multi-report reports\fibonacci_pullback_multi_asset_1h_12h_180d.md
```

对照限价挂单成本假设：

```powershell
python -m mu_strategy.cli --days 180 --strategy baseline --fee-profile limit --report reports\mu_okx_backtest_limit.md
```

运行策略组实验，并生成组件矩阵 HTML：

```powershell
python -m mu_strategy.walk_forward --window-days 180 --windows 1 --report reports\mu_okx_strategy_group_review.md --html-report reports\mu_okx_strategy_components.html
```

生成 Plotly 可视化回测：

```powershell
python -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports\mu_okx_baseline_backtest.html
```

显式使用 Binance 做对照：

```powershell
python -m mu_strategy.cli --source binance --symbol MUUSDT --days 180 --strategy baseline --report reports\mu_binance_backtest.md
```

OKX API 只读检查：

```powershell
$env:OKX_API_KEY="..."
$env:OKX_SECRET_KEY="..."
$env:OKX_PASSPHRASE="..."
python -m mu_strategy.live.okx_cli read-only --demo --inst-type SWAP --inst-id MU-USDT-SWAP --ccy USDT
```

Windows 上默认会优先读取持久化的 User/Machine 环境变量，避免 Codex 等长驻进程沿用旧的进程环境；如需强制使用当前 shell 的临时变量，设置 `$env:OKX_ENV_SOURCE="process"`。
也可以在需要凭据的 CLI 命令上显式传入 `--credential-source auto|process|user|machine`。`read-only` 输出会保留 OKX 原始响应，并额外给出 `status` 与 `warnings`；例如 demo positions 对单合约返回业务错误时会进入 warning，而不会被误判为认证失败。

记录 shadow execution 事件，不发订单：

```powershell
python -m mu_strategy.live.okx_cli shadow-record --event-id evt-001 --symbol MU-USDT-SWAP --action buy --plan-price 100 --observed-price 100.2 --quantity 1 --status filled --reason "manual observation" --timestamp-ms 1780000000000
```

生成 OKX demo trading 订单 dry-run，不发订单：

```powershell
python -m mu_strategy.live.okx_cli demo-order --inst-id MU-USDT-SWAP --side buy --size 1 --order-type limit --price 100 --client-order-id demo-001
```

显式发送 OKX demo trading 订单：

```powershell
python -m mu_strategy.live.okx_cli demo-order --inst-id MU-USDT-SWAP --side buy --size 1 --order-type limit --price 100 --client-order-id demo-001 --confirm-demo-order
```

## 当前产物

- `reports/mu_okx_backtest.md`：当前 OKX baseline Markdown 回测报告。
- `reports/mu_okx_strategy_group_review.md`：策略组实验结果表。
- `reports/mu_okx_strategy_components.html`：策略组件可视化矩阵。
- `reports/mu_okx_baseline_backtest.html`：交互式 `1h` 可视化回测，包含价格、成交量和权益曲线联动。
- `reports/mu_fibonacci_pullback_1h_12h_7d.md`：MU 最新一周 `1h-12h` Fibonacci 窗口回测，用于确认 2h baseline。
- `reports/fibonacci_pullback_multi_asset_1h_12h_180d.md`：MU/SPACEX/META/BTC 的优选窗口对照。
- `data/OKX_MU-USDT-SWAP_*_180d.csv`：OKX 已确认 K 线缓存，用于本地复现实验。
- `docs/fibonacci-preferred-parameters.md`：当前标的优选 Fibonacci 参数记录。

## 数据注意事项

- OKX 返回的最后一根 K 线不一定完整，数据层会忽略未确认 K 线。
- 每次刷新会在已有缓存基础上增量补充后续已确认数据。
- 缓存会按请求窗口裁剪，避免长期回测误用超出窗口的数据。
- 如果增量刷新失败，已有缓存仍可用于本地复现，但结果不应被视为最新市场状态。
- baseline 的入场信号仍是二次回踩限价触发，但回测没有建模挂单队列、盘口价差、部分成交或错失成交；因此默认费用采用 `market/taker` 万五，避免用 `limit/maker` 万二高估结果。
- OKX API 工具默认使用环境变量读取密钥，不应把 API key、secret、passphrase 写入代码、报告或命令输出。
- 生产实盘下单入口尚未实现；当前只允许 read-only、shadow、本地 dry-run，以及显式确认后的 OKX demo trading 下单。
- v1 不接入策略自动下单，不处理生产订单生命周期、撤单/重试、成交回报、仓位同步、风控熔断或幂等执行。

## 策略组说明

当前策略组可以通过 `--strategy` 指定，主要包括：

- `legacy_break_high`：旧版突破前高确认策略，保留为备用对照。
- `baseline`：当前固定 baseline，采用二次回踩确认买入；MU 使用 2h Fibonacci 回看窗口。
- `direct_next_open`：确认后下一根开盘直接买入。
- `baseline_half_protect`：baseline 入场 + 半保护止损。
- `baseline_green_wide`：baseline 入场 + `1h green` 宽止损。
- `baseline_yellow_wide`：baseline 入场 + `1h yellow` 宽止损。
- `baseline_yellow_green_wide`：baseline 入场 + yellow/green 均宽止损。
- `baseline_half_green_wide`：baseline 入场 + 半保护 + green 宽止损。
- `optimized_v2`：旧突破入场 + 首仓追价、信号 K 宽度、反向 Fibonacci 压力过滤。

策略组可视化报告会按入场策略、加减仓策略、出场策略、过滤策略拆分展示，方便持续组合和消融回测。
