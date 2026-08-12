"""Service Layer REST API — spec sections 8 and 9.

The second half of the spec section 2 two-layer architecture. The Agent Layer
(``app/agent``) owns the LLM, the strategy, the risk engine and the key. This
layer owns the PUBLIC surface: status, performance, positions, transactions,
and the three control actions. It holds no key and builds no calldata — every
route below delegates to ``strategy.py``, which is the same code the autonomous
monitor loop runs.

    Agent Layer                     Service Layer  (this file)
      strategy.py  <-- delegates --   api.py
      risk.py                         /status /strategy /performance
      lp_signing.py                   /positions /transactions
      blockchain.py                   /activate /pause /execute

## Why the control routes are gated

``/activate``, ``/pause`` and ``/execute`` move real funds or start the loop
that does. They require ``X-API-Key`` matching ``$SERVICE_API_KEY``, and when
that variable is UNSET they refuse with 503 rather than running open — a public
endpoint that anyone can POST ``/execute`` to is a rebalance anyone can trigger,
and a rebalance costs gas and crosses the spread every time. Read routes are
open: they expose nothing that is not already public on-chain.

Run:  python main.py          (or: uvicorn api:app --port 8080)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# The Agent Layer uses flat module imports (`import strategy`), matching the
# layout `bag init` generates. Put it on the path rather than restructuring the
# agent into a package — the deploy bundle ships app/agent as-is.
AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import blockchain as chain  # noqa: E402
import risk  # noqa: E402
import strategy as strat  # noqa: E402

SERVICE_NAME = "bnb-lp-range-rebalancer"
VERSION = "1.0.0"

app = FastAPI(
    title="BNB LP Range Rebalancer",
    version=VERSION,
    description=(
        "Autonomous PancakeSwap V3 BNB/USDT concentrated-liquidity rebalancer. "
        "Service Layer of the BNB Agent Studio two-layer architecture."
    ),
)


# --- Auth for the control routes ------------------------------------------------
def _require_api_key(provided: str | None) -> None:
    expected = os.environ.get("SERVICE_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "control routes are disabled: set $SERVICE_API_KEY to enable "
                "/activate, /pause and /execute. Refusing to run them unauthenticated "
                "because they move funds."
            ),
        )
    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def _fail(e: Exception, status: int = 502) -> JSONResponse:
    """Chain/RPC failures are reported, never swallowed into a fake success."""
    return JSONResponse(status_code=status, content={"error": str(e), "type": type(e).__name__})


# --- Read routes (spec 8) -------------------------------------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + the things that silently break an agent.

    Deliberately more than ``{"status":"ok"}``: it reports RPC reachability,
    whether a managed position is configured, and any cross-field config
    problem the risk engine finds. A rebalancer that is "up" but pointed at the
    wrong chain is not healthy, and only this composite answer shows that.
    """
    out: dict[str, Any] = {
        "service": SERVICE_NAME,
        "version": VERSION,
        "network": strat.NETWORK,
        "status": "ok",
    }
    try:
        w3 = chain._w3(strat.NETWORK)
        out["chain_id"] = w3.eth.chain_id
        out["block"] = w3.eth.block_number
        out["rpc"] = "up"
    except Exception as e:  # noqa: BLE001
        out["rpc"] = "down"
        out["status"] = "degraded"
        out["rpc_error"] = str(e)

    try:
        out["token_id"] = strat.current_token_id()
    except Exception as e:  # noqa: BLE001
        out["token_id"] = None
        out["status"] = "degraded"
        out["position_error"] = str(e)

    problems = risk.check_config_consistency()
    out["config_problems"] = problems
    if problems:
        out["status"] = "degraded"

    # Spec 15 "protocol unavailable": an address with no code on this chain
    # fails every call identically and forever, and reads as a confusing ABI
    # error rather than a missing contract. Only checked when the RPC is up —
    # otherwise every role reports a failure that is really the RPC's.
    if out.get("rpc") == "up":
        protocol = risk.protocol_problems(strat.NETWORK)
        out["protocol_problems"] = protocol
        out["protocol"] = "unavailable" if protocol else "up"
        if protocol:
            out["status"] = "degraded"

    # `monitor_running` is a thread liveness check and nothing more — a loop
    # failing every pass still reports true. `last_check` is when a pass last
    # COMPLETED, so a stale timestamp against an active strategy is the real
    # "the monitor is wedged" signal.
    state = strat.load_state()
    out["monitor_running"] = strat.is_monitor_running()
    out["strategy_status"] = state["status"]
    out["last_check"] = state["last_check"]
    return out


@app.get("/status")
def status() -> Any:
    """The spec 4.6 marketplace payload."""
    try:
        return strat.get_status()
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@app.get("/strategy")
def strategy_config() -> Any:
    """Active strategy parameters (spec 4.3) and what they currently imply."""
    try:
        cfg = dict(chain.strategy_config())
        # token_ids is a per-network map; report the one actually in force so
        # the API answers "which position is this agent managing right now"
        # rather than making the caller resolve it.
        cfg["token_id"] = strat.current_token_id()
        price = chain.get_bnb_price(strat.NETWORK)["price_usdt_per_bnb"]
        target = chain.calculate_rebalance_range(price, float(cfg["range_pct"]))
        metrics = chain.calculate_range_metrics(
            price, target["lower_price"], target["upper_price"], float(cfg["trigger_pct"])
        )
        return {
            "network": strat.NETWORK,
            "status": strat.load_state()["status"],
            "parameters": cfg,
            "current_price": price,
            "target_range_if_rebalanced_now": {
                "lower_price": target["lower_price"],
                "upper_price": target["upper_price"],
                "trigger_lower_price": metrics["trigger_lower_price"],
                "trigger_upper_price": metrics["trigger_upper_price"],
            },
            # trigger_pct is 5% OF RANGE WIDTH measured inward from each bound —
            # the only reading that reproduces spec 4.3's own 637/763 example.
            "trigger_semantics": "percent of full range width, inward from each bound",
        }
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@app.get("/performance")
def performance() -> Any:
    """Fees, gas, PnL and rebalance history (spec 4.7 ``getPerformance``)."""
    try:
        return strat.get_performance()
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@app.get("/positions")
def positions() -> Any:
    """The managed position, verified against live chain state."""
    try:
        token_id = strat.current_token_id()
        return {
            "network": strat.NETWORK,
            "count": 1,  # spec 4.2: exactly one pair in v1
            "positions": [{
                **chain.get_lp_position(token_id, strat.NETWORK),
                "value": chain.get_position_value(token_id, strat.NETWORK),
                "pending_fees": chain.get_pending_fees(token_id, strat.NETWORK),
                "verification": chain.verify_position(token_id, network=strat.NETWORK),
            }],
        }
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@app.get("/transactions")
def transactions(limit: int = 50) -> Any:
    """Rebalance history with the spec 14 log fields.

    Every entry carries its own tx hashes, so this doubles as the spec 19
    blockchain-evidence trail.
    """
    try:
        history = strat.load_state()["history"]
        explorer = ("https://bscscan.com/tx/" if strat.NETWORK == "bsc-mainnet"
                    else "https://testnet.bscscan.com/tx/")
        entries = []
        for item in history[-limit:]:
            entries.append({**item, "explorer_urls": [explorer + h for h in item.get("txs", [])]})
        return {"network": strat.NETWORK, "count": len(entries), "transactions": entries}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# --- Metadata (spec 9) ----------------------------------------------------------
@app.get("/metadata")
def metadata() -> dict[str, Any]:
    """Shared metadata document — the same shape for all four agents (spec 9)."""
    return {
        "name": "BNB LP Range Rebalancer",
        "slug": SERVICE_NAME,
        "version": VERSION,
        "category": "rebalancing",
        "description": (
            "Monitors a PancakeSwap V3 BNB/USDT concentrated-liquidity position "
            "and rebalances it when price approaches a range boundary."
        ),
        "protocol": "PancakeSwap V3",
        "protocols": ["PancakeSwap V3"],
        "pair": "BNB/USDT",
        "pairs": ["BNB/USDT"],
        "networks": chain.supported_networks(),
        "default_network": strat.NETWORK,
        "wallet": strat.agent_id(),
        "capabilities": [
            "monitor_position", "calculate_range", "detect_rebalance_condition",
            "decrease_liquidity", "collect_fees", "swap_tokens", "mint_position",
            "increase_liquidity", "verify_position",
        ],
        "actions": sorted(strat.ACTIONS),
        "endpoints": {
            "health": "/health", "status": "/status", "strategy": "/strategy",
            "performance": "/performance", "positions": "/positions",
            "transactions": "/transactions", "metadata": "/metadata",
            "activate": "POST /activate", "pause": "POST /pause",
            "execute": "POST /execute",
        },
        "agent_protocol": "A2A",
        "agent_card": "/.well-known/agent-card.json",
        "payment": {"standard": "ERC-8183", "identity": "ERC-8004"},
        "risk_controls": [
            "address allowlist from shared config/bsc-contracts.json",
            "gas price ceiling",
            "quote-derived slippage floors on every value-moving leg",
            "exact-amount approvals, never unlimited",
            "managed-position and owner checks",
            "single-flight rebalance lock",
            "pause / emergency stop",
        ],
    }


# --- Control routes (spec 8) ----------------------------------------------------
@app.post("/activate")
def activate(x_api_key: str | None = Header(default=None)) -> Any:
    """Start autonomous monitoring (spec 4.7 ``activate``)."""
    _require_api_key(x_api_key)
    try:
        result = strat.activate()
        strat.start_monitor()
        return result
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@app.post("/pause")
def pause(x_api_key: str | None = Header(default=None)) -> Any:
    """Emergency stop (spec 4.7 ``pause``, spec 22 pause/emergency-stop).

    In-flight work finishes; nothing new starts. Deliberately does NOT kill the
    monitor thread mid-rebalance — abandoning a sequence between the withdraw
    and the re-mint is worse than letting it land.
    """
    _require_api_key(x_api_key)
    try:
        return strat.pause()
    except Exception as e:  # noqa: BLE001
        return _fail(e)


@app.post("/execute")
def execute(force: bool = False, x_api_key: str | None = Header(default=None)) -> Any:
    """Run a rebalance now (spec 4.7 ``rebalance``).

    Without ``force``, the deterministic check still decides: the request is
    refused with the reason when no rebalance is due. ``force=true`` is the
    manual override and is the only way to rebalance an in-range position.
    """
    _require_api_key(x_api_key)
    try:
        return strat.rebalance(force=force)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
