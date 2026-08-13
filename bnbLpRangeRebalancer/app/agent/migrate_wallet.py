"""Move everything this agent owns to a new signer. Operator script, never a tool.

Rotating the signing key is not a config change. The old address is not a
setting the agent reads — it is the on-chain OWNER of the two NFTs the agent's
whole identity rests on:

    LP position NFT  — PancakeSwap V3 NonfungiblePositionManager. `strategy.py`
                       refuses to act on a position it does not own
                       (`require_position_owner`), so a new signer without this
                       token manages nothing.
    ERC-8004 NFT     — the agent identity. `setAgentURI` is owner-gated, so a
                       new signer cannot correct or repoint the identity either.

`bag wallet` cannot do this: v0.0.x has `new / show / list / sign / balance /
policy` and no transfer of any kind. Hence this script.

WHAT IT DOES NOT DO, on purpose:

  * It does not create the new wallet. Run `bag wallet new` yourself first, from
    a DIFFERENT directory or with --keystore-dir, because `bag wallet new`
    rewrites `[wallet].address` in studio.toml — run it here and the agent
    switches signer on its next restart, mid-migration, to a wallet that owns
    nothing yet.
  * It does not update studio.toml or the deployed keystore. Those are the LAST
    steps, after transfers confirm — see the checklist it prints at the end.
  * It does not settle or touch ERC-8183 jobs. `provider` is recorded on-chain
    per job; already-submitted jobs keep paying the OLD address no matter what
    this script does. Settle them BEFORE rotating, or accept that the proceeds
    land on the old key.

Usage:

    python migrate_wallet.py --to 0xNEW                 # dry run: report only
    python migrate_wallet.py --to 0xNEW --execute       # actually send

Dry run is the default because every operation here is irreversible and sends a
token to an address that, if mistyped, nobody controls.
"""
from __future__ import annotations

import argparse
import sys

from web3 import Web3

from bnbagent_studio_core.wallet import get_wallet

import blockchain as pcs
import lp_signing
import strategy

# Minimal ERC-721. Both the position manager and the ERC-8004 registry are
# ERC-721s, so one ABI covers both.
ERC721_ABI = [
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "safeTransferFrom", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "tokenId", "type": "uint256"}],
     "outputs": []},
]

# The ERC-8004 IdentityRegistry is NOT in config/bsc-contracts.json — that file
# is the PancakeSwap address book, and lp_signing._require_allowed rejects
# anything outside it. Pinned here per network, from the registrations[] entry
# in the agent's own on-chain document (eip155:<chain>:<registry>).
IDENTITY_REGISTRY = {
    "bsc-mainnet": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
    "bsc-testnet": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
}


def _fmt(wei: int) -> str:
    return f"{wei / 1e18:.6f}"


def _erc721(w3, address: str):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC721_ABI)


def _owner_of(contract, token_id: int) -> str | None:
    """Owner, or None when the token does not exist (ownerOf reverts)."""
    try:
        return contract.functions.ownerOf(int(token_id)).call()
    except Exception:  # noqa: BLE001 — a nonexistent token is a normal answer here
        return None


def plan(new_owner: str, network: str, agent_id: int | None = None) -> list[dict]:
    """Read-only. What would move, and what is already where it should be."""
    w3 = pcs._w3(network)
    old = Web3.to_checksum_address(get_wallet().address)
    new = Web3.to_checksum_address(new_owner)

    npm = pcs._addresses(network, int(pcs.strategy_config()["fee"]))["position_manager"]
    registry = IDENTITY_REGISTRY.get(network)

    items: list[dict] = []

    token_id = strategy.current_token_id()
    pos = _erc721(w3, npm)
    owner = _owner_of(pos, token_id)
    items.append({
        "what": f"LP position NFT {token_id}",
        "contract": npm,
        "token_id": token_id,
        "owner": owner,
        "action": "transfer" if owner == old else ("already moved" if owner == new else "NOT OURS"),
    })

    agent_id = agent_id or _agent_id(network)
    if registry and agent_id:
        ident = _erc721(w3, registry)
        owner = _owner_of(ident, agent_id)
        items.append({
            "what": f"ERC-8004 identity NFT {agent_id}",
            "contract": registry,
            "token_id": agent_id,
            "owner": owner,
            "action": "transfer" if owner == old else ("already moved" if owner == new else "NOT OURS"),
        })
    else:
        items.append({"what": "ERC-8004 identity", "contract": registry,
                      "token_id": None, "owner": None,
                      "action": "SKIP — no agent id resolved for this network"})

    return items


def _agent_id(network: str) -> int | None:
    """This wallet's ERC-8004 agent id, discovered rather than configured.

    Prefer passing --agent-id. The discovery path here goes through
    ``_find_owned_agent``, which pages the 8004scan INDEXER (an HTTP service,
    not the chain) and stops after 1000 agents, newest-first. Mainnet mints
    roughly a thousand a day, so an agent registered more than a day or two ago
    falls off the end and reports as unregistered while being perfectly fine
    on-chain. --agent-id skips the lookup entirely and is the reliable input.
    """
    try:
        from bnbagent_studio_core.erc8004.helpers import _find_owned_agent, _make_sdk, _extract_token_id
        record = _find_owned_agent(_make_sdk(get_wallet(), network))
        return _extract_token_id(record) if record else None
    except Exception as e:  # noqa: BLE001 — reported, never fatal to a dry run
        print(f"  ! could not resolve agent id on {network} ({type(e).__name__}: {e})")
        print("    pass --agent-id explicitly")
        return None


def sweep_native(new_owner: str, network: str, *, execute: bool) -> dict | None:
    """Send the BNB balance minus a gas reserve. Runs LAST — the transfers need gas.

    The reserve is derived from the live gas price rather than a flat constant:
    BSC gas moves, and a fixed reserve is either wasteful or leaves the wallet
    unable to pay for its own last transaction.
    """
    w3 = pcs._w3(network)
    old = Web3.to_checksum_address(get_wallet().address)
    balance = w3.eth.get_balance(old)
    gas_price = w3.eth.gas_price
    cost = gas_price * 21_000
    reserve = cost * 2                      # this send, plus headroom for a retry
    amount = balance - reserve

    print(f"\nnative sweep: balance {_fmt(balance)} BNB, "
          f"gas {gas_price / 1e9:.3f} gwei, reserve {_fmt(reserve)}")
    if amount <= 0:
        print("  nothing to sweep after the gas reserve")
        return None
    print(f"  would send {_fmt(amount)} BNB -> {new_owner}")
    if not execute:
        return None

    wallet = get_wallet()
    tx = {
        "from": old,
        "to": Web3.to_checksum_address(new_owner),
        "value": amount,
        "gas": 21_000,
        "gasPrice": gas_price,
        "nonce": w3.eth.get_transaction_count(old, "pending"),
        "chainId": w3.eth.chain_id,
    }
    signed = wallet.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed["rawTransaction"])
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"native sweep reverted: {tx_hash.hex()}")
    print(f"  sent {tx_hash.hex()}")
    return {"tx_hash": tx_hash.hex()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="the NEW owner address")
    ap.add_argument("--network", default=None, help="default: [network].default")
    ap.add_argument("--agent-id", type=int, default=None,
                    help="ERC-8004 agent id. Recommended: discovery goes via the "
                         "8004scan indexer, which only scans the newest 1000 agents.")
    ap.add_argument("--execute", action="store_true",
                    help="actually send. Without this, report only.")
    ap.add_argument("--skip-native", action="store_true",
                    help="leave the BNB behind (e.g. the old key still owes a settle)")
    args = ap.parse_args()

    network = args.network or pcs.default_network()
    old = Web3.to_checksum_address(get_wallet().address)

    try:
        new = Web3.to_checksum_address(args.to)
    except Exception:
        print(f"--to is not a valid address: {args.to!r}")
        return 2
    if new == old:
        print("--to is the CURRENT wallet; nothing to migrate")
        return 2
    if int(new, 16) == 0:
        print("--to is the zero address; that would burn both NFTs")
        return 2

    print(f"network : {network}")
    print(f"from    : {old}")
    print(f"to      : {new}")
    print(f"mode    : {'EXECUTE' if args.execute else 'dry run'}\n")

    items = plan(new, network, args.agent_id)
    for it in items:
        print(f"  {it['what']:<34} owner={it['owner']}  -> {it['action']}")

    movable = [i for i in items if i["action"] == "transfer"]
    blocked = [i for i in items if i["action"] == "NOT OURS"]
    if blocked:
        print("\nREFUSING: this wallet does not own "
              + ", ".join(i["what"] for i in blocked))
        return 1
    if not movable:
        print("\nnothing to transfer")

    if not args.execute:
        print("\nDry run — nothing sent. Re-run with --execute to perform it.")
        print(_CHECKLIST.format(new=new, network=network))
        return 0

    # The new owner must be able to pay for its own gas afterwards, and an NFT
    # sent to a contract that cannot handle ERC-721 receives is stuck forever.
    # safeTransferFrom enforces the second; nothing enforces the first, so say it.
    w3 = pcs._w3(network)
    if w3.eth.get_code(new):
        print("\nREFUSING: --to is a CONTRACT. safeTransferFrom would need it to "
              "implement onERC721Received, and a wallet rotation should target an EOA.")
        return 1

    for it in movable:
        print(f"\ntransferring {it['what']} …")
        contract = _erc721(w3, it["contract"])
        fn = contract.functions.safeTransferFrom(old, new, int(it["token_id"]))
        # Reuses the agent's hardened sender: gas-price ceiling, pending nonce,
        # and an eth_call simulation before anything is broadcast.
        result = lp_signing._send(network, fn, gas=250_000)
        print(f"  {result['tx_hash']}  gas {result['gas_used']}")

    if not args.skip_native:
        sweep_native(new, network, execute=True)

    print("\nverifying …")
    for it in plan(new, network, args.agent_id):
        ok = it["owner"] == new or it["action"].startswith("SKIP")
        print(f"  {'OK  ' if ok else 'FAIL'} {it['what']:<34} owner={it['owner']}")

    print(_CHECKLIST.format(new=new, network=network))
    return 0


_CHECKLIST = """
Remaining steps — NOT done by this script:

  1. studio.toml   [wallet].address = "{new}"
                   (and confirm [wallet].keystore_dir holds the new keystore)
  2. VPS keystore  scp the new .studio/wallets/{new}.json to the host,
                   then: chown 10001:10001 && chmod 600   (the container runs as
                   uid 10001; a root-owned keystore boots fine and fails only at
                   the first signature)
  3. WALLET_PASSWORD in .env.production, if the new keystore uses a new one
  4. docker compose up -d   and re-check `bag wallet show` / GET /health
  5. ERC-8004      the identity NFT moved, but its DOCUMENT still names the old
                   endpoint/name. Repoint it with setAgentURI (--network {network}).
  6. ERC-8183      any job already submitted pays the OLD address. Settle first,
                   or treat those proceeds as stranded on the old key.
"""


if __name__ == "__main__":
    sys.exit(main())
