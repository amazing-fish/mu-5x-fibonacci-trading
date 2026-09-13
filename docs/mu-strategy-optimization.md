# MU 策略优化：2026-09-13

保留两条可执行的研究路线：现有 `baseline_delayed_tighten_smooth` 偏重全期收益；新增 `baseline_green_only_wide` 偏重较少交易和较低回撤。按照运行前固定的“末段优先”排序，后者是本次首选观察候选。二者均未取得新的前向表现，默认运行策略仍是 `baseline`。

## 具体改动

新预设复用现有二次回踩入场和 green 宽止损，只把 `allowed_regimes` 改为 `("green",)`。已收盘 1h 需同时满足收盘高于 EMA21、RSI ≥ 50、MACD 柱不弱于上一根，才创建新的 15m 回踩信号。15m RSI 下限仍为 45。

信号后限价单继续等待最多 8 根 15m K 线，因此成交时不保证仍是 green。持仓中的止损仍随最新可见状态选择：green 使用已有宽止损，yellow 使用 baseline，止损不会向下放松。仓位阶梯、5x 杠杆和加仓规则沿用原配置。

平滑止损路线使用已有预设：加仓后用 8 根 15m K 线平滑收紧到既定保护位。它不需要新增引擎实现。本次没有证据支持整体重构；改动是一个注册预设和复用既有回测引擎的固定实验入口。

## 实际结果

generation `c6a16255ecda480299f84081e014e3f8`，UTC `[2026-03-16, 2026-09-11)`，179 天，17,184 根 15m / 4,296 根 1h。初始 10,000，5x，20/20/20/40% 阶梯，每侧手续费 0.05%。以下是扣手续费的账户收益，不是保证金收益或年化收益。

| 配置 | 全期收益 | 最大回撤 | 交易数 | 末 59 天收益 | 双倍手续费全期收益 |
|---|---:|---:|---:|---:|---:|
| baseline | 142.03% | -24.19% | 57 | 9.59% | 117.59% |
| 平滑止损 | 217.55% | -24.19% | 55 | 10.76% | 185.77% |
| 仅 green 信号 + green 宽止损 | 145.94% | -21.97% | 35 | 26.01% | 129.39% |

新预设相对 baseline 全期收益增加 3.91 个百分点，回撤幅度减少 2.22 个百分点，少 22 笔交易。胜率由 17.54% 到 22.86%。末段仅 9 笔交易，不能据此认定持续优势。

利润仍主要来自少数大趋势：baseline / 平滑止损 / 新预设剔除前五盈利交易后的净利润分别为 -11,497.18 / -7,878.67 / -6,366.42。新预设获利的 8 笔均达到第 4 仓位阶段，尚未摆脱对趋势和加仓的依赖。此诊断只是从原交易净利润中扣除前五笔，不是删除交易后的复利重跑。

全部 8 个配置及固定保留条件见[实验设计](plans/2026-09-13-mu-trend-exit-experiment.md)。新假设中，仅 green 的 baseline、仅 green 的平滑止损、RSI 下限提高到 50 均未达标。此前全期收益最高的两种宽止损对照，也因回撤或末段表现退步而没有入选。

行情已经用于之前的策略比较；三段各自重新以 10,000 开始，均为重复使用的历史样本，不是未见样本。15m 指标在分段起点独立初始化；1h 上下文沿用全窗口的因果历史。双倍手续费是额外成本敏感性近似，仍未模拟资金费、价差、挂单队列或部分成交。

## 复跑与查看

将 `$dataDir` 指向实际保存该 generation 的本地可信数据目录；命令只读它。

```powershell
$dataDir = '<trusted_data_dir>'
$generationId = 'c6a16255ecda480299f84081e014e3f8'
python -B -m mu_strategy.experiments.trend_exit --data-dir $dataDir --generation-id $generationId --days 179 --output-dir reports/live/trend-exit
python -B -m mu_strategy.cli --data-dir $dataDir --generation-id $generationId --days 179 --strategy baseline_green_only_wide --report reports/live/green-only-wide.md
python -B -m mu_strategy.visualize --data-dir $dataDir --generation-id $generationId --days 179 --strategy baseline_green_only_wide --output reports/live/green-only-wide.html
```

实验名称 `green_only_green_wide` 对应公开预设 `baseline_green_only_wide`，两者完整配置相等。实验保留全部尝试的配置和结果，不把未达标项隐藏。JSON 内含交易、权益曲线、分段、配置和输入/代码指纹。`--days` 可用于其他完整历史窗口（至少 3 天），上述结论只对应本次 179 天；默认集合不加入新预设。

下一步固定这两个候选，用新增行情与 baseline 并行记录信号、失效及实际成交偏差。不要边观察边挑选更漂亮的参数。这里交付历史研究与可选配置，不把 #99 的前向验证或 #83 的全 registry 比较标成完成。

## 参考

经用户提供的 [awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading/tree/4e23dd84c9ff746ddfcbc856316bcbcef0855b81) 定位到 [Quantpedia 的多周期趋势文章](https://quantpedia.com/how-to-design-a-simple-multi-timeframe-trend-strategy-on-bitcoin/)。借用“高周期过滤、趋势退出管理”的方法假设，用 MU 数据重测；没有复制原文 BTC 绩效，也没有采用其季节性时段。
