"""PancakeSwap V3 read-only queries + range math for the BNB/USDT LP rebalancer.

READ-ONLY by the studio definition (see tools.py): every function here is an
``eth_call``. Nothing signs, nothing broadcasts. The rebalance write path
(decrease/collect/swap/mint) belongs in ``signing.py`` as fixed code — never a
tool the LLM can invoke.

Addresses below were verified live on BSC testnet (chain 97): each periphery
contract reports the same ``factory()``, and the pool reports ``token0 = USDT``.

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

from functools import lru_cache
from typing import Any

from web3 import Web3

from bnbagent_studio_core.networks import get_network

# --- Verified BSC deployment ---------------------------------------------------
# EVERY address below was verified by calling it on the live chain, not taken
# from docs. That mattered: docs.pancakeswap.finance lists a "Factory V3"
# (0x1296b67b...) and "Router V3" (0xEfF92A26...) that have NO CODE on BSC
# mainnet, and the real mainnet factory is the same 0x0BFbCF9f... as testnet.
#
# Two traps this table encodes, both confirmed by calling:
#   * position_manager DIFFERS per network (testnet 0x427bF5b3, mainnet
#     0x46A15B0b) even though the factory address is IDENTICAL on both.
#   * quoter_v2 addresses are effectively SWAPPED: both contracts exist on both
#     chains, but 0xbC203d7f only answers on testnet and 0xB048Bbc1 only on
#     mainnet — the wrong one reverts with bare "execution reverted: 0x".
#
# ponytail: hardcoded table. Move to studio.toml if a third network appears.
ADDRESSES = {
    "bsc-testnet": {
        "factory": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        "position_manager": "0x427bF5b37357632377eCbEC9de3626C71A5396c1",
        "quoter_v2": "0xbC203d7f83677c7ed3F7acEc959963E7F4ECC5C2",
        "swap_router": "0x1b81D678ffb9C0263b24A97847620C99d213eB14",
        "wbnb": "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd",
        "usdt": "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd",
        "pool": "0x2dbB5a4c235164B9f772179A43faca2c71a8abDB",  # fee 500, deepest
        "fee": 500,
    },
    "bsc-mainnet": {
        "factory": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        "position_manager": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
        "quoter_v2": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "swap_router": "0x1b81D678ffb9C0263b24A97847620C99d213eB14",
        "wbnb": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "usdt": "0x55d398326f99059fF775485246999027B3197955",
        # fee-500 pool, for parity with testnet. The fee-100 pool
        # (0x172fcD41E0913e95784454622d1c3724f546f849) is ~2x deeper but earns
        # a fifth the fee rate — pick deliberately before going live.
        "pool": "0x36696169C63e42cd08ce11f5deeBbCeBae652050",
        "fee": 500,
    },
}

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
    try:
        return ADDRESSES[network]
    except KeyError:
        raise ValueError(
            f"PancakeSwap V3 addresses not configured for network {network!r}; "
            f"known: {sorted(ADDRESSES)}"
        ) from None


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
    lower_price: float, upper_price: float, network: str = "bsc-testnet"
) -> dict[str, Any]:
    """Convert a BNB price range to the pool's snapped ``(tickLower, tickUpper)``.

    Because token0 is USDT the tick axis runs OPPOSITE to the BNB price, so the
    LOWER price becomes the UPPER tick. Getting this backwards produces an
    inverted range that mint rejects (or, worse, silently accepts as a range
    that never contains the price).
    """
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
def get_bnb_price(network: str = "bsc-testnet") -> dict[str, Any]:
    """Current BNB price in USDT from the PancakeSwap V3 BNB/USDT pool.

    Reads the pool's live ``slot0`` tick. Note: on BSC testnet this pool is not
    arbitraged against real markets, so the value will not track the real BNB
    price.
    """
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
def get_lp_position(token_id: int, network: str = "bsc-testnet") -> dict[str, Any]:
    """Full PancakeSwap V3 LP position for an NFT ``token_id``.

    Returns the raw position record (tokens, fee tier, tick bounds, liquidity)
    plus the tick bounds converted to BNB price bounds and the owner address.
    """
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


def get_lp_current_range(token_id: int, network: str = "bsc-testnet") -> dict[str, Any]:
    """Where the live price sits inside an LP position's range.

    Combines the position's tick bounds with the pool's current tick and
    reports ``position_in_range_pct`` (0 = at the lower BNB price bound, 100 =
    at the upper), whether the price is still in range, and how far it sits
    from the nearest bound.
    """
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
def get_lp_liquidity(token_id: int, network: str = "bsc-testnet") -> dict[str, Any]:
    """Liquidity of an LP position, alongside the pool's total active liquidity."""
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
def get_pending_fees(token_id: int, network: str = "bsc-testnet") -> dict[str, Any]:
    """Uncollected trading fees for an LP position, in both tokens.

    Uses an ``eth_call`` against ``collect`` from the position's owner — a
    simulation, never a transaction. This is more accurate than reading
    ``tokensOwed``, which only counts fees already checkpointed by a prior
    interaction and reads 0 for a position that has simply been accruing.
    """
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
    """``[network].default`` from studio.toml.

    Everything network-scoped routes through here so switching mainnet/testnet
    is a config edit, not a code edit. Note that ``[strategy].token_id`` is
    network-specific: an ID minted on testnet names a DIFFERENT (or missing)
    position on mainnet. The ``is_managed_pair`` guard catches the mismatch
    rather than trading against a stranger's position.
    """
    try:
        from bnbagent_studio_core import config as _config

        name = str(((_config.load_studio_toml() or {}).get("network") or {}).get("default") or "")
        if name in ADDRESSES:
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
        from bnbagent_studio_core import config as _config

        cfg.update((_config.load_studio_toml() or {}).get("strategy") or {})
    except Exception:  # noqa: BLE001 — config is an override, never a hard dep
        pass
    return cfg


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

    net = network or default_network()
    problems: list[str] = []
    try:
        from bnbagent_studio_core import config as _config

        cfg = _config.load_studio_toml() or {}
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

    token_id = int(((cfg.get("strategy") or {}).get("token_id")) or 0)
    if token_id and net in ADDRESSES:
        try:
            pos = get_lp_position(token_id, net)
            if not pos["is_managed_pair"]:
                problems.append(
                    f"[strategy].token_id {token_id} is not a BNB/USDT fee-"
                    f"{ADDRESSES[net]['fee']} position on {net} — token IDs are "
                    f"per-network and this one does not belong to this chain."
                )
        except Exception as e:  # noqa: BLE001
            problems.append(f"[strategy].token_id {token_id} unreadable on {net}: {e}")
    return problems


def managed_token_id() -> int:
    """The LP NFT this agent manages (``[strategy].token_id``).

    Raises when unset — acting on token_id 0 would silently read someone
    else's position.
    """
    token_id = int(strategy_config().get("token_id") or 0)
    if token_id <= 0:
        raise ValueError(
            "no managed LP position: set [strategy].token_id in studio.toml "
            "after minting one (see `python mint_position.py`)"
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
def get_position_value(token_id: int, network: str = "bsc-testnet") -> dict[str, Any]:
    """Token amounts and USDT value (TVL) held by a position — spec 4.6 ``tvl``."""
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


def get_position_summary(token_id: int, network: str = "bsc-testnet") -> dict[str, Any]:
    """One-call status of the managed LP position: price, range, liquidity,
    pending fees, and whether a rebalance is currently required."""
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
