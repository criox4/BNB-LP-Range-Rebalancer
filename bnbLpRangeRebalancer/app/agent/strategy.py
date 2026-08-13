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

import contextlib
import fcntl
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from web3 import Web3

import lp_signing as lp
import blockchain as pcs
import state_store
import risk

log = logging.getLogger("seller-agent.strategy")

NETWORK = pcs.default_network()


@lru_cache(maxsize=1)
def agent_id() -> str:
    """Stable identifier for this agent in logs (spec 14 ``agent_id``).

    The wallet address is the real on-chain identity and is the same value the
    ERC-8004 registration resolves to, so log lines join to chain activity
    without a lookup table. Falls back to the project name when no wallet is
    unlocked (local reads, CI).
    """
    try:
        from bnbagent_studio_core.wallet import get_wallet

        return str(get_wallet().address)
    except Exception:  # noqa: BLE001 — a log label must never break the agent
        return "bnbLpRangeRebalancer"


# Per-network state: a testnet run must never be read as mainnet history
# (different chain, different token_id, different money).
#
# $LP_STATE_DIR relocates it onto durable storage. A DIRECTORY, deliberately not
# a full file path: the filename carries the network, and letting an operator
# name the file directly is how mainnet and testnet end up sharing one — which
# would hand a mainnet rebalance the testnet token_id.
#
# This matters wherever the process filesystem is not durable. On AgentCore the
# microVM is reclaimed after 15 min idle (or 8h max), taking the state file with
# it; the agent would then come back `paused` with no history, and — because the
# state file is the single source of truth for token_id (B10) — fall back to the
# BOOTSTRAP token_id in studio.toml, i.e. manage whichever NFT a past rebalance
# already emptied. Point this at a mounted volume there.
STATE_DIR = Path(os.environ.get("LP_STATE_DIR") or Path(__file__).parent)
STATE_PATH = STATE_DIR / f".lp_state.{NETWORK}.json"   # legacy; migrated once
DB_PATH = STATE_DIR / f"lp_state.{NETWORK}.db"
POLL_SECONDS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Persistent state ----------------------------------------------------------
# SQLite (state_store.py). Was a whole-file JSON rewrite; it was replaced because
# writes were non-atomic on a 60s path — a truncated file reads as "start fresh",
# which resets token_id to the studio.toml bootstrap and points the agent at a
# position a past rebalance already emptied (B10).
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
    "snapshots": [],
}
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _store() -> state_store.StateStore:
    """The SQLite store, created on first use and migrated from JSON once.

    Legacy `.lp_state.<network>.json` beside the database is imported on the
    first open (guarded on the DB being empty, so it cannot double-import).
    The old file is left in place, untouched, as a manual rollback path.
    """
    store = state_store.StateStore(DB_PATH, _DEFAULT_STATE)
    try:
        if store.migrate_from_json(STATE_PATH):
            log.info("migrated legacy state %s -> %s", STATE_PATH.name, DB_PATH.name)
    except (OSError, ValueError) as e:
        # Refusing here is the point: importing nothing would silently reset
        # token_id to the studio.toml bootstrap value (B10).
        raise RuntimeError(
            f"legacy state {STATE_PATH} exists but is unreadable ({e}); refusing "
            f"to start with empty state — fix or move the file, then restart"
        ) from e
    return store


def load_state() -> dict[str, Any]:
    state = _store().load()
    if state.get("token_id") is None:
        try:
            state["token_id"] = pcs.managed_token_id()
        except ValueError:
            pass
    return state


def _update(**fields) -> dict[str, Any]:
    """Set scalar fields. Append-only lists go through the store's own helpers.

    The in-process lock stays: it makes read-modify-write of the SAME field
    (``rebalance_count + 1``) atomic between this process's threads. SQLite's
    transaction covers the write, not the read that computed the value.
    """
    with _lock:
        _store().update(**fields)
        return load_state()


def current_token_id() -> int:
    """The LP NFT this agent manages — ONE source of truth, the state file.

    A rebalance mints a new NFT and writes the state file first; pushing the id
    back into ``studio.toml`` is best-effort and logs-and-continues on failure
    (see :func:`_persist_token_id`). Anything that read ``[strategy].token_id``
    directly would therefore act on the OLD, emptied position after such a
    failure while ``get_status`` reported on the new one. ``load_state`` seeds
    itself from studio.toml when the state file has no id, so the toml value is
    still the bootstrap — just never a second live answer.
    """
    token_id = int(load_state().get("token_id") or 0)
    if token_id <= 0:
        raise ValueError(
            "no managed LP position: set [strategy].token_id in studio.toml "
            "after minting one (see `python mint_position.py`)"
        )
    return token_id


# A rebalance moves real funds in a multi-transaction sequence, so exactly one
# may run at a time. ``_lock`` is not enough: it is a threading.Lock, and the
# documented operator path (`python strategy.py rebalance --force`) is a SECOND
# PROCESS racing the server's monitor thread. Two concurrent runs each read the
# same non-zero liquidity and would decrease/swap/mint twice.
LOCK_PATH = Path(str(STATE_PATH) + ".lock")


@contextlib.contextmanager
def _rebalance_lock():
    with open(LOCK_PATH, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "another rebalance is already in progress (lock held on "
                f"{LOCK_PATH}) — refusing to run a second one"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- Fee / PnL accounting ------------------------------------------------------
# Spec 4.6 wants fees_24h and pnl. Neither is readable from chain state alone:
# the pool exposes only fees pending RIGHT NOW, and a rebalance resets that to
# zero by collecting. So the agent samples its own running total over time and
# differences the samples. That makes fees_24h exact only back to the first
# snapshot — before the agent started watching, it is blind.
# ponytail: snapshots in the state file; an indexer would give true history.
SNAPSHOT_KEEP = 500
SNAPSHOT_MIN_GAP_SECONDS = 300  # don't fill the file on a 60s poll


def _to_usdt(usdt: float, bnb: float, price: float) -> float:
    return usdt + bnb * price


def fees_earned(state: dict[str, Any], pending: dict[str, Any]) -> tuple[float, float]:
    """``(usdt, bnb)`` earned in fees so far: collected plus still-pending.

    Kept as two token amounts, NOT a single USDT value, because the BNB side
    revalues with the price. See :func:`_fees_since`.
    """
    return (
        state["fees_collected_usdt"] + pending.get("fees_usdt", 0.0),
        state["fees_collected_bnb"] + pending.get("fees_bnb", 0.0),
    )


def fees_earned_usdt(state: dict[str, Any], pending: dict[str, Any], price: float) -> float:
    """Everything this agent has earned in fees, valued at ``price``."""
    return _to_usdt(*fees_earned(state, pending), price)


def _record_snapshot(fees_usdt: float, fees_bnb: float,
                     tvl_usdt: float, price: float) -> None:
    """Append a fee/TVL sample, rate-limited and bounded.

    Both fee token amounts are stored. Storing only their combined USDT value
    (what this did originally) made the 24h window unusable: differencing two
    values taken at different BNB prices books the revaluation of the ENTIRE
    historical BNB fee balance as fees earned in the window.

    One INSERT + a bounded DELETE, in one transaction. Previously this returned
    the whole capped list to be written back, so a 60s tick rewrote up to 500
    samples to record one.
    """
    now = time.time()
    last = _store().last_snapshot_ts()
    if last is not None and (now - last) < SNAPSHOT_MIN_GAP_SECONDS:
        return
    _store().append_snapshot(
        {"ts": now, "at": _now(), "fees_usdt": fees_usdt,
         "fees_bnb": fees_bnb, "tvl_usdt": tvl_usdt, "price": price},
        keep=SNAPSHOT_KEEP,
    )


def _fees_since(snaps: list[dict[str, Any]], seconds: float, current_usdt: float,
                current_bnb: float, price: float) -> tuple[float, bool]:
    """``(fees earned in the window, window_is_complete)``.

    Each token side is differenced separately and only the DELTA is valued at
    the current price, so a BNB price move does not masquerade as fee income.

    ``window_is_complete`` is False when the agent has not been watching for the
    full window — the figure is then a floor, not a 24h total, and callers must
    say so rather than presenting a partial number as complete.

    Snapshots written before the two-sided format are skipped rather than
    guessed at; that only costs a shorter window until they age out.
    """
    snaps = [s for s in snaps if "fees_bnb" in s]
    if not snaps:
        return (0.0, False)

    def delta(s: dict[str, Any]) -> float:
        return max(0.0, _to_usdt(current_usdt - float(s["fees_usdt"]),
                                 current_bnb - float(s["fees_bnb"]), price))

    cutoff = time.time() - seconds
    older = [s for s in snaps if float(s["ts"]) <= cutoff]
    if older:
        return (delta(older[-1]), True)
    return (delta(snaps[0]), False)


# --- The decision (deterministic; no LLM) --------------------------------------
def check() -> dict[str, Any]:
    """One Monitor-loop pass: read chain state, decide, do not act.

    Also records the fee/TVL snapshot that fees_24h and pnl are derived from.
    """
    token_id = current_token_id()
    summary = pcs.get_position_summary(token_id, NETWORK)
    try:
        value = pcs.get_position_value(token_id, NETWORK)
        state = load_state()
        f_usdt, f_bnb = fees_earned(state, summary["pending_fees"])
        _record_snapshot(f_usdt, f_bnb, value.get("tvl_usdt", 0.0),
                         summary["current_price"])
        _update(last_check=_now())
    except Exception as e:  # noqa: BLE001 — accounting must not break monitoring
        log.warning("snapshot failed: %s", e)
        _update(last_check=_now())
    return summary


# --- The six user actions (spec 4.7) -------------------------------------------
def activate() -> dict[str, Any]:
    """Start autonomous monitoring."""
    current_token_id()  # refuse to activate without a managed position
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
    price = s["current_price"]
    try:
        tvl = pcs.get_position_value(s["token_id"], NETWORK).get("tvl_usdt", 0.0)
    except Exception:  # noqa: BLE001
        tvl = 0.0
    f_usdt, f_bnb = fees_earned(state, fees)
    earned = _to_usdt(f_usdt, f_bnb, price)
    gas_usdt = state["gas_spent_wei"] / 1e18 * price
    fees_24h, complete_24h = _fees_since(
        state.get("snapshots") or [], 86400, f_usdt, f_bnb, price
    )
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
        # Raw V3 liquidity units — NOT a token amount and NOT a money value.
        # Named explicitly because an LLM handed a bare `liquidity: 3.35e17`
        # reported it as "TVL: 335,389.79 BNB". Money lives in `tvl` below.
        "liquidity_raw": s["liquidity"]["position_liquidity"],
        "pending_fees_usdt": fees.get("fees_usdt", 0.0),
        "pending_fees_bnb": fees.get("fees_bnb", 0.0),
        # spec 4.6 economics
        "tvl": tvl,
        "fees_24h": fees_24h,
        # False => the agent has been watching for less than 24h, so fees_24h is
        # a floor over a shorter window. Never present it as a full day.
        "fees_24h_window_complete": complete_24h,
        "fees_total": earned,
        "pnl": earned - gas_usdt,
        "gas_cost": gas_usdt,
        "rebalance_count": state["rebalance_count"],
        "last_rebalance": state["last_rebalance"],
        # Beyond spec 4.6, and the only evidence a monitor pass actually
        # COMPLETED. `monitor_running` says a thread exists; a thread that
        # throws on every pass still counts as running. This moves, or the loop
        # is not doing its job — the field the operator should alert on.
        "last_check": state["last_check"],
        "gas_cost_bnb": state["gas_spent_wei"] / 1e18,
    }


def get_status_report() -> str:
    """The position status as a finished, human-readable report.

    Every figure here is formatted by CODE. The LLM is told to quote this
    verbatim rather than compute or reformat numbers itself, because when it
    was handed the raw dict it reported the raw liquidity integer as "TVL:
    335,389.79 BNB" (real TVL: $0.81) and invented pending-fee values that were
    twelve orders of magnitude too large. Deterministic figures are the whole
    product for a paid status report — this is the same principle as spec
    section 3, applied to numbers the agent SAYS rather than numbers it signs.
    """
    s = get_status()
    if "error" in s:
        return f"LP Rebalancer status unavailable: {s['error']}"

    def money(v: float, unit: str = "USDT") -> str:
        if v == 0:
            return f"0 {unit}"
        return f"{v:.8f} {unit}".rstrip("0").rstrip(".") if abs(v) < 0.01 \
            else f"{v:,.4f} {unit}"

    window = "" if s["fees_24h_window_complete"] else " (partial window — the agent " \
                                                      "has been watching for under 24h)"
    return "\n".join([
        "BNB LP Rebalancer — PancakeSwap V3 BNB/USDT",
        f"  network            : {s['network']}",
        f"  status             : {s['status']}",
        f"  position (NFT)     : {s['token_id']}",
        f"  current price      : {money(s['current_price'])} per BNB",
        f"  range              : {money(s['lower_price'])} - {money(s['upper_price'])}",
        f"  range utilization  : {s['range_utilization']:.2f}% "
        f"(0% = centred, 100% = at a bound)",
        f"  in range           : {'yes' if s['in_range'] else 'NO'}",
        f"  rebalance required : {'YES' if s['rebalance_required'] else 'no'} "
        f"({s['rebalance_reason']})",
        f"  TVL                : {money(s['tvl'])}",
        f"  fees (24h)         : {money(s['fees_24h'])}{window}",
        f"  fees (total)       : {money(s['fees_total'])}",
        f"  gas spent          : {money(s['gas_cost'])}",
        f"  net PnL            : {money(s['pnl'])}",
        f"  rebalances         : {s['rebalance_count']} "
        f"(last: {s['last_rebalance'] or 'never'})",
    ])


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
    price, tvl = 0.0, 0.0
    try:
        pending = pcs.get_pending_fees(state["token_id"], NETWORK)
        price = pcs.get_bnb_price(NETWORK)["price_usdt_per_bnb"]
        tvl = pcs.get_position_value(state["token_id"], NETWORK).get("tvl_usdt", 0.0)
    except Exception:  # noqa: BLE001
        pass
    f_usdt, f_bnb = fees_earned(state, pending)
    earned = _to_usdt(f_usdt, f_bnb, price)
    gas_usdt = state["gas_spent_wei"] / 1e18 * price
    fees_24h, complete_24h = _fees_since(
        state.get("snapshots") or [], 86400, f_usdt, f_bnb, price
    )
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
        "fees_total_value_usdt": earned,
        "fees_24h_usdt": fees_24h,
        "fees_24h_window_complete": complete_24h,
        "gas_spent_usdt": gas_usdt,
        "pnl_usdt": earned - gas_usdt,
        "tvl_usdt": tvl,
        "snapshots_recorded": len(state.get("snapshots") or []),
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

    token_id = current_token_id()
    summary = pcs.get_position_summary(token_id, NETWORK)
    if not summary["rebalance_required"] and not force:
        return {"rebalanced": False, "reason": summary["rebalance_reason"],
                "range_utilization": summary["range_utilization"]}

    with _rebalance_lock():
        return _do_rebalance(token_id, summary, get_wallet)


def _do_rebalance(token_id: int, summary: dict[str, Any], get_wallet) -> dict[str, Any]:
    """The fund-moving sequence itself. Only ever called under
    :func:`_rebalance_lock`."""
    owner = Web3.to_checksum_address(get_wallet().address)
    cfg = pcs._cfg(NETWORK)
    # Before anything is withdrawn: confirm the protocol is actually there.
    # Failing here is free; failing after decrease_liquidity leaves the position
    # dismantled with nowhere to mint it back.
    risk.require_protocol_available(NETWORK)
    pos = pcs.get_lp_position(token_id, NETWORK)
    risk.require_position_owner(pos, owner)
    risk.require_managed_position(pos, "rebalance")

    # Spec 14 log fields: what went IN to the rebalance, in token terms.
    #
    # The underlying amounts, NOT tokens_owed0/1. V3 only refreshes tokensOwed
    # when the position is touched, so on an untouched position they read 0 and
    # the log claimed a rebalance consumed nothing — while get_pending_fees(),
    # which simulates a collect, showed real fees. get_position_value derives
    # the amounts from liquidity at the live tick, which is what actually moves.
    value = pcs.get_position_value(token_id, NETWORK)
    input_amount = {
        "usdt": value.get("amount_usdt", 0.0),
        "bnb": value.get("amount_bnb", 0.0),
        "liquidity_raw": pos["liquidity"],
        "tvl_usdt": value.get("tvl_usdt", 0.0),
    }

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
    # Decimals are read from the tokens, not assumed to be 18. They ARE 18 for
    # BSC-USDT on both chains, but a hardcoded 1e18 against a 6-decimal stable
    # would be off by 1e12 — the imbalance test would always trip and the swap
    # size would be clamped to the entire balance every single rebalance.
    d_usdt = pcs._decimals(NETWORK, cfg["usdt"])
    d_wbnb = pcs._decimals(NETWORK, cfg["wbnb"])
    usdt, wbnb = _balance(cfg["usdt"], owner), _balance(cfg["wbnb"], owner)
    wbnb_value_usdt = wbnb / 10**d_wbnb * price
    usdt_value = usdt / 10**d_usdt
    gap = abs(wbnb_value_usdt - usdt_value)
    if gap > 0.05 * max(wbnb_value_usdt + usdt_value, 1e-9):  # >5% off balance
        if wbnb_value_usdt > usdt_value:
            swap_wei = int((gap / 2) / price * 10**d_wbnb)
            if swap_wei > 0:
                r = lp.execute_swap(cfg["wbnb"], cfg["usdt"], min(swap_wei, wbnb), network=NETWORK)
                txs.append(r["tx_hash"]); gas += r["gas_cost_wei"]
        else:
            swap_wei = int((gap / 2) * 10**d_usdt)
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

    # 6. Verify the replacement — spec 4.4's "Verify new position" and 4.8's
    # "New LP position is verified". Re-reads chain state rather than trusting
    # the mint's return values: a mint can succeed and still leave a position
    # that is empty or already out of range.
    check_result = pcs.verify_position(new_id, minted["tx_hash"], NETWORK)
    verified = pcs.get_position_summary(new_id, NETWORK)
    if not check_result["verified"]:
        log.error("new position %s FAILED verification: %s",
                  new_id, check_result["problems"])

    state = load_state()
    # Spec 14 log fields: agent_id, input_amount, output_amount, error.
    entry = {"at": _now(), "from_token_id": token_id, "to_token_id": new_id,
             "reason": summary["rebalance_reason"], "price": price,
             "lower": verified["lower_price"], "upper": verified["upper_price"],
             "txs": txs,
             "agent_id": agent_id(),
             "action": "rebalance",
             "input_amount": input_amount,
             "output_amount": {
                 "liquidity_raw": check_result["liquidity"],
                 "tvl_usdt": check_result["tvl_usdt"],
                 "lower_price": check_result["lower_price"],
                 "upper_price": check_result["upper_price"],
             },
             "gas_cost_wei": gas,
             "verified": check_result["verified"],
             "error": "; ".join(check_result["problems"]) or None}
    _update(
        token_id=new_id,
        rebalance_count=state["rebalance_count"] + 1,
        last_rebalance=entry["at"],
        gas_spent_wei=state["gas_spent_wei"] + gas,
        fees_collected_usdt=state["fees_collected_usdt"] + collected.get("fees_usdt", 0.0),
        fees_collected_bnb=state["fees_collected_bnb"] + collected.get("fees_bnb", 0.0),
    )
    _store().append_history(entry)
    _persist_token_id(new_id)

    return {"rebalanced": True, "old_token_id": token_id, "new_token_id": new_id,
            "reason": summary["rebalance_reason"], "txs": txs, "gas_cost_wei": gas,
            "new_range": [verified["lower_price"], verified["upper_price"]],
            "in_range": verified["in_range"], "verification": check_result}


def _persist_token_id(token_id: int, path: Path | None = None) -> None:
    """Point [strategy].token_id at the replacement position.

    Convenience only — :func:`current_token_id` reads the state file, so a
    failure here no longer splits the agent's idea of which NFT it manages. It
    keeps a fresh checkout (empty state file) pointed at the right position.

    Scoped to the ``[strategy]`` table: a bare "first line starting with
    token_id" scan would rewrite an unrelated ``token_id`` in any earlier
    section.
    """
    path = path or Path(__file__).parent / "studio.toml"
    try:
        lines = path.read_text().splitlines(keepends=True)
        section = ""
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("["):
                section = stripped.strip("[]")
                continue
            if section == "strategy" and stripped.startswith("token_id"):
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


# A failing pass must not be retried at full speed. A rebalance that dies at the
# mint leaves the liquidity already withdrawn, so every retry re-sends collect
# (and possibly a swap) before failing again — at a 60s poll that is ~1440 paid
# transactions a day against a wallet holding a couple of dollars of gas.
MAX_BACKOFF_MULTIPLIER = 32  # 60s -> ~32min between attempts at the ceiling


def _loop() -> None:
    failures = 0
    while not _stop.is_set():
        try:
            if load_state()["status"] == "active":
                summary = check()
                log.info("check: price=%.4f util=%.1f%% required=%s",
                         summary["current_price"], summary["range_utilization"],
                         summary["rebalance_required"])
                if summary["rebalance_required"]:
                    log.info("rebalance result: %s", rebalance())
            failures = 0
        except Exception as e:  # noqa: BLE001 — the loop must survive any single failure
            failures += 1
            log.exception("monitor pass failed (%s consecutive): %s", failures, e)
        backoff = min(2 ** failures, MAX_BACKOFF_MULTIPLIER) if failures else 1
        if backoff > 1:
            log.warning("backing off %ss before the next pass", POLL_SECONDS * backoff)
        _stop.wait(POLL_SECONDS * backoff)


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


def is_monitor_running() -> bool:
    """Whether THIS process is running the loop.

    Per-process, not global: the agent and the service are separate processes
    sharing a state file, so a False here means "not in this process", not
    "nobody is monitoring". /health reports it alongside the strategy status
    for exactly that reason.
    """
    return bool(_thread and _thread.is_alive())


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
