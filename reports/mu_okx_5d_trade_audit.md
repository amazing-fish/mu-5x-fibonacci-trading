# MU-USDT-SWAP 5d Baseline Trade Audit

## Summary

- data files: data\OKX_MU-USDT-SWAP_15m_5d.csv, data\OKX_MU-USDT-SWAP_1h_5d.csv
- 15m coverage: 2026-06-09T05:15:00+00:00 to 2026-06-14T04:45:00+00:00 (479 bars)
- 1h coverage: 2026-06-09T06:00:00+00:00 to 2026-06-14T04:00:00+00:00 (119 bars)
- strategy: baseline, entry_execution=second_pullback, windows ET=(('09:45', '11:30'), ('14:30', '15:45'))
- starting equity: 10000.00
- ending equity: 11757.88
- total return: 17.58%
- max drawdown: -10.20%
- trades: 3
- win rate: 33.33%
- profit factor: 4.70
- audited events: 11
- price-range anomalies: 0

## Trade Overview

| # | entry UTC | exit UTC | avg entry | exit | stage | pnl | return | reason |
|---:|---|---|---:|---:|---:|---:|---:|---|
| 1 | 2026-06-10T14:00:00+00:00 | 2026-06-10T15:00:00+00:00 | 925.4733 | 916.4000 | 2 | -215.98 | -2.16% | stop |
| 2 | 2026-06-11T14:15:00+00:00 | 2026-06-12T01:15:00+00:00 | 950.8773 | 995.2621 | 4 | 2233.42 | 22.33% | stop |
| 3 | 2026-06-12T14:30:00+00:00 | 2026-06-12T15:45:00+00:00 | 990.8851 | 981.1705 | 2 | -259.55 | -2.60% | stop |

## Trade 1

- entry: 2026-06-10T14:00:00+00:00, avg entry 925.4733
- exit: 2026-06-10T15:00:00+00:00, exit 916.4000, reason stop
- stage: 2, fills 2, pnl -215.98, return -2.16%, fees 19.90

| event | time UTC / ET / BJT | price | basis | 1h regime | cash window | 15m candle | 15m ok | 1h candle | 1h ok |
|---|---|---:|---|---|---|---|---|---|---|
| fill 1 | 2026-06-10T14:00:00+00:00 / ET 2026-06-10 10:00 / BJT 2026-06-10 22:00 | 916.4000 | entry: second_pullback limit fill | yellow | yes | O 916.8000 / H 931.9300 / L 912.3600 / C 925.4300 | OK | O 916.8000 / H 958.8900 / L 912.3600 / C 922.6800 | OK |
| fill 2 | 2026-06-10T14:15:00+00:00 / ET 2026-06-10 10:15 / BJT 2026-06-10 22:15 | 934.7280 | add stage 2: threshold fill 934.7280 | yellow | yes | O 925.6800 / H 950.3600 / L 922.1100 / C 949.0600 | OK | O 916.8000 / H 958.8900 / L 912.3600 / C 922.6800 | OK |
| exit | 2026-06-10T15:00:00+00:00 / ET 2026-06-10 11:00 / BJT 2026-06-10 23:00 | 916.4000 | exit reason: stop | red | yes | O 922.6200 / H 928.5000 / L 910.4900 / C 910.7000 | OK | O 922.6200 / H 928.5000 / L 892.3700 / C 912.5300 | OK |

## Trade 2

- entry: 2026-06-11T14:15:00+00:00, avg entry 950.8773
- exit: 2026-06-12T01:15:00+00:00, exit 995.2621, reason stop
- stage: 4, fills 4, pnl 2233.42, return 22.33%, fees 50.06

| event | time UTC / ET / BJT | price | basis | 1h regime | cash window | 15m candle | 15m ok | 1h candle | 1h ok |
|---|---|---:|---|---|---|---|---|---|---|
| fill 1 | 2026-06-11T14:15:00+00:00 / ET 2026-06-11 10:15 / BJT 2026-06-11 22:15 | 912.1055 | entry: second_pullback limit fill | red | yes | O 917.1800 / H 921.9900 / L 909.0500 / C 917.4000 | OK | O 912.7500 / H 931.1600 / L 900.2300 / C 903.3300 | OK |
| fill 2 | 2026-06-11T18:30:00+00:00 / ET 2026-06-11 14:30 / BJT 2026-06-12 02:30 | 951.9900 | add stage 2: gap-open fill, threshold 930.3477 | green | yes | O 951.9900 / H 959.3200 / L 950.9300 / C 958.9000 | OK | O 947.0600 / H 966.8700 / L 935.8300 / C 965.7500 | OK |
| fill 3 | 2026-06-11T18:45:00+00:00 / ET 2026-06-11 14:45 / BJT 2026-06-12 02:45 | 958.8700 | add stage 3: gap-open fill, threshold 948.5898 | green | yes | O 958.8700 / H 966.8700 / L 958.4000 / C 965.7500 | OK | O 947.0600 / H 966.8700 / L 935.8300 / C 965.7500 | OK |
| fill 4 | 2026-06-11T19:00:00+00:00 / ET 2026-06-11 15:00 / BJT 2026-06-12 03:00 | 966.8319 | add stage 4: threshold fill 966.8319 | green | yes | O 965.7800 / H 973.7600 / L 958.7300 / C 973.3300 | OK | O 965.7800 / H 996.9300 / L 958.7300 / C 996.4300 | OK |
| exit | 2026-06-12T01:15:00+00:00 / ET 2026-06-11 21:15 / BJT 2026-06-12 09:15 | 995.2621 | exit reason: stop | yellow | no | O 1003.8000 / H 1004.4300 / L 986.2600 / C 989.7400 | OK | O 1009.8700 / H 1009.8700 / L 983.6500 / C 995.7500 | OK |

## Trade 3

- entry: 2026-06-12T14:30:00+00:00, avg entry 990.8851
- exit: 2026-06-12T15:45:00+00:00, exit 981.1705, reason stop
- stage: 2, fills 2, pnl -259.55, return -2.60%, fees 23.92

| event | time UTC / ET / BJT | price | basis | 1h regime | cash window | 15m candle | 15m ok | 1h candle | 1h ok |
|---|---|---:|---|---|---|---|---|---|---|
| fill 1 | 2026-06-12T14:30:00+00:00 / ET 2026-06-12 10:30 / BJT 2026-06-12 22:30 | 981.1705 | entry: second_pullback limit fill | green | yes | O 991.2300 / H 997.4900 / L 980.6700 / C 982.1700 | OK | O 987.9800 / H 1014.0100 / L 978.0000 / C 992.3300 | OK |
| fill 2 | 2026-06-12T15:00:00+00:00 / ET 2026-06-12 11:00 / BJT 2026-06-12 23:00 | 1000.7939 | add stage 2: threshold fill 1000.7939 | red | yes | O 992.3100 / H 1001.8400 / L 990.6200 / C 1001.8400 | OK | O 992.3100 / H 1005.0300 / L 981.1100 / C 983.1300 | OK |
| exit | 2026-06-12T15:45:00+00:00 / ET 2026-06-12 11:45 / BJT 2026-06-12 23:45 | 981.1705 | exit reason: stop | red | no | O 993.0700 / H 999.4900 / L 981.1100 / C 983.1300 | OK | O 992.3100 / H 1005.0300 / L 981.1100 / C 983.1300 | OK |

## Anomalies

- None. Every fill and exit price is inside both the matching 15m candle and the matching 1h chart candle.

This report is a research artifact, not financial advice.