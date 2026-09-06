# MU 策略研究与信号复盘

将 MU 多头交易想法变成可重复研究、可解释信号和人工复盘记录。默认使用 OKX `MU-USDT-SWAP`；当前重点是**信号验证与网易邮件提醒人工交易**，随后才建设完整的受控 Demo 闭环。

已有可信行情、固定策略与回测、持续扫描和健康、邮件事件、实时复盘及人工持仓台账。已有显式确认的 Demo 入口，但完整订单/成交/仓位恢复链尚未接通，Production 未实现。模块状态见[架构与建设现状](docs/architecture.md)，交付顺序见[产品路线](docs/product-roadmap.md)。

## 开始使用

使用 Python 3.12，从仓库根目录运行。Python 运行时仅用标准库；交互式回测 HTML 使用 Plotly CDN。

**日常复盘**：使用正在运行的信号服务对应的 `--data-dir`。

```powershell
python -B -m mu_strategy.commands.render_signal_review --data-dir data/live --serve
```

打开输出的本机地址，可查看扫描、邮件和持仓，保存人工反馈、实际成交与当前状态确认。它不会自行启动行情或邮件服务。操作、备份和静态导出见[复盘手册](docs/signal-review.md)。

**研究回测**：先由独立刷新流程发布可信快照，再运行只读消费者。

```powershell
python -B -m mu_strategy.commands.refresh_market_data --symbol MU-USDT-SWAP --days 180 --data-dir data/live --html-output reports/live/data_health.html
python -B -m mu_strategy.cli --days 180 --strategy baseline --report reports/live/mu_okx_backtest.md
python -B -m mu_strategy.visualize --days 180 --strategy baseline --output reports/live/mu_okx_backtest.html
```

刷新访问公共行情并写入数据；回测和可视化不联网补行情。缺失、损坏、过期或覆盖不足会按现有可信 gate 拒绝消费。固定历史 generation、实验及收益口径见[研究指南](docs/research-guide.md)。

**开发检查**：

```powershell
python -B -m unittest discover -s tests
```

## 文档入口

| 要解决的问题 | 文档 |
|---|---|
| 各模块建成了什么，哪里尚未接通 | [架构与建设现状](docs/architecture.md) |
| S1/S2 的目标、依赖与剩余工作 | [产品路线](docs/product-roadmap.md) |
| 刷新范围、数据错误、存储和迁移 | [可信行情指南](docs/data-guide.md) |
| baseline、历史回放、实验和结果解释 | [研究指南](docs/research-guide.md) |
| 持续扫描、健康查询、Windows 常驻与恢复 | [信号服务](docs/signal-service.md) |
| 网易邮箱配置、去重、送达和前瞻准备 | [邮件提醒](docs/email-alerts.md) |
| 每日复盘、反馈、人工成交和状态确认 | [复盘手册](docs/signal-review.md) |
| 只读 OKX、shadow 和显式确认的 Demo 操作 | [Demo 操作指南](docs/demo-guide.md) |
| 策略 release、执行 Stage 0–4 与冻结契约 | [发布溯源](docs/strategy-release-provenance.md) · [执行设计](docs/execution-roadmap.md) |

生成报告默认放在 ignored 的 `reports/live/`。历史报告和 `candidate` 状态不代表当前策略有效或获得交易授权；“手动交易”反馈、实际成交记录、策略建议和交易所状态各自保留来源。
