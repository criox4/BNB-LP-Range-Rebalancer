"""One-off: mint the BNB/USDT LP position this agent will manage.

Run once to create a position on the network in [network].default, then put the
printed token_id in
studio.toml under [strategy].token_id.

    export WALLET_PASSWORD=...
    .venv/bin/python mint_position.py --dry-run     # plan only, no transactions
    .venv/bin/python mint_position.py               # actually mint

Default size is deliberately tiny (0.02 BNB). The testnet BNB/USDT pool is
shallow — swapping 1 BNB moves the price ~44% — so a large test mint would
mostly measure slippage rather than the strategy.

Every write goes through lp_signing.py, the same fixed code the rebalance path
uses. Nothing here is reachable by the LLM.
"""
from __future__ import annotations

import argparse
import sys

from web3 import Web3

import lp_signing as lp
import pancake as pcs

NETWORK = pcs.default_network()


def _fmt(wei: int) -> str:
    return f"{wei / 1e18:.6f}"


def plan(total_bnb: float, range_pct: float) -> dict:
    """Work out the mint without sending anything."""
    cfg = pcs._cfg(NETWORK)
    price = pcs.get_bnb_price(NETWORK)["price_usdt_per_bnb"]
    rng = pcs.calculate_rebalance_range(price, range_pct)
    ticks = pcs.price_range_to_ticks(rng["lower_price"], rng["upper_price"], NETWORK)

    total_wei = Web3.to_wei(total_bnb, "ether")
    # Half the BNB is swapped to USDT so the mint can supply both sides of a
    # range centred on spot.
    swap_wei = total_wei // 2
    quoted_usdt = lp.quote_swap(cfg["wbnb"], cfg["usdt"], swap_wei, NETWORK)

    return {
        "price": price, "range": rng, "ticks": ticks,
        "total_wei": total_wei, "swap_wei": swap_wei,
        "keep_wbnb_wei": total_wei - swap_wei, "quoted_usdt_wei": quoted_usdt,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bnb", type=float, default=0.02,
                    help="total native BNB to commit (default: 0.02)")
    ap.add_argument("--range-pct", type=float, default=None,
                    help="range half-width; default from [strategy].range_pct")
    ap.add_argument("--dry-run", action="store_true", help="plan only, send nothing")
    args = ap.parse_args()

    range_pct = args.range_pct
    if range_pct is None:
        range_pct = float(pcs.strategy_config()["range_pct"])

    cfg = pcs._cfg(NETWORK)
    w3 = pcs._w3(NETWORK)

    from bnbagent_studio_core.wallet import get_wallet

    addr = Web3.to_checksum_address(get_wallet().address)
    native = w3.eth.get_balance(addr)
    print(f"wallet   : {addr}")
    print(f"native   : {_fmt(native)} {'tBNB' if NETWORK == 'bsc-testnet' else 'BNB'}")

    p = plan(args.bnb, range_pct)
    print(f"\nspot     : {p['price']:.4f} USDT/BNB")
    print(f"range    : {p['range']['lower_price']:.4f} - {p['range']['upper_price']:.4f} "
          f"(+/-{range_pct}%)")
    print(f"ticks    : {p['ticks']['tick_lower']} .. {p['ticks']['tick_upper']} "
          f"(spacing {p['ticks']['tick_spacing']})")
    print(f"           snapped to {p['ticks']['actual_lower_price']:.4f} - "
          f"{p['ticks']['actual_upper_price']:.4f}")
    print(f"\ncommit   : {_fmt(p['total_wei'])} BNB")
    print(f"  swap   : {_fmt(p['swap_wei'])} WBNB -> ~{_fmt(p['quoted_usdt_wei'])} USDT")
    print(f"  keep   : {_fmt(p['keep_wbnb_wei'])} WBNB")

    # Gas headroom, derived from the live gas price rather than a fixed constant:
    # a flat 0.01 BNB is trivial on testnet but ~$6 on mainnet, which would
    # falsely block any small real position. 1.5M gas covers the whole
    # wrap+approve+swap+approve+mint sequence; 3x is the safety margin.
    gas_price = w3.eth.gas_price
    headroom = int(gas_price * 1_500_000 * 3)
    needed = p["total_wei"] + headroom
    print(f"\ngas      : {gas_price / 1e9:.3f} gwei -> reserve {_fmt(headroom)} BNB")
    if native < needed:
        print(f"\nINSUFFICIENT: need ~{_fmt(needed)} BNB (commit + gas), have {_fmt(native)}")
        if NETWORK == "bsc-testnet":
            print("fund at https://testnet.bnbchain.org/faucet-smart")
        return 1

    if args.dry_run:
        print("\ndry run — nothing sent")
        return 0

    # Each step checks the balance it would create, so a re-run after a failure
    # part-way through resumes instead of wrapping/swapping a second time.
    def _bal(token: str) -> int:
        abi = lp.WBNB_ABI if token == cfg["wbnb"] else lp.ERC20_WRITE_ABI
        return w3.eth.contract(address=Web3.to_checksum_address(token),
                               abi=abi).functions.balanceOf(addr).call()

    have_wbnb = _bal(cfg["wbnb"])
    if have_wbnb >= p["total_wei"]:
        print(f"\n1/4 already hold {_fmt(have_wbnb)} WBNB — skipping wrap")
    else:
        need = p["total_wei"] - have_wbnb
        print(f"\n1/4 wrapping {_fmt(need)} BNB -> WBNB")
        print("   ", lp.wrap_bnb(need, NETWORK)["tx_hash"])

    if _bal(cfg["usdt"]) > 0:
        print(f"2/4 already hold {_fmt(_bal(cfg['usdt']))} USDT — skipping swap")
    else:
        print("2/4 swapping half to USDT")
        swap = lp.execute_swap(cfg["wbnb"], cfg["usdt"], p["swap_wei"], network=NETWORK)
        print("   ", swap["tx_hash"])

    # Use real post-swap balances — the swap fills at its own price, so the
    # quoted figure is an estimate, not what we actually hold.
    usdt = w3.eth.contract(address=Web3.to_checksum_address(cfg["usdt"]),
                           abi=lp.ERC20_WRITE_ABI).functions.balanceOf(addr).call()
    wbnb = w3.eth.contract(address=Web3.to_checksum_address(cfg["wbnb"]),
                           abi=lp.WBNB_ABI).functions.balanceOf(addr).call()
    print(f"3/4 balances: {_fmt(usdt)} USDT, {_fmt(wbnb)} WBNB")

    print("4/4 minting position")
    result = lp.mint_position(usdt, wbnb, p["range"]["lower_price"],
                              p["range"]["upper_price"], network=NETWORK)
    print("   ", result["tx_hash"])

    token_id = result.get("token_id")
    if token_id is None:
        print("\nminted, but could not parse token_id from the receipt — "
              "check the tx on https://testnet.bscscan.com")
        return 1

    print(f"\n  TOKEN ID: {token_id}")
    print(f"  set [strategy].token_id = {token_id} in studio.toml")
    explorer = "testnet.bscscan.com" if NETWORK == "bsc-testnet" else "bscscan.com"
    print(f"  https://{explorer}/tx/{result['tx_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
