# Phase 3 多标的横截面发现与 `#129` Edge 门禁方案

> **规划基线**：`main @ 75aeaff5b8920f2750eec7fb0a3fa7be8b5ff791`（2026-11-28，Phase 1 `#133` / PR `#135` 与 Phase 2 `#136` / PR `#137` 已合入主干）。
> **硬性边界**：纯 Python 3.12 标准库、只读可信缓存消费（零 `.bar_cache` 写盘、零联网）、严禁实盘或 Demo 自动晋升下单（`dry_run_only=true`，`live_orders_allowed=false`）。

---

## 1. 背景与现状缺口（Phase 1/2 对账结论）

Phase 1 与 Phase 2 已将单标的回测引擎、合约/成本/资金费率建模与 6-Gate 单标的策略发现工作台（`mu_strategy/lab/`）落盘主干，并清理了旧 `mu_strategy/experiments/`。但在 `main @ 75aeaff5` 复跑 `MU-USDT-SWAP 1h`（40 trials，`none` 与 `dd20_vol25` 叠加）显示：

- 40 个候选全部为 `NO_GO`（`GO=0`, `CONDITIONAL=0`, `NO_GO=40`）；即使在无外部 `funding_history.json` 时 G2 因 `funding_verified=false` 严格 fail-closed，单标的亦无法独立证明“可迁移择时 edge”。
- 对照 [#129](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/129)、[#101](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/101) 与 [#73](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/73)，仍存在 5 项核心缺口：
  1. **多标的横截面缺失**：当前 `discover_strategies(series, ...)` 仅支持单 `BarSeries`，尚无基于 `TrustedDataStore` manifest 全量可用永续标的一键横截面面板与聚类统计。
  2. **零分布检验层级不足**：Phase 2 `compute_null_distribution_pvalue` 是对 OOS 权益曲线收益做分块符号翻转，尚未对真实 OHLCV 合成路径重跑**完整回测引擎**（`run_backtest` 或 `Simulator.run`）。
  3. **缺少波动率缩放持有基准**：当前仅有 `Spot 1x` 与 `Perp 1x` 买入持有，缺少按策略实现波动率缩放的持有对照基准。
  4. **缺少 `#129` 独立门禁命令与文档**：仓库尚无 `python -m mu_strategy.commands.edge_gate` 与 `docs/edge-gate.md`。
  5. **旧执行侧配置耦合待按需保留**：`#101` 中研究侧 `strategy_ladder` 旧读者已随 PR `#137` 删除，但旧执行侧 `strategies/registry.py`（`side == "buy"`）与 `StrategyConfigPayloadV1`/`V2` 仍保留原契约，仅在策略通过 `#129` + S1 验证后才决定最小拆分。

---

## 2. 只读多标的数据面板契约（`mu_strategy/lab/panel.py`）

### 2.1 输入来源与 fail-closed 门禁

- **唯一只读入口**：通过 `TrustedDataStore(data_dir)` 读取 `--generation-id`（精确历史快照）或 `current.json`（当前可信发布），**严禁**联网刷新、写入 `data/live/` 或落盘 `.bar_cache`。
- **全量可用永续标的发现**：默认从 pinned manifest 提取全部可用、通过完整性校验的股票永续 / 永续标的（不以手挑 `MU-USDT-SWAP` 单标的或手挑子集作为默认），支持 `--symbols` 显式收窄仅用于调试并在报告中标明 `subset_override=true`。
- **Fail-closed 条件**（任意一项触发即非零退出，不做回退）：
  - manifest 缺失、损坏、`attempt_status == "failed"` 或 `snapshot_usability == "invalid"`（当启用当前 freshness 策略时，过期数据亦阻断）。
  - 可用标的数 `< min_symbols`：横截面统计强制要求 `min_symbols >= 2`（**默认 `min_symbols = 4`**，显式 `--min-symbols` 最低仅可设为 `2`，拒绝 `N = 1` 单标的面板用于横截面门禁或 `PASS` 判定；单标的 `MU-USDT-SWAP` 仍由 Phase 2 `mu_strategy.lab.cli discover` 承担）。`edge_gate` 默认要求覆盖 manifest 全量可用永续标的，且不少于 `min_symbols`。
  - 任一纳入面板的标的缺少所请求周期的完整已收盘 K 线、存在校验失败或日期对齐异常。
- **资金费率与成本契约**：
  - 优先读取显式传入的 `--funding-dir` 对应标的 `FundingSeries` JSON 并记录逐标的 `funding_sha256`。
  - `#129` Edge 门禁默认费率为 `market/taker` 往返 10bp（单侧 5bp），资金费按**实际多头名义敞口 × 持仓时长**扣除；无外部实测资金费率文件时按 `#129` 显式默认 `8%/年`（可参数覆盖并在报告写明所用 APR），**严禁静默按 0 计**。
  - `lab discover-panel` 继续保留显式实测资金费率校验与 `0 / +15% / +30% APR` 压力矩阵；未提供实测资金费率时 G2 继续 fail-closed。

### 2.2 窗口对齐与覆盖披露

- 按公共日历区间或逐标的有效已收盘窗口（显式记录 `first_ts_ms`、`last_ts_ms`、`effective_days`、`listing_start` 与 `coverage_state`）运行，不静默丢弃新上市或短样本标的；所有入选/被剔除标的及原因写入面板 provenance 清单。

---

## 3. 引擎级合成路径零分布（`mu_strategy/lab/null_engine.py`）

### 3.1 允许的合成路径生成器

对每个标的分别生成 `n_null >= 200` 条保留经验收益分布特征的合成 OHLCV 路径（固定 `seed` 完全可复现）：

1. **`block_bootstrap`（按交易日分块自助采样）**：按 UTC/标的交易日切分日内 K 线块（保留日内开盘/收盘结构、跳空与日内波动聚类），有放回抽样拼接为等长价格路径，保留日内相对 OHLC 比例与成交量结构。
2. **`dedrifted_shuffle`（去漂移残差打乱）**：去除标的样本内平均对数漂移后，按交易日或连续 K 线残差块置乱再累乘还原 OHLCV，检验策略是否仅吃到了标的本身的单边漂移。

### 3.2 严格同引擎重跑与反偏倚红线

- **同引擎重跑**：
  - 对 `#129`（已注册策略组）：在每条合成路径上直接调用**同一个 `run_backtest`**。
  - 对 `mu_strategy/lab/`（`Strategy` 协议族）：在每条合成路径上调用**同一个 `Simulator.run(...)`** 与相同 `CostModel` / `RiskOverlay`。
- **禁止项**：严禁实现“固定持仓期随机放置入场点”的暴露匹配零检验（已在 `#129` 背景审计中证实对带移动止损策略存在 `82–85` 分位系统性正偏倚）。
- **分位定义**：对每个标的 $i$，计算真实回测核心指标（净收益 / Sharpe）在 $M \ge 200$ 条零分布路径中的经验分位 $p_i \in [0, 100]$（采用 Phipson-Smyth 连续或 rank 修正并固定在代码与文档中）。

---

## 4. 横截面统计推断与双基准（`mu_strategy/lab/cross_section.py`）

### 4.1 单标的基准

对每个标的同评估窗口同步计算：
1. **1x 买入持有（`bh_1x`）**：同费率/同窗口 1x 持有收益、最大回撤、Sharpe。
2. **波动率缩放持有（`bh_vol_scaled`）**：按策略实现年化波动率与 1x 持有实现年化波动率之比 $\sigma_{\text{strat}} / \sigma_{\text{bh}}$ 缩放仓位后的持有基准（含对应成本），用于剥离单纯降杠杆/降敞口带来的回撤错觉。

### 4.2 横截面汇总与相关性/时间聚类校正

设共有 $N \ge \text{min\_symbols}$ 个标的参与评估（其中硬性下限 $\text{min\_symbols} \ge 2$，默认 $4$；因 $N=1$ 时零分布均匀分位的标准差约 $100/\sqrt{12}\approx 28.87$，即便实测排第一也有 $z_{\text{corr}} \le 50/28.87 \approx 1.73 < 2.0$，故在面板准入期即拒绝单标的输入。面板零分布采用**跨标的日期块同步抽样**：第 $m=1\dots M$ 次抽样对所有标的共用同一交易日块索引序列，保留标的间的同期横截面相关性）：
- **相关性校正分位标准误与 $z_{\text{corr}}$**：先在每条同步零分布面板抽样 $m$ 上计算横截面平均经验分位 $\bar{p}^{(m)} = \frac{1}{N}\sum_{i=1}^N p_i^{(m)}$（其中 $p_i^{(m)}$ 为标的 $i$ 第 $m$ 条合成路径相对于该标的其余 $M-1$ 条合成路径的经验分位），再计算同步零抽样的经验标准误 $\text{SE}_{\text{corr}} = \operatorname{std}(\{\bar{p}^{(m)}\}_{m=1}^M)$，并与朴素独立标准误 $\text{SE}_{\text{naive}} = s_p / \sqrt{N}$ 取上限以保持 fail-closed：
  $$z_{\text{corr}} = \frac{\bar{p} - 50}{\max(\text{SE}_{\text{corr}},\, \text{SE}_{\text{naive}})}$$
  若分母 $\max(\text{SE}_{\text{corr}},\, \text{SE}_{\text{naive}}) \le 0$ 或非有限（如零成交、平盘价格序列导致全部合成路径打平），严禁执行除法，`z_corr` 置为 `null` 并直接返回带明确原因（`zero_or_non_finite_se`）的 `FAIL` 报告。`SE_naive` 与 `z_naive` 仅作诊断列示，不单独用于放行判定。
- **时间聚类推断（Time-Clustered Inference）**：为消除多标的同期市场 Beta 共振导致的 $t$ 值约 $2\times$ 虚高，按日历周期 $t$（如每个交易日）构建横截面超额收益序列 $d_t = \frac{1}{N_t}\sum_{i \in S_t} (r_{i,t}^{\text{strat}} - r_{i,t}^{\text{bh}})$，采用 Newey-West / 块聚类标准误计算聚类 $t_{\text{cluster}}$（若聚类标准误 $\le 0$ 或非有限同样不作除法并返回 `FAIL`），并与同期相关性诊断一并纳入判定与报告。
- **尾部与胜率计数**：
  - 分位 `> 90` 的标的个数 vs 零假设期望值 $N \times 0.1$。
  - 扣费与扣资金费后跑赢同窗 1x 持有的标的数量及比例 $k / N$。

### 4.3 硬性 Fail-Closed 判定规则（`#129`）

默认判定为 `FAIL`，仅当下列全部条件同时满足时才判定为 `PASS`（阈值可通过参数配置但默认值写死在代码与 `docs/edge-gate.md`）：
1. 纳入评估的有效标的数满足横截面最低门槛：`N >= min_symbols`（`min_symbols` 硬下限 `2`，默认 `4`）。
2. 跨标的零分布分位均值经**相关性校正**后单侧检验显著高于 50：`z_corr >= 2.0`（单独 `z_naive >= 2.0` **不得**通过门禁）。
3. 时间聚类日度横截面超额收益检验显著大于 0：`t_cluster >= 2.0`。
4. 扣费及资金费后跑赢同窗 1x 持有的标的比例：`> 50%`。
5. 所有纳入标的均具备完整数据与显式成本/资金费核算，无缺口或异常跳过。

否则一律返回 `FAIL`，并逐项列出未通过条件与各标的明细。

---

## 5. CLI、工作台与文档交付面

1. **`python -m mu_strategy.commands.edge_gate`（独立 `#129` 门禁命令）**：
   - 接收已注册策略组名（`--strategy`）、`--data-dir` / `--generation-id`、`--null-method {block_bootstrap,dedrifted_shuffle}`、`--n-null 200`、`--seed`、`--funding-apr 0.08`、`--output-dir`（默认 ignored `reports/live/edge_gate/<run>/`）。
   - 输出包含代码 commit、generation id、seed、参数与全部标的明细的 `report.json` + `report.md`，**终端仅打印输出路径**。
2. **`python -B -m mu_strategy.lab.cli discover-panel`（Phase 3 多标的发现子命令）**：
   - 复用 `mu_strategy/lab/discovery.py` + `gates.py` 对多标的面版运行 Walk-Forward 6-Gate + 横截面聚合，扩展 `workbench_html.py` 渲染：
     - 跨标的 `Family × Symbol` OOS Sharpe / 净收益热力图（纯内联 SVG）。
     - 零分布分位直方图（对比 $N \times 0.1$ 期望线）与 `bh_vol_scaled` 散点对比。
     - 零外部 CDN，保持 `dry_run_only=true` 与 `live_orders_allowed=false`。
3. **方法论文档 `docs/edge-gate.md`**：
   - 阐明每项检验的零假设、为何禁用“固定持仓期随机放置”、为何必须对同期横截面做聚类校正、以及永续多头资金费按真实名义暴露 × 持仓时长扣除的口径。

---

## 6. 验证标准与分批 PR 拆解

### 6.1 必需验证矩阵

- **无偏性回归测试（Unbiasedness）**：在零漂移随机游走合成数据上，legacy `baseline` 与带移动止损的策略的门禁结果**必须 `FAIL`**，且跨标的均值分位落在 `50 ± 3*SE` 区间内。
- **检验功效测试（Power）**：在植入可控延续/动量结构的多标的合成面板上，利用该结构的测试策略**必须 `PASS`**。
- **Fail-Closed 与零副作用**：
  - 数据缺失、过期或损坏 → 非零退出。
  - 断言 `data/live/` 零新增/修改文件（零 `.bar_cache`）。
  - Mock 网络层断言零外部请求、零私钥读取。
  - 相同 `seed` 两次运行 JSON/Markdown 输出除时间戳字段外逐字节一致。
- **架构守护**：扩展 `tests/test_architecture_dependencies.py` 验证 `core`、`market_data`、`strategies`、`engine` 不反向依赖 `lab` 或 `commands.edge_gate`。

### 6.2 建议分批 PR 实施顺序

| 批次 | 范围 | 主要产出 | 关联 Issue |
|---|---|---|---|
| **PR-3A**（`#129` 核心闭环） | 引擎级合成路径零分布、横截面推理与波动缩放持有基准、`edge_gate` CLI、`docs/edge-gate.md` 及无偏性/功效/确定性回归测试 | `mu_strategy/lab/{null_engine,cross_section}.py`、`mu_strategy/commands/edge_gate.py`、`docs/edge-gate.md`、`tests/test_edge_gate.py` | `Closes #129`（仅在验收全过且授权后）、`Refs #73` |
| **PR-3B**（多标的实验室面板） | 只读多标的面板加载器、`lab.cli discover-panel`、离线 HTML 横截面热力图与分位可视化 | `mu_strategy/lab/panel.py`、扩展 `mu_strategy/lab/{discovery,cli,workbench_html}.py`、`tests/test_lab_panel.py` | `Refs #73 · Refs #101 · Refs #129` |

### 6.3 明确非目标（Non-Goals）

- 不修改既有 `run_backtest`、`baseline` 规则、默认费率或 `TrustedDataStore` 写入布局。
- 不因多标的发现结果自动触发策略 release 晋升、不触碰 `#100` OKX Demo 下单与 `#7` Production。
- 不提前拆分 `#101` 旧执行侧配置契约，直到存在通过 `#129` 与 S1 验证的真实策略推广需求。
