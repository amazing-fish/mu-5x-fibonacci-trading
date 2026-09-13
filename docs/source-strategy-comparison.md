# 指定仓库策略复跑与组合判断（2026-09-13）

本轮实际使用 `paperswithbacktest/awesome-systematic-trading/static/strategies/` 中的具体策略，固定来源提交 `4e23dd84c9ff746ddfcbc856316bcbcef0855b81`。之前把“采用优秀策略”做成了自拟 MU 指标和 Fibonacci 参数实验，原因是只借用了概念，没有以源码的资产池、周期和交易规则约束实现。那些报告现已标为历史实验；本轮不再从其中挑出一个当作来源策略。

## 实际采用哪些策略

| 来源策略 | 保留的源码规则 | 本次执行差异 |
|---|---|---|
| [跨资产动量](https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/asset-class-momentum-rotational-system.py) | SPY/EFA/IEF/VNQ/GSG，252 日 ROC 排序，前三等权，每月调仓 | 已收盘日线生成信号，下一交易日开盘；统一成本和小数份额 |
| [行业动量轮动](https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/sector-momentum-rotational-system.py) | VNQ 加九只原行业 ETF，252 日 ROC，前三等权，每月调仓 | 同上；源码虽然设置 5 倍可用杠杆，目标持仓总和仍为 100%，本次没有擅自加杠杆 |
| [股债配对切换](https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/paired-switching.py) | 源码 SPY/AGG，90 个日历日表现，在 3/6/9/12 月首次交易时择强持有 | 按代码的 SPY/AGG，而非注释的 VFINX/VUSTX；历史区间为交易前 90 日，下一开盘执行 |
| [一月效应](https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/january-barometer.py) | 一月持 SPY；二月判定一月表现，正则继续 SPY，否则 BIL | 月界以已完成日线观察，下一交易日开盘执行；10 倍可用杠杆不等于 10 倍实际持仓 |
| [跨资产趋势](https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/asset-class-trend-following.py) | 五资产、210 日 SMA、月调仓；通过筛选的资产重新等权至 100%，全不通过才现金 | **日线适配项**：源码以月初分钟价格对比日线 SMA，本次以前一日收盘判断、下一日开盘；不是分钟执行的精确复制 |

跨资产趋势的注释写“10 月 SMA、固定等权、不通过留现金”，但实际代码是“210 日 SMA、通过者满仓等权”。本次明确采用后者；源码 `hour != 9 and minute != 31` 的时刻条件也不能解释成严格 09:31。没有把两套规则混写成一套新策略。

这五项是从可读源码和可获取原资产池筛选出的首批，不是声称复现了仓库全部策略，也不是照抄 README 的 Sharpe 排行。行业与跨资产动量共享动量机制，不能当成五个完全独立收益来源。

## 数据、比较方法与结果

原标的共 16 只 ETF，Yahoo Finance 日线，2007-05-30 至 2026-09-11 共 4,853 个共同交易日。以 SPY 交易日历对齐，缺失价格直接报错，未用插值补洞。复权收盘及按同日复权因子换算的开盘价作为总回报价格近似；供应商、时间戳、原始文件 SHA256 及规范化输入 SHA256 保留在 JSON。

这不是原 QuantConnect 数据和撮合器的重放，也不是 MU 业绩。信号不使用交易当日日线收盘。组合真实分配同一笔本金；调仓前扣每侧 5 bps 的手续费及滑点合并假设，以净资产重新计算份额，不借钱支付成本。压力每侧 15 bps。闲置现金零利息，持 BIL 则有实际复权价格收益。

2009–2017 年用于展示早期表现，不调源码参数；2018–2021 年用于从 70 个、步长 25% 的资金分配中选权重，目标是在不超过同期 SPY 日末回撤的条件下最大化净收益；2022–2026-09-11 留作后段检验，不再用它修改权重。该历史仍是今天选定策略后做的回顾检验，不称为前瞻实盘验证。

| 策略/组合 | 2018–2021 收益 | 2022以来收益 | 2022以来年化 | 2022以来日末最大回撤 | 压力成本后收益 |
|---|---:|---:|---:|---:|---:|
| 跨资产动量 | 42.32% | 45.13% | 8.30% | 21.43% | 42.67% |
| 行业动量轮动 | 61.40% | 103.11% | 16.38% | 16.15% | 97.65% |
| 股债配对切换 | 69.91% | 37.64% | 7.08% | 24.30% | 35.19% |
| 一月效应 | 24.52% | 99.00% | 15.87% | 18.76% | 97.81% |
| 跨资产趋势（日线适配） | 47.21% | 23.96% | 4.71% | 27.91% | 20.52% |
| 五策略各 20% 初始本金 | 49.07% | 61.77% | 10.85% | 14.18% | 58.77% |
| SPY 买入持有 | 89.87% | 70.37% | 12.08% | 24.50% | 70.03% |
| SPY/AGG 月调仓 60/40 | 58.15% | 38.31% | 7.19% | 20.49% | 37.92% |

资金子账户内部按各自策略调仓，子账户之间只分配初始本金、之后权重漂移。没有把每日策略收益按固定比例相加而漏掉组合再平衡成本。区间末统一扣除清仓成本；每个区间独立从 10,000 起步。回撤为日末权益，未测日内最深回撤。

**权重优化没有通过后段检验。** 2018–2021 的目标选出 100% 股债切换，但后段只有 37.64%，低于五策略等权的 61.77%，回撤也更大。因此不采用这一“历史最优权重”。五策略等权比 60/40 在后段更好，但总收益低于 SPY；它体现降低波动的取舍，不能写成全面优胜。行业轮动和一月效应是后段的强项，但不能在看过后段后再把它们拼成一个“已验证最优组合”。

## 回到 MU：哪些能用

额外测试了原仓库 [BTC 日内季节性](https://github.com/paperswithbacktest/awesome-systematic-trading/blob/4e23dd84c9ff746ddfcbc856316bcbcef0855b81/static/strategies/intraday-seasonality-in-bitcoin.py)：UTC 22:00 买、00:00 卖，目标 1 倍本金，不增加任何过滤器。此处明确是**原时段规则迁移到 MU**，没有宣称复现原 Bitfinex BTC 收益。

MU trusted generation `c6a16255ecda480299f84081e014e3f8`，179 天、179 笔：每侧 5 bps 时收益 **-27.27%**，小时权益回撤 **30.00%**；每侧 10 bps 加 1 tick 滑点，收益 **-42.26%**。因此不把它加入 MU 组合。输入直接读取原隔离历史 generation，不改 trusted store。

当前 MU 只有 179 天，不能支持原 252 **交易日**动量的预热，更不能把 252 日改成 168 小时仍称原策略。跨资产、行业轮动和股债切换的收益依靠不同资产之间的选择，也不能删到只剩一只 MU 后照搬结论。

当前行动判断：保留现有 MU 运行配置；从来源策略中保留行业轮动、跨资产动量及低频配置作为多资产研究候选，等权作为组合基准；拒绝失败的追逐历史收益权重和 MU 时段迁移。尚未证明能直接替代当前 MU 策略。本轮未授权实盘或改变账户资产范围，原资产池结果作为研究交付。

## 复跑与验证

实现入口 `mu_strategy/experiments/source_strategies.py` 为离线消费者，无网络采集、交易或发布动作，不进入运行策略注册表。原始采集与本轮输入、HTML/Markdown/JSON 保留在 worktree 的 ignored `reports/live/source-strategies-20260913/`，本轮 `fetch_inputs.py` 仅采集外部研究输入，与 trusted 数据生产无关。

```powershell
python -B -m mu_strategy.experiments.source_strategies --panel reports/live/source-strategies-20260913/inputs/panel.json --output-dir reports/live/source-strategies-20260913/results
python -B -m unittest discover -s tests -p test_source_strategies.py
```

聚焦验证覆盖原 252 日前三排序、信号不能读取当前或未来收盘、原季度月份、一月负收益切 BIL、含费用本金守恒与末端清仓、组合选择目标。先观察未实现规则的失败，再完成规则实现后通过。全量测试在本次实现后通过 1,096 项，跳过 7 项。结果是本地研究验证，不等于实盘表现。
