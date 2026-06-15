# Fibonacci 优选参数记录

本文件记录当前已验证标的的 Fibonacci 回看窗口。窗口换算基于 15m K 线：

- `1h = 4` 根 15m K
- `2h = 8` 根 15m K
- `3h = 12` 根 15m K
- `9h = 36` 根 15m K

## 当前记录

| 标的 | 解析符号 | 数据源 | 优选窗口 | `fib_lookback` | 用途 | 证据 |
|---|---|---|---:|---:|---|---|
| MU | `MU-USDT-SWAP` | OKX | 2h | 8 | 当前 baseline | `reports/mu_fibonacci_pullback_1h_12h_7d.md` |
| SPACEX | `SPCX-USDT-SWAP` | OKX | 2h | 8 | 标的优选参数记录 | `reports/fibonacci_pullback_multi_asset_1h_12h_180d.md` |
| META | `META-USDT-SWAP` | OKX | 9h | 36 | 标的优选参数记录 | `reports/fibonacci_pullback_multi_asset_1h_12h_180d.md` |
| BTC | `BTC-USDT-SWAP` | OKX | 3h | 12 | 标的优选参数记录 | `reports/fibonacci_pullback_multi_asset_1h_12h_180d.md` |

## 使用约定

- `baseline` 会读取 `mu_strategy.strategies.presets.fibonacci` 中的优选记录。
- 未记录标的回退到旧默认 `fib_lookback=32`，即 8h。
- MU 的 2h baseline 来自最新一周回测；SPACEX/META/BTC 来自多标的窗口敏感性报告。
- 这些记录只用于研究回测参数选择，不构成投资建议，也不触发自动交易。
