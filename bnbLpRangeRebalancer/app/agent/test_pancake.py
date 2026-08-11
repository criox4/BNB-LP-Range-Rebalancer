"""Self-check for the pure range math in pancake.py.

The numbers come straight from the spec's section 4.3 worked example, so if the
trigger rule ever drifts from what the spec asks for, this fails. Chain reads
are not covered here (they need a live RPC) — run `python -m test_pancake --live`
for a read-only smoke test against BSC testnet.

    python test_pancake.py
"""
from __future__ import annotations

import sys

from pancake import (
    _bnb_price_from_tick,
    _price_bounds_from_ticks,
    _tick_from_bnb_price,
    calculate_range_metrics,
    calculate_rebalance_range,
    calculate_rebalance_required,
    snap_tick,
)


def test_spec_example_range():
    """Spec 4.3: BNB at 700, +/-10% => 630 / 770."""
    r = calculate_rebalance_range(700.0, 10.0)
    assert abs(r["lower_price"] - 630.0) < 1e-9, r
    assert abs(r["upper_price"] - 770.0) < 1e-9, r


def test_spec_example_triggers():
    """Spec 4.3: within a 630-770 range, rebalance fires at >=763 or <=637."""
    m = calculate_range_metrics(700.0, 630.0, 770.0)
    assert abs(m["trigger_upper_price"] - 763.0) < 1e-9, m
    assert abs(m["trigger_lower_price"] - 637.0) < 1e-9, m

    # Exactly on the boundary trips it; a hair inside does not.
    assert calculate_rebalance_required(763.0, 630.0, 770.0)["rebalance_required"]
    assert calculate_rebalance_required(637.0, 630.0, 770.0)["rebalance_required"]
    assert not calculate_rebalance_required(762.99, 630.0, 770.0)["rebalance_required"]
    assert not calculate_rebalance_required(637.01, 630.0, 770.0)["rebalance_required"]
    assert not calculate_rebalance_required(700.0, 630.0, 770.0)["rebalance_required"]


def test_trigger_reasons():
    assert calculate_rebalance_required(765.0, 630.0, 770.0)["reason"] == "near_upper_bound"
    assert calculate_rebalance_required(632.0, 630.0, 770.0)["reason"] == "near_lower_bound"
    assert calculate_rebalance_required(700.0, 630.0, 770.0)["reason"] == "within_range"
    # Out of range must still report required, not silently look "within".
    out = calculate_rebalance_required(800.0, 630.0, 770.0)
    assert out["reason"] == "out_of_range" and out["rebalance_required"], out
    assert not out["in_range"], out


def test_range_metrics_geometry():
    centre = calculate_range_metrics(700.0, 630.0, 770.0)
    assert abs(centre["position_in_range_pct"] - 50.0) < 1e-9, centre
    assert abs(centre["range_utilization_pct"] - 0.0) < 1e-9, centre

    at_top = calculate_range_metrics(770.0, 630.0, 770.0)
    assert abs(at_top["position_in_range_pct"] - 100.0) < 1e-9, at_top
    assert abs(at_top["range_utilization_pct"] - 100.0) < 1e-9, at_top

    at_bottom = calculate_range_metrics(630.0, 630.0, 770.0)
    assert abs(at_bottom["position_in_range_pct"] - 0.0) < 1e-9, at_bottom
    assert abs(at_bottom["range_utilization_pct"] - 100.0) < 1e-9, at_bottom


def test_degenerate_ranges_rejected():
    for bad in ((700.0, 770.0, 630.0), (700.0, 700.0, 700.0)):
        try:
            calculate_range_metrics(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")

    try:
        calculate_rebalance_range(0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-positive price")


def test_tick_price_inversion():
    """token0 is USDT, so BNB price must move INVERSELY to the tick, and
    tickLower must map to the UPPER BNB price bound."""
    assert _bnb_price_from_tick(-28173) > _bnb_price_from_tick(-28000), (
        "lower tick must mean a higher BNB price"
    )

    # The live testnet pool reads tick -28173 => ~16.7 USDT/BNB (unarbitraged).
    p = _bnb_price_from_tick(-28173)
    assert 16.0 < p < 17.5, p

    lower, upper = _price_bounds_from_ticks(-28500, -27500)
    assert lower < upper, (lower, upper)
    # tickLower (-28500) is the higher price, so it must have become `upper`.
    assert abs(upper - _bnb_price_from_tick(-28500)) < 1e-9, (lower, upper)


def test_roundtrip_through_range_math():
    """A range built from a price must place that price dead centre."""
    price = _bnb_price_from_tick(-28173)
    r = calculate_rebalance_range(price, 10.0)
    m = calculate_range_metrics(price, r["lower_price"], r["upper_price"])
    assert abs(m["position_in_range_pct"] - 50.0) < 1e-9, m
    assert not calculate_rebalance_required(
        price, r["lower_price"], r["upper_price"]
    )["rebalance_required"]


def test_tick_price_roundtrip():
    """price -> tick -> price must land back on the same price."""
    for price in (0.5, 16.729, 611.0, 1234.5):
        tick = _tick_from_bnb_price(price)
        back = _bnb_price_from_tick(tick)
        # One tick is 1 basis point, so rounding costs at most ~0.01%.
        assert abs(back - price) / price < 2e-4, (price, tick, back)


def test_snap_tick_direction():
    assert snap_tick(-28173, 10, up=False) == -28180
    assert snap_tick(-28173, 10, up=True) == -28170
    # Already aligned: both directions are identity.
    assert snap_tick(-28170, 10, up=False) == -28170
    assert snap_tick(-28170, 10, up=True) == -28170


def test_price_range_maps_to_inverted_ticks():
    """The LOWER BNB price must become the UPPER tick (token0 is USDT).

    Pure-math version of price_range_to_ticks — the real one needs a live pool
    for tickSpacing, so this checks the inversion that function relies on.
    """
    lower_price, upper_price = 630.0, 770.0
    tick_for_lower = _tick_from_bnb_price(lower_price)
    tick_for_upper = _tick_from_bnb_price(upper_price)
    assert tick_for_lower > tick_for_upper, (tick_for_lower, tick_for_upper)


def test_liquidity_amounts_roundtrip():
    """amounts -> liquidity -> amounts must return what the contract would take."""
    from pancake import amounts_to_liquidity, liquidity_to_amounts

    # Spot inside the range: both tokens used.
    tick, lower, upper = -64154, -65090, -63070
    sqrt_x96 = int((1.0001 ** (tick / 2)) * 2**96)
    a0, a1 = 10**18, 10**16
    liq = amounts_to_liquidity(a0, a1, sqrt_x96, lower, upper)
    assert liq > 0, liq
    got0, got1 = liquidity_to_amounts(liq, sqrt_x96, lower, upper)
    # The binding side is consumed fully; neither side may exceed what we offered.
    assert got0 <= a0 * 1.0001 and got1 <= a1 * 1.0001, (got0, got1, a0, a1)
    assert abs(got0 - a0) < a0 * 1e-6 or abs(got1 - a1) < a1 * 1e-6, (got0, got1)


def test_liquidity_amounts_out_of_range_is_single_sided():
    """Below the range it is all token0; above, all token1. This is what makes a
    flat percentage-of-desired mint floor wrong."""
    from pancake import liquidity_to_amounts

    lower, upper, liq = -65090, -63070, 10**18
    below = int((1.0001 ** (-66000 / 2)) * 2**96)
    above = int((1.0001 ** (-62000 / 2)) * 2**96)
    a0, a1 = liquidity_to_amounts(liq, below, lower, upper)
    assert a0 > 0 and a1 == 0, (a0, a1)
    b0, b1 = liquidity_to_amounts(liq, above, lower, upper)
    assert b0 == 0 and b1 > 0, (b0, b1)


def test_zero_liquidity_has_no_amounts():
    from pancake import liquidity_to_amounts

    assert liquidity_to_amounts(0, 2**96, -100, 100) == (0, 0)


def test_fees_since_window_incompleteness_is_reported():
    """A short watch window must never be presented as a full 24h figure."""
    import time as _t

    from strategy import _fees_since

    now = _t.time()
    # Only 1h of history: the window is incomplete.
    recent = [{"ts": now - 3600, "fees_usdt": 1.0}]
    val, complete = _fees_since(recent, 86400, 3.0)
    assert val == 2.0 and complete is False, (val, complete)

    # A sample older than 24h completes the window.
    old = [{"ts": now - 90000, "fees_usdt": 1.0}, {"ts": now - 3600, "fees_usdt": 2.5}]
    val, complete = _fees_since(old, 86400, 3.0)
    assert val == 2.0 and complete is True, (val, complete)

    # No history at all.
    assert _fees_since([], 86400, 5.0) == (0.0, False)


def _live_smoke():
    """Read-only smoke test against BSC testnet. Needs network."""
    from pancake import get_bnb_price, get_lp_position, get_pending_fees

    price = get_bnb_price("bsc-testnet")
    print("live pool read:", price)
    assert price["price_usdt_per_bnb"] > 0
    assert price["pair"] == "BNB/USDT"

    # token_id 1 is a real testnet position in a DIFFERENT pool. It must decode,
    # and it must refuse to call its amounts BNB/USDT fees.
    foreign = get_lp_position(1)
    assert foreign["liquidity"] > 0, foreign
    assert not foreign["is_managed_pair"], foreign
    fees = get_pending_fees(1)
    assert not fees["is_managed_pair"] and "warning" in fees, fees
    assert "fees_bnb" not in fees and "fees_usdt" not in fees, fees
    # These fees are token0 (0x0fB5...), which the old mapping mislabelled as
    # "fees_bnb" even though token1 here is not WBNB at all.
    assert fees["amount0"] > 0, fees
    print("live smoke OK (foreign position correctly refused BNB/USDT labels)")


def _live_addressbook():
    """Verify every configured address ON CHAIN, for both networks.

    The address book is hand-built from probing, and the failure mode is silent:
    a wrong quoter reverts with a bare "execution reverted: 0x", and a wrong
    pool still decodes into plausible-looking numbers. This calls each one.
    """
    from web3 import Web3

    from pancake import ADDRESSES, _cfg, _w3

    factory_abi = [{"name": "getPool", "type": "function", "stateMutability": "view",
                    "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}],
                    "outputs": [{"type": "address"}]}]
    quoter_abi = [{"name": "quoteExactInputSingle", "type": "function",
                   "stateMutability": "nonpayable", "inputs": [
                       {"type": "tuple", "name": "params", "components": [
                           {"type": "address"}, {"type": "address"}, {"type": "uint256"},
                           {"type": "uint24"}, {"type": "uint160"}]}],
                   "outputs": [{"type": "uint256"}, {"type": "uint160"},
                               {"type": "uint32"}, {"type": "uint256"}]}]

    for network in ADDRESSES:
        cfg = _cfg(network)
        w3 = _w3(network)
        ck = Web3.to_checksum_address

        for key in ("factory", "position_manager", "quoter_v2", "swap_router",
                    "wbnb", "usdt", "pool"):
            assert w3.eth.get_code(ck(cfg[key])), f"{network}: {key} has NO CODE"

        # The configured pool must be the one the factory derives for this pair.
        factory = w3.eth.contract(address=ck(cfg["factory"]), abi=factory_abi)
        derived = factory.functions.getPool(ck(cfg["wbnb"]), ck(cfg["usdt"]), cfg["fee"]).call()
        assert derived.lower() == cfg["pool"].lower(), (
            f"{network}: configured pool {cfg['pool']} != factory-derived {derived}"
        )

        # The quoter must actually answer — the wrong-network one reverts.
        quoter = w3.eth.contract(address=ck(cfg["quoter_v2"]), abi=quoter_abi)
        out = quoter.functions.quoteExactInputSingle(
            (ck(cfg["wbnb"]), ck(cfg["usdt"]), 10**18, cfg["fee"], 0)
        ).call()
        assert out[0] > 0, f"{network}: quoter returned zero"
        print(f"  ok  {network}: pool derives, quoter answers "
              f"(1 BNB -> {out[0] / 1e18:.4f} USDT)")

    print("address book OK on both networks")


def _live_guards():
    """The write path's address guard, and the live tick conversion.

    sign_transaction is NOT SigningPolicy-gated, so _require_allowed is the only
    thing stopping a transaction to an arbitrary address. Verify it actually
    refuses one, and that it does so BEFORE any signing could happen.
    """
    import lp_signing as lp
    from pancake import get_bnb_price, price_range_to_ticks

    attacker = "0x000000000000000000000000000000000000dEaD"
    for label, call in {
        "_require_allowed": lambda: lp._require_allowed("bsc-testnet", attacker),
        "quote_swap": lambda: lp.quote_swap(attacker, attacker, 10**16, "bsc-testnet"),
    }.items():
        try:
            call()
        except PermissionError:
            print(f"  ok  {label} refused an unlisted address")
        else:
            raise AssertionError(f"{label} did NOT refuse {attacker}")

    # Live tick conversion against the real pool's tickSpacing.
    price = get_bnb_price("bsc-testnet")["price_usdt_per_bnb"]
    t = price_range_to_ticks(price * 0.9, price * 1.1, "bsc-testnet")
    assert t["tick_lower"] < t["tick_upper"], t
    assert t["tick_lower"] % t["tick_spacing"] == 0, t
    assert t["tick_upper"] % t["tick_spacing"] == 0, t
    # Snapping must WIDEN the range, never narrow it below what was asked.
    assert t["actual_lower_price"] <= t["requested_lower_price"] * 1.0001, t
    assert t["actual_upper_price"] >= t["requested_upper_price"] * 0.9999, t
    print(f"  ok  live ticks {t['tick_lower']}..{t['tick_upper']} "
          f"(spacing {t['tick_spacing']}) bracket spot {price:.4f}")
    print("write-path guards OK")


def _live_config_consistency():
    """The config guard must flag a wrong-chain currency — the bug that shipped
    a mainnet quote payable in the testnet token."""
    from bnbagent.networks import BNB_CHAIN_ADDRESSES

    from pancake import check_config_consistency, default_network

    problems = check_config_consistency()
    for p in problems:
        print(f"  PROBLEM: {p}")
    assert not problems, f"config is inconsistent: {problems}"

    net = default_network()
    chain_id = {"bsc-mainnet": 56, "bsc-testnet": 97}[net]
    print(f"  ok  config consistent for {net}: currency == "
          f"{BNB_CHAIN_ADDRESSES[chain_id].payment_token}")

    # Prove the guard actually fires rather than always returning clean.
    import pancake as pcs

    other = 97 if chain_id == 56 else 56
    wrong = BNB_CHAIN_ADDRESSES[other].payment_token
    real_loader = pcs.check_config_consistency
    from bnbagent_studio_core import config as _config

    orig = _config.load_studio_toml
    try:
        _config.load_studio_toml = lambda *a, **k: {
            "payments": {"erc8183": {"currency": wrong}}, "strategy": {"token_id": 0}
        }
        got = real_loader(net)
        assert got and "not the U token" in got[0], got
        print("  ok  guard fires on a wrong-chain currency")
    finally:
        _config.load_studio_toml = orig
    print("config guard OK")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    if "--live" in sys.argv:
        _live_smoke()
        _live_addressbook()
        _live_guards()
        _live_config_consistency()


if __name__ == "__main__":
    main()
