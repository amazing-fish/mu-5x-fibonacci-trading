# 风险与退出去重：#122 之后的行为等价

Refs #123 / #83 / #99，不关闭父 Issue。本轮仅消除 `backtest.py` 的同义风险和退出记录重复，不调整策略、费用或遍历协议。

## 修改前表征

开工核实远端 main = `1fc366cacbe4c0d635e2cc756289afce40517ca3`，#116 / #118 / #120 / #122 均已合入。开放 PR 只有文档 PR #86，无风险/退出重叠工作。主仓本地 main = `bf564af`，保留原有数据和产物改动；实现及基线运行均在独立 worktree。

| 路径 | 风险判断上下文 | 风险后控制流与采样 |
|---|---|---|
| 二次回踩首仓 | 实际 limit fill 后；非时段风险优先，否则 `initial_stop` | fill 点 → 可选 exit 点 → 本根 close；无论存活与否都 `continue`，首仓根不执行加仓、收紧止损、计划加仓 |
| next-bar 首仓（direct / break-high） | 信号根控制流提前执行下一根 fill；同样先非时段风险，再 `initial_stop` | 信号根 close → 下一根 fill → 可选 exit；下一轮进入成交根，已有初始风险检查被跳过，存活仓位仍进行管理并记录 close |
| 已有仓位 | 初始成交根之外；非时段风险优先，否则 `stop`；风险早于有效加仓 | 风险退出后 exit 点 → 空仓 close → `continue`；存活才执行加仓、收紧止损、计划加仓 |

非时段首仓通常被原日历入场门禁挡住；保留该防御分支，不为了测试绕过日历制造新行为。跳空止损仍使用第一个可得 open。末根仍先处理风险和有效加仓、记录 close，存活才唯一结算 `end_of_data`；不执行未来成交。

生产代码修改前，24 组真实 `run_backtest` 完整输出已保存并冻结规范 SHA-256：3 种入场 × 2 种费率 × 4 个场景（初始止损、已有止损、首仓管理差异、非时段跳空）。68 项回测测试通过，含原 #118 / #120 / #122 的全部 63 项。表征不 mock 风险、成交、费用、结算或仓位管理，只固定指标与信号位置。

手算场景：100 买入 100 单位、每侧 0.0005，初始止损根净值顺序为 `9995 → 9790.1 → 9790.1`（fill / exit / close）；已有仓位止损根为 `9790.1 → 9790.1`。相同标签和值均保留。首仓根 high=103、low=99、close=102，次根 open=103 时，second_pullback 保持 1 个 fill；next-bar 两种方式可以在次根执行第 2 个 fill，原因仍为 `end_of_data`，不回看低点触发新止损。

## 测试审计边界

`tests/replay_net_equity.py` 复用严格历史读取、完整配置、规范导出和递归比较。当前 `pair` 只运行两个独立进程，均调用未经包装的真实 `run_backtest`，精确比较整个 `BacktestResult`（全部 Trade/Fill 字段、所有净值点的数值/数量/顺序、期末权益）及派生汇总。

账式审计只用输入 K 线和结果中的真实 Fill / Trade，按已有公开采样契约重建账式：已实现毛损益 + 浮盈亏 − 已发生费用。它不决定入场、加仓、风险或退出，不观察调用栈、局部变量或函数名称，不向生产代码加入 observer。独立手算与费用守恒保留；缺失、额外、错序采样和重复扣费的破坏性反例必须失败。

旧 #121 三路诊断不能用于当前净权益；其[历史脚本](https://github.com/amazing-fish/mu-5x-fibonacci-trading/blob/1fc366cacbe4c0d635e2cc756289afce40517ca3/tests/replay_net_equity.py)与[历史验证结论](net-equity-measurement.md)保留。新的 `pair` 不执行 fee-only，也不硬编码旧回撤为当前目标。

## 固定数据与修改前输出

复用 [Actions run 34148910346](https://github.com/amazing-fish/mu-5x-fibonacci-trading/actions/runs/34148910346) 的 `pr118-replay-evidence-21545f86/market.zip`，已验证 ZIP SHA-256 = `343caaaf9e82f3d3f5406be7e5d06842d90aa313731942b5be05a31810b4bba5`，解压副本的 27 个文件与 archive 逐一精确相同。根目录包含 `generations/segments`，没有刷新行情。

generation `f694d1d9a3fd46daa8d0dc12cb12aa0b`；UTC `[2026-03-12 17:00, 2026-09-07 17:00)`，179 天；baseline / 5x / 20-20-20-40% / 每侧 0.0005 / 初始 10000；同一完整配置、日历和解释器。修改前独立进程重放已重现 57 Trade、106 买入 Fill、17347 净权益点，期末权益 29399.55932527934、收益 193.99559325279338%、最大回撤 -24.188368164054207%、费用 2507.3578334614494。

## 实际改动与等价结果

新增普通私有函数 `_risk_exit`，只负责已有风险优先级、阈值选择、触价与跳空价格，返回价格和原因。局部私有函数 `_exit_on_risk` 用现有运行状态完成唯一的风险结算责任：调用原 `_close_position` → 追加交易 → 清空仓位 → 记录退出净值，返回是否退出。调用方明确传入 `initial_stop` 或 `stop`，仍自行保留 close / continue 边界；`end_of_data` 独立。没有新的状态类型、全局 observer、生产模块或依赖。

| 静态定位指标 | 修改前 | 修改后 |
|---|---:|---:|
| 生产 Python 文件 | 104 | 104 |
| `backtest.py` 物理行 | 496 | 451 |
| `run_backtest` 行数（含局部函数） | 233 | 168 |
| `run_backtest` AST if / continue（含局部函数） | 22 / 13 | 18 / 12 |
| `record_close` 调用点 | 15 | 14 |
| `_close_position` 调用点 | 7 | 2 |
| `_has_non_session_liquidation_risk` 调用点 | 3 | 1 |
| `_sell_stop_fill_price` 调用点 | 6 | 1 |

生产 diff **+37 / -82，净减 45 行**。新增两个职责明确的普通私有函数；调用方有 3 个 `_exit_on_risk` 入口。只合并了已有仓位两种风险退出后的同义尾部，没有把每根收尾统一到 finally，没有改变 next-bar 提前执行。AST 核对已有顶层符号仅 `run_backtest` 改变；`_make_fill` / `_close_position` / `_marked_equity`、其余原函数与类完全未变，公共入口参数未变。

双进程固定行情 `pair` 的完整结果 **精确相等**，未动用浮点容差接受行为差异：

| 输出 | #122 基线 | 重构后 |
|---|---:|---:|
| Trade / 买入 Fill / 净权益点 | 57 / 106 / 17347 | 精确相同 |
| 期末权益 | 29399.55932527934 | 精确相同 |
| 账户收益 % | 193.99559325279338 | 精确相同 |
| 最大回撤 % | -24.188368164054207 | 精确相同 |
| 总手续费 | 2507.3578334614494 | 精确相同 |

两边全部 Trade/Fill 规范 JSON SHA-256：`5ff2b75c6fcec5b8c30335380dcddc1e773c282fc9a7c083fa2bd6bac78336f9`；净值序列 SHA-256：`1e853d61937a960f4f4f86ea23ba76e32d3d92b0c7a1d0995ea2694e2e561bbe`。规范参数 `sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False`。所有离散字段和重复时间标签均完整保留。

两边各 17347 个点独立账式审计通过，最大残差均 `2.1827872842550278e-11`；该账式浮点对账沿用 abs=1e-9 / rel=1e-12，**不是前后比较容差**。手续费守恒、Trade 净损益也独立对账。

行情的全部 27 文件在 archive 校验、两边运行前后保持一致；完整 provenance 除 `code_sha256` 外完全一致。配置 SHA-256 `196bf7e4e3477eb6fe30d20f5982ffbac2e37c67533467e840fe3dc7256d4866`；日历 SHA-256 `0fc82fba68f3dea507d8d20c83fb3f0a72fe700182b3d66b9004eab1a3d1b2c5`。

源码 `code_sha256` 从 `23ea9a472f46729fc0fdee8f2fdfc4dd587480407a3270f7f92521e720addeb9` 变为 `76130e205d138bf0867d50394f14a07ebca561ef4a179941e136975a06112b44` 是预期的 provenance 变化，不要求报告文件字节一致。

## 验证与复现

本次实际环境：Windows 11 build 26100 / CPython 3.12.4 / MSC v.1929 AMD64，解释器 `D:\Develop\Tool\Miniconda\python.exe`。默认 PATH 的 Python 是 3.10.14，因此显式选择上述 3.12 解释器。

- 修改前与修改后：68 项回测测试均通过（exit 0），原 63 项未删除、修改或弱化；新增 5 项测试包含 24 组完整输出表征、初始/已有原因差异、next-bar 已检查初始风险、second_pullback 首仓 continue、同标签顺序及审计破坏反例。
- 修改后全量 `python -B -m unittest discover -s tests -v`：**1063 项，96.995 秒，OK (skipped=7)，exit 0**。额外 `-v` 仅记录逐项证据；7 项跳过均为既有 artifact-publication 的 Windows symlink 权限场景，没有回测测试跳过。
- 固定 generation 双进程配对及独立账式核算：exit 0；`git diff --check`：exit 0。
- 原回归覆盖初始/已有仓位、非时段优先级、跳空、同根入场止损、三种入场、挂单有效/过期、末根有效加仓/禁止未来成交、零/非零费用、四阶段、短/空输入，以及 #116 日历/时区边界。
- 远端 CI 与机器人审查以 PR 最终状态为准，独立报告；不把本地通过等同于远端通过。

以下命令在本 PR worktree 执行。数据目录必须是已核验的 ZIP 解压根目录，不能刷新替代。

```powershell
git worktree add --detach reports/live/before-source 1fc366cacbe4c0d635e2cc756289afce40517ca3
$dataDir = '<absolute extraction root containing generations and segments>'
& D:\Develop\Tool\Miniconda\python.exe -B tests/replay_net_equity.py pair --before-source reports/live/before-source --after-source . --data-dir $dataDir --output-dir reports/live/risk-exit-pair
& D:\Develop\Tool\Miniconda\python.exe -B -m unittest discover -s tests
```

配对导出 `before.json` / `after.json` / `comparison.json` 至 ignored `reports/live/risk-exit-pair/`，包括完整 Trade/Fill、净权益点、逐点账式、配置/数据/源码身份。`reports/live/archive-verification.json`、`source-metrics.json` 和 `unittest-windows.log` 保存本次定位与测试证据；产物不写入行情目录。

本次不承诺其他真实 generation 的等价测量或策略盈利有效性。未触碰数据 writer/gate、部署、服务、邮件、Demo/Production；未合并、关闭父 Issue 或继续下一轮重构。
