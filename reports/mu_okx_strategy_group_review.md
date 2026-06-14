# MU-USDT-SWAP 策略组对比

目的：保留 legacy_break_high 旧突破前高策略作为备用，同时把二次回踩确认升级为 baseline；后续可按名称加载策略组，避免直接丢弃备用策略或只看单段过拟合结果。

## 数据

- data files: data\OKX_MU-USDT-SWAP_15m_180d.csv, data\OKX_MU-USDT-SWAP_1h_180d.csv

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

## 策略组件矩阵

| 策略组 | 入场策略 | 加减仓策略 | 出场策略 | 过滤策略 |
|---|---|---|---|---|
| legacy_break_high | 突破前高确认 | 5x 金字塔 20/20/20/40 | baseline 抬止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| baseline | 二次回踩限价 | 5x 金字塔 20/20/20/40 | baseline 抬止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| direct_next_open | 下一根开盘直接买入 | 5x 金字塔 20/20/20/40 | baseline 抬止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| baseline_half_protect | 二次回踩限价 | 5x 金字塔 20/20/20/40 | 半保护止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| baseline_green_wide | 二次回踩限价 | 5x 金字塔 20/20/20/40 | yellow baseline / green 宽止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| baseline_yellow_wide | 二次回踩限价 | 5x 金字塔 20/20/20/40 | yellow 宽止损 / green baseline | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| baseline_yellow_green_wide | 二次回踩限价 | 5x 金字塔 20/20/20/40 | yellow/green 均宽止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| baseline_half_green_wide | 二次回踩限价 | 5x 金字塔 20/20/20/40 | 半保护 + green 宽止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口 |
| optimized_v2 | 突破前高确认 | 5x 金字塔 20/20/20/40 | baseline 抬止损 | 1h regime<br>15m RSI/MACD<br>美股现金盘窗口<br>首仓追价限制<br>信号K宽度限制<br>反向 Fibonacci 压力过滤 |

## 分段结果

| 策略组 | 样本 | UTC 区间 | K线数 | 总收益 | 最大回撤 | 交易数 | 胜率 | 首仓止损 | 加仓后胜率 | 最佳单笔 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy_break_high | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 82.34% | -39.34% | 56 | 16.07% | 25 | 30.00% | 54.25% |
| baseline | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 65.64% | -23.92% | 46 | 21.74% | 16 | 33.33% | 35.34% |
| direct_next_open | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 103.81% | -32.25% | 64 | 15.62% | 27 | 27.78% | 43.89% |
| baseline_half_protect | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 37.58% | -23.88% | 42 | 28.57% | 15 | 44.44% | 28.74% |
| baseline_green_wide | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 71.73% | -23.89% | 46 | 21.74% | 16 | 33.33% | 36.64% |
| baseline_yellow_wide | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 60.90% | -23.92% | 46 | 21.74% | 16 | 33.33% | 30.88% |
| baseline_yellow_green_wide | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 53.68% | -24.06% | 45 | 24.44% | 15 | 36.67% | 29.49% |
| baseline_half_green_wide | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 52.18% | -23.02% | 42 | 28.57% | 15 | 44.44% | 31.79% |
| optimized_v2 | 第1段 | 2026-03-04 07:15 ~ 2026-06-13 19:15 | 9744 | 43.06% | -26.86% | 29 | 17.24% | 14 | 33.33% | 40.50% |

## 反向 Fibonacci 说明

optimized_v2 会检查最近下跌波段的 0.382/0.5/0.618 反抽位；如果反弹买入价格正贴近这些反向 Fibonacci 压力位，就跳过这次首仓。这个过滤用于识别“反弹到压力位后立即回落”的结构，不用于替代 1h regime 过滤。

研究用途，不构成投资建议。