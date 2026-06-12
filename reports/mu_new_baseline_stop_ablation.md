# MUUSDT New Baseline Stop Ablation

数据：`data/MUUSDT_15m_28d.csv`、`data/MUUSDT_1h_28d.csv`  
窗口：两段独立 14 天。  
目标：确认 `second_pullback_limit_8` 的交易次数是否足够，并在新 baseline 上做止损策略组消融。

## 新 baseline 判断

`second_pullback_limit_8` 与 `direct_next_open` 交易次数对比：

| 策略 | 第1段交易数 | 第2段交易数 | 合计 |
|---|---:|---:|---:|
| direct_next_open | 7 | 11 | 18 |
| second_pullback_limit_8 | 7 | 8 | 15 |

二次回踩确认的交易数约为 direct 的 `83%`，不是稀疏样本。因此将二次回踩确认升级为新的 `baseline` 是合理的。旧突破前高策略保留为 `legacy_break_high`。

## 策略组定义

| 策略组 | 首仓执行 | 止损方式 |
|---|---|---|
| legacy_break_high | Fib 回踩确认后，下一根突破前高买入 | 原 baseline 抬止损 |
| baseline | Fib 回踩确认后，最多等 8 根 15m K 的二次回踩买入 | 原 baseline 抬止损 |
| baseline_half_protect | 新 baseline | 半保护止损 |
| baseline_green_wide | 新 baseline | 1h green 更宽止损 |
| baseline_half_green_wide | 新 baseline | 半保护 + 1h green 更宽止损 |

## 回测结果

| 策略组 | 第1段收益 | 第1段回撤 | 第1段交易 | 第1段胜率 | 第2段收益 | 第2段回撤 | 第2段交易 | 第2段胜率 | 加仓后胜率 | 最佳单笔 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy_break_high | -10.47% | -18.39% | 7 | 0.00% | -5.51% | -12.40% | 12 | 16.67% | 33.33% | 6.98% |
| baseline | -9.58% | -18.63% | 7 | 0.00% | 24.13% | -10.06% | 8 | 37.50% | 50.00% | 11.96% |
| baseline_half_protect | 2.93% | -18.12% | 6 | 33.33% | 10.18% | -14.77% | 7 | 42.86% | 50.00% | 12.51% |
| baseline_green_wide | -9.58% | -18.63% | 7 | 0.00% | 11.80% | -15.81% | 8 | 37.50% | 50.00% | 10.67% |
| baseline_half_green_wide | 2.93% | -18.12% | 6 | 33.33% | 5.38% | -16.66% | 7 | 42.86% | 50.00% | 10.05% |

## 消融解释

- 新 `baseline` 相比旧 `legacy_break_high` 明显改善第二段，并减少首仓止损；第1段仍亏，但交易次数没有明显下降。
- `baseline_half_protect` 是当前最均衡的止损变体：两段都为正收益，但第2段收益低于原 baseline，说明半保护减少了部分趋势延展。
- `baseline_green_wide` 没改善第1段，且第2段收益低于 baseline、回撤扩大；单独 green 更宽不是优先方向。
- `baseline_half_green_wide` 两段都为正，但第2段收益/回撤都弱于 `baseline_half_protect`，说明 green 宽止损叠加后没有带来额外优势。
- 补充黄/绿分档宽窄消融见 `reports/mu_yellow_green_stop_ablation.md`：单独放宽 yellow 基本不改变结果；单独放宽 green 会明显削弱第2段；yellow+green 同宽能改善第1段但仍牺牲第2段。

## 当前建议

1. 将 `baseline` 正式定义为二次回踩确认买入，旧突破前高保留为 `legacy_break_high`。
2. 止损策略优先保留两个候选：`baseline` 和 `baseline_half_protect`。
3. 暂时不要采用 `baseline_green_wide` 或 `baseline_half_green_wide` 作为主线；它们扩大回撤，收益不如单纯半保护。
4. 下一轮应在新 baseline 上继续测试“半保护 + 延迟抬止损”，而不是继续放宽 green 止损。
5. 后续所有策略比较都应至少同时跑 `legacy_break_high`、`baseline`、`baseline_half_protect`，避免忘记旧策略的参考价值。

研究用途，不构成投资建议。
