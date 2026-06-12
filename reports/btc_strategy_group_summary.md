# BTCUSDT Strategy Group Summary

目标：

- MU 主策略口径收敛为 `baseline`，其他策略组只保留为备用/实验对照，不再作为 MU 主策略候选。
- 将标的切换为 `BTCUSDT`，复用之前几个策略组做回测。

## 数据与口径

- 交易所：Binance Futures REST
- 15m/1h 数据：
  - `data/BTCUSDT_15m_28d.csv`
  - `data/BTCUSDT_1h_28d.csv`
  - `data/BTCUSDT_15m_180d.csv`
  - `data/BTCUSDT_1h_180d.csv`
- 策略仍沿用原有美股现金盘时间窗，因此 BTC 回测不是 24/7 全天候交易，而是测试“同一套时间过滤 + Fib 回踩策略”能否迁移到 BTC。

## 最近 28 天：两段 14 天

完整报告：`reports/btc_strategy_group_review.md`

| 策略组 | 第1段收益 | 第2段收益 | 第1段交易 | 第2段交易 | 备注 |
|---|---:|---:|---:|---:|---|
| legacy_break_high | -4.15% | -3.27% | 2 | 4 | 旧突破确认仍弱 |
| baseline | -4.15% | -0.21% | 2 | 3 | 交易少，第二段接近打平 |
| direct_next_open | -4.15% | -5.10% | 2 | 5 | 直接开盘买入更差 |
| baseline_half_protect | -4.15% | -0.21% | 2 | 3 | 与 baseline 基本一致 |
| baseline_green_wide | -4.15% | -0.21% | 2 | 3 | 与 baseline 基本一致 |
| baseline_yellow_wide | -4.15% | -0.21% | 2 | 3 | 与 baseline 基本一致 |
| baseline_yellow_green_wide | -4.15% | -0.21% | 2 | 3 | 与 baseline 基本一致 |
| baseline_half_green_wide | -4.15% | -0.21% | 2 | 3 | 与 baseline 基本一致 |
| optimized_v2 | -4.15% | -1.20% | 2 | 3 | 第二段弱于 baseline |

## 最近 180 天：单段

完整报告：`reports/btc_strategy_group_180d_review.md`

| 策略组 | 总收益 | 最大回撤 | 交易数 | 胜率 | 首仓止损 | 加仓后胜率 | 最佳单笔 |
|---|---:|---:|---:|---:|---:|---:|---:|
| legacy_break_high | -14.55% | -30.52% | 53 | 13.21% | 31 | 31.82% | 16.21% |
| baseline | -37.53% | -43.50% | 51 | 11.76% | 25 | 23.08% | 8.06% |
| direct_next_open | -31.28% | -42.90% | 56 | 10.71% | 31 | 24.00% | 13.53% |
| baseline_half_protect | -10.82% | -33.22% | 44 | 22.73% | 24 | 50.00% | 11.18% |
| baseline_green_wide | -38.72% | -44.58% | 51 | 11.76% | 25 | 23.08% | 7.81% |
| baseline_yellow_wide | -38.83% | -44.67% | 51 | 11.76% | 25 | 23.08% | 8.06% |
| baseline_yellow_green_wide | -37.07% | -43.41% | 50 | 12.00% | 24 | 23.08% | 9.14% |
| baseline_half_green_wide | -11.50% | -33.22% | 44 | 22.73% | 24 | 50.00% | 10.93% |
| optimized_v2 | -2.72% | -31.95% | 37 | 16.22% | 19 | 33.33% | 17.11% |

## 判断

- BTC 迁移效果不佳：大部分策略 180 天为负，说明 MU 的入场/时间窗/加仓假设不能直接迁移到 BTC。
- `optimized_v2` 在 BTC 上亏损最小，核心可能是反向 Fibonacci 压力过滤与追价限制减少了坏交易。
- `baseline_half_protect` 明显优于 `baseline`，说明 BTC 上过快抬止损的问题更严重；但它仍未转正。
- 当前 BTC 下一步不应直接沿用 MU baseline，而应先重设交易时间窗和波动参数。BTC 是 24/7 标的，继续使用美股现金盘时间窗会人为丢失大量结构。

研究用途，不构成投资建议。
