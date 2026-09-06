# 产品路线：先验证信号，再完成模拟盘闭环

产品目标来自 [#73](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/73)：S1 用可复核信号和网易邮件辅助人工交易；S2 在 S1 验收后建立稳定的受控 OKX Demo。Production 留在 [#7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7)，当前不启用。

模块当前实现和合并证据集中在[架构与建设现状](architecture.md)。本文只保留交付依赖和验收，不用 Issue 是否开放、代码量或旧回测数字推算完成率。Issue 正文可能保留当时的状态快照，当前完成度需对照实际合并提交。

## S1：信号验证与人工交易复盘

| 交付线 | Issue 归属 | 后续验收与依赖 |
|---|---|---|
| 研究结果可解释、可重复 | [#83](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/83)、[#88](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/88)、[#96](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/96) | 保留已完成的固定 generation、robustness 和杠杆披露；补齐全 registry 同快照比较、剩余收益标签和解析错误契约。 |
| 持续运行与真实邮件验收 | [#98](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/98)、[#99](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/99)、[#107](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/107) | 信号服务、邮件和上市边界代码已合并。分别核对受控 SMTP 接受/收件箱、常驻运行和故障恢复证据；代码交付不替代运维验收。 |
| 完整持仓输入与管理提醒 | [#85](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/85)、[#90](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/90) | baseline 已接人工完整配置、锚点、初始止损、实际杠杆、逐笔买入到阶段的映射，并按需复用共享规则。剩余卖出后映射、延迟 transition、可信事件持久化/失效与邮件接入；OKX 聚合仓位的真实杠杆来源另归 #90。 |
| 样本外与前瞻评估 | [#99](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/99) | 冻结规则、代码、配置、成本和实验窗口；持续保存全部信号、失效、送达和实际成交，分开评价策略、行情、提醒与执行偏差。 |

可以并行推进研究质量与前瞻准备。入场邮件不必等待持仓模块完成；持仓类邮件必须等待完整可信状态。已有复盘页、人工处理标记和成交台账提供记录工具，尚不能替代冻结实验或前瞻结论。

目前应核实运行与邮件的剩余验收，开始按协议保留前瞻证据。#85 已从记录推进至受支持 baseline 的按需规则复核；下一步让管理判断成为可追溯、可失效、可去重的事件，再接持仓邮件，同时逐步覆盖卖出后的状态。已有构件优先复用；不能用默认 stage、合成成交、宽松 reader 或规划止损来填补未知事实。

### S1 完成门槛

- 信号绑定可追溯来源、配置、时点、有效期和失效条件，未知与估算可见。
- 数据阻断、扫描失败、无信号、送达失败与停机可区分，恢复没有静默遗漏。
- 入场及持仓管理均有可追溯记录，策略建议和人工动作分开。
- 至少 20 个交易日连续运行证据用于工程验收；6–12 周前瞻观察只是初步窗口，样本不足继续积累。
- 有明确的继续观察、淘汰或进入 Demo 候选结论，依据包含成本、回撤、尾部集中度和样本外结果，不能用一次高回测收益替代。

准备和操作要求见[前瞻证据协议](email-alerts.md#前瞻准备99)、[信号服务](signal-service.md)与[复盘手册](signal-review.md)。本轮文档核对未验证实际进程、SMTP 或连续运行数据，不宣布这些门槛已完成或尚未开始。

## S2：受控 Demo 自动运行

[#100](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/100) 统一拥有订单、成交、持仓、退出和恢复闭环，在 S1 验收和策略选择结论后分片实施：

1. 按既有审批模式生成真实、可 exact-ID 解析的 strategy release；#70 的历史关闭状态不表示仓库已有 release。
2. 接通 scan → intent → 持久确认/授权 → reserve → Demo adapter，复用执行存储并封闭直接下单旁路。
3. 覆盖 open/partial/fill/cancel/expire、timeout/unknown、成交去重、仓位与余额对账、重启恢复。
4. 复用完整持仓状态与共享规则，建立受控退出和交易所保护流程。
5. 完成敞口、损失、频率与异常停机，以及恢复核对和故障演练。

现有 Demo 买入、部分失效订单撤销和 exposure 限制只是前置能力。真实 Demo 验收仍要求现有显式确认；Production mutation 保持不可达。

## 与执行 Stage 0–4 的关系

S1/S2 描述用户得到的产品能力；Stage 0–4 描述执行权限与成熟度，二者不能相互代替。

- S1 主要使用 Stage 0 的观察与报告，加上人工记录和独立通知；通过 S1 不自动获得订单授权。
- Stage 1 intent 和执行存储有部分构件，现有 Stage 2 guarded Demo 有可调用路径，但尚未完成持久授权和完整恢复的验收。
- Stage 3 设计冻结不等于 conformance 已通过；Stage 4 Production 仍后置。

完整身份隔离、预留、故障状态与验收契约保留在[分阶段执行设计](execution-roadmap.md)，本轮文档整理不改变门禁。

## 可分离后续项

[#101](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/101) 的候选族解耦只在真实扩展需求下推进，不建设通用策略平台。候选池、跨标的证据和数据驻留管理也不能仅为目录完整而提前扩张；按实际研究、运行需求独立评估。
