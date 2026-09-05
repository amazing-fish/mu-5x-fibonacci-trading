# 信号提醒与模拟盘路线

用户目标先后为：第一阶段让信号有依据、可复核，通过网易邮件提醒人工交易；第二阶段才接通受控 OKX Demo 自动运行。动态交付状态以 [路线 Issue #73](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/73) 为准。

这里的两个产品阶段不替代 [architecture.md](architecture.md) 中 Stage 0–4 的执行门禁。Production 仍归 [#7](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/7)。

## 第一阶段：信号验证与邮件提醒

| 交付 | Issue | 依赖与验收 |
|---|---|---|
| 可解释的研究结果 | [#95](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/95)、[#88](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/88)、[#83](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/83) | 区分账户收益、单笔保证金收益和配置杠杆；保留集中度、stage 分布和比较限制 |
| 固定历史快照回放 | [#84](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/84) | 复用已有历史 reader 的能力，普通研究入口显式绑定 generation；实时 freshness 不变 |
| 一致的扫描结果 | [#62](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/62) | 观测、看板和提醒消费同一个结果；每标的每轮只扫描一次 |
| 持续运行与健康 | [#97](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/97) | 独立刷新与 cache-only 扫描；区分无信号、数据阻断、扫描失败、持久化失败和进程停止 |
| 网易邮件提醒 | [#98](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/98) | 持久事件身份、去重、送达结果和故障恢复；接收地址及 SMTP 授权码仅在本地配置 |
| 持仓管理提醒 | [#85](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/85)、[#90](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/90) | 可信成交与持仓状态支持共享加仓/止损/退出规则；聚合持仓估算保持 unknown/degraded |
| 前瞻与样本外复盘 | [#99](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/99) | 冻结规则与配置，记录所有信号、失效、提醒和人工成交；分开评价策略、数据和送达 |

研究与扫描开发可以独立推进。入场邮件依赖权威扫描结果，健康邮件依赖健康结果，持仓类邮件必须等待可信持仓状态。当前已有 shadow exit 观察和看板；这些不等同于完整持仓记录或实际退出执行。尚未交付的接口由各 Issue 的验收约束定义，不把路线文档当成实现已完成。

邮件的 [前瞻证据准备协议](email-alerts.md#前瞻准备99) 在受控启用前执行：先冻结部署版本、配置和实验窗口，开始运行即保存全部信号与送达记录。邮件代码验收、真实受控送达、连续运行和策略效果分别记录；前瞻准备无需等待持仓模块完成，完整持仓复盘仍依赖 #85。

工程验收要求至少 20 个交易日连续运行证据：数据过期应停止产生交易信号并可见地报告原因，正常无信号不得误报故障，邮件失败不能被静默丢弃。6–12 周前瞻观察是初步评估窗口；样本不足需要延长，不承诺届时证明稳定盈利。历史收益和 `candidate` 状态不能代替前瞻证据或交易授权。

## 第二阶段：模拟盘自动运行

[#100](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/100) 按以下顺序跟踪可分离的交付：

1. 生成真实、可 exact-ID 解析的 strategy release，解决既有审批身份前置。
2. 接通扫描、intent、持久确认、reservation 与 Demo adapter，复用已有执行存储。
3. 处理订单与部分成交、撤单、超时结果不明、去重、仓位核对和重启恢复。
4. 以完整持仓状态复用加仓/出场规则，明确规划止损与交易所保护状态。
5. 完成风险停机、恢复核对、持续运行和故障演练。

第一阶段验收与策略选择结论是启动条件。已有 intent/store 不代表执行链已接通；缺失 release 不能用 mock 或宽松 resolver 代替。测试使用 fakes，真实 Demo 验收遵守显式确认边界，Production mutation 保持不可达。

## 可分离债务

研究结论解析错误边界由 [#96](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/96) 处理；候选族名单解耦移至低优先级 [#101](https://github.com/amazing-fish/mu-5x-fibonacci-trading/issues/101)，不阻塞当前报告修正或邮件提醒。评价进展以行为、验证和持续运行证据为准，不以 Issue 数或代码量推算完成率。
