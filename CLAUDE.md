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
- goal prompt **不进仓库**，在会话里给出即可。它是一次性载体：codex 执行完，验收标准已落成测试、边界已落成代码，
  prompt 本身价值归零。持久的东西放 issue（验收标准）和 PR（实际改动）。
  `docs/plans/` 留给设计文档（`doc_kind: design`，讨论契约与不变量），完成后按 `docs/archive/legacy-plans/` 的惯例移走并标注过期。
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

完整版见 [docs/architecture.md](docs/architecture.md)（约 1000 行，含 Stage 0-4 执行路线图和 Stage 3 契约）。分层：

| 包 | 职责 |
|---|---|
| `market_data` | OKX/Binance provider、可信 generation 刷新与 cache-only 载入、universe |
| `strategies` | 策略组注册表（`baseline` 为当前固定 baseline） |
| `entry` | 入场扫描，输出 `EntryScanResult` |
| `execution` | 非交易的入场/风险规划、`OrderIntent` v1、SQLite 审计与幂等存储 |
| `experiments` | walk-forward 与消融 |
| `research` | 当前结论、策略发布溯源 |
| `viz` | 回测 / 数据健康 / 扫描看板 HTML |
| `live` | OKX API 适配（只读、shadow、guarded demo） |
| `demo_trading` | 5 分钟 Demo 扫描编排 |

关键语义：

- `1h` regime 只在该 K 线**收盘后**可见，禁止用 open-time 泄漏未来信息。
- 默认费率用 `market/taker` 万五。回测没有建模挂单队列、盘口价差、部分成交，不要用 `limit/maker` 万二当默认。
- `initial_stop` 只是复核用的规划值，**不是交易所侧保护单**。

## 当前状态

- 可信数据层已完工，schema-v4 分段存储已合并（#72 / issue #68）。
- 30-B `OrderIntentFactory`(#59) 和 30-C 执行存储(#65) 已合并、有测试，但**无生产消费者**，处于休眠状态。原因：需要 `config/strategy-releases/` 里有可解析的 release，而发布 release 需要第二个 GitHub 身份（GitHub 禁止 PR 作者 approve 自己的 PR）。见已关闭的 #70。
- 出场规则已提取为回测与实时共享的纯函数（#75），但**实时侧仍无消费者** —— `demo_trading.py` 里 `initial_stop` 只是报告字段，没有持仓监控或平仓动作。回测 47 笔全部由 stop 出场，收益 100% 由出场规则决定，所以这是上线前最关键的缺口（issue #85）。
- 通知能力为零。
- **回测报告不自陈质量**（issue #83）。实测 146 天 MU 样本：策略 +445.5% vs 标的裸涨 +121.2%，
  但 12 笔盈利**全部**是 stage 4，top5 赢家贡献净利 111% —— 去掉即为亏损。报告不给基准也不给集中度。
  另 `Trade.return_pct` 除的是 `starting_equity`（`backtest.py:398`）而非该笔占用资金，同一个 -2% 止损
  在报告里从 -2.10% 显示到 -11.70%，看着像风控恶化，实际只是复利。
- **committed 数据无法消费**（issue #84，待定方向）。`.gitignore:12-20` 是**刻意设计的白名单**（只放行
  `manifest.json` + `okx/*/*.csv`，挡掉其余运行时状态），不是历史遗留 —— 别"修"它。但
  `max_staleness_bars=3` 意味着 5m 数据 15 分钟即过期，而 committed 数据按定义是几周前的，
  `research_strict` / `trading_strict` 都 `require_fresh=True`，所以消费必然 fail-closed。
  唯一放松 freshness 的 `observe_only_policy` 同时开 `allow_invalid=True` +
  `require_manifest_success=False`，**不可用它跑回测**（等于整体关掉完整性校验）。

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

## 诊断纪律（都是真栽过的）

- **每轮开工先 `git fetch` 并核对 `HEAD` 与 `origin` 的差距。** 曾经在落后 7 个提交的工作树上 grep，
  据此断定 codex 没做某项工作，实际它早已完成（#77）—— 拿旧工作树去质疑一个正确的远端结果。
- **grep 出 0 结果时，先自证输入非空**（`wc -c` 或同批次另一条有输出的 grep），再采信这个 0。
  上面那次误判的第二层原因是：`git show origin/... | grep -c` 返回的 0 是**真实结果**，
  却因为刚踩过 `gh pr diff` 返空的坑，反过来怀疑它是空输入 —— 选错了要相信的那一边。
  两条合起来是同一件事：**先确认自己看的是不是最新、是不是真的东西，再下判断。**
- codex 报告"已完成、零 diff、不新开 PR"时，先核实再反驳。它做对过，而我判断错过。
