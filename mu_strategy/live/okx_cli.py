from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from mu_strategy.live.okx import (
    DemoOrderRequest,
    OKXCredentials,
    OKXRestClient,
    ShadowExecutionLedger,
    build_shadow_event,
)


CREDENTIAL_SOURCE_CHOICES = ("auto", "process", "user", "machine")


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "read-only":
        payload = _run_read_only(args)
    elif args.command == "shadow-record":
        payload = _run_shadow_record(args)
    elif args.command == "demo-order":
        payload = _run_demo_order(args)
    else:
        parser.error("missing command")

    stdout.write(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    stdout.write("\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OKX API execution safety tools.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_only = subparsers.add_parser("read-only", help="Fetch OKX public/private read-only account context.")
    read_only.add_argument("--inst-type", default="SWAP")
    read_only.add_argument("--inst-id", default="MU-USDT-SWAP")
    read_only.add_argument("--ccy", default="USDT")
    read_only.add_argument("--demo", action="store_true")
    read_only.add_argument("--public-only", action="store_true")
    read_only.add_argument("--credential-source", choices=CREDENTIAL_SOURCE_CHOICES)

    shadow = subparsers.add_parser("shadow-record", help="Append a local shadow execution event.")
    shadow.add_argument("--ledger", type=Path, default=Path("reports/live/shadow_execution.jsonl"))
    shadow.add_argument("--event-id", required=True)
    shadow.add_argument("--symbol", required=True)
    shadow.add_argument("--action", choices=("buy", "sell"), required=True)
    shadow.add_argument("--plan-price", type=float, required=True)
    shadow.add_argument("--observed-price", type=float)
    shadow.add_argument("--quantity", type=float, required=True)
    shadow.add_argument("--status", choices=("paper", "filled", "missed", "blocked"), required=True)
    shadow.add_argument("--reason", required=True)
    shadow.add_argument("--timestamp-ms", type=int, required=True)

    demo_order = subparsers.add_parser("demo-order", help="Prepare or explicitly send an OKX demo trading order.")
    demo_order.add_argument("--inst-id", required=True)
    demo_order.add_argument("--inst-type", default="SWAP")
    demo_order.add_argument("--side", choices=("buy", "sell"), required=True)
    demo_order.add_argument("--size", required=True)
    demo_order.add_argument("--order-type", default="market", choices=("market", "limit", "post_only", "fok", "ioc"))
    demo_order.add_argument("--price")
    demo_order.add_argument("--td-mode", default="isolated")
    demo_order.add_argument("--client-order-id")
    demo_order.add_argument("--pos-side")
    demo_order.add_argument("--reduce-only", action="store_true")
    demo_order.add_argument("--confirm-demo-order", action="store_true")
    demo_order.add_argument("--credential-source", choices=CREDENTIAL_SOURCE_CHOICES)
    return parser


def _run_read_only(args: argparse.Namespace) -> dict:
    credentials = None if args.public_only else OKXCredentials.from_env(source=args.credential_source)
    client = OKXRestClient(credentials=credentials, demo=args.demo)
    output = {
        "mode": "read_only",
        "demo": args.demo,
        "instrument": client.get_instruments(inst_type=args.inst_type, inst_id=args.inst_id),
    }
    if not args.public_only:
        output["balance"] = client.get_balance(ccy=args.ccy)
        output["positions"] = client.get_positions(inst_type=args.inst_type, inst_id=args.inst_id)
    warnings = _response_warnings(output, ("instrument", "balance", "positions"))
    output["status"] = "warning" if warnings else "ok"
    output["warnings"] = warnings
    return output


def _run_shadow_record(args: argparse.Namespace) -> dict:
    ledger = ShadowExecutionLedger(args.ledger)
    event = build_shadow_event(
        event_id=args.event_id,
        symbol=args.symbol,
        action=args.action,
        plan_price=args.plan_price,
        observed_price=args.observed_price,
        quantity=args.quantity,
        status=args.status,
        reason=args.reason,
        timestamp_ms=args.timestamp_ms,
    )
    ledger.append(event)
    return {"mode": "shadow_recorded", "event": asdict(event), "metrics": asdict(ledger.metrics())}


def _run_demo_order(args: argparse.Namespace) -> dict:
    client = OKXRestClient(credentials=OKXCredentials.from_env(source=args.credential_source), demo=True)
    request = DemoOrderRequest(
        inst_id=args.inst_id,
        side=args.side,
        size=args.size,
        order_type=args.order_type,
        price=args.price,
        td_mode=args.td_mode,
        client_order_id=args.client_order_id,
        pos_side=args.pos_side,
        reduce_only=True if args.reduce_only else None,
    )
    if not args.confirm_demo_order:
        prepared = client.prepare_demo_order(request)
        return {"mode": "dry_run", "request": prepared.sanitized()}
    instrument = client.get_instruments(inst_type=args.inst_type, inst_id=args.inst_id)
    instrument_warnings = _instrument_unavailable_warnings(instrument)
    if instrument_warnings:
        return {
            "mode": "blocked_demo_order",
            "status": "blocked",
            "reason": "demo_instrument_unavailable",
            "instrument": instrument,
            "warnings": instrument_warnings,
        }
    response = client.place_demo_order(request, confirm_demo_order=True)
    return {"mode": "sent_demo_order", "response": response}


def _response_warnings(output: dict, components: tuple[str, ...]) -> list[dict[str, str]]:
    warnings = []
    for component in components:
        response = output.get(component)
        if not isinstance(response, dict):
            continue
        code = str(response.get("code", ""))
        if code in {"", "0"}:
            continue
        warnings.append(
            {
                "component": component,
                "code": code,
                "msg": str(response.get("msg", "")),
            }
        )
    return warnings


def _instrument_unavailable_warnings(instrument: dict) -> list[dict[str, str]]:
    warnings = _response_warnings({"instrument": instrument}, ("instrument",))
    if warnings:
        return warnings
    data = instrument.get("data")
    if isinstance(data, list) and data:
        return []
    return [
        {
            "component": "instrument",
            "code": str(instrument.get("code", "")),
            "msg": "No instrument rows returned from OKX demo instruments endpoint.",
        }
    ]


if __name__ == "__main__":
    raise SystemExit(main())
