"""Rebalancing strategy: the monitor loop and the six user actions.

Spec section 4.4's Monitor loop and section 4.7's
``activate/pause/rebalance/getStatus/getPosition/getPerformance``.

The division of labour required by spec section 3 is load-bearing here:

    LLM            -> may explain or summarise, and nothing else
    strategy.py    -> decides WHETHER to rebalance (deterministic, this file)
    lp_signing.py  -> decides WHAT CALLDATA that means, and signs it

No LLM output reaches a transaction. ``rebalance()`` recomputes the decision
from live chain state before touching the write path, so even a caller that
asks for a rebalance at the wrong moment gets refused unless ``force=True``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from web3 import Web3

import lp_signing as lp
import pancake as pcs

log = logging.getLogger("seller-agent.strategy")

NETWORK = pcs.default_network()
# Per-network state: a testnet run must never be read as mainnet history
# (different chain, different token_id, different money).
STATE_PATH = Path(__file__).parent / f".lp_state.{NETWORK}.json"
POLL_SECONDS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Persistent state ----------------------------------------------------------
# Small JSON file. ponytail: a file is enough for one position on one runtime;
# move to real storage if this ever manages several.
_DEFAULT_STATE: dict[str, Any] = {
    "status": "paused",          # active | paused
    "token_id": None,
    "rebalance_count": 0,
    "last_rebalance": None,
    "last_check": None,
    "gas_spent_wei": 0,
    "fees_collected_usdt": 0.0,
    "fees_collected_bnb": 0.0,
    "history": [],
}
_lock = threading.Lock()


def load_state() -> dict[str, Any]:
    state = dict(_DEFAULT_STATE)
    try:
        state.update(json.loads(STATE_PATH.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 — corrupt state must not brick the agent
        log.warning("state unreadable (%s); starting fresh", e)
    if state.get("token_id") is None:
        try:
            state["token_id"] = pcs.managed_token_id()
        except ValueError:
            pass
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _update(**fields) -> dict[str, Any]:
    with _lock:
        state = load_state()
        state.update(fields)
        save_state(state)
        return state


# --- The decision (deterministic; no LLM) --------------------------------------
def check() -> dict[str, Any]:
    """One Monitor-loop pass: read chain state, decide, do not act."""
    token_id = pcs.managed_token_id()
    summary = pcs.get_position_summary(token_id, NETWORK)
    _update(last_check=_now())
    return summary


# --- The six user actions (spec 4.7) -------------------------------------------
def activate() -> dict[str, Any]:
    """Start autonomous monitoring."""
    pcs.managed_token_id()  # refuse to activate without a managed position
    state = _update(status="active")
    log.info("activated (token_id=%s)", state["token_id"])
    return {"status": "active", "token_id": state["token_id"], "since": _now()}


def pause() -> dict[str, Any]:
    """Stop autonomous monitoring. In-flight work finishes; nothing new starts."""
    _update(status="paused")
    log.info("paused")
    return {"status": "paused", "at": _now()}


def get_status() -> dict[str, Any]:
    """Marketplace status payload (spec 4.6)."""
    state = load_state()
    try:
        s = pcs.get_position_summary(state["token_id"], NETWORK)
    except Exception as e:  # noqa: BLE001 — status must answer even when RPC is down
        return {"agent": "BNB LP Rebalancer", "category": "rebalancing",
                "status": state["status"], "error": str(e)}

    fees = s["pending_fees"]
    return {
        "agent": "BNB LP Rebalancer",
        "category": "rebalancing",
        "protocol": "PancakeSwap V3",
        "pair": "BNB/USDT",
        "network": NETWORK,
        "status": state["status"],
        "token_id": s["token_id"],
        "current_price": s["current_price"],
        "lower_price": s["lower_price"],
        "upper_price": s["upper_price"],
        "range_utilization": s["range_utilization"],
        "in_range": s["in_range"],
        "rebalance_required": s["rebalance_required"],
        "rebalance_reason": s["rebalance_reason"],
        "liquidity": s["liquidity"]["position_liquidity"],
        "pending_fees_usdt": fees.get("fees_usdt", 0.0),
        "pending_fees_bnb": fees.get("fees_bnb", 0.0),
        "rebalance_count": state["rebalance_count"],
        "last_rebalance": state["last_rebalance"],
        "gas_cost_bnb": state["gas_spent_wei"] / 1e18,
    }


def get_position() -> dict[str, Any]:
    """The raw on-chain position record."""
    return pcs.get_lp_position(load_state()["token_id"], NETWORK)


def get_performance() -> dict[str, Any]:
    """Fees earned, gas spent, and rebalance history.

    ponytail: fees_total counts what this agent has COLLECTED plus what is
    currently pending. Fees earned before the agent took over, or collected by
    someone else, are not visible from chain state alone — a full PnL needs
    indexed history.
    """
    state = load_state()
    pending = {"fees_usdt": 0.0, "fees_bnb": 0.0}
    try:
        pending = pcs.get_pending_fees(state["token_id"], NETWORK)
    except Exception:  # noqa: BLE001
        pass
    return {
        "rebalance_count": state["rebalance_count"],
        "last_rebalance": state["last_rebalance"],
        "gas_spent_bnb": state["gas_spent_wei"] / 1e18,
        "fees_collected_usdt": state["fees_collected_usdt"],
        "fees_collected_bnb": state["fees_collected_bnb"],
        "fees_pending_usdt": pending.get("fees_usdt", 0.0),
        "fees_pending_bnb": pending.get("fees_bnb", 0.0),
        "fees_total_usdt": state["fees_collected_usdt"] + pending.get("fees_usdt", 0.0),
        "fees_total_bnb": state["fees_collected_bnb"] + pending.get("fees_bnb", 0.0),
        "history": state["history"][-20:],
    }


# --- The rebalance (spec 4.4) --------------------------------------------------
def _balance(token: str, owner: str) -> int:
    abi = lp.WBNB_ABI if token.lower() == pcs._cfg(NETWORK)["wbnb"].lower() else lp.ERC20_WRITE_ABI
    return pcs._w3(NETWORK).eth.contract(
        address=Web3.to_checksum_address(token), abi=abi
    ).functions.balanceOf(Web3.to_checksum_address(owner)).call()


def rebalance(force: bool = False) -> dict[str, Any]:
    """Recentre the position on the current price.

    decrease -> collect -> rebalance the token ratio -> mint -> verify, which is
    spec 4.4's sequence. Refuses unless the deterministic check says a rebalance
    is due, or ``force=True`` (the manual ``rebalance()`` action of spec 4.7).
    """
    from bnbagent_studio_core.wallet import get_wallet

    token_id = pcs.managed_token_id()
    summary = pcs.get_position_summary(token_id, NETWORK)
    if not summary["rebalance_required"] and not force:
        return {"rebalanced": False, "reason": summary["rebalance_reason"],
                "range_utilization": summary["range_utilization"]}

    owner = Web3.to_checksum_address(get_wallet().address)
    cfg = pcs._cfg(NETWORK)
    pos = pcs.get_lp_position(token_id, NETWORK)
    if pos["owner"] != owner:
        raise PermissionError(f"position {token_id} is owned by {pos['owner']}, not this agent")

    txs: list[str] = []
    gas = 0  # wei actually spent on gas, not gas units
    log.info("rebalancing %s (%s)", token_id, summary["rebalance_reason"])

    # Read fees BEFORE decreasing. decrease_liquidity moves the withdrawn
    # PRINCIPAL into the position's owed balance, so a read taken afterwards
    # reports principal+fees and would book the whole position as fee income.
    collected = pcs.get_pending_fees(token_id, NETWORK)

    # 1. Withdraw all liquidity, 2. sweep it (plus fees) to the wallet.
    if pos["liquidity"] > 0:
        r = lp.decrease_liquidity(token_id, pos["liquidity"], network=NETWORK)
        txs.append(r["tx_hash"]); gas += r["gas_cost_wei"]
    r = lp.collect_fees(token_id, NETWORK)
    txs.append(r["tx_hash"]); gas += r["gas_cost_wei"]

    # 3. New range centred on the live price.
    price = pcs.get_bnb_price(NETWORK)["price_usdt_per_bnb"]
    rng = pcs.calculate_rebalance_range(price, float(pcs.strategy_config()["range_pct"]))

    # 4. Rebalance the token ratio to ~50/50 by value so both sides can be used.
    usdt, wbnb = _balance(cfg["usdt"], owner), _balance(cfg["wbnb"], owner)
    wbnb_value_usdt = wbnb / 1e18 * price
    usdt_value = usdt / 1e18
    gap = abs(wbnb_value_usdt - usdt_value)
    if gap > 0.05 * max(wbnb_value_usdt + usdt_value, 1e-9):  # >5% off balance
        if wbnb_value_usdt > usdt_value:
            swap_wei = int((gap / 2) / price * 1e18)
            if swap_wei > 0:
                r = lp.execute_swap(cfg["wbnb"], cfg["usdt"], min(swap_wei, wbnb), network=NETWORK)
                txs.append(r["tx_hash"]); gas += r["gas_cost_wei"]
        else:
            swap_wei = int((gap / 2) * 1e18)
            if swap_wei > 0:
                r = lp.execute_swap(cfg["usdt"], cfg["wbnb"], min(swap_wei, usdt), network=NETWORK)
                txs.append(r["tx_hash"]); gas += r["gas_cost_wei"]
        usdt, wbnb = _balance(cfg["usdt"], owner), _balance(cfg["wbnb"], owner)

    # 5. Mint the replacement position.
    minted = lp.mint_position(usdt, wbnb, rng["lower_price"], rng["upper_price"], network=NETWORK)
    txs.append(minted["tx_hash"]); gas += minted["gas_cost_wei"]
    new_id = minted.get("token_id")
    if new_id is None:
        raise RuntimeError(f"mint succeeded but token_id unreadable; txs={txs}")

    # 6. Verify the replacement really is in range before calling it done.
    verified = pcs.get_position_summary(new_id, NETWORK)
    if not verified["in_range"]:
        log.warning("new position %s is NOT in range: %s", new_id, verified)

    state = load_state()
    entry = {"at": _now(), "from_token_id": token_id, "to_token_id": new_id,
             "reason": summary["rebalance_reason"], "price": price,
             "lower": verified["lower_price"], "upper": verified["upper_price"],
             "txs": txs}
    _update(
        token_id=new_id,
        rebalance_count=state["rebalance_count"] + 1,
        last_rebalance=entry["at"],
        gas_spent_wei=state["gas_spent_wei"] + gas,
        fees_collected_usdt=state["fees_collected_usdt"] + collected.get("fees_usdt", 0.0),
        fees_collected_bnb=state["fees_collected_bnb"] + collected.get("fees_bnb", 0.0),
        history=[*state["history"], entry],
    )
    _persist_token_id(new_id)

    return {"rebalanced": True, "old_token_id": token_id, "new_token_id": new_id,
            "reason": summary["rebalance_reason"], "txs": txs, "gas_cost_wei": gas,
            "new_range": [verified["lower_price"], verified["upper_price"]],
            "in_range": verified["in_range"]}


def _persist_token_id(token_id: int) -> None:
    """Point [strategy].token_id at the replacement position.

    A rebalance mints a NEW NFT, so without this a restart would go back to
    managing the old, now-empty one.
    """
    path = Path(__file__).parent / "studio.toml"
    try:
        lines = path.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.strip().startswith("token_id"):
                comment = line.split("#", 1)
                suffix = f"  #{comment[1]}" if len(comment) > 1 else "\n"
                lines[i] = f"token_id = {token_id}{suffix if suffix.endswith(chr(10)) else suffix + chr(10)}"
                break
        path.write_text("".join(lines))
        pcs.strategy_config.cache_clear()
    except Exception as e:  # noqa: BLE001 — state file is the source of truth anyway
        log.warning("could not persist token_id to studio.toml: %s", e)


# --- Monitor loop (spec 4.4) ---------------------------------------------------
_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    while not _stop.is_set():
        try:
            if load_state()["status"] == "active":
                summary = check()
                log.info("check: price=%.4f util=%.1f%% required=%s",
                         summary["current_price"], summary["range_utilization"],
                         summary["rebalance_required"])
                if summary["rebalance_required"]:
                    log.info("rebalance result: %s", rebalance())
        except Exception as e:  # noqa: BLE001 — the loop must survive any single failure
            log.exception("monitor pass failed: %s", e)
        _stop.wait(POLL_SECONDS)


def start_monitor() -> None:
    """Start the background Monitor loop (idempotent)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="lp-monitor", daemon=True)
    _thread.start()
    log.info("monitor loop started (poll=%ss)", POLL_SECONDS)


def stop_monitor() -> None:
    _stop.set()


ACTIONS = {
    "activate": activate,
    "pause": pause,
    "rebalance": rebalance,
    "getStatus": get_status,
    "getPosition": get_position,
    "getPerformance": get_performance,
}


if __name__ == "__main__":
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "getStatus"
    if action not in ACTIONS:
        print(f"unknown action {action!r}; one of: {', '.join(ACTIONS)}")
        sys.exit(2)
    kwargs = {"force": True} if action == "rebalance" and "--force" in sys.argv else {}
    print(json.dumps(ACTIONS[action](**kwargs), indent=2, default=str))
