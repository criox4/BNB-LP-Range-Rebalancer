"""PancakeSwap V3 read-only queries + range math for the BNB/USDT LP rebalancer.

READ-ONLY by the studio definition (see tools.py): every function here is an
``eth_call``. Nothing signs, nothing broadcasts. The rebalance write path
(decrease/collect/swap/mint) belongs in ``signing.py`` as fixed code — never a
tool the LLM can invoke.

Contract addresses come from the SHARED address book (config/bsc-contracts.json,
spec 13), never from a table in this file — see `_addresses`.
Two facts that the math depends on, both easy to get backwards:

* ``token0`` is USDT and ``token1`` is WBNB (USDT sorts lower on both BSC
  testnet and mainnet). A V3 tick prices token0 in token1, so the BNB price is
  the *inverse* of the tick price and moves *inversely* to the tick. That means
  ``tickLower`` is the **upper** BNB price bound and vice versa — every
  conversion here goes through ``_bnb_price_from_tick`` so the flip happens in
  exactly one place.
* On testnet the pool is unarbitraged (BNB reads ~16 USDT, not ~700). Nothing
  here may assume a mainnet-like price.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from web3 import Web3

from bnbagent_studio_core.networks import get_network

log = logging.getLogger("seller-agent.blockchain")

# --- Shared BSC address book (spec 13) -----------------------------------------
# Addresses are NOT hardcoded here. Spec 13: "Do NOT allow individual developers
# to independently hardcode or invent protocol addresses. Create a shared
# configuration config/bsc-contracts.json". All four marketplace agents read that
# one file, so a correction lands everywhere at once instead of drifting between
# four private copies. Every address in it was verified by CALLING it on the live
# chain — see the file's own _readme for the two traps that caught us.
#
# Resolution order: $BNB_CONTRACTS_CONFIG, then the nearest config/ directory
# walking up from this file (so it works from the agent dir, the repo root, or a
# deployed bundle that ships config/ alongside app/).
CONTRACTS_FILENAME = "config/bsc-contracts.json"


def _contracts_path() -> Path:
    override = os.environ.get("BNB_CONTRACTS_CONFIG")
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / CONTRACTS_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"shared address book {CONTRACTS_FILENAME} not found above {__file__}; "
        "set $BNB_CONTRACTS_CONFIG or restore the file (spec 13)"
    )


@lru_cache(maxsize=1)
def _contracts() -> dict[str, Any]:
    return json.loads(_contracts_path().read_text())


@lru_cache(maxsize=8)
def _addresses(network: str, fee: int, pair: str = "BNB/USDT") -> dict[str, Any]:
    """Flatten the shared config into the flat lookup the rest of this file uses.

    The pool is selected BY FEE TIER rather than fixed, because mainnet carries
    both a fee-500 and a fee-100 BNB/USDT pool: the fee-100 one is ~2x deeper but
    earns a fifth the rate. Which one this agent uses is a strategy decision
    (``[strategy].fee``), not an address-book fact.
    """
    try:
        net = _contracts()["networks"][network]
    except KeyError:
        raise ValueError(
            f"network {network!r} is not in {CONTRACTS_FILENAME}; "
            f"known: {sorted(_contracts()['networks'])}"
        ) from None

    dex = net["pancakeswap_v3"]
    pools = dex["pools"].get(pair) or []
    match = next((p for p in pools if int(p["fee"]) == int(fee)), None)
    if match is None:
        raise ValueError(
            f"no {pair} fee-{fee} pool listed for {network} in {CONTRACTS_FILENAME}; "
            f"available fees: {sorted(int(p['fee']) for p in pools)}"
        )
    return {
        "chain_id": int(net["chain_id"]),
        "factory": dex["factory"],
        "position_manager": dex["position_manager"],
        "quoter_v2": dex["quoter_v2"],
        "swap_router": dex["swap_router"],
        "wbnb": net["tokens"]["wbnb"],
        "usdt": net["tokens"]["usdt"],
        "pool": match["address"],
        "fee": int(match["fee"]),
    }


def supported_networks() -> list[str]:
    return sorted(_contracts()["networks"])


# --- Agent config ---------------------------------------------------------------
# ALWAYS this file's own studio.toml, never a cwd-relative lookup.
# `load_studio_toml()` with no argument walks up from the CURRENT WORKING
# DIRECTORY. The Service Layer (app/service) has its own studio.toml with no
# [strategy] section, so running from there silently loaded THAT file and fell
# back to default range_pct / trigger_pct / token_id — a service reporting and
# acting on parameters the operator never set. Pinning the path makes the
# answer identical from every process and every cwd.
AGENT_STUDIO_TOML = Path(__file__).resolve().parent / "studio.toml"


def _studio_toml() -> dict[str, Any]:
    from bnbagent_studio_core import config as _config

    return _config.load_studio_toml(AGENT_STUDIO_TOML) or {}


MAX_UINT128 = 2**128 - 1

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"type": "uint160", "name": "sqrtPriceX96"}, {"type": "int24", "name": "tick"},
        {"type": "uint16", "name": "observationIndex"},
        {"type": "uint16", "name": "observationCardinality"},
        {"type": "uint16", "name": "observationCardinalityNext"},
        {"type": "uint32", "name": "feeProtocol"}, {"type": "bool", "name": "unlocked"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
    {"name": "tickSpacing", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "int24"}]},
]

NPM_ABI = [
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256", "name": "tokenId"}], "outputs": [
        {"type": "uint96", "name": "nonce"}, {"type": "address", "name": "operator"},
        {"type": "address", "name": "token0"}, {"type": "address", "name": "token1"},
        {"type": "uint24", "name": "fee"}, {"type": "int24", "name": "tickLower"},
        {"type": "int24", "name": "tickUpper"}, {"type": "uint128", "name": "liquidity"},
        {"type": "uint256", "name": "feeGrowthInside0LastX128"},
        {"type": "uint256", "name": "feeGrowthInside1LastX128"},
        {"type": "uint128", "name": "tokensOwed0"}, {"type": "uint128", "name": "tokensOwed1"}]},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]},
    # `collect` is nonpayable, but eth_call against it (never a transaction) is
    # the standard way to read fees that have accrued but are not yet in
    # tokensOwed. See get_pending_fees.
    {"name": "collect", "type": "function", "stateMutability": "payable", "inputs": [
        {"type": "tuple", "name": "params", "components": [
            {"type": "uint256", "name": "tokenId"}, {"type": "address", "name": "recipient"},
            {"type": "uint128", "name": "amount0Max"}, {"type": "uint128", "name": "amount1Max"}]}],
     "outputs": [{"type": "uint256", "name": "amount0"}, {"type": "uint256", "name": "amount1"}]},
]

ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "string"}]},
]


def _retry_rpc(fn):
    """Retry a chain read a few times before giving up.

    The public BSC endpoints are load balanced, and a node that has not caught
    up answers ``positions()`` for a perfectly valid NFT with
    ``execution reverted: Invalid token ID``. Observed live on mainnet twice,
    while an immediate 20-call re-run passed 20/20 — so it is transient routing,
    not bad state.

    This matters more than a normal flaky read: the caller is deciding whether
    to move real money, and a spurious revert mid-rebalance would abandon the
    sequence between the withdraw and the re-mint. A permanent failure still
    raises after the retries, so a genuinely bad token_id is not masked, just
    slower to report.
    """
    import functools
    import time as _time

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        last: Exception | None = None
        for attempt in range(RPC_RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — retry any RPC-layer failure
                last = e
                if attempt < RPC_RETRIES - 1:
                    _time.sleep(RPC_RETRY_SLEEP * (attempt + 1))
        raise last  # type: ignore[misc]

    return wrapped


RPC_RETRIES = 3
RPC_RETRY_SLEEP = 0.4


def _cfg(network: str) -> dict[str, Any]:
    """Addresses for ``network`` at the fee tier this agent trades (spec 4.2:
    BNB/USDT only in v1)."""
    cfg = strategy_config()
    return _addresses(network, int(cfg["fee"]), str(cfg["pair"]))


@lru_cache(maxsize=4)
def _w3(network: str) -> Web3:
    """Web3 client on studio's configured RPC (honors STUDIO_BSC_*_RPC)."""
    return Web3(Web3.HTTPProvider(get_network(network).rpc_url))


@lru_cache(maxsize=16)
def _decimals(network: str, token: str) -> int:
    c = _w3(network).eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    return int(c.functions.decimals().call())


def _pool(network: str):
    cfg = _cfg(network)
    return _w3(network).eth.contract(
        address=Web3.to_checksum_address(cfg["pool"]), abi=POOL_ABI
    )


def _npm(network: str):
    cfg = _cfg(network)
    return _w3(network).eth.contract(
        address=Web3.to_checksum_address(cfg["position_manager"]), abi=NPM_ABI
    )


# --- Tick <-> BNB price --------------------------------------------------------
# A tick prices token0 in token1: price_raw = 1.0001**tick, in raw base units.
# Human price of token0 in token1 = price_raw * 10**(dec0 - dec1).
# token0 is USDT, so that is WBNB-per-USDT — the BNB price is its reciprocal.
def _bnb_price_from_tick(tick: int, dec0: int = 18, dec1: int = 18) -> float:
    """USDT per BNB at ``tick``. Inverts the tick price (token0 is USDT)."""
    usdt_in_bnb = (1.0001**tick) * (10 ** (dec0 - dec1))
    if usdt_in_bnb <= 0:
        raise ValueError(f"degenerate tick price at tick={tick}")
    return 1.0 / usdt_in_bnb


def _tick_from_bnb_price(price: float, dec0: int = 18, dec1: int = 18) -> int:
    """Inverse of :func:`_bnb_price_from_tick` — USDT-per-BNB to a raw tick."""
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    import math

    usdt_in_bnb = 1.0 / price / (10 ** (dec0 - dec1))
    return int(round(math.log(usdt_in_bnb) / math.log(1.0001)))


def snap_tick(tick: int, spacing: int, *, up: bool) -> int:
    """Round ``tick`` to a multiple of ``spacing``.

    V3 only allows initialized ticks at multiples of the pool's tickSpacing;
    mint reverts otherwise. ``up`` picks the rounding direction so a caller can
    keep a range from silently narrowing past the price it asked for.
    """
    import math

    f = math.ceil if up else math.floor
    return int(f(tick / spacing) * spacing)


def price_range_to_ticks(
    lower_price: float, upper_price: float, network: str | None = None
) -> dict[str, Any]:
    """Convert a BNB price range to the pool's snapped ``(tickLower, tickUpper)``.

    Because token0 is USDT the tick axis runs OPPOSITE to the BNB price, so the
    LOWER price becomes the UPPER tick. Getting this backwards produces an
    inverted range that mint rejects (or, worse, silently accepts as a range
    that never contains the price).
    """
    network = network or default_network()
    if not (upper_price > lower_price):
        raise ValueError(f"upper_price must exceed lower_price ({upper_price} <= {lower_price})")
    cfg = _cfg(network)
    d0 = _decimals(network, cfg["usdt"])
    d1 = _decimals(network, cfg["wbnb"])
    spacing = int(_pool(network).functions.tickSpacing().call())

    # upper PRICE -> lower TICK, and vice versa.
    tick_lower = snap_tick(_tick_from_bnb_price(upper_price, d0, d1), spacing, up=False)
    tick_upper = snap_tick(_tick_from_bnb_price(lower_price, d0, d1), spacing, up=True)
    if tick_lower >= tick_upper:
        raise ValueError(
            f"range too narrow for tickSpacing={spacing}: got "
            f"tick_lower={tick_lower} >= tick_upper={tick_upper}"
        )
    actual_lower, actual_upper = _price_bounds_from_ticks(tick_lower, tick_upper, d0, d1)
    return {
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "tick_spacing": spacing,
        "requested_lower_price": lower_price,
        "requested_upper_price": upper_price,
        # Snapping moves the real bounds — report where they actually landed.
        "actual_lower_price": actual_lower,
        "actual_upper_price": actual_upper,
    }


def _price_bounds_from_ticks(
    tick_lower: int, tick_upper: int, dec0: int = 18, dec1: int = 18
) -> tuple[float, float]:
    """(lower_price, upper_price) in USDT per BNB.

    Sorted, because the tick->BNB-price inversion swaps which bound is which:
    ``tickLower`` yields the HIGHER BNB price.
    """
    a = _bnb_price_from_tick(tick_lower, dec0, dec1)
    b = _bnb_price_from_tick(tick_upper, dec0, dec1)
    return (min(a, b), max(a, b))


# --- Read-only chain queries (LLM-visible) -------------------------------------
@_retry_rpc
def get_bnb_price(network: str | None = None) -> dict[str, Any]:
    """Current BNB price in USDT from the PancakeSwap V3 BNB/USDT pool.

    Reads the pool's live ``slot0`` tick. Note: on BSC testnet this pool is not
    arbitraged against real markets, so the value will not track the real BNB
    price.
    """
    network = network or default_network()
    cfg = _cfg(network)
    pool = _pool(network)
    slot0 = pool.functions.slot0().call()
    tick = int(slot0[1])
    d0 = _decimals(network, cfg["usdt"])
    d1 = _decimals(network, cfg["wbnb"])
    return {
        "network": network,
        "pool": cfg["pool"],
        "fee": cfg["fee"],
        "tick": tick,
        "sqrt_price_x96": str(slot0[0]),
        "price_usdt_per_bnb": _bnb_price_from_tick(tick, d0, d1),
        "pair": "BNB/USDT",
    }


@_retry_rpc
def get_lp_position(token_id: int, network: str | None = None) -> dict[str, Any]:
    """Full PancakeSwap V3 LP position for an NFT ``token_id``.

    Returns the raw position record (tokens, fee tier, tick bounds, liquidity)
    plus the tick bounds converted to BNB price bounds and the owner address.
    """
    network = network or default_network()
    npm = _npm(network)
    p = npm.functions.positions(int(token_id)).call()
    (_nonce, operator, token0, token1, fee, tick_lower, tick_upper, liquidity,
     _fg0, _fg1, owed0, owed1) = p
    d0, d1 = _decimals(network, token0), _decimals(network, token1)
    lower_price, upper_price = _price_bounds_from_ticks(tick_lower, tick_upper, d0, d1)
    try:
        owner = npm.functions.ownerOf(int(token_id)).call()
    except Exception:  # noqa: BLE001 — burned/nonexistent NFT must not break the read
        owner = None

    # The NFT contract holds EVERY V3 position on the chain, not just ours, so a
    # wrong token_id decodes cleanly into some unrelated pair. Flag that here,
    # once, rather than letting each caller assume token0 is USDT.
    cfg = _cfg(network)
    is_managed_pair = (
        token0.lower() == cfg["usdt"].lower()
        and token1.lower() == cfg["wbnb"].lower()
        and int(fee) == int(cfg["fee"])
    )
    return {
        "token_id": int(token_id),
        "network": network,
        "owner": owner,
        "operator": operator,
        "token0": token0,
        "token1": token1,
        "fee": int(fee),
        "tick_lower": int(tick_lower),
        "tick_upper": int(tick_upper),
        "lower_price_usdt_per_bnb": lower_price,
        "upper_price_usdt_per_bnb": upper_price,
        "liquidity": int(liquidity),
        "tokens_owed0": int(owed0),
        "tokens_owed1": int(owed1),
        "is_empty": int(liquidity) == 0,
        # False => this NFT is some other pool's position, NOT the BNB/USDT
        # position this agent manages. Do not act on it.
        "is_managed_pair": is_managed_pair,
    }


def get_lp_current_range(token_id: int, network: str | None = None) -> dict[str, Any]:
    """Where the live price sits inside an LP position's range.

    Combines the position's tick bounds with the pool's current tick and
    reports ``position_in_range_pct`` (0 = at the lower BNB price bound, 100 =
    at the upper), whether the price is still in range, and how far it sits
    from the nearest bound.
    """
    network = network or default_network()
    pos = get_lp_position(token_id, network)
    price = get_bnb_price(network)
    lower, upper = pos["lower_price_usdt_per_bnb"], pos["upper_price_usdt_per_bnb"]
    current = price["price_usdt_per_bnb"]
    metrics = calculate_range_metrics(
        current, lower, upper, float(strategy_config()["trigger_pct"])
    )
    return {
        "token_id": pos["token_id"],
        "network": network,
        "current_price_usdt_per_bnb": current,
        "current_tick": price["tick"],
        "lower_price_usdt_per_bnb": lower,
        "upper_price_usdt_per_bnb": upper,
        "tick_lower": pos["tick_lower"],
        "tick_upper": pos["tick_upper"],
        **metrics,
    }


@_retry_rpc
def get_lp_liquidity(token_id: int, network: str | None = None) -> dict[str, Any]:
    """Liquidity of an LP position, alongside the pool's total active liquidity."""
    network = network or default_network()
    pos = get_lp_position(token_id, network)
    pool_liq = int(_pool(network).functions.liquidity().call())
    liq = pos["liquidity"]
    return {
        "token_id": pos["token_id"],
        "network": network,
        "position_liquidity": liq,
        "pool_active_liquidity": pool_liq,
        "share_of_active_pct": (liq / pool_liq * 100) if pool_liq else 0.0,
        "is_empty": pos["is_empty"],
    }


@_retry_rpc
def get_pending_fees(token_id: int, network: str | None = None) -> dict[str, Any]:
    """Uncollected trading fees for an LP position, in both tokens.

    Uses an ``eth_call`` against ``collect`` from the position's owner — a
    simulation, never a transaction. This is more accurate than reading
    ``tokensOwed``, which only counts fees already checkpointed by a prior
    interaction and reads 0 for a position that has simply been accruing.
    """
    network = network or default_network()
    cfg = _cfg(network)
    npm = _npm(network)
    pos = get_lp_position(token_id, network)
    owner = pos["owner"]
    d0 = _decimals(network, pos["token0"])
    d1 = _decimals(network, pos["token1"])

    amount0, amount1 = pos["tokens_owed0"], pos["tokens_owed1"]
    source = "tokensOwed"
    if owner:
        try:
            amount0, amount1 = npm.functions.collect(
                (int(token_id), owner, MAX_UINT128, MAX_UINT128)
            ).call({"from": owner})
            source = "collect_simulation"
        except Exception:  # noqa: BLE001 — fall back to the checkpointed value
            pass

    out = {
        "token_id": int(token_id),
        "network": network,
        "source": source,
        "is_managed_pair": pos["is_managed_pair"],
        "token0": pos["token0"],
        "token1": pos["token1"],
        "amount0": amount0 / 10**d0,
        "amount1": amount1 / 10**d1,
        "amount0_raw": int(amount0),
        "amount1_raw": int(amount1),
    }
    # Only name the tokens when this really IS the BNB/USDT position. A foreign
    # token_id decodes fine but its token0 is not USDT, and labelling its
    # amounts "fees_bnb"/"fees_usdt" would hand the LLM confident nonsense.
    if pos["is_managed_pair"]:
        out["fees_usdt"] = amount0 / 10**d0  # token0 == USDT for this pool
        out["fees_bnb"] = amount1 / 10**d1  # token1 == WBNB
        out["fees_usdt_raw"] = int(amount0)
        out["fees_bnb_raw"] = int(amount1)
    else:
        out["warning"] = (
            f"token_id {token_id} is not the managed BNB/USDT fee-{cfg['fee']} "
            "position; amounts are reported as token0/token1 only"
        )
    return out


# --- Pure range math (no chain access) -----------------------------------------
# Spec section 4.3 gives: range +/-10% around 700 => 630/770, "rebalance trigger
# 5%" => fire at >=763 or <=637. Both boundaries are exactly 7 away, and
# 7 = 5% of the 140-wide range. So the trigger is 5% OF THE FULL RANGE WIDTH
# measured inward from each bound — that is the only reading that reproduces
# both of the spec's own numbers, so it is what TRIGGER_PCT means here.
DEFAULT_RANGE_PCT = 10.0
DEFAULT_TRIGGER_PCT = 5.0


@lru_cache(maxsize=1)
def default_network() -> str:
    """The active network: ``$BNB_NETWORK`` if set, else ``[network].default``.

    Everything network-scoped routes through here, so switching chains is one
    env var rather than a code change — and, with ``[strategy].token_ids``
    below, no config edit either.

    The env override exists because switching by hand meant editing three
    coupled lines (``[network].default``, ``[strategy].token_id``,
    ``[payments.erc8183].currency``); getting one wrong is B7, and their
    comments drifted out of date twice while testing.

    One thing the override CANNOT move: ``[payments.erc8183].currency`` is read
    from studio.toml by the SDK, not by this module, so a network set purely by
    env still signs quotes in whatever token that line names.
    ``check_config_consistency()`` fails loudly when the two disagree rather
    than letting the agent sign a worthless quote.
    """
    override = os.environ.get("BNB_NETWORK")
    if override:
        if override not in supported_networks():
            raise ValueError(
                f"$BNB_NETWORK={override!r} is not a supported network "
                f"({', '.join(supported_networks())})"
            )
        return override
    try:
        name = str((_studio_toml().get("network") or {}).get("default") or "")
        if name in supported_networks():
            return name
    except Exception:  # noqa: BLE001
        pass
    return "bsc-testnet"


@lru_cache(maxsize=1)
def strategy_config() -> dict[str, Any]:
    """``[strategy]`` from studio.toml, with the spec defaults filled in.

    Spec 4.3 requires the strategy parameters be configurable, so they live in
    studio.toml rather than as constants here. Falls back to the defaults when
    the section (or the config itself) is unreadable — a missing config must
    not stop the agent from reading chain state.
    """
    cfg = {
        "pair": "BNB/USDT",
        "fee": 500,
        "token_id": 0,
        "range_pct": DEFAULT_RANGE_PCT,
        "trigger_pct": DEFAULT_TRIGGER_PCT,
        "max_slippage_pct": 1.0,
    }
    try:
        cfg.update(_studio_toml().get("strategy") or {})
    except Exception:  # noqa: BLE001 — config is an override, never a hard dep
        pass
    return cfg


def check_config_consistency(network: str | None = None) -> list[str]:
    """Moved to :mod:`risk` (spec 2's Risk Engine). Re-exported so existing
    callers and the boot check keep working."""
    from risk import check_config_consistency as _impl

    return _impl(network)


def managed_token_id(network: str | None = None) -> int:
    """The LP NFT this agent manages, for the ACTIVE network.

    Token IDs are per-network: an ID minted on testnet names a different (or
    missing) position on mainnet. ``[strategy].token_ids`` holds one per
    network so switching chains needs no edit::

        [strategy.token_ids]
        bsc-mainnet = 7116214
        bsc-testnet = 36799

    Falls back to the flat ``[strategy].token_id`` when the table is absent, so
    existing configs keep working.

    Raises when unset — acting on token_id 0 would silently read someone
    else's position.
    """
    network = network or default_network()
    cfg = strategy_config()
    per_network = cfg.get("token_ids") or {}
    token_id = int(per_network.get(network) or cfg.get("token_id") or 0)
    if token_id <= 0:
        raise ValueError(
            f"no managed LP position for {network}: set [strategy.token_ids].{network} "
            "in studio.toml after minting one (see `python mint_position.py`)"
        )
    return token_id


def calculate_range_metrics(
    current_price: float,
    lower_price: float,
    upper_price: float,
    trigger_pct: float = DEFAULT_TRIGGER_PCT,
) -> dict[str, Any]:
    """Position of ``current_price`` within ``[lower_price, upper_price]``.

    ``position_in_range_pct`` is 0 at the lower bound and 100 at the upper.
    ``range_utilization_pct`` is how far the price has travelled from the
    range's centre toward whichever bound is nearer: 0 = dead centre,
    100 = sitting on a bound.
    """
    if not (upper_price > lower_price):
        raise ValueError(f"upper_price must exceed lower_price ({upper_price} <= {lower_price})")

    width = upper_price - lower_price
    pos = (current_price - lower_price) / width
    margin = trigger_pct / 100.0
    return {
        "position_in_range_pct": pos * 100.0,
        # Spec 4.6 lists "range_utilization" but never defines it, and its own
        # example (704.21 in 630-770 -> 87) is not reproducible from those
        # numbers by any reading. Defined here as distance-from-centre, which
        # is what actually matters for a rebalancer.
        "range_utilization_pct": abs(pos - 0.5) * 2 * 100.0,
        "in_range": 0.0 <= pos <= 1.0,
        "distance_to_lower_pct": (current_price - lower_price) / width * 100.0,
        "distance_to_upper_pct": (upper_price - current_price) / width * 100.0,
        "trigger_pct": trigger_pct,
        "trigger_lower_price": lower_price + width * margin,
        "trigger_upper_price": upper_price - width * margin,
    }


def calculate_rebalance_required(
    current_price: float,
    lower_price: float,
    upper_price: float,
    trigger_pct: float = DEFAULT_TRIGGER_PCT,
) -> dict[str, Any]:
    """Whether the price has come within ``trigger_pct`` of a range boundary.

    Returns the decision plus the reason, so the caller (and the operator
    reading logs) can see exactly which bound tripped it.
    """
    m = calculate_range_metrics(current_price, lower_price, upper_price, trigger_pct)
    if not m["in_range"]:
        reason, required = "out_of_range", True
    elif current_price >= m["trigger_upper_price"]:
        reason, required = "near_upper_bound", True
    elif current_price <= m["trigger_lower_price"]:
        reason, required = "near_lower_bound", True
    else:
        reason, required = "within_range", False
    return {
        "rebalance_required": required,
        "reason": reason,
        "current_price": current_price,
        "lower_price": lower_price,
        "upper_price": upper_price,
        **m,
    }


def calculate_rebalance_range(
    current_price: float, range_pct: float = DEFAULT_RANGE_PCT
) -> dict[str, Any]:
    """A fresh +/-``range_pct`` range centred on ``current_price``.

    Prices only — converting these to tick bounds snapped to the pool's
    ``tickSpacing`` belongs with the mint path, not here.
    """
    if current_price <= 0:
        raise ValueError(f"current_price must be positive, got {current_price}")
    f = range_pct / 100.0
    return {
        "center_price": current_price,
        "range_pct": range_pct,
        "lower_price": current_price * (1 - f),
        "upper_price": current_price * (1 + f),
    }


# --- Liquidity <-> token amounts (pure math) -----------------------------------
# Standard V3 formulas. L and the returned amounts are all in RAW token units.
#
#   in range:   amount0 = L * (sqrtPb - sqrtP) / (sqrtP * sqrtPb)
#               amount1 = L * (sqrtP - sqrtPa)
#   below Pa:   entirely token0        above Pb: entirely token1
#
# Floats are fine here: these feed reporting (TVL/PnL) and the mint slippage
# floor, never an exact transfer amount. The contract itself recomputes in
# integer math — we never tell it an exact amount to move.
def _sqrt_ratio_at_tick(tick: int) -> float:
    return 1.0001 ** (tick / 2)


def liquidity_to_amounts(
    liquidity: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int
) -> tuple[int, int]:
    """``(amount0_raw, amount1_raw)`` held by ``liquidity`` over the range."""
    if liquidity <= 0:
        return (0, 0)
    sp = sqrt_price_x96 / (2**96)
    sa, sb = _sqrt_ratio_at_tick(tick_lower), _sqrt_ratio_at_tick(tick_upper)
    if sa > sb:
        sa, sb = sb, sa

    if sp <= sa:  # entirely token0
        return (int(liquidity * (sb - sa) / (sa * sb)), 0)
    if sp >= sb:  # entirely token1
        return (0, int(liquidity * (sb - sa)))
    return (int(liquidity * (sb - sp) / (sp * sb)), int(liquidity * (sp - sa)))


def amounts_to_liquidity(
    amount0: int, amount1: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int
) -> int:
    """Liquidity mintable from the given amounts — whichever side binds.

    Mirrors what NonfungiblePositionManager.mint does internally, so callers
    can predict the deposit before sending it.
    """
    sp = sqrt_price_x96 / (2**96)
    sa, sb = _sqrt_ratio_at_tick(tick_lower), _sqrt_ratio_at_tick(tick_upper)
    if sa > sb:
        sa, sb = sb, sa

    if sp <= sa:
        return int(amount0 * (sa * sb) / (sb - sa)) if sb > sa else 0
    if sp >= sb:
        return int(amount1 / (sb - sa)) if sb > sa else 0
    l0 = amount0 * (sp * sb) / (sb - sp) if sb > sp else float("inf")
    l1 = amount1 / (sp - sa) if sp > sa else float("inf")
    liq = min(l0, l1)
    return 0 if liq == float("inf") else int(liq)


@_retry_rpc
def get_position_value(token_id: int, network: str | None = None) -> dict[str, Any]:
    """Token amounts and USDT value (TVL) held by a position — spec 4.6 ``tvl``."""
    network = network or default_network()
    cfg = _cfg(network)
    pos = get_lp_position(token_id, network)
    slot0 = _pool(network).functions.slot0().call()
    d0 = _decimals(network, pos["token0"])
    d1 = _decimals(network, pos["token1"])
    amt0, amt1 = liquidity_to_amounts(
        pos["liquidity"], int(slot0[0]), pos["tick_lower"], pos["tick_upper"]
    )
    price = _bnb_price_from_tick(int(slot0[1]), d0, d1)
    usdt, bnb = amt0 / 10**d0, amt1 / 10**d1
    if not pos["is_managed_pair"]:
        # token0 is not USDT for this NFT, so naming the sides would be a lie.
        return {"token_id": int(token_id), "network": network,
                "is_managed_pair": False, "amount0": usdt, "amount1": bnb,
                "warning": "not the managed BNB/USDT pair; amounts unlabelled"}
    return {
        "token_id": int(token_id),
        "network": network,
        "is_managed_pair": True,
        "amount_usdt": usdt,
        "amount_bnb": bnb,
        "price_usdt_per_bnb": price,
        "tvl_usdt": usdt + bnb * price,
    }


def get_position_summary(token_id: int, network: str | None = None) -> dict[str, Any]:
    """One-call status of the managed LP position: price, range, liquidity,
    pending fees, and whether a rebalance is currently required."""
    network = network or default_network()
    rng = get_lp_current_range(token_id, network)
    decision = calculate_rebalance_required(
        rng["current_price_usdt_per_bnb"],
        rng["lower_price_usdt_per_bnb"],
        rng["upper_price_usdt_per_bnb"],
        float(strategy_config()["trigger_pct"]),
    )
    return {
        "agent": "BNB LP Rebalancer",
        "category": "rebalancing",
        "protocol": "PancakeSwap V3",
        "pair": "BNB/USDT",
        "network": network,
        "token_id": int(token_id),
        "current_price": rng["current_price_usdt_per_bnb"],
        "lower_price": rng["lower_price_usdt_per_bnb"],
        "upper_price": rng["upper_price_usdt_per_bnb"],
        "range_utilization": rng["range_utilization_pct"],
        "in_range": rng["in_range"],
        "rebalance_required": decision["rebalance_required"],
        "rebalance_reason": decision["reason"],
        "liquidity": get_lp_liquidity(token_id, network),
        "pending_fees": get_pending_fees(token_id, network),
    }


@_retry_rpc
def verify_position(token_id: int, tx_hash: str | None = None,
                    network: str | None = None) -> dict[str, Any]:
    """Confirm a position is real, owned, funded, and in range (spec 4.5).

    The last step of the 4.4 workflow ("Verify new position") and the last
    on-chain check of the 4.8 definition of done. Deliberately re-reads
    everything from chain state rather than trusting the mint's return values:
    a mint can succeed and still leave a position that is empty (mins met with
    a dust deposit) or already out of range (price moved between the quote and
    the block).

    ``tx_hash`` is optional — when given, its receipt is checked too, so one
    call answers "did the transaction land AND is the resulting position
    sound?".

    Returns ``verified`` plus the individual checks, so a caller that fails can
    see WHICH check failed rather than just that something did.
    """
    network = network or default_network()
    from bnbagent_studio_core.wallet import get_wallet

    checks: dict[str, Any] = {}
    problems: list[str] = []

    pos = get_lp_position(token_id, network)
    checks["exists"] = pos["owner"] is not None
    if not checks["exists"]:
        problems.append(f"token_id {token_id} has no owner (burned or never minted)")

    checks["is_managed_pair"] = pos["is_managed_pair"]
    if not pos["is_managed_pair"]:
        problems.append(
            f"token_id {token_id} is not the managed {strategy_config()['pair']} "
            f"fee-{_cfg(network)['fee']} position"
        )

    try:
        expected_owner = Web3.to_checksum_address(get_wallet().address)
        checks["owned_by_agent"] = pos["owner"] == expected_owner
        if not checks["owned_by_agent"]:
            problems.append(f"owned by {pos['owner']}, not this agent ({expected_owner})")
    except Exception as e:  # noqa: BLE001 — no wallet locally is not a position fault
        checks["owned_by_agent"] = None
        log.debug("owner check skipped: %s", e)

    checks["has_liquidity"] = pos["liquidity"] > 0
    if not checks["has_liquidity"]:
        problems.append("position holds zero liquidity")

    price = get_bnb_price(network)["price_usdt_per_bnb"]
    lower, upper = pos["lower_price_usdt_per_bnb"], pos["upper_price_usdt_per_bnb"]
    checks["in_range"] = lower <= price <= upper
    if not checks["in_range"]:
        problems.append(f"price {price:.4f} is outside {lower:.4f}-{upper:.4f}")

    if tx_hash:
        try:
            receipt = _w3(network).eth.get_transaction_receipt(tx_hash)
            checks["tx_confirmed"] = receipt["status"] == 1
            checks["tx_block"] = receipt["blockNumber"]
            if receipt["status"] != 1:
                problems.append(f"transaction {tx_hash} reverted")
        except Exception as e:  # noqa: BLE001
            checks["tx_confirmed"] = False
            problems.append(f"transaction {tx_hash} unreadable: {e}")

    value = get_position_value(token_id, network)
    return {
        "token_id": int(token_id),
        "network": network,
        "verified": not problems,
        "checks": checks,
        "problems": problems,
        "owner": pos["owner"],
        "liquidity": pos["liquidity"],
        "current_price": price,
        "lower_price": lower,
        "upper_price": upper,
        "tvl_usdt": value.get("tvl_usdt", 0.0),
        "tx_hash": tx_hash,
    }
