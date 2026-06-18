from __future__ import annotations

from dataclasses import dataclass


OKX_SWAP_ALIASES = {
    "SPACEX": "SPCX-USDT-SWAP",
    "SPACE X": "SPCX-USDT-SWAP",
    "SPCX": "SPCX-USDT-SWAP",
}


@dataclass(frozen=True)
class ResolvedSymbol:
    requested: str
    inst_id: str
    source: str = "okx"


def resolve_okx_swap_symbol(value: str) -> ResolvedSymbol:
    requested = value.strip()
    raw = requested.upper().replace("_", "-")
    compact = "".join(ch for ch in raw if ch.isalnum())
    if raw in OKX_SWAP_ALIASES:
        inst_id = OKX_SWAP_ALIASES[raw]
    elif compact in OKX_SWAP_ALIASES:
        inst_id = OKX_SWAP_ALIASES[compact]
    elif raw.endswith("-USDT-SWAP"):
        inst_id = raw
    elif raw.endswith("-USDT"):
        inst_id = f"{raw}-SWAP"
    elif compact.endswith("USDT"):
        inst_id = f"{compact[:-4]}-USDT-SWAP"
    else:
        inst_id = f"{compact}-USDT-SWAP"
    return ResolvedSymbol(requested=requested, inst_id=inst_id)
