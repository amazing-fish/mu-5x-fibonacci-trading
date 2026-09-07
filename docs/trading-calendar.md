# 标的交易日历

日历限制策略允许的评估日期，`StrategyConfig.trading_windows_et` 限制美东策略时间；两者取交集。它是策略参考市场的配置，不能据此断言 OKX 永续本身休市。行情刷新不受该日历过滤，止损与已有持仓风险检查也不会因正常休市而停止。

日历超出覆盖范围或时区不可用时，入场、加仓及依赖这些判断的完整回测继续失败。已有持仓的只读退出检查仍评估确认止损和收紧值：已触及止损就保留明确结果，同时记录 `calendar_error`；未触及止损时 `exit_triggered` 为未知（`null`），不能据此声称所有退出条件均未触发。人工持仓复核只有已确认退出证据时显示部分结果，并保留日历未知提示；否则保持阻断，不产生加仓建议。日历错误不会被解释为已知休市或切换到另一个日历。

## 配置与接入

在 [config/instrument_calendars.json](../config/instrument_calendars.json) 按 canonical OKX 标的指定日历。当前 MU、META、SPCX 显式采用 `us_equities_2025_2028_v1`；其他标的采用文件明确声明的 `default_calendar: weekday_windows_v1`，沿用原策略工作日限制。新增标的应先确定适用日历；不要按 `USDT` 后缀推定全天交易。

| 日历 ID | 日期约束 | 时段约束 |
|---|---|---|
| `us_equities_2025_2028_v1` | 2025–2028 官方现金股票市场休市/提前收盘表 | 正常 09:30–16:00 ET，提前收盘 13:00 ET，与策略窗口取交集 |
| `continuous_v1` | 无周末或美股节假日限制，须显式配置 | 仍受 `trading_windows_et` 限制 |
| `weekday_windows_v1` | 美东周一至周五；旧版本语义 | 沿用策略窗口，不额外识别节假日 |

日历选择与 [okx_stock_tokens.json](../config/okx_stock_tokens.json) 的刷新分类各有职责；后者保留原字符串数组和三个成员，不从候选池分类推导交易规则。配置文件结构严格校验，拒绝未知字段、重复键、未知版本/日历和非 canonical 标的，不回退到默认值掩盖损坏。合法未知标的使用文件中显式声明的默认日历。

`strategies.registry` 在建立新配置时选择日历并计算内容 hash；扫描、研究、执行条件与持仓规则通过同一个 [core.trading_calendar](../mu_strategy/core/trading_calendar.py) 判断。配置每进程读取一次，改动后须重启相应进程才生效；查看器与扫描服务应使用同一版本。仅修改/合并代码不代表现有进程已更新。

## 时间与边界

- 持久时间使用 UTC 毫秒；日历和策略窗口按 `America/New_York`，复盘显示北京时间，并标出美东日期。夏令时由 `zoneinfo` 转换；缺少时区数据直接失败，不使用 UTC 猜测美东钟点。
- 保留原策略窗口的“包含结束分钟”语义，例如 09:45–11:30 包含 11:30:59；参考市场收盘是排他边界，13:00 已不属于提前收盘日的开放时段。自定义窗口也不能越过该边界。
- 首版不支持跨美东自然日的单段策略窗口；需要跨日时拆成各自然日内的窗口。
- 新扫描先处理可信数据问题，再判断日历/时段，最后判断 regime、RSI、MACD 和回踩。已存在 pending signal 时仍须通过当前窗口，窗口内继续沿用原 pending 规则。
- 下一窗口来自同一交集，跳过周末、节假日与被提前收盘截掉的时段。覆盖范围外日期、未知日历、内容 hash 不匹配或时区不可用均停止评估；不推算表外交易日。
- 日历关闭只禁止入场/加仓。现有止损、非时段杠杆风险及人工成交继续按既有规则处理；日历识别新增休市会改变新配置下的非时段风险判断。

例如 2026-09-07 为 Labor Day：MU 显示参考市场休市；下一策略窗口从 9 月 8 日 21:45 北京时间开始。2026-11-27 提前收盘，原 14:30 ET 下午策略窗口不可用。

## 来源、版本与历史

首版离线表取自 NYSE 的[2025–2027 公告](https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx)、[2026–2028 公告](https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/default.aspx)，并纳入 [2025-01-09 Carter 悼念日额外休市](https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx)。这是已公布的参考日历，临时增加的未来休市需要更新版本，运行扫描不联网自动修订。

新完整配置使用 `StrategyConfigPayloadV2`，冻结日历 ID 和内容 hash；新增日历会影响结果，研究报告应以新配置身份重新评估。旧 V1 配置保持精确旧字段与原 hash，恢复为 `weekday_windows_v1`，不从当前标的配置自动注入美股日历。已有持仓的后续确认沿用其冻结配置，candidate/release 可读取严格的 V1/V2 嵌套配置，审批与执行门禁不变。不能用 V1 序列化新的美股日历配置。

新 observation/cycle v2 绑定本次实际评估的 15m K 线 open/close、日历 ID/hash、当日状态与策略窗口；这些字段参与结果 fingerprint。v1 日志原样读取、保留原 hash，与 v2 可在同一日志中共存；旧记录没有的时间/日历依据保持未知。`signal_time_ms` 不改为最新 K 线时间。

如何区分当前限制、最近扫描与历史证据，见[每日复盘](signal-review.md#当前结论与时间依据)。
