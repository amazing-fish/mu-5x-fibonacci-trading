# BTCUSDT 策略组对比

目的：保留 legacy_break_high 旧突破前高策略作为备用，同时把二次回踩确认升级为 baseline；后续可按名称加载策略组，避免直接丢弃备用策略或只看单段过拟合结果。

## 数据

- data files: data\BTCUSDT_15m_180d.csv, data\BTCUSDT_1h_180d.csv

## 策略组

- legacy_break_high (旧突破前高baseline备用)
  - 规则：原始 Fib 回踩确认 + 下一根突破前高执行。
  - 止损：baseline 抬止损。
- baseline (新baseline：二次回踩确认买入)
  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 8 根 15m K。
  - 止损：baseline 抬止损。
- direct_next_open (回踩确认后下一根开盘买入)
  - 规则：Fib 回踩确认后下一根开盘直接买入，不等待突破前高。
  - 止损：baseline 抬止损。
- baseline_half_protect (新baseline + 半保护止损)
  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 8 根 15m K。
  - 止损：半保护。
- baseline_green_wide (新baseline + green宽止损（yellow窄）)
  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 8 根 15m K。
  - 止损：yellow=窄止损/baseline，green=宽止损。
- baseline_yellow_wide (新baseline + yellow宽止损（green窄）)
  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 8 根 15m K。
  - 止损：yellow=宽止损，green=窄止损/baseline。
- baseline_yellow_green_wide (新baseline + yellow/green均宽止损)
  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 8 根 15m K。
  - 止损：yellow=宽止损，green=宽止损。
- baseline_half_green_wide (新baseline + 半保护 + green宽止损)
  - 规则：Fib 回踩确认后等待二次回踩买入，最多等待 8 根 15m K。
  - 止损：半保护；1h green 时更宽，不立即抬到首仓成本/均价。
- optimized_v2 (优化策略 v2)
  - 规则：原始 Fib 回踩确认 + 下一根突破前高执行。
  - 止损：baseline 抬止损。
  - 过滤：限制首仓追价、限制信号 K 过宽、yellow 更严格，并可启用反向 Fibonacci 压力位过滤。
  - max entry above Fib: 1.00%
  - yellow max entry above Fib: 0.60%
  - max signal range: 1.50%
  - max entry above signal close: 0.60%
  - reverse Fibonacci resistance filter: True

## 两段结果

| 策略组 | 样本 | UTC 区间 | K线数 | 总收益 | 最大回撤 | 交易数 | 胜率 | 首仓止损 | 加仓后胜率 | 最佳单笔 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy_break_high | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -14.55% | -30.52% | 53 | 13.21% | 31 | 31.82% | 16.21% |
| baseline | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -37.53% | -43.50% | 51 | 11.76% | 25 | 23.08% | 8.06% |
| direct_next_open | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -31.28% | -42.90% | 56 | 10.71% | 31 | 24.00% | 13.53% |
| baseline_half_protect | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -10.82% | -33.22% | 44 | 22.73% | 24 | 50.00% | 11.18% |
| baseline_green_wide | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -38.72% | -44.58% | 51 | 11.76% | 25 | 23.08% | 7.81% |
| baseline_yellow_wide | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -38.83% | -44.67% | 51 | 11.76% | 25 | 23.08% | 8.06% |
| baseline_yellow_green_wide | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -37.07% | -43.41% | 50 | 12.00% | 24 | 23.08% | 9.14% |
| baseline_half_green_wide | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -11.50% | -33.22% | 44 | 22.73% | 24 | 50.00% | 10.93% |
| optimized_v2 | 第1段 | 2025-12-14 04:30 ~ 2026-06-12 04:30 | 17280 | -2.72% | -31.95% | 37 | 16.22% | 19 | 33.33% | 17.11% |

## 反向 Fibonacci 说明

optimized_v2 会检查最近下跌波段的 0.382/0.5/0.618 反抽位；如果反弹买入价格正贴近这些反向 Fibonacci 压力位，就跳过这次首仓。这个过滤用于识别“反弹到压力位后立即回落”的结构，不用于替代 1h regime 过滤。

研究用途，不构成投资建议。