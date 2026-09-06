# OKX 只读、Shadow 与受控 Demo

本指南承接 API 和 Demo 操作示例。日常信号与邮件使用[信号服务](signal-service.md)和[邮件手册](email-alerts.md)；当前完整执行缺口见[架构总览](architecture.md)，未来端到端流程归 [#100](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/100)。

现有 `demo_trading` 确实能够在显式确认后读取账户、设置杠杆、买入及撤销部分失效 bot 限价单。这条路径尚未接入 `OrderIntentFactory` / `SQLiteExecutionStore` 的逐 intent 持久授权、预留和恢复流程，不能视为完整自动交易系统。Production 下单未实现。

行情消费保持 trusted gate；API 适配不修改研究规则。只读账户操作可能读取私有信息，密钥只从环境配置取得，不写入报告。本文命令是操作说明，不代表已经执行或得到本轮交易授权。

## 只读检查

```powershell
$env:OKX_API_KEY="..."
$env:OKX_SECRET_KEY="..."
$env:OKX_PASSPHRASE="..."
python -m mu_strategy.live.okx_cli read-only --demo --inst-type SWAP --inst-id MU-USDT-SWAP --ccy USDT
```

Windows 上默认会优先读取持久化的 User/Machine 环境变量，避免 Codex 等长驻进程沿用旧的进程环境；如需强制使用当前 shell 的临时变量，设置 `$env:OKX_ENV_SOURCE="process"`。
也可以在需要凭据的 CLI 命令上显式传入 `--credential-source auto|process|user|machine`。未传 `--credential-source` 时，CLI 会先尊重 `OKX_ENV_SOURCE`，再回到自动选择。`read-only` 输出会保留 OKX 原始响应，并额外给出 `status` 与 `warnings`；例如 demo positions 对单合约返回业务错误时会进入 warning，而不会被误判为认证失败。

## Shadow 记录

记录 shadow execution 事件，不发订单：

```powershell
python -m mu_strategy.live.okx_cli shadow-record --event-id evt-001 --symbol MU-USDT-SWAP --action buy --plan-price 100 --observed-price 100.2 --quantity 1 --status filled --reason "manual observation" --timestamp-ms 1780000000000
```

## 单笔 Demo 预览与显式确认

生成 OKX demo trading 订单 dry-run，不发订单：

```powershell
python -m mu_strategy.live.okx_cli demo-order --inst-id MU-USDT-SWAP --side buy --size 1 --order-type limit --price 100 --client-order-id DEMO001
```

`--client-order-id` 应使用 1-32 位 ASCII 字母数字。`--price` 只用于 `limit`、`post_only`、`fok`、`ioc`，`market` 订单会拒绝传入价格。
OKX demo 私有交易接口不一定支持 `MU-USDT-SWAP` 下单；demo 模式下 public instruments 也会带 `x-simulated-trading: 1` 做一致性检查。如果 `MU-USDT-SWAP` 返回 `51001`，应把它视为 demo 品种支持问题，而不是凭据失败。确认发送前会先做 demo instrument 预检；预检失败时返回 `blocked_demo_order`，不会继续发送订单。以下小尺寸 IOC 仅示范参数形态；只有在明确授权的 Demo 连通性验收中才使用确认参数，当前合约支持及返回结果需当次核对：

```powershell
python -m mu_strategy.live.okx_cli demo-order --inst-id BTC-USDT-SWAP --side buy --size 0.01 --order-type ioc --price 1 --client-order-id DEMO001 --pos-side long --confirm-demo-order
```

## 扫描与看板

运行 OKX Demo 5 分钟扫描 loop 的单次 dry-run，不读取私有凭证，不发订单。默认从 current generation manifest 的 universe snapshot 读取候选标的，并固定加入 `MU-USDT-SWAP` 作为 watchlist。默认 trusted-manifest 模式下，`--limit N` 表示最多 N 个 crypto universe symbols，加最多 N 个 stock-token universe symbols；`--limit 0` 表示 watchlist-only，不读取动态 universe；`--limit` 必须非负：

```powershell
python -m mu_strategy.commands.okx_demo_loop --once --dry-run --limit 10 --days 1 --data-dir data\live --dashboard-output reports\live\okx_entry_dashboard.html
```

每轮返回值包含 `exit_observations` 和 `exit_observation_status`。普通 dry-run 没有账户持仓来源，因此前者为空并显式报告 unavailable；confirmed demo 只复用一次既有 `get_positions` 读取做 shadow 评估，不会由该路径发送平仓、撤单或改单。OKX 聚合持仓缺少 fills、当前 stop 和 stage，输出会保持实际 decision 为 `unknown`，并把单一合成 fill 的估算单独标为 `assumption_evaluation`。

Stage 0 dry-run cycles also append a versioned observation-only record to `data/observations/stage0.jsonl` by default. Use `--observation-log <path>` to choose another ignored local path. This sidecar preserves the existing stdout/dashboard JSON shape and is not an `OrderIntent`, broker authorization, or execution ledger; see [Stage 0 observations](stage0-observations.md).

## 持续 Demo 操作

持续每 5 分钟扫描并允许 OKX Demo 限价买入，需要显式确认并提供 `OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE`：

```powershell
python -m mu_strategy.commands.okx_demo_loop --confirm-demo-orders --interval-seconds 300 --limit 10 --notional-usdt 10 --max-open-positions 3
```

默认每单 `10 USDT`，最多 `3` 个 open order/position，使用 isolated `5x` 和 Fib 附近限价买入；不会市价追价。缺少凭证时 dry-run 仍可用，确认下单模式会在发送任何订单前失败。dry-run 与 confirmed demo 都先执行同一个 trusted data gate；invalid/stale/failed run 不会进入 scanner，也不会生成新订单。`--dashboard-output` 会覆盖生成本地自动刷新 HTML 看板；页面只展示人工复核信息，不提供真实下单/撤单按钮。`orders[]` 为空表示当前无挂单建议、无撤单目标；出现 `status=planned` 时才展示具体挂单价、挂单量、初始止损和绑定该建议单的撤单触发点。

## 现有边界

确定性 `clOrdId` 和当前 open exposure 检查不等于持久幂等、部分成交对账或断线恢复。现有撤销仅覆盖能识别的 bot 限价买单失效路径，不构成完整 cancel/unknown/reconciliation 契约；没有真实退出及保护单闭环。

`initial_stop` 是规划参考，不证明交易所已有保护单。聚合 OKX 仓位的 stage/stop 估算保持 unknown/degraded，不能转成确定持仓提醒。完整 Stage 0–4 门禁、环境身份和执行协议见[执行设计](execution-roadmap.md)。

## 程序化 dry-run

程序化调用 `run_once(dry_run=True)` 时，是否配置观测日志不影响扫描结果：数据阻断、扫描失败、无信号与 READY 使用同一分类，每个允许扫描的标的只扫描一次。自定义 loader 必须提供 canonical generation/hash 来源，自定义 scanner 必须返回有效且非 `UNKNOWN` 的类型化结果；关闭日志不能放宽这些要求。写盘失败单独报错，保留已计算的信号含义，不重扫，也不返回本轮订单计划。
