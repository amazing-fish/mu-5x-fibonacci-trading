# CLAUDE.md

MU 多头策略研究仓库。研究工作台 + 受控的 OKX Demo 应用层，**没有实盘下单实现**。

## 协作分工

本仓库采用固定的三方循环，Claude 不直接写产品代码：

1. **Claude 定方向** — 审阅现状、设计架构、判断优先级、决定下一步。
2. **Claude 产出 goal prompt** — 交给 codex agent 执行。多任务先开 issue 跟踪，prompt 引用 issue 号。
3. **codex 开发并提交 PR** — GitHub 上做初步 review。
4. **Claude 审阅 PR** — 核实验收标准、CI、边界是否被侵犯，给出合并建议。
5. 用户点 merge，回到第 1 步。

据此：

- 用户要求"分析/评审/定方向"时，**只输出分析和 prompt，不要动产品代码**。
- 允许 Claude 直接写的：`CLAUDE.md`、issue/PR 正文与评论、本地一次性诊断脚本（用完删）。
- goal prompt 必须写明 GOAL、IMPLEMENT、MUST NOT CHANGE、NON-GOALS、TESTS、VERIFY、PR 要求。codex 看不到本次对话，上下文要自带。
- 审阅 PR 时逐条核对 issue 的验收标准，并确认没有偷偷放宽 fail-closed 门禁。

## 铁律

- **禁止实盘下单。** 只允许 read-only、shadow、本地 dry-run，以及显式 `--confirm-demo-order(s)` 后的 OKX Demo 下单。生产执行归 issue #7，不要在其他 PR 里碰。
- **主仓库禁止 checkout 到非默认分支**，改代码开 worktree。默认分支是 `main`。
- **默认分支受 ruleset `upgradebyPR` 保护**（绑定 `~DEFAULT_BRANCH`，规则 `pull_request`），一切改动必须走 PR，直推会被 remote 拒绝 —— 包括只改 `CLAUDE.md` 或文档。
- **可信数据层读写分离。** `python -m mu_strategy.commands.refresh_market_data` 是 `data/live/current.json` 和 `data/live/generations/<run_id>/` 的唯一写者。backtest、visualize、demo loop 全部 cache-only，禁止联网、禁止写缓存。
- **数据缺失/过期一律 fail-closed**，不许加消费侧回退路径。
- **零第三方依赖。** 当前纯 Python 3.12 标准库（Plotly 仅作为 HTML 里的 CDN 引用）。新增依赖需先讨论。
- **密钥只从环境变量读**，不进代码、报告、日志、审计事件。
- 生成的报告写 ignored 路径（`reports/live/`），返回路径而不是粘贴 HTML 内容。

## 常用命令

```powershell
# 测试（唯一验收门禁，CI 跑同一条）
python -B -m unittest discover -s tests

# 刷新可信数据（消费前必须先跑）
python -m mu_strategy.commands.refresh_market_data --data-dir data\live --html-output reports\live\data_health.html

# 回测 / 可视化
python -m mu_strategy.cli --days 180 --strategy baseline --report reports\live\mu_okx_backtest.md
python -m mu_strategy.visualize --days 180 --strategy baseline --output reports\live\mu_backtest.html

# Demo 扫描 dry-run（不读私钥、不下单）
python -m mu_strategy.commands.okx_demo_loop --once --dry-run --limit 10 --days 1 --data-dir data\live
```

刷新和消费是两个独立进程，顺序不能颠倒。backtest/visualize 已移除 `--refresh`、`--source`、`--trusted-data`。

## 架构

模块职责、实际接入与证据见 [docs/architecture.md](docs/architecture.md)；产品阶段见 [docs/product-roadmap.md](docs/product-roadmap.md)，Stage 0–4 执行门禁及 Stage 3 契约见 [docs/execution-roadmap.md](docs/execution-roadmap.md)。

关键语义：

- `1h` regime 只在该 K 线**收盘后**可见，禁止用 open-time 泄漏未来信息。
- 默认费率用 `market/taker` 万五。回测没有建模挂单队列、盘口价差、部分成交，不要用 `limit/maker` 万二当默认。
- `initial_stop` 只是复核用的规划值，**不是交易所侧保护单**。

## 当前状态

模块实现和未接通链路集中记录在 [架构与建设现状](docs/architecture.md)，交付依赖见 [产品路线](docs/product-roadmap.md)。本文件不重复保留容易过时的通知、研究或执行进度快照。

## 存储层复杂度债（v4 分段）

`data/live/` 布局：generation 目录只存 metadata manifest，K 线落在共享的
`segments/<source>/<symbol>/<interval>/<YYYY-MM>.csv`。已封闭月份的 segment **不可变、永不重写**，
稳态下一次 refresh 只重写当月那一个文件。

需要盯住的债，**不要在无关 PR 里顺手扩张**：

- `imported-<generation_id>.csv` 兼容段命名空间是 **v3→v4 迁移期专用**。它在 `store.py` 里制造了
  与 canonical segment 平行的第二条路径，`imported_segment` / `compatibility` / `partial` 相关分支
  约 45 处，`_is_allowed_segment_source()` 因此需要 9 个参数才能判断一个路径是否合法。
  **等 schema-v3 generation 全部迁完后应当整体拆掉**，让路径判断回到单一 canonical 形态。
  新功能不要依赖 imported 段语义。
- `data/live/refresh_runs.jsonl` 单调增长（合并时 8466 行），无边界。可分离，未解。
- 无保留/GC：分段消除了增长的阶数（每 run 新写字节 ∝ 新增 K 线），但总 `segments/` 仍 ∝ 总历史。
  真要加驻留上限，是分段之上几十行的事 —— **不要引入跨进程锁、Windows 命名互斥体、reader lease**，
  分段方案不删任何文件，用不上这些。已关闭的 #78 / #79 两次栽在这里（各 10+ 个连环 fix commit）。

## 与 codex 协作的已知失效模式

- codex 的 goal 挂载会解析 issue 的**最新 comment** 当权威范围。如果在 issue 下追加了窄化范围的
  comment，它会据此关掉正在进行的 PR 并新开一个 —— #72 被这样连关两次（#78、#79 都是这么来的）。
  修正方式：把误导性 comment 显式标注 RETRACTED，并在 prompt 里写明「认为该关就发评论说明并停下，
  不要关」。
- 往 `data/live/` 提交运行时文件是它犯过的错（#79 的 `retention-pin.json`）。该目录由唯一写者产出，
  不进版本控制。
- review 时先 `grep "def test_"` 列全量测试名再判断缺什么。PR 描述里没写 ≠ 代码里没有。
- `gh pr diff <n>` 会静默返回空文件。拿到 diff 先确认非空，或改用
  `git diff $(git merge-base <base> <head>) <head>`。
