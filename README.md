# BNB LP Range Rebalancer

Agent #1 of the BNB Agent Studio Marketplace set. Section numbers below refer to
the marketplace specification.

Deep design notes, the decision log and the full bug history live in
[DOCUMENTATION.md](DOCUMENTATION.md); this file is the operator's entry point.

## Overview

An autonomous agent that manages a PancakeSwap V3 concentrated-liquidity
position on BNB/USDT. It polls the pool, works out where the live price sits
inside the position's range, and when price approaches a boundary it withdraws,
collects fees, rebalances the token ratio, and mints a fresh position centred on
the new price — then verifies the result on-chain before calling it done.

It is live on **BSC mainnet** with real funds: position `7116214`, one completed
rebalance, all transaction hashes below.

It also **sells itself**. The same process runs an ERC-8183 seller over A2A: a
buyer negotiates a signed quote, funds the job in $U, and gets a position report
produced by the LLM from read-only chain state and submitted on-chain. Four paid
jobs have run the full lifecycle on mainnet — see [ERC-8183](#erc-8183).

The design constraint that shapes everything: **the LLM never signs and never
produces calldata.** It can read chain state and explain what it sees. Whether
to rebalance is decided by deterministic code; what calldata that means is
decided by other deterministic code. See [Agent Architecture](#agent-architecture).

## Supported Network

| Network | Chain ID | Status |
|---|---|---|
| BSC Mainnet | 56 | **live** — real funds, `[network].default = "bsc-mainnet"` |
| BSC Testnet | 97 | supported; used through development |

Switching is a one-line change to `[network].default` in
`bnbLpRangeRebalancer/app/agent/studio.toml`. `risk.check_config_consistency()`
runs at boot and refuses to let the two networks' settings mix — it exists
because switching to mainnet once left the payment currency at the testnet
token, and every layer was individually correct.

Note the testnet BNB/USDT pool is **unarbitraged** — it reads ~12–16 USDT/BNB,
not ~600. Nothing in the code assumes a mainnet-like price.

## Supported Protocols

**PancakeSwap V3** only (§4.1). Contracts used: Pool, Factory,
NonfungiblePositionManager, QuoterV2, SwapRouter.

LP operations implemented: `positions()`, `mint()`, `increaseLiquidity()`,
`decreaseLiquidity()`, `collect()`.

**Pair: BNB/USDT only** (§4.2 — deliberately single-pair in v1).

Addresses are **not hardcoded in agent code**. They live in the shared
[`config/bsc-contracts.json`](config/bsc-contracts.json) (§13), and every one was
verified by calling it on the live chain rather than copied from documentation —
which mattered, because the published PancakeSwap docs list a mainnet Factory V3
and Router V3 that **have no code at those addresses**.

## Strategy

Defaults per §4.3, all configurable in `[strategy]` of the agent's `studio.toml`:

| Parameter | Default | Meaning |
|---|---|---|
| `range_pct` | `10.0` | target range is ±10% around spot |
| `trigger_pct` | `5.0` | rebalance when price comes within 5% **of range width** of either bound |
| `fee` | `500` | pool fee tier (0.05%); selects the pool from the shared config |
| `mint_slippage_pct` | `1.0` | floor on the mint deposit vs predicted amounts |
| `max_slippage_pct` | `1.0` | swap and withdrawal floors |

Worked example (the spec's own): BNB at $700 → range $630–$770, rebalance fires
at ≥ $763 or ≤ $637.

`trigger_pct` means *percent of the full range width, inward from each bound*.
That is the only reading that reproduces both of the spec's numbers: 763 and 637
are each 7 away from a bound, and 7 is 5% of the 140-wide range — not 5% of the
price. `test_blockchain.py::test_spec_example_triggers` asserts against those
exact figures, so the rule cannot drift silently.

## Agent Architecture

Two layers, per §2:

```
app/
├── agent/                    Agent Layer — LLM, strategy, risk, key
│   ├── main.py               A2A seller entrypoint (:9000) + monitor loop
│   ├── strategy.py           WHETHER to rebalance (deterministic) + 6 actions
│   ├── risk.py               Risk Engine — the gate before any signature
│   ├── blockchain.py         PancakeSwap V3 reads + all tick/range/liquidity math
│   ├── lp_signing.py         WHAT CALLDATA that means, and signs it
│   ├── tools.py              the LLM's tool list — READ-ONLY, 23 tools
│   ├── signing.py            ERC-8183 money ops (fixed code)
│   └── studio.toml           network, wallet, payments, [strategy]
└── service/                  Service Layer — public surface, holds no key
    ├── main.py               REST entrypoint (:8080)
    ├── api.py                the §8 routes
    └── studio.toml
```

The §3.1 flow, and where each step lives:

```
LLM (tools.py)          may read and explain; cannot act
  ↓
Strategy Engine         strategy.py   — deterministic decision
  ↓
Risk Engine             risk.py       — hard refusals
  ↓
Protocol Adapter        blockchain.py — V3 semantics
  ↓
Transaction Builder     lp_signing.py — calldata + slippage floors
  ↓
Wallet                  local keystore, sole signer
  ↓
BSC
```

`activate`, `pause` and `rebalance` are deliberately **absent** from `tools.py`.
They move funds or start the loop that does, so they are operator actions — no
prompt injection can start, stop or trigger the strategy.

## Tools

All 13 tools §4.5 asks for, plus extras. Every one is read-only.

| §4.5 tool | Where |
|---|---|
| `get_bnb_price()` | `blockchain.py` |
| `get_lp_position(token_id)` | `blockchain.py` |
| `get_lp_current_range()` | `blockchain.py` |
| `get_lp_liquidity()` | `blockchain.py` |
| `get_pending_fees()` | `blockchain.py` |
| `calculate_rebalance_range()` | `blockchain.py` |
| `calculate_rebalance_required()` | `blockchain.py` |
| `quote_swap()` | `lp_signing.py` (read-only `eth_call`) |
| `execute_swap()` | `lp_signing.py` — fixed code, **not** an LLM tool |
| `decrease_liquidity()` | `lp_signing.py` — fixed code |
| `collect_fees()` | `lp_signing.py` — fixed code |
| `mint_position()` | `lp_signing.py` — fixed code |
| `verify_position()` | `blockchain.py` |

Also: `get_position_summary`, `get_position_value`, `calculate_range_metrics`,
`increase_liquidity`, plus wallet/balance/ERC-8004/ERC-8183 reads.

## Risk Controls

`risk.py` is the whole boundary, and that is not belt-and-braces: the SDK's
`SigningPolicy` gates **EIP-712 typed data only**. `sign_transaction` is not
policy-checked, and every LP operation is a plain transaction — so nothing sits
behind these checks.

- **Address allowlist** — no transaction is built to any address outside the
  shared config. Refuses before signing.
- **Gas price ceiling** (20 gwei; BSC sits well under 1).
- **Quote-derived slippage floors on every value-moving leg** — swap, mint, and
  withdrawal. The withdrawal floor is derived at the live tick, because a V3
  withdrawal's token split follows the current price.
- **Exact-amount approvals**, never unlimited.
- **Managed-position + owner checks** — the NFT contract holds every V3 position
  on the chain, so a wrong `token_id` decodes cleanly into a stranger's position.
- **Single-flight rebalance lock** (`flock`, cross-process).
- **Pre-flight simulation** — every transaction is `eth_call`ed before signing.
- **Config consistency check** at boot.
- **Pause / emergency stop.**
- **Exponential backoff** on repeated failures.

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `WALLET_PASSWORD` | to sign | unlocks the local keystore. Never written to disk. |
| `OPENROUTER_API_KEY` | for LLM | the `[llm]` provider key |
| `SERVICE_API_KEY` | for control routes | gates `POST /activate` `/pause` `/execute`. **Unset ⇒ those routes refuse with 503** (fail closed) |
| `SERVICE_PORT` | no | REST port (default 8080) |
| `SERVICE_RUN_MONITOR` | no | `1` to run the monitor loop from the service instead of the agent |
| `AGENT_PORT` | no | A2A port (default 9000) |
| `BNB_CONTRACTS_CONFIG` | no | override the shared address book path |
| `STUDIO_BSC_MAINNET_RPC` / `STUDIO_BSC_TESTNET_RPC` | no | custom RPC endpoints |

Secrets live in `.env.local` (gitignored) or AWS Secrets Manager when deployed.
The encrypted keystore lives at the workspace root, **outside** the deploy
codeLocation, so no packaging path can bundle it.

## Local Development

```bash
git clone https://github.com/criox4/BNB-LP-Range-Rebalancer.git
uv venv && uv pip install -e bnbLpRangeRebalancer

# commit hooks — once per clone. core.hooksPath is local config, so cloning
# does NOT bring the hook with it; this line is what turns it on.
git config core.hooksPath .githooks

# tests — offline, no RPC, no wallet. 45 total.
python bnbLpRangeRebalancer/app/agent/test_blockchain.py     # 20 range/tick/liquidity
python bnbLpRangeRebalancer/app/agent/test_strategy.py       # 18 accounting/state/locking
python bnbLpRangeRebalancer/app/agent/test_service.py        #  7 REST routes + auth

# live read-only smoke against both chains
python bnbLpRangeRebalancer/app/agent/test_blockchain.py --live

# the six §4.7 actions from the CLI
python bnbLpRangeRebalancer/app/agent/strategy.py getStatus
python bnbLpRangeRebalancer/app/agent/strategy.py getPosition
python bnbLpRangeRebalancer/app/agent/strategy.py getPerformance
python bnbLpRangeRebalancer/app/agent/strategy.py activate
python bnbLpRangeRebalancer/app/agent/strategy.py pause
python bnbLpRangeRebalancer/app/agent/strategy.py rebalance --force   # moves funds

# the two servers
python bnbLpRangeRebalancer/app/agent/main.py      # A2A  :9000
python bnbLpRangeRebalancer/app/service/main.py    # REST :8080
```

Run the monitor loop in exactly one process. Both is safe — the cross-process
lock refuses the second rebalance rather than duplicating it — but wasteful.

Agent state (rebalance counters, fee/gas accounting, the snapshot series behind
`fees_24h` and APR) lives in a **SQLite** database, `lp_state.<network>.db`,
next to the agent. It replaced a JSON file that was rewritten whole on every
update — a truncate-then-write that a crash mid-write could leave empty. Writes
are now single transactions with `synchronous=FULL`. A legacy
`.lp_state.<network>.json` is imported automatically on first open, once, and
only into an empty database.

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org):
`type(scope): description`, under 72 characters, from `feat fix docs style
refactor perf test build ci chore revert`. `.githooks/commit-msg` rejects
anything else; merge, revert and fixup subjects pass through untouched.

## BSC Testnet Deployment

1. `[network].default = "bsc-testnet"` in the agent's `studio.toml`.
2. Fund the wallet with tBNB from the BNB faucet.
3. `python mint_position.py` to open a position (resumable — safe to re-run).
4. Copy the printed `token_id` into `[strategy].token_id`.
5. `python strategy.py getStatus` to confirm, then `activate`.

Testnet uses different `position_manager` and `quoter_v2` addresses than
mainnet; both are in the shared config and `--live` verifies them.

## BSC Mainnet Deployment

Same flow with `[network].default = "bsc-mainnet"`, plus:

- **`[payments.erc8183].currency` must be the mainnet $U token.** Switching
  networks does not update it. The boot check catches this.
- Gas headroom is derived from the live gas price, not a flat reserve.
- Start small. The whole live validation ran on ~$1.

Currently deployed against position `7116214`, strategy **paused**.

## Docker

One image, two entrypoints — the two §2 layers are separate processes, but they
share a dependency set, so two Dockerfiles would only be two ways to drift. The
build context is the **repo root**, because `blockchain.py` finds
`config/bsc-contracts.json` by walking parent directories.

```bash
set -a && . bnbLpRangeRebalancer/.studio/.env.local && set +a
docker compose up -d --build
```

The host must supply `WALLET_PASSWORD`, `OPENROUTER_API_KEY`, `SERVICE_API_KEY`
and `PUBLIC_URL`. The keystore is **never baked in** — `.dockerignore` excludes
`.studio/`, and the wallets directory is bind-mounted read-only at
`/secrets/wallets`.

The container runs as uid **10001**, so the keystore must be readable by that
uid on the host:

```bash
chown -R 10001:10001 bnbLpRangeRebalancer/.studio/wallets
chmod 700 bnbLpRangeRebalancer/.studio/wallets
chmod 600 bnbLpRangeRebalancer/.studio/wallets/*.json
```

A `600 root:root` keystore looks correct and boots fine — the agent only fails
at the first signature, as a `PermissionError` surfaced through the A2A error
channel. macOS Docker hides this by remapping ownership; a Linux VPS does not.

Ports are published on **loopback only**. Docker writes its own iptables rules,
consulted ahead of ufw's, so a `0.0.0.0` publish is world-reachable on a
deny-by-default host while `ufw status` still reports the port blocked. Put a
reverse proxy in front — `deploy/nginx/bnb-lp-rebalancer.conf` is a working
two-host site — or set `BIND_ADDR=0.0.0.0` deliberately.

State, deliverables and the signing audit log share one named volume at `/data`.
That is not tidiness: a fresh volume reads as a brand-new agent (counters at
zero, no history) while the on-chain position is unchanged, and losing the
deliverables directory 404s every already-paid job's `deliverable_url` forever.
`docker-compose.yml` documents the migration copy.

## Live Deployment

| | |
|---|---|
| Agent (A2A seller) | https://bnb-lp.172-104-171-139.nip.io |
| Agent card | https://bnb-lp.172-104-171-139.nip.io/.well-known/agent-card.json |
| Service (REST) | https://bnb-lp-api.172-104-171-139.nip.io |
| Health | https://bnb-lp-api.172-104-171-139.nip.io/health |

TLS via Let's Encrypt, both hostnames on one certificate, auto-renewing.
`ERC8183_AGENT_URL` is `https://bnb-lp.172-104-171-139.nip.io/erc8183` — the
origin published on-chain by `submit_result`, so it must not change while any
submitted job is unsettled.

## ERC-8004

| Network | Agent id |
|---|---|
| BSC Mainnet | `265375` |
| BSC Testnet | `1796` |

The identity document is **mutable**. `agentURI` is a
`data:application/json;base64,…` blob, and `setAgentURI` replaces it wholesale —
so name, description, skills and endpoint can all be corrected in place, on the
same agent id, without re-registering. An earlier note here claimed otherwise;
it was wrong, and proved wrong on testnet `1796`.

**Open:** the mainnet identity still carries a development name and a
`localhost` endpoint. Correcting it is one `setAgentURI` call, but it is worth
spending once — against the real public URL, from the rotated wallet — so it
waits on deployment.

## ERC-8183

The agent serves two ERC-8183 seller skills over A2A:

- **`negotiate`** — reads the fixed list price from `studio.toml`, clamps it to
  `[min_price, max_price]`, and EIP-191 signs the offer. **No LLM touches
  pricing.**
- **`notify_funded`** — verifies the funded job on-chain, acks, then produces
  the deliverable in the background and submits it.

`settle` is deliberately operator-driven: `bag erc8183 settle <job_id>`, after
the contract's 24h dispute window.

**Proven on mainnet** — four paid jobs, real $U, full lifecycle (create →
register → set_budget → fund → notify_funded → submit):

| Job | Notes |
|---|---|
| `56587` | first end-to-end paid delivery |
| `56588` | exposed B23 — the deliverable was published at a URL nothing served |
| `56589` | first containerised run; its report carries a wrong TVL (B25) |
| `56590` | all fixes proven at once — TVL, APR, 30D PnL, durable audit trail |

Two lessons are baked into the code as a result. The buyer's `deliverable_url`
must be **fetchable**: `submit_result` publishes `{ERC8183_AGENT_URL}/job/{id}/response`
on-chain, so the agent now serves that route itself and `ERC8183_AGENT_URL` must
be the public URL, not `localhost`. And the LLM must never be handed a raw V3
liquidity integer — job `56589` reported "TVL: 335,389.79 BNB" for a position
holding $0.81, because the number was in its context and looked like money.
`tools.py` now replaces those integers with a self-describing string.

## API

REST (Service Layer, §8) on `:8080`:

| Route | Auth | Returns |
|---|---|---|
| `GET /health` | — | liveness, RPC reachability, config problems, monitor state |
| `GET /status` | — | the §4.6 marketplace payload |
| `GET /strategy` | — | active parameters + the range they currently imply |
| `GET /performance` | — | fees, gas, PnL, rebalance history |
| `GET /positions` | — | the managed position, with live verification |
| `GET /transactions` | — | rebalance history with §14 log fields + explorer links |
| `GET /metadata` | — | the §9 shared metadata document |
| `POST /activate` | `X-API-Key` | start autonomous monitoring |
| `POST /pause` | `X-API-Key` | emergency stop |
| `POST /execute` | `X-API-Key` | rebalance now (`?force=true` to override the check) |

A2A (Agent Layer) on `:9000`: agent card at `/.well-known/agent-card.json`,
JSON-RPC `message/send`, `GET /ping`.

`/health` is deliberately more than `{"status":"ok"}` — a rebalancer that is up
but pointed at the wrong chain is not healthy, and only a composite answer shows
that.

## Transaction Examples

Wallet `0x20f1cA5d1e5A3Ee94C29DbF95e6BF6ceA6a8d64b`.

**BSC Mainnet (chain 56)**

| Action | Tx |
|---|---|
| Wrap BNB→WBNB | `220700c8659464b6d9dbfeb847ab83b324c534b8ec97f242f1315cbdc15cb432` |
| Swap WBNB→USDT | `36c0ca812f0cf29bf44586ac74d715f1fcbba9308e045e72ceb83b3914f2dfbd` |
| Mint → position `7116193` | `55fdd0a4d688be7eb12dd958146d018ebdfe88b059e6ffc2aa50fac4da9c5c3d` |
| **Rebalance `7116193`→`7116214`** | `7068e8c3…`, `73890896…`, `4f2e4d57…` (gas $0.019) |

**BSC Testnet (chain 97)**

| Action | Tx |
|---|---|
| Mint → position `36779` | `98b1a8fe22a72f497983be3fd28dcde148f8ec5bca1b197a232d343774fa603e` |
| Swap WBNB→USDT | `c0cd45744c29bb4596c163c9538bea8286878b58b5208082f0dd80f45d6c6e3e` |
| Rebalance `36779`→`36780` | `28360b8b…`, `f875e01e…`, `befff314…` |

Position `7116214`: range $548.22–$670.27, TVL ~$0.81, in range.

## Known Limitations

- **`fees_24h` only counts from the first snapshot.** The pool exposes fees
  pending *right now*, and a rebalance zeroes that by collecting, so history is
  sampled by the agent itself. `fees_24h_window_complete: false` marks a window
  shorter than 24h — the figure is then a floor, never presented as a full day.
  True history needs an indexer.
- **`range_utilization` is defined as distance-from-centre** (0 = centred,
  100 = at a bound). §4.6 lists the field but never defines it, and its own
  example (704.21 in 630–770 → 87) is not reproducible from those numbers under
  any reading we could find. Worth confirming with the spec author.
- **PnL excludes impermanent loss.** It is fees earned minus gas spent. A full
  PnL needs entry-price accounting.
- **Fees earned before this agent took over are invisible** — chain state alone
  cannot attribute them.
- **APR is annualized from a short window, and says so.** `apr` is `null` — not
  `0.0` — when the observed window is too thin to annualize, because "0% yield"
  and "not enough data" are different claims. `pnl_30d` is withheld entirely
  until 30 days are genuinely observed.
- **Single position, single pair, single fee tier** (§4.2, by design in v1).
- **One host only.** Concurrent buyers are fine — jobs run as independent tasks
  with in-flight de-duplication — but the rebalance mutex is an `flock`, which
  cannot span machines. A second VPS needs a shared lock, and exactly one host
  may run the monitor.
- **Per-user positions are not supported.** There is one agent-owned position;
  buyers purchase reports on it, not their own managed liquidity.
- **ERC-8004 mainnet identity still reads as development** — see
  [ERC-8004](#erc-8004).
- **No public URL yet**, so `deliverable_url` points at `localhost` and
  `[storage].kind = "local"` means deliverables live on one disk. Durable
  buyer-fetchable storage needs IPFS (§19).
- **The wallet is a throwaway.** Its key was pasted in plaintext during
  development and must be rotated before this holds meaningful value.
