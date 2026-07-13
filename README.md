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
- OKX 可信数据层只发布已确认 K 线；主回测、可视化和 demo 只读取已发布的 cache-only generation。

执行规划模块只输出规划决策，不会下单，也不会调用 broker/order API。
OKX API 与 demo 自动化独立放在应用层：`mu_strategy.live` 只负责 OKX API 适配，`mu_strategy.demo_trading` 负责 5 分钟扫描、风控、幂等和 demo 限价单编排；它们只消费固定策略结果，不反向修改研究/回测逻辑。

## 架构

当前包结构见 [docs/architecture.md](docs/architecture.md)。

- `mu_strategy.market_data`：OKX/Binance 数据提供方、缓存策略、Top USDT-SWAP 标的池、可信数据刷新状态与 `5m/15m/1h` K 线包。
- `mu_strategy.entry`：统一入场扫描服务，输出固定结构的 `EntryScanResult`。
- `mu_strategy.strategies`：策略组注册表与组件元数据。
- `mu_strategy.experiments`：walk-forward 与消融实验。
- `mu_strategy.viz`：HTML 回测报告渲染。
- `mu_strategy.research`：当前研究结论入口。
- `mu_strategy.research.strategy_releases`：内容寻址的策略候选、SCM 审批快照和 strict exact-ID release resolver；详见 [docs/strategy-release-provenance.md](docs/strategy-release-provenance.md)。
- `mu_strategy.selection`：固定策略下的候选标的排序。
- `mu_strategy.execution`：非交易的入场与风险规划。
- `mu_strategy.live`：OKX API 执行准备工具；默认只读或 dry-run，不接入回测主流程。
- `mu_strategy.demo_trading`：OKX Demo 自动化应用层；动态 Top10 扫描、风险上限、`clOrdId` 幂等和小额限价单。

兼容入口仍然保留，例如 `mu_strategy.data`、`mu_strategy.walk_forward`、`mu_strategy.visualize`、`mu_strategy.cli`；其中 backtest 和 visualization 主入口默认只消费可信 OKX cache-only 数据。

## 常用命令

运行测试：

```powershell
python -m unittest discover -s tests
```

运行当前 OKX baseline 回测：

```powershell
python -m mu_strategy.commands.refresh_market_data --data-dir data\live --html-output reports\live\data_health.html
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\live\mu_okx_backtest.md
```

对照限价挂单成本假设：

```powershell
python -m mu_strategy.cli --days 180 --strategy baseline --fee-profile limit --report reports\live\mu_okx_backtest_limit.md
```

生成 Plotly 可视化回测：

```powershell
python -m mu_strategy.commands.refresh_market_data --data-dir data\live --html-output reports\live\data_health.html
python -m mu_strategy.visualize --days 180 --strategy baseline --chart-interval 1h --output reports\live\mu_okx_MU_USDT_SWAP_180d_baseline_backtest.html
```

主回测和可视化默认使用可信 OKX cache-only 数据。该路径只读取 `data/live/current.json` 指向的 `data/live/generations/<run_id>/` schema v3 manifest 和对应 CSV，不访问网络、不写缓存；需要先运行独立刷新命令发布当前 generation：

```powershell
python -m mu_strategy.commands.refresh_market_data --data-dir data\live --html-output reports\live\data_health.html
python -m mu_strategy.cli --days 14 --report reports\live\mu_okx_trusted_backtest.md
python -m mu_strategy.visualize --days 14 --output reports\live\mu_okx_trusted_backtest.html
```

Fibonacci 参数扫描和 walk-forward 消融仍保留在 `mu_strategy.experiments`，但它们不是当前 trusted baseline 的标准验收链路。需要重新做参数研究时，先明确数据来源和输出目录，再把生成报告写到 `reports/live/` 或用户指定的 ignored 路径。

OKX API 只读检查：

```powershell
$env:OKX_API_KEY="..."
$env:OKX_SECRET_KEY="..."
$env:OKX_PASSPHRASE="..."
python -m mu_strategy.live.okx_cli read-only --demo --inst-type SWAP --inst-id MU-USDT-SWAP --ccy USDT
```

Windows 上默认会优先读取持久化的 User/Machine 环境变量，避免 Codex 等长驻进程沿用旧的进程环境；如需强制使用当前 shell 的临时变量，设置 `$env:OKX_ENV_SOURCE="process"`。
也可以在需要凭据的 CLI 命令上显式传入 `--credential-source auto|process|user|machine`。未传 `--credential-source` 时，CLI 会先尊重 `OKX_ENV_SOURCE`，再回到自动选择。`read-only` 输出会保留 OKX 原始响应，并额外给出 `status` 与 `warnings`；例如 demo positions 对单合约返回业务错误时会进入 warning，而不会被误判为认证失败。

记录 shadow execution 事件，不发订单：

```powershell
python -m mu_strategy.live.okx_cli shadow-record --event-id evt-001 --symbol MU-USDT-SWAP --action buy --plan-price 100 --observed-price 100.2 --quantity 1 --status filled --reason "manual observation" --timestamp-ms 1780000000000
```

生成 OKX demo trading 订单 dry-run，不发订单：

```powershell
python -m mu_strategy.live.okx_cli demo-order --inst-id MU-USDT-SWAP --side buy --size 1 --order-type limit --price 100 --client-order-id DEMO001
```

`--client-order-id` 应使用 1-32 位 ASCII 字母数字。`--price` 只用于 `limit`、`post_only`、`fok`、`ioc`，`market` 订单会拒绝传入价格。
OKX demo 私有交易接口不一定支持 `MU-USDT-SWAP` 下单；demo 模式下 public instruments 也会带 `x-simulated-trading: 1` 做一致性检查。如果 `MU-USDT-SWAP` 返回 `51001`，应把它视为 demo 品种支持问题，而不是凭据失败。确认发送前会先做 demo instrument 预检；预检失败时返回 `blocked_demo_order`，不会继续发送订单。显式验证 OKX demo trading 连通性可使用已验证的小尺寸 IOC 示例：

```powershell
python -m mu_strategy.live.okx_cli demo-order --inst-id BTC-USDT-SWAP --side buy --size 0.01 --order-type ioc --price 1 --client-order-id DEMO001 --pos-side long --confirm-demo-order
```

运行 OKX Demo 5 分钟扫描 loop 的单次 dry-run，不读取私有凭证，不发订单。默认从 current generation manifest 的 universe snapshot 读取候选标的，并固定加入 `MU-USDT-SWAP` 作为 watchlist。默认 trusted-manifest 模式下，`--limit N` 表示最多 N 个 crypto universe symbols，加最多 N 个 stock-token universe symbols；`--limit 0` 表示 watchlist-only，不读取动态 universe；`--limit` 必须非负：

```powershell
python -m mu_strategy.commands.okx_demo_loop --once --dry-run --limit 10 --days 1 --data-dir data\live --dashboard-output reports\live\okx_entry_dashboard.html
```

Stage 0 dry-run cycles also append a versioned observation-only record to `data/observations/stage0.jsonl` by default. Use `--observation-log <path>` to choose another ignored local path. This sidecar preserves the existing stdout/dashboard JSON shape and is not an `OrderIntent`, broker authorization, or execution ledger; see [docs/stage0-observations.md](docs/stage0-observations.md).

刷新可信 OKX 数据层，默认维护 OKX Top10 热门币和本地配置池中的 OKX 股票概念代币 Top10，周期固定为 `5m/15m/1h`。这是发布 `data/live/generations/<run_id>/` 并原子替换 `data/live/current.json` 的唯一流程：

```powershell
python -m mu_strategy.commands.refresh_market_data --data-dir data\live --html-output reports\live\data_health.html
```

只刷新显式 symbol 子集（例如 MU-only）时，可重复传入 `--symbol`。传入后刷新范围只由这些 symbol 决定，不会拉取 Top universe ticker list，也不会因 `--limit` 扩大范围：

```powershell
python -m mu_strategy.commands.refresh_market_data --symbol MU-USDT-SWAP --data-dir data\live --html-output reports\live\data_health.html
```

持续刷新模式：

```powershell
python -m mu_strategy.commands.refresh_market_data --loop --interval-seconds 300 --data-dir data\live --html-output reports\live\data_health.html
```

持续每 5 分钟扫描并允许 OKX Demo 限价买入，需要显式确认并提供 `OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE`：

```powershell
python -m mu_strategy.commands.okx_demo_loop --confirm-demo-orders --interval-seconds 300 --limit 10 --notional-usdt 10 --max-open-positions 3
```

默认每单 `10 USDT`，最多 `3` 个 open order/position，使用 isolated `5x` 和 Fib 附近限价买入；不会市价追价。缺少凭证时 dry-run 仍可用，确认下单模式会在发送任何订单前失败。dry-run 与 confirmed demo 都先执行同一个 trusted data gate；invalid/stale/failed run 不会进入 scanner，也不会生成新订单。`--dashboard-output` 会覆盖生成本地自动刷新 HTML 看板；页面只展示人工复核信息，不提供真实下单/撤单按钮。`orders[]` 为空表示当前无挂单建议、无撤单目标；出现 `status=planned` 时才展示具体挂单价、挂单量、初始止损和绑定该建议单的撤单触发点。

## 当前产物约定

- `data/live/current.json` 和其指向的 `data/live/generations/<run_id>/` 是当前可信数据 baseline；只有明确发布 baseline 时才应入库。
- `reports/live/data_health.html`：本地数据健康看板，展示 Top universe、blocking symbols、segment diagnostics、每个周期的 latest candle、rows、valid/stale 状态和失败原因。
- `reports/live/mu_okx_MU_USDT_SWAP_180d_baseline_backtest.html`：推荐的本地 MU 180d 交互式回测报告路径。
- `reports/live/*.md` / `reports/live/*.html` 是可再生成的本地 artifact，默认不入库；如果需要审阅，给出本地链接或重新生成。
- `docs/fibonacci-preferred-parameters.md` 是历史参数记录，不等同于当前可信市场数据 baseline。

## 数据注意事项

- OKX 返回的最后一根 K 线不一定完整，数据层会忽略未确认 K 线。
- 每次可信刷新会优先复用当前已发布 generation，并增量补充后续已确认数据；没有可复用 generation 时才需要完整拉取。
- 可信数据层只使用 OKX 公开行情；OKX 股票概念/代币化标的由 `config/okx_stock_tokens.json` 维护候选池，再按 OKX 24h turnover 取 Top10。
- 可信数据层把 refresh process 与 consumer process 分开：`python -m mu_strategy.commands.refresh_market_data` 是 `data/live/current.json` 和 `data/live/generations/<run_id>/` trusted universe snapshot 的唯一写者；backtest、visualization 和 demo 只走 cache-only load。
- 可信 refresh 支持重复 `--symbol` 显式子集刷新；例如 `--symbol MU --symbol BTC-USDT-SWAP` 会经 OKX swap resolver 规范化、稳定去重，并只发布这些 symbol 的新 generation。未传 `--symbol` 时，仍使用默认 Top crypto + stock-token universe。
- backtest 和 visualization 主入口不再支持旧数据参数 `--refresh`、`--source` 或 `--trusted-data`；demo loop 的 `--refresh` 也会被拒绝。正确顺序是先运行 `python -m mu_strategy.commands.refresh_market_data ...`，再运行 `python -m mu_strategy.cli ...`、`python -m mu_strategy.visualize ...` 或 `python -m mu_strategy.commands.okx_demo_loop ...`。
- 已删除旧 per-symbol refresh API；需要刷新 canonical trusted data 时只能使用独立 refresh command。
- 可信数据层当前使用 `data/live/current.json` + `data/live/generations/<run_id>/` + `data/live/refresh_runs.jsonl`；generation manifest schema v3 包含 `run_id`、`attempt_status` (`RefreshAttemptStatus`)、`snapshot_usability` (`SnapshotUsability`)、`requested_intervals`、`effective_intervals`、`universes`、每个 dataset 的 availability/integrity/freshness/reasons、warnings 和 cycle-level error。manifest 可以附带 backward-compatible optional `diagnostics.refresh_segments`，用于审计每个 symbol/interval 的 fetch mode、耗时、rows、reuse 和失败原因；trusted consumers 不依赖该字段，缺失时仍按 dataset health fail-closed。
- `refresh_runs.jsonl` 和 `refresh_market_data` JSON 输出会暴露 per-symbol/per-interval diagnostics，包括 `refresh_segments`、最多 5 条 `slowest_segments`、失败/非 ok 的 `failed_segments`，以及按 symbol 汇总的 `blocking_symbols`。
- interval dependency 统一由 planner 处理：请求 `15m` 会实际读取/刷新 `5m,15m`；请求 `1h` 会实际读取/刷新 `5m,1h`；请求 `15m,1h` 会实际读取/刷新 `5m,15m,1h`。
- freshness 按当前 clock、interval 和最后一根已确认 K 线计算；不会因为上次 fetch 成功就默认 fresh。
- `RefreshAttemptStatus` 表示 refresh attempt 健康：只有全量 usable 且无 provider/cache/validation failure 才是 `success`；mixed usable/unusable 是 `degraded`；zero usable 不论来自 provider failure、cache read failure、validation failure、coverage failure 或 content hash mismatch 都是 `failed`。
- `SnapshotUsability` 表示 publication 是否可被消费者使用，由所有 `DatasetHealth` 的 availability/integrity/freshness 三轴推导；zero usable fail-closed 为 `invalid`，mixed usable/unusable 会按最严格 dataset 轴推导为 `stale` 或 `invalid`。
- `DatasetHealth` 表示单个 `symbol/interval` 的 availability、integrity、freshness、reason、validation 和 `content_sha256`，refresh/load/dashboard 不各自重新定义全局状态。
- malformed manifest 会 fail-closed；trading strict policy 下缺失或损坏 manifest 都会阻断消费。旧 public import 仍保留在 `mu_strategy.market_data.trusted` / `service`，但只是兼容 facade。
- 缓存会按请求窗口裁剪，避免长期回测误用超出窗口的数据。
- 数据层会检查相邻 K 线的 `previous close -> next open` 连续性，默认超过 `2%` 会阻断读取/写入，避免坏缓存或异常拼接进入回测和 demo 扫描。
- 如果增量刷新失败，已有缓存仍可用于本地复现，但结果不应被视为最新市场状态。
- baseline 的入场信号仍是二次回踩限价触发，但回测没有建模挂单队列、盘口价差、部分成交或错失成交；因此默认费用采用 `market/taker` 万五，避免用 `limit/maker` 万二高估结果。
- `1h` regime 使用收盘后才成立的 close/EMA/RSI/MACD 信息，因此只能从该 1h K 线收盘时开始供 `15m` 回测、扫描、可视化和 walk-forward 使用。[Issue #50](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/50) 修复前生成的普通报告使用了 open-time visibility，不能与修复后的结果直接比较。
- OKX API 工具默认使用环境变量读取密钥，不应把 API key、secret、passphrase 写入代码、报告或命令输出。
- 生产实盘下单入口尚未实现；当前只允许 read-only、shadow、本地 dry-run，以及显式确认后的 OKX demo trading 下单。
- OKX Demo loop 已实现 `clOrdId` 幂等、open exposure 上限、isolated `5x` 和限价买入；仍不处理生产订单生命周期、撤单/重试、成交回报、仓位同步或风控熔断。
- OKX Demo loop 在默认 trusted-manifest 模式下对 manifest bucket 独立应用 `--limit N`：最多 N 个 crypto universe symbols 加最多 N 个 stock-token universe symbols；`--limit 0` 表示只扫描 watchlist，不会从 manifest dynamic universe 追加标的。`--limit < 0` 是无效配置。

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

