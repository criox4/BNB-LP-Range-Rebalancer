"""Operator dashboard — one page, one authenticated read.

WHY A SINGLE AGGREGATE ENDPOINT
===============================

The eight read routes in ``api.py`` are deliberately public: a marketplace card
has no credential, and everything they report is already on-chain. They also
answer piecemeal — an operator view wants the whole machine in one shot, and
seven fetches against a wedged RPC take seven timeouts to fail.

This endpoint is NOT public, for a reason the individual routes do not have: it
puts the agent's own wallet balances next to the position it manages. Each half
is public; together they say how much gas the signer has left before the next
rebalance stops working, which is operator information.

EVERY SECTION FAILS SOFT
========================

A dead RPC must blank one card, not the page. Each section is built inside
:func:`_section`, which turns a failure into ``{"error": ...}`` for that section
alone. "position: ChainUnavailable" tells an operator the RPC is down and the
rest of the page is still true; a blank page tells them less than the logs.

THE KEY IS NEVER SERVED
=======================

The HTML carries no credential. The browser holds whatever the operator typed,
in ``sessionStorage``, and sends it as ``X-API-Key``. Baking a key into the page
would put it in every proxy log and browser cache between here and them.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

import blockchain as chain
import risk
import strategy as strat

import termix

log = logging.getLogger("lp-rebalancer.dashboard")

router = APIRouter()

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"

EXPLORER = {
    "bsc-mainnet": "https://bscscan.com",
    "bsc-testnet": "https://testnet.bscscan.com",
}


def _require_api_key(provided: str | None) -> None:
    """Same gate as the control routes, and unset means closed, not open."""
    expected = os.environ.get("SERVICE_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="dashboard is disabled: set $SERVICE_API_KEY to enable it.",
        )
    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def _section(name: str, build: Callable[[], Any]) -> Any:
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 — one bad section must not blank the page
        log.warning("dashboard section %s failed: %s: %s", name, type(exc).__name__, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _agent() -> dict[str, Any]:
    state = strat.load_state()
    out: dict[str, Any] = {
        "name": "BNB LP Range Rebalancer",
        "status": state["status"],
        "network": strat.NETWORK,
        "explorer": EXPLORER.get(strat.NETWORK, "https://bscscan.com"),
        "wallet": strat.agent_id(),
        "monitor_running": strat.is_monitor_running(),
        "last_check": state["last_check"],
        "poll_seconds": strat.POLL_SECONDS,
        # Unset SERVICE_API_KEY 503s /activate /pause /execute. Surfaced here
        # because a paused agent and a locked-out agent look identical from the
        # outside and are fixed in completely different places.
        "controls_enabled": bool(os.environ.get("SERVICE_API_KEY")),
        "config_problems": risk.check_config_consistency(),
    }
    try:
        w3 = chain._w3(strat.NETWORK)
        out["chain_id"] = w3.eth.chain_id
        out["block"] = w3.eth.block_number
        out["rpc"] = "up"
        problems = risk.protocol_problems(strat.NETWORK)
        out["protocol_problems"] = problems
        out["protocol"] = "unavailable" if problems else "up"
    except Exception as e:  # noqa: BLE001 — the header must render without a chain
        out["rpc"] = "down"
        out["rpc_error"] = str(e)
    return out


def _wallet() -> dict[str, Any]:
    """Signer balances. The gas line is the one that silently ends the agent.

    A rebalance is four transactions; a wallet that cannot pay for them leaves
    the position stranded outside its range, and nothing else on this page says
    so.
    """
    owner = strat.agent_id()
    if not owner.startswith("0x"):
        return {"error": "no wallet unlocked"}
    addr = chain._addresses(strat.NETWORK, int(chain.strategy_config()["fee"]))
    w3 = chain._w3(strat.NETWORK)
    return {
        "address": owner,
        "bnb": w3.eth.get_balance(w3.to_checksum_address(owner)) / 1e18,
        "wbnb": strat._balance(addr["wbnb"], owner) / 1e18,
        "usdt": strat._balance(addr["usdt"], owner) / 1e18,
        "gas_price_gwei": w3.eth.gas_price / 1e9,
    }


def _trigger() -> dict[str, Any]:
    """Where the bounds are, and where the agent would put them today."""
    cfg = dict(chain.strategy_config())
    price = chain.get_bnb_price(strat.NETWORK)["price_usdt_per_bnb"]
    token_id = strat.current_token_id()
    # get_lp_current_range already folds calculate_range_metrics into its own
    # return, so the trigger prices come back from one call. Its price keys are
    # suffixed `_usdt_per_bnb`; get_position_summary is what renames them to the
    # short forms the /status payload uses.
    live = chain.get_lp_current_range(token_id, strat.NETWORK)
    target = chain.calculate_rebalance_range(price, float(cfg["range_pct"]))
    return {
        "range_pct": float(cfg["range_pct"]),
        "trigger_pct": float(cfg["trigger_pct"]),
        "fee_tier": int(cfg["fee"]),
        "max_slippage_pct": float(cfg["max_slippage_pct"]),
        "lower_price": live["lower_price_usdt_per_bnb"],
        "upper_price": live["upper_price_usdt_per_bnb"],
        "trigger_lower_price": live["trigger_lower_price"],
        "trigger_upper_price": live["trigger_upper_price"],
        "target_lower_price": target["lower_price"],
        "target_upper_price": target["upper_price"],
        # trigger_pct is percent OF RANGE WIDTH measured inward from each bound,
        # not percent of price — the only reading that reproduces spec 4.3.
        "trigger_semantics": "percent of range width, inward from each bound",
    }


def _identity() -> dict[str, Any]:
    """What a buyer resolves before paying, read from the same config the agent signs with."""
    toml = chain._studio_toml()
    ident = toml.get("identity") or {}
    pay = (toml.get("payments") or {}).get("erc8183") or {}
    price_wei = str(pay.get("price") or "0")
    return {
        "agent_id": ident.get("agent_id"),
        "endpoint": ident.get("endpoint"),
        "registered_at": ident.get("registered_at"),
        "registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        "public_url": os.environ.get("PUBLIC_URL"),
        "erc8183_url": os.environ.get("ERC8183_AGENT_URL"),
        "currency": pay.get("currency"),
        "price_u": int(price_wei) / 1e18 if price_wei.isdigit() else None,
        "quote_ttl_seconds": pay.get("quote_ttl_seconds"),
    }


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    """The shell. Inert until an operator supplies a key, so it needs no gate."""
    if not DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not deployed")
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@router.get("/dashboard/data")
def dashboard_data(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Everything the page draws, in one gated read."""
    _require_api_key(x_api_key)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": _section("agent", _agent),
        "position": _section("position", strat.get_status),
        "trigger": _section("trigger", _trigger),
        "performance": _section("performance", strat.get_performance),
        "wallet": _section("wallet", _wallet),
        "identity": _section("identity", _identity),
        "clients": _section("clients", termix.clients),
    }
