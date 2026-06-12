# MU Current Strategy Status

当前结论：MU 主策略只保留 `baseline`。

其他策略组仍保留在代码中，作用是备用回放、消融实验和跨标的对照；它们不再作为 MU 当前主策略候选。

## 主策略

| 项 | 当前选择 |
|---|---|
| symbol | `MUUSDT` / `MU-USDT-SWAP` 数据源对照 |
| main strategy group | `baseline` |
| entry execution | 二次回踩确认买入，最多等待 8 根 15m K |
| stop policy | baseline 抬止损 |
| leverage/margin | 5x，20% / 20% / 20% / 40% 金字塔 |
| 1h regime | red 禁止开仓，yellow/green 允许首仓，green 允许推进到第3/第4段 |

## 为什么不把半保护设为主策略

`baseline_half_protect` 的优点是胜率和加仓后胜率更高，能减少卖飞；但在 MU 的 OKX 约 100 天样本里，它的平均亏损更大，最终收益低于 `baseline`。

OKX 样本结果：

| 策略组 | 总收益 | 最大回撤 | 交易数 | 胜率 | 加仓后胜率 | 平均亏损单 |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 69.30% | -23.92% | 45 | 22.22% | 34.48% | -2.21% |
| baseline_half_protect | 37.68% | -23.88% | 41 | 29.27% | 46.15% | -3.83% |

因此当前不把半保护升级为主策略。后续如果先改善首仓/加仓位置质量，再重新评估半保护是否能提高趋势持有质量。

## 数据源状态

| 数据源 | 标的 | 最早 15m K | 覆盖 |
|---|---|---|---|
| Binance REST | `MUUSDT` | 2026-04-07 13:15 UTC | 约 65.6 天 |
| Binance 官方公开归档 | `MUUSDT` | 2026-04-07 | 没有更早历史 |
| OKX public API | `MU-USDT-SWAP` | 2026-03-04 07:15 UTC | 约 99.9 天 |

OKX 是当前可用最长公开来源，但仍不足完整半年。真正半年需要更换数据假设，例如底层美股 `MU` 行情或第三方合成/付费数据。

## 已固化报告

- `reports/mu_half_year_baseline_half_protect.md`
- `reports/mu_okx_half_year_baseline_half_protect.md`
- `reports/mu_data_source_comparison.md`
- `reports/mu_okx_100d_baseline_backtest.html`
- `reports/mu_okx_100d_baseline_half_protect_backtest.html`

研究用途，不构成投资建议。
