"""PancakeSwap V3 WRITE path — fixed code, never an LLM tool.

Companion to ``signing.py``: that file signs ERC-8183 money ops, this one signs
the LP operations (wrap / approve / swap / mint / decrease / collect). Both are
FIXED entrypoint code. Neither appears in ``tools.py``, so the LLM can never
call them — it decides *whether* to rebalance, deterministic code decides *what
calldata that means*. That is spec section 3's whole requirement.

## Where the guards live

The SDK's ``SigningPolicy`` gates ``sign_typed_data`` (EIP-712) only —
``sign_transaction`` is NOT policy-checked. Every call here is a plain
transaction, so the policy will not stop a bad one. ``risk.py`` is therefore the
real boundary, and this module calls into it before every send:

* ``risk.require_allowed_address`` — no transaction is built to any address
  outside the shared address book.
* ``risk.require_gas_price`` — bounded gas price; gas limits are fixed here.
* ``risk.min_out_from_quote`` / ``risk.liquidity_amount_floors`` — every
  value-moving leg carries a floor derived from live chain state.
* ``risk.require_managed_position`` — no NFT but this agent's is ever modified.

Approvals are EXACT-amount, never unlimited (an unlimited approve to a router is
the classic way an agent wallet gets drained later), and every call carries a
deadline.

None of these read LLM output. All inputs are numbers this module computed or
that came from studio.toml.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from web3 import Web3

from bnbagent_studio_core.wallet import get_wallet

import blockchain as pcs
import risk

log = logging.getLogger("seller-agent.lp")

DEADLINE_SECONDS = 600
DEFAULT_GAS_LIMIT = 900_000

WBNB_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "payable",
     "inputs": [], "outputs": []},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256"}], "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
]

QUOTER_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "address", "name": "tokenIn"}, {"type": "address", "name": "tokenOut"},
         {"type": "uint256", "name": "amountIn"}, {"type": "uint24", "name": "fee"},
         {"type": "uint160", "name": "sqrtPriceLimitX96"}]}],
     "outputs": [{"type": "uint256", "name": "amountOut"}, {"type": "uint160"},
                 {"type": "uint32"}, {"type": "uint256"}]},
]

ROUTER_ABI = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "address", "name": "tokenIn"}, {"type": "address", "name": "tokenOut"},
         {"type": "uint24", "name": "fee"}, {"type": "address", "name": "recipient"},
         {"type": "uint256", "name": "deadline"}, {"type": "uint256", "name": "amountIn"},
         {"type": "uint256", "name": "amountOutMinimum"},
         {"type": "uint160", "name": "sqrtPriceLimitX96"}]}],
     "outputs": [{"type": "uint256", "name": "amountOut"}]},
]

NPM_WRITE_ABI = [
    {"name": "mint", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "address", "name": "token0"}, {"type": "address", "name": "token1"},
         {"type": "uint24", "name": "fee"}, {"type": "int24", "name": "tickLower"},
         {"type": "int24", "name": "tickUpper"},
         {"type": "uint256", "name": "amount0Desired"},
         {"type": "uint256", "name": "amount1Desired"},
         {"type": "uint256", "name": "amount0Min"}, {"type": "uint256", "name": "amount1Min"},
         {"type": "address", "name": "recipient"}, {"type": "uint256", "name": "deadline"}]}],
     "outputs": [{"type": "uint256", "name": "tokenId"}, {"type": "uint128", "name": "liquidity"},
                 {"type": "uint256", "name": "amount0"}, {"type": "uint256", "name": "amount1"}]},
    {"name": "increaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "uint256", "name": "tokenId"},
         {"type": "uint256", "name": "amount0Desired"},
         {"type": "uint256", "name": "amount1Desired"},
         {"type": "uint256", "name": "amount0Min"}, {"type": "uint256", "name": "amount1Min"},
         {"type": "uint256", "name": "deadline"}]}],
     "outputs": [{"type": "uint128", "name": "liquidity"},
                 {"type": "uint256", "name": "amount0"}, {"type": "uint256", "name": "amount1"}]},
    {"name": "decreaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "uint256", "name": "tokenId"}, {"type": "uint128", "name": "liquidity"},
         {"type": "uint256", "name": "amount0Min"}, {"type": "uint256", "name": "amount1Min"},
         {"type": "uint256", "name": "deadline"}]}],
     "outputs": [{"type": "uint256", "name": "amount0"}, {"type": "uint256", "name": "amount1"}]},
    {"name": "collect", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "tuple", "name": "params", "components": [
         {"type": "uint256", "name": "tokenId"}, {"type": "address", "name": "recipient"},
         {"type": "uint128", "name": "amount0Max"}, {"type": "uint128", "name": "amount1Max"}]}],
     "outputs": [{"type": "uint256", "name": "amount0"}, {"type": "uint256", "name": "amount1"}]},
]

ERC20_WRITE_ABI = WBNB_ABI  # approve/allowance/balanceOf are the shared subset


# --- Guards --------------------------------------------------------------------
# Thin alias so call sites read the same as before; the check itself lives in
# risk.py (spec 2's Risk Engine), which is the only place it may be changed.
_require_allowed = risk.require_allowed_address


def _deadline() -> int:
    return int(time.time()) + DEADLINE_SECONDS


# --- Transaction plumbing ------------------------------------------------------
def _gas_limit(fn, sender: str, value: int, fallback: int) -> int:
    """Gas limit for a call: the node's estimate + headroom, else ``fallback``.

    Spec 15 names "gas estimation failure" as its own error class. Estimation is
    a node call and fails for reasons that have nothing to do with the
    transaction — a lagging or rate-limited RPC (B8), or a state the estimator
    cannot simulate. Falling back to the hand-set per-op limit degrades to the
    previous behaviour instead of aborting a rebalance mid-sequence, where the
    liquidity is already withdrawn and stopping is the expensive option.

    A ceiling failure is NOT swallowed: that means the estimate itself was
    absurd, which is a reason to stop rather than to proceed on a guess.
    """
    try:
        estimate = fn.estimate_gas({"from": sender, "value": value})
    except Exception as e:  # noqa: BLE001 — estimation is advisory, not a gate
        log.warning("gas estimation failed for %s (%s); using fixed limit %s",
                    getattr(fn, "fn_name", "?"), e, fallback)
        return fallback
    limit = risk.gas_limit_from_estimate(estimate)
    log.debug("gas for %s: estimated %s, sending with %s",
              getattr(fn, "fn_name", "?"), estimate, limit)
    return limit


def _send(network: str, fn, *, value: int = 0, gas: int = DEFAULT_GAS_LIMIT) -> dict[str, Any]:
    """Build → sign → broadcast → wait for a contract call. Raises on revert.

    ``gas`` is the FALLBACK limit, used only when estimation fails.
    """
    w3 = pcs._w3(network)
    wallet = get_wallet()
    sender = Web3.to_checksum_address(wallet.address)

    gas_price = risk.require_gas_price(w3)
    gas = _gas_limit(fn, sender, value, fallback=gas)

    tx = fn.build_transaction({
        "from": sender,
        # 'pending', not the default 'latest'. These ops run back-to-back
        # (approve then swap then mint), and a node that has not yet surfaced
        # the previous tx in 'latest' hands back a stale nonce -> the next send
        # dies with "nonce too low" halfway through a sequence.
        "nonce": w3.eth.get_transaction_count(sender, "pending"),
        "gas": gas,
        "gasPrice": gas_price,
        "value": value,
        "chainId": w3.eth.chain_id,
    })

    # Simulate first: a revert here costs nothing, a revert on-chain costs gas
    # and leaves a confusing receipt.
    try:
        w3.eth.call({k: v for k, v in tx.items() if k != "nonce"})
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"transaction would revert, not sending: {e}") from e

    signed = wallet.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed["rawTransaction"])
    log.info("sent %s -> %s", fn.fn_name, tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"transaction reverted on-chain: {tx_hash.hex()}")
    return {
        "tx_hash": tx_hash.hex(),
        "gas_used": receipt["gasUsed"],          # gas UNITS
        "gas_cost_wei": receipt["gasUsed"] * gas_price,  # actual BNB spent
        "block": receipt["blockNumber"],
    }


def _contract(network: str, address: str, abi: list):
    return pcs._w3(network).eth.contract(address=_require_allowed(network, address), abi=abi)


# --- Operations ----------------------------------------------------------------
def wrap_bnb(amount_wei: int, network: str | None = None) -> dict[str, Any]:
    """Wrap native tBNB/BNB into WBNB (the pool trades WBNB, not native)."""
    network = network or pcs.default_network()
    if amount_wei <= 0:
        raise ValueError("amount_wei must be positive")
    wbnb = _contract(network, pcs._cfg(network)["wbnb"], WBNB_ABI)
    return _send(network, wbnb.functions.deposit(), value=amount_wei, gas=120_000)


def approve_exact(token: str, spender: str, amount_wei: int,
                  network: str | None = None) -> dict[str, Any] | None:
    """Approve EXACTLY ``amount_wei`` — never unlimited.

    Returns None when the existing allowance already covers the amount.
    """
    network = network or pcs.default_network()
    if amount_wei <= 0:
        raise ValueError("amount_wei must be positive")
    wallet_addr = Web3.to_checksum_address(get_wallet().address)
    spender_addr = _require_allowed(network, spender)
    erc20 = _contract(network, token, ERC20_WRITE_ABI)

    current = erc20.functions.allowance(wallet_addr, spender_addr).call()
    if current >= amount_wei:
        return None
    # Some tokens refuse a non-zero -> non-zero approve; zero it first.
    if current > 0:
        _send(network, erc20.functions.approve(spender_addr, 0), gas=100_000)
    return _send(network, erc20.functions.approve(spender_addr, amount_wei), gas=100_000)


def quote_swap(token_in: str, token_out: str, amount_in_wei: int,
               network: str | None = None) -> int:
    """Live quote for an exact-input single-pool swap. Read-only (``eth_call``)."""
    network = network or pcs.default_network()
    cfg = pcs._cfg(network)
    quoter = _contract(network, cfg["quoter_v2"], QUOTER_ABI)
    out = quoter.functions.quoteExactInputSingle((
        _require_allowed(network, token_in),
        _require_allowed(network, token_out),
        int(amount_in_wei), int(cfg["fee"]), 0,
    )).call()
    return int(out[0])


def execute_swap(token_in: str, token_out: str, amount_in_wei: int,
                 max_slippage_pct: float | None = None,
                 network: str | None = None) -> dict[str, Any]:
    """Swap exact input through the V3 SwapRouter with a quote-derived floor.

    ``amountOutMinimum`` comes from a live quote minus ``max_slippage_pct``, so
    a sandwich or a moving pool reverts the swap instead of filling it at any
    price. On the shallow testnet pool the QUOTE ITSELF already reflects large
    price impact — the guard bounds movement between quote and execution, not
    the impact of the trade size. Keep test sizes small.
    """
    network = network or pcs.default_network()
    cfg = pcs._cfg(network)
    if max_slippage_pct is None:
        max_slippage_pct = risk.slippage_pct("swap")

    quoted = quote_swap(token_in, token_out, amount_in_wei, network)
    min_out = risk.min_out_from_quote(quoted, max_slippage_pct)

    approve_exact(token_in, cfg["swap_router"], amount_in_wei, network)
    router = _contract(network, cfg["swap_router"], ROUTER_ABI)
    recipient = Web3.to_checksum_address(get_wallet().address)
    result = _send(network, router.functions.exactInputSingle((
        _require_allowed(network, token_in),
        _require_allowed(network, token_out),
        int(cfg["fee"]), recipient, _deadline(),
        int(amount_in_wei), min_out, 0,
    )), gas=400_000)
    result.update({"quoted_out": quoted, "min_out": min_out,
                   "amount_in": int(amount_in_wei)})
    return result


def mint_position(amount_usdt_wei: int, amount_wbnb_wei: int,
                  lower_price: float, upper_price: float,
                  amount0_min: int | None = None, amount1_min: int | None = None,
                  network: str | None = None) -> dict[str, Any]:
    """Mint a new concentrated-liquidity position over a BNB price range.

    ``lower_price``/``upper_price`` are USDT per BNB; the tick conversion (and
    its inversion, since token0 is USDT) happens in
    :func:`blockchain.price_range_to_ticks`.

    When ``amount0_min``/``amount1_min`` are None they are DERIVED: predict the
    deposit the contract will actually take (the same liquidity math it uses
    internally) and floor it by ``[strategy].mint_slippage_pct``. A flat
    percentage of the *desired* amounts would be wrong — for a range sitting to
    one side of spot the contract legitimately consumes almost none of one
    token, so a naive floor would revert every such mint.
    """
    network = network or pcs.default_network()
    cfg = pcs._cfg(network)
    ticks = pcs.price_range_to_ticks(lower_price, upper_price, network)
    recipient = Web3.to_checksum_address(get_wallet().address)

    if amount0_min is None or amount1_min is None:
        slip = risk.slippage_pct("mint")
        sqrt_price_x96 = int(pcs._pool(network).functions.slot0().call()[0])
        liq = pcs.amounts_to_liquidity(
            amount_usdt_wei, amount_wbnb_wei, sqrt_price_x96,
            ticks["tick_lower"], ticks["tick_upper"],
        )
        floor0, floor1 = risk.liquidity_amount_floors(
            liq, sqrt_price_x96, ticks["tick_lower"], ticks["tick_upper"], slip
        )
        amount0_min = floor0 if amount0_min is None else amount0_min
        amount1_min = floor1 if amount1_min is None else amount1_min
        log.info("mint mins: floor (%s, %s) at %.2f%%", amount0_min, amount1_min, slip)

    if amount_usdt_wei > 0:
        approve_exact(cfg["usdt"], cfg["position_manager"], amount_usdt_wei, network)
    if amount_wbnb_wei > 0:
        approve_exact(cfg["wbnb"], cfg["position_manager"], amount_wbnb_wei, network)

    npm = _contract(network, cfg["position_manager"], NPM_WRITE_ABI)
    params = (
        _require_allowed(network, cfg["usdt"]),   # token0
        _require_allowed(network, cfg["wbnb"]),   # token1
        int(cfg["fee"]), ticks["tick_lower"], ticks["tick_upper"],
        int(amount_usdt_wei), int(amount_wbnb_wei),
        int(amount0_min), int(amount1_min),
        recipient, _deadline(),
    )
    result = _send(network, npm.functions.mint(params), gas=900_000)
    result.update(ticks)

    # The tokenId is in the Transfer log; read it back rather than guessing.
    w3 = pcs._w3(network)
    receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
    transfer_topic = w3.keccak(text="Transfer(address,address,uint256)")
    npm_addr = Web3.to_checksum_address(cfg["position_manager"])
    for entry in receipt["logs"]:
        if entry["address"] == npm_addr and entry["topics"][0] == transfer_topic:
            result["token_id"] = int(entry["topics"][3].hex(), 16)
            break
    return result


def increase_liquidity(token_id: int, amount_usdt_wei: int, amount_wbnb_wei: int,
                       amount0_min: int | None = None, amount1_min: int | None = None,
                       network: str | None = None) -> dict[str, Any]:
    """Add liquidity to the EXISTING position, keeping its range and its NFT.

    Spec 4.1 lists ``increaseLiquidity()`` among the required LP operations. The
    rebalance flow does not use it — a rebalance changes the range, which means
    a new position — but topping up an in-range position is the cheaper path
    when the range is still good: no burn, no re-mint, no new token_id, and the
    accrued fees stay put.

    Mins are derived exactly as in :func:`mint_position`.
    """
    network = network or pcs.default_network()
    if amount_usdt_wei <= 0 and amount_wbnb_wei <= 0:
        raise ValueError("nothing to add: both amounts are zero")
    cfg = pcs._cfg(network)
    pos = pcs.get_lp_position(token_id, network)
    risk.require_managed_position(pos, "increase")

    if amount0_min is None or amount1_min is None:
        slip = risk.slippage_pct("mint")
        sqrt_price_x96 = int(pcs._pool(network).functions.slot0().call()[0])
        liq = pcs.amounts_to_liquidity(
            amount_usdt_wei, amount_wbnb_wei, sqrt_price_x96,
            pos["tick_lower"], pos["tick_upper"],
        )
        floor0, floor1 = risk.liquidity_amount_floors(
            liq, sqrt_price_x96, pos["tick_lower"], pos["tick_upper"], slip
        )
        amount0_min = floor0 if amount0_min is None else amount0_min
        amount1_min = floor1 if amount1_min is None else amount1_min
        log.info("increase mins: floor (%s, %s) at %.2f%%", amount0_min, amount1_min, slip)

    if amount_usdt_wei > 0:
        approve_exact(cfg["usdt"], cfg["position_manager"], amount_usdt_wei, network)
    if amount_wbnb_wei > 0:
        approve_exact(cfg["wbnb"], cfg["position_manager"], amount_wbnb_wei, network)

    npm = _contract(network, cfg["position_manager"], NPM_WRITE_ABI)
    result = _send(network, npm.functions.increaseLiquidity((
        int(token_id), int(amount_usdt_wei), int(amount_wbnb_wei),
        int(amount0_min), int(amount1_min), _deadline(),
    )), gas=500_000)
    result["token_id"] = int(token_id)
    return result


def decrease_liquidity(token_id: int, liquidity: int, amount0_min: int | None = None,
                       amount1_min: int | None = None,
                       network: str | None = None) -> dict[str, Any]:
    """Remove ``liquidity`` from a position. Tokens land in the position's
    owed balance — ``collect_fees`` then sweeps them to the wallet.

    Like :func:`mint_position`, the mins are DERIVED when not given: predict the
    payout at the live tick and floor it by ``[strategy].max_slippage_pct``.
    Withdrawing with mins of 0 (the old default) was the one unprotected leg of
    the rebalance — the token split of a V3 withdrawal depends on the current
    tick, so a searcher who pushes the price to a bound in the same block makes
    the position pay out entirely in whichever token just became the cheap side,
    then restores the price. Pass 0 explicitly to opt out.
    """
    network = network or pcs.default_network()
    if liquidity <= 0:
        raise ValueError("liquidity must be positive")
    pos = pcs.get_lp_position(token_id, network)
    risk.require_managed_position(pos, "modify")
    if liquidity > pos["liquidity"]:
        raise ValueError(f"position has {pos['liquidity']} liquidity, asked to remove {liquidity}")

    if amount0_min is None or amount1_min is None:
        slip = risk.slippage_pct("swap")
        sqrt_price_x96 = int(pcs._pool(network).functions.slot0().call()[0])
        floor0, floor1 = risk.liquidity_amount_floors(
            liquidity, sqrt_price_x96, pos["tick_lower"], pos["tick_upper"], slip
        )
        amount0_min = floor0 if amount0_min is None else amount0_min
        amount1_min = floor1 if amount1_min is None else amount1_min
        log.info("decrease mins: floor (%s, %s) at %.2f%%", amount0_min, amount1_min, slip)

    npm = _contract(network, pcs._cfg(network)["position_manager"], NPM_WRITE_ABI)
    return _send(network, npm.functions.decreaseLiquidity(
        (int(token_id), int(liquidity), int(amount0_min), int(amount1_min), _deadline())
    ), gas=400_000)


def collect_fees(token_id: int, network: str | None = None) -> dict[str, Any]:
    """Sweep all owed tokens (accrued fees + anything freed by
    ``decrease_liquidity``) from the position to the wallet."""
    network = network or pcs.default_network()
    pos = pcs.get_lp_position(token_id, network)
    risk.require_managed_position(pos, "collect")
    npm = _contract(network, pcs._cfg(network)["position_manager"], NPM_WRITE_ABI)
    recipient = Web3.to_checksum_address(get_wallet().address)
    return _send(network, npm.functions.collect(
        (int(token_id), recipient, pcs.MAX_UINT128, pcs.MAX_UINT128)
    ), gas=400_000)
