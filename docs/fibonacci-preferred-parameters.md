# Fibonacci 优选参数记录

> Deprecated as current evidence: this file is a historical parameter record only. It is not the current trusted market-data baseline, and it must not be used as proof that the current strategy still works without rerunning experiments on fresh trusted data.

本文件记录当前已验证标的的 Fibonacci 回看窗口。窗口换算基于 15m K 线：

- `1h = 4` 根 15m K
- `2h = 8` 根 15m K
- `3h = 12` 根 15m K
- `9h = 36` 根 15m K

## 当前记录

| 标的 | 解析符号 | 数据源 | 优选窗口 | `fib_lookback` | 用途 |
|---|---|---|---:|---:|---|
| MU | `MU-USDT-SWAP` | OKX | 2h | 8 | 历史 baseline 参数 |
| SPACEX | `SPCX-USDT-SWAP` | OKX | 2h | 8 | 历史标的优选参数记录 |
| META | `META-USDT-SWAP` | OKX | 9h | 36 | 历史标的优选参数记录 |
| BTC | `BTC-USDT-SWAP` | OKX | 3h | 12 | 历史标的优选参数记录 |

## 使用约定

- `baseline` 会读取 `mu_strategy.strategies.presets.fibonacci` 中的优选记录。
- 未记录标的回退到旧默认 `fib_lookback=32`，即 8h。
- MU 的 2h baseline 来自历史一周回测；SPACEX/META/BTC 来自历史多标的窗口敏感性报告。相关报告已不再作为 tracked artifact 保存，需要时应重新生成到 `reports/live/` 或用户指定的 ignored 路径。
- 这些记录只用于研究回测参数选择，不构成投资建议，也不触发自动交易。
