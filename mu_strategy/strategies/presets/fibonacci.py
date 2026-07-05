from __future__ import annotations

from dataclasses import dataclass


DEFAULT_FIB_LOOKBACK_BARS = 32


@dataclass(frozen=True)
class PreferredFibonacciParameter:
    asset: str
    symbol: str
    source: str
    horizon_hours: int
    fib_lookback_bars: int
    evidence_report: str
    note: str


PREFERRED_FIBONACCI_PARAMETERS: tuple[PreferredFibonacciParameter, ...] = (
    PreferredFibonacciParameter(
        asset="MU",
        symbol="MU-USDT-SWAP",
        source="okx",
        horizon_hours=2,
        fib_lookback_bars=8,
        evidence_report="reports/live/mu_fibonacci_pullback_1h_12h_7d.md",
        note="Promoted to MU baseline after the latest-week sweep; also ranked #2 in the multi-asset 180d request.",
    ),
    PreferredFibonacciParameter(
        asset="SPACEX",
        symbol="SPCX-USDT-SWAP",
        source="okx",
        horizon_hours=2,
        fib_lookback_bars=8,
        evidence_report="reports/live/fibonacci_pullback_multi_asset_1h_12h_180d.md",
        note="Best available full-sample rank in the OKX SPCX-USDT-SWAP sweep.",
    ),
    PreferredFibonacciParameter(
        asset="META",
        symbol="META-USDT-SWAP",
        source="okx",
        horizon_hours=9,
        fib_lookback_bars=36,
        evidence_report="reports/live/fibonacci_pullback_multi_asset_1h_12h_180d.md",
        note="Best available full-sample rank in the OKX META-USDT-SWAP sweep.",
    ),
    PreferredFibonacciParameter(
        asset="BTC",
        symbol="BTC-USDT-SWAP",
        source="okx",
        horizon_hours=3,
        fib_lookback_bars=12,
        evidence_report="reports/live/fibonacci_pullback_multi_asset_1h_12h_180d.md",
        note="Best full-180d OKX BTC-USDT-SWAP rank; 4h remained a close #2.",
    ),
)


_BY_SYMBOL = {parameter.symbol: parameter for parameter in PREFERRED_FIBONACCI_PARAMETERS}
_ALIASES = {
    "MU": "MU-USDT-SWAP",
    "MUUSDT": "MU-USDT-SWAP",
    "SPACEX": "SPCX-USDT-SWAP",
    "SPCX": "SPCX-USDT-SWAP",
    "META": "META-USDT-SWAP",
    "BTC": "BTC-USDT-SWAP",
    "BTCUSDT": "BTC-USDT-SWAP",
}


def preferred_fibonacci_parameter(symbol_or_asset: str) -> PreferredFibonacciParameter | None:
    key = symbol_or_asset.strip().upper()
    symbol = _ALIASES.get(key, key)
    return _BY_SYMBOL.get(symbol)


def preferred_fib_lookback(symbol_or_asset: str, default: int = DEFAULT_FIB_LOOKBACK_BARS) -> int:
    parameter = preferred_fibonacci_parameter(symbol_or_asset)
    return parameter.fib_lookback_bars if parameter is not None else default
