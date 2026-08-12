"""Risk engine — the deterministic gate between a decision and a signature.

Spec section 2 names this layer; spec section 3.1 defines what it is for:

    LLM -> Strategy Engine -> RISK ENGINE -> Validated Action
        -> Protocol Adapter -> Transaction Builder -> Wallet -> BSC

Every check here is a hard refusal, not a warning. Nothing in this file reads
LLM output — the inputs are numbers computed by ``strategy.py`` or read from
``studio.toml`` and the shared address book.

## Why these particular checks

The SDK's ``SigningPolicy`` gates ``sign_typed_data`` (EIP-712) only.
``sign_transaction`` is NOT policy-checked, and every LP operation is a plain
transaction — so for the whole rebalance path, THIS FILE is the boundary. There
is nothing behind it.

Each check exists because of a specific way this agent could lose funds:

* ``require_allowed_address`` — a wrong or injected address is the difference
  between approving PancakeSwap's router and approving an attacker's.
* ``require_gas_price`` — a gas spike (or a lying node) turning a $0.02
  rebalance into a $50 one.
* ``require_managed_position`` — the NFT contract holds EVERY V3 position on
  the chain, so a wrong token_id decodes cleanly into a stranger's position.
* ``require_position_owner`` — acting on an NFT the agent does not own.
* ``min_out_from_quote`` / ``liquidity_amount_floors`` — every value-moving leg
  carries a floor derived from live state. A leg without one is a leg a
  searcher can take the difference on.
* ``check_config_consistency`` — cross-field config errors that per-field
  validation cannot see (a mainnet agent signing quotes in the testnet token).
"""
from __future__ import annotations

import logging
from typing import Any

from web3 import Web3

import blockchain as chain

log = logging.getLogger("seller-agent.risk")

# BSC mainnet sits well under 1 gwei and testnet ~1-3. A ceiling this far above
# normal only ever fires on a genuine anomaly, which is exactly when a
# rebalance should wait rather than proceed.
MAX_GAS_PRICE_GWEI = 20

# Ceiling on a single transaction's gas LIMIT. Paired with the price ceiling
# above, this bounds what one transaction can cost: 20 gwei x 2M = 0.04 BNB.
# Needed because the limit is now estimated from the node rather than fixed —
# an absurd estimate (broken node, or a call that would loop) must not become an
# absurd transaction. The largest real op here is mint at ~900k.
MAX_GAS_LIMIT = 2_000_000

# Headroom over the node's estimate. Estimation runs against the CURRENT state;
# by the time the transaction lands, ticks may have moved and a swap may cross
# an extra initialised tick. Too tight and it reverts out-of-gas after paying.
GAS_ESTIMATE_BUFFER_PCT = 25

# Contracts this agent may transact with, by role in the shared address book.
# Anything else is refused before a transaction is built.
ALLOWED_ROLES = (
    "factory", "position_manager", "quoter_v2", "swap_router", "wbnb", "usdt", "pool",
)


class ProtocolUnavailable(RuntimeError):
    """A contract this agent depends on is not usable on this chain.

    Spec 15 names "protocol unavailable" as its own error class, and it needs to
    be distinguishable: an RPC failure is worth retrying, a reverting call is
    worth reporting, but a protocol that is not deployed at the configured
    address will fail identically forever, and the fix is config, not patience.

    This is not hypothetical. ``config/bsc-contracts.json`` records two
    addresses published in PancakeSwap's own docs that have NO CODE on mainnet,
    and a QuoterV2 that answers on one network and not the other. Calling one of
    those returns empty data, which web3 decodes as a confusing ABI error rather
    than "this contract does not exist".
    """


def protocol_problems(network: str) -> list[str]:
    """Roles in the address book with no contract code on ``network``.

    One ``eth_getCode`` per role. Cheap enough for a health check, and the only
    check that distinguishes "wrong address" from "bad call".
    """
    problems: list[str] = []
    try:
        cfg = chain._cfg(network)
        w3 = chain._w3(network)
    except Exception as e:  # noqa: BLE001
        return [f"cannot reach {network}: {e}"]

    for role in ALLOWED_ROLES:
        address = cfg.get(role)
        if not address:
            problems.append(f"{role} missing from the address book for {network}")
            continue
        try:
            if w3.eth.get_code(Web3.to_checksum_address(address)) in (b"", b"0x"):
                problems.append(
                    f"{role} {address} has no code on {network} — the address is "
                    f"wrong for this chain, or the protocol is not deployed here"
                )
        except Exception as e:  # noqa: BLE001 — an RPC failure is a DIFFERENT class
            problems.append(f"could not read code for {role} {address}: {e}")
    return problems


def require_protocol_available(network: str) -> None:
    """Refuse to start a fund-moving sequence against a protocol that isn't there."""
    problems = protocol_problems(network)
    if problems:
        raise ProtocolUnavailable(
            f"PancakeSwap V3 is not usable on {network}: " + "; ".join(problems)
        )


# --- Address allowlist ---------------------------------------------------------
def allowed_addresses(network: str) -> set[str]:
    cfg = chain._cfg(network)
    return {str(cfg[role]).lower() for role in ALLOWED_ROLES}


def require_allowed_address(network: str, address: str) -> str:
    """Refuse any address outside the verified shared address book.

    Returns the checksummed address so callers can use it directly.
    """
    if address.lower() not in allowed_addresses(network):
        raise PermissionError(
            f"refusing to transact with {address} on {network}: not in the "
            f"verified address book ({chain.CONTRACTS_FILENAME})"
        )
    return Web3.to_checksum_address(address)


# --- Gas ------------------------------------------------------------------------
def require_gas_price(w3) -> int:
    """Current gas price, refused if above the ceiling."""
    gas_price = w3.eth.gas_price
    if gas_price > Web3.to_wei(MAX_GAS_PRICE_GWEI, "gwei"):
        raise RuntimeError(
            f"gas price {Web3.from_wei(gas_price, 'gwei')} gwei exceeds the "
            f"{MAX_GAS_PRICE_GWEI} gwei ceiling — refusing to send"
        )
    return gas_price


def gas_limit_from_estimate(estimate: int) -> int:
    """Buffered gas limit for an estimate, refused if the result is absurd.

    The limit is not what you pay — that is gas *used* — but it is what the
    balance must cover, and an unbounded limit from a misbehaving node turns a
    routine send into one the wallet cannot afford.
    """
    limit = int(estimate * (1 + GAS_ESTIMATE_BUFFER_PCT / 100.0))
    if limit > MAX_GAS_LIMIT:
        raise RuntimeError(
            f"estimated gas {estimate} (+{GAS_ESTIMATE_BUFFER_PCT}% = {limit}) "
            f"exceeds the {MAX_GAS_LIMIT} ceiling — refusing to send"
        )
    return limit


# --- Position identity ----------------------------------------------------------
def require_managed_position(pos: dict[str, Any], action: str = "modify") -> None:
    """Refuse to act on an NFT that is not this agent's BNB/USDT position."""
    if not pos.get("is_managed_pair"):
        raise PermissionError(
            f"token_id {pos.get('token_id')} is not the managed "
            f"{chain.strategy_config()['pair']} fee-{chain.strategy_config()['fee']} "
            f"position — refusing to {action}"
        )


def require_position_owner(pos: dict[str, Any], owner: str) -> None:
    """Refuse to act on an NFT this agent does not own."""
    actual = pos.get("owner")
    if actual is None or actual.lower() != owner.lower():
        raise PermissionError(
            f"position {pos.get('token_id')} is owned by {actual}, not this agent ({owner})"
        )


# --- Slippage floors ------------------------------------------------------------
def min_out_from_quote(quoted: int, slippage_pct: float) -> int:
    """Floor for a swap output, from a live quote.

    Note this bounds movement BETWEEN quote and execution, not the price impact
    of the trade itself — the quote already reflects that. On a shallow pool,
    keep sizes small regardless.
    """
    if quoted <= 0:
        raise RuntimeError("quoter returned zero output — no liquidity for this size")
    return int(quoted * (1 - slippage_pct / 100.0))


def liquidity_amount_floors(liquidity: int, sqrt_price_x96: int, tick_lower: int,
                            tick_upper: int, slippage_pct: float) -> tuple[int, int]:
    """``(amount0Min, amount1Min)`` for a mint or a withdrawal.

    Derived from the amounts the contract will actually move at the live tick,
    NOT a flat percentage of the desired amounts. For a range sitting to one
    side of spot the contract legitimately touches almost none of one token, so
    a flat floor would revert every such call.
    """
    exp0, exp1 = chain.liquidity_to_amounts(liquidity, sqrt_price_x96, tick_lower, tick_upper)
    floor = 1 - slippage_pct / 100.0
    return (int(exp0 * floor), int(exp1 * floor))


def slippage_pct(kind: str = "swap") -> float:
    """``[strategy]`` slippage tolerance. ``kind`` is ``swap`` or ``mint``."""
    cfg = chain.strategy_config()
    key = "mint_slippage_pct" if kind == "mint" else "max_slippage_pct"
    return float(cfg.get(key, 1.0))


# --- Config consistency ---------------------------------------------------------
def check_config_consistency(network: str | None = None) -> list[str]:
    """Config that is internally inconsistent in ways nothing else catches.

    Exists because of a real bug: switching ``[network].default`` to mainnet
    left ``[payments.erc8183].currency`` at the scaffold's prefilled TESTNET
    token, so the agent signed chain-56 quotes payable in a token that does not
    exist on mainnet. Every layer was individually correct — only the
    combination was wrong, and nothing was positioned to notice.

    Returns a list of problem strings (empty when clean).
    """
    from bnbagent.networks import BNB_CHAIN_ADDRESSES

    net = network or chain.default_network()
    problems: list[str] = []
    try:
        cfg = chain._studio_toml()
    except Exception as e:  # noqa: BLE001
        return [f"studio.toml unreadable: {e}"]

    chain_id = {"bsc-mainnet": 56, "bsc-testnet": 97}.get(net)
    if chain_id is None:
        return [f"unknown network {net!r}"]

    configured = str(
        ((cfg.get("payments") or {}).get("erc8183") or {}).get("currency") or ""
    )
    expected = BNB_CHAIN_ADDRESSES[chain_id].payment_token
    if configured and configured.lower() != expected.lower():
        problems.append(
            f"[payments.erc8183].currency {configured} is not the U token for "
            f"{net} (chain {chain_id}); expected {expected}. Quotes would be "
            f"signed for a token that does not exist on this chain."
        )

    # The shared address book must agree with the network we think we're on.
    if net in chain.supported_networks():
        try:
            book_chain_id = int(chain._cfg(net)["chain_id"])
            if book_chain_id != chain_id:
                problems.append(
                    f"{chain.CONTRACTS_FILENAME} lists chain_id {book_chain_id} "
                    f"for {net}, but {net} is chain {chain_id}"
                )
        except Exception as e:  # noqa: BLE001
            problems.append(f"address book unreadable for {net}: {e}")

    token_id = int(((cfg.get("strategy") or {}).get("token_id")) or 0)
    if token_id and net in chain.supported_networks():
        try:
            pos = chain.get_lp_position(token_id, net)
            if not pos["is_managed_pair"]:
                problems.append(
                    f"[strategy].token_id {token_id} is not a "
                    f"{chain.strategy_config()['pair']} fee-{chain._cfg(net)['fee']} "
                    f"position on {net} — token IDs are per-network and this one "
                    f"does not belong to this chain."
                )
        except Exception as e:  # noqa: BLE001
            problems.append(f"[strategy].token_id {token_id} unreadable on {net}: {e}")
    return problems
