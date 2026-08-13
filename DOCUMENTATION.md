# BNB LP Range Rebalancer — Project Documentation

Living record of what was built, why each decision was made the way it was, what
is verified on-chain, and what remains. Spec reference throughout is
`BNB Agent Studio Marketplace.md` (v1.0), cited as **§n**.

**Last updated:** 2026-08-12
**Agent:** #1 of 4 — BNB LP Range Rebalancer (§4), category `rebalancing`, spec Priority 1 (§21)
**Repo commits:** `c4374cf`, `1c259d4`, `86884d9`, `f7c518d`, `aae0a34`, `dbea070`, `ba303b2`, `36be8c1`, `1274f9d`, `57c866d`, `c41df14`, `aaa3e3c`, `76e97b3`, `3386717`, `b7f3f2d`, `aef1c4e`, `7731cdc`, `1cb6d84`, `14df2ba`
(rewritten to Conventional Commits; the pre-rewrite history is tagged `backup-pre-conventional`)

---

## 1. Status at a glance

| | |
|---|---|
| Strategy logic | complete and exercised on both networks |
| Testnet position | `36799` (rebalanced from `36780`, which came from `36779`) |
| **Mainnet position** | **`7116214`** (rebalanced from `7116193`), live, in range |
| Agent wallet | `0x20f1cA5d1e5A3Ee94C29DbF95e6BF6ceA6a8d64b` |
| **ERC-8004 mainnet** | **`agent_id 265375`** — `BNB LP Rebalancer (Test)` |
| ERC-8004 testnet | `agent_id 1796` — agentURI **rewritten** to `BNB LP Range Rebalancer (Testnet)` (§4.12: registrations are mutable) |
| Monitor loop | 60s poll; **opt-in** via `$AGENT_RUN_MONITOR` or `$SERVICE_RUN_MONITOR` — exactly one, never two hosts (§11) |
| Active network | selected by **`$BNB_NETWORK`** (§7), falling back to `[network].default`. Mainnet position `7116214` is untouched and paused |
| Runtime state | **paused** (will not trade unattended) |
| Service Layer | `app/service` — all 10 §8 routes live on :8080 |
| Tests | 38 offline (20 math + 11 strategy + 7 service) + live address-book / guard / config checks |
| Architecture | both §2 layers present: `app/agent` (LLM, strategy, risk, key) + `app/service` (public API, no key) |
| Unblocked work remaining | **one item** — agents #2–4 |
| Blocked on credentials | AWS deploy, IPFS storage, public URL |
| Blocked on a clean wallet | key rotation; a *production* ERC-8004 identity |
| **ERC-8183** | **closed on mainnet** — job `56587` reached `SUBMITTED`; the testnet buyer path stays blocked (§4.13) |

**§4.8 Definition of Done is met**: the agent reads a real PancakeSwap V3
position, detects the rebalance condition, executes on BSC, the transaction
confirms, the new position is verified, and the hash is returned. Done on
testnet and then on mainnet with real funds.

**§22 Final Acceptance is not met** and cannot be by this agent alone — it
requires all four agents. For Agent #1 specifically, Marketplace API, **ERC-8004
on both networks** and now **ERC-8183 on mainnet** are all satisfied: the full
lifecycle ran end to end (§6). What remains for this agent is hosting, not code.

**Every spec requirement that could be closed by writing code has been closed.**
What remains needs AWS credentials, a whitelisted buyer policy, elapsed time, or
an answer from the spec author — plus agents #2–4, which is the bulk of the
remaining project.

---

## 2. Requirements traceability

### 2.1 Agent #1 specifics (§4)

| Req | Requirement | Status | Where |
|---|---|---|---|
| §4.1 | PancakeSwap V3: Pool, Factory, NPM, QuoterV2, SwapRouter; positions/mint/increaseLiquidity/decreaseLiquidity/collect | **done** | `config/bsc-contracts.json`, `lp_signing.py` |
| §4.2 | BNB/USDT only, v1 | **done** | single pool in config |
| §4.3 | ±10% range, 5% trigger, configurable | **done** | `[strategy]` in `studio.toml` |
| §4.4 | Monitor → detect → decrease → collect → swap → mint → verify | **done** | `strategy.rebalance()` |
| §4.5 | 15 required tools | **done** | see 2.2 |
| §4.6 | Marketplace data payload | **done** | `strategy.get_status()` |
| §4.7 | activate / pause / rebalance / getStatus / getPosition / getPerformance | **done** | `strategy.ACTIONS` |
| §4.8 | Definition of Done | **done, on mainnet** | §6 evidence below |

### 2.2 §4.5 required tools

| Spec tool | Implementation | Notes |
|---|---|---|
| `get_bnb_price()` | `blockchain.get_bnb_price` | from pool `slot0` tick |
| `get_lp_position(token_id)` | `blockchain.get_lp_position` | + `is_managed_pair` guard |
| `get_lp_current_range()` | `blockchain.get_lp_current_range` | |
| `get_lp_liquidity()` | `blockchain.get_lp_liquidity` | + share of pool |
| `get_pending_fees()` | `blockchain.get_pending_fees` | via `collect` simulation |
| `calculate_rebalance_range()` | `blockchain.calculate_rebalance_range` | |
| `calculate_rebalance_required()` | `blockchain.calculate_rebalance_required` | |
| `quote_swap()` | `lp_signing.quote_swap` | QuoterV2 |
| `execute_swap()` | `lp_signing.execute_swap` | quote-derived floor |
| `decrease_liquidity()` | `lp_signing.decrease_liquidity` | |
| `collect_fees()` | `lp_signing.collect_fees` | |
| `mint_position()` | `lp_signing.mint_position` | derived mins |
| `verify_position()` | `blockchain.verify_position` | named tool; re-reads chain state, checks exists/pair/owner/liquidity/in-range/receipt |

### 2.3 Cross-cutting requirements (§3, §8–§17)

| Req | Requirement | Status | Notes |
|---|---|---|---|
| §3.1 | LLM must never generate arbitrary calldata | **done** | §4.1 below; enforced + tested |
| §8 | REST: `/health` `/status` `/strategy` `/performance` `/positions` `/transactions`, `POST /activate` `/pause` `/execute` | **done** | `app/service/api.py`; control routes gated by `$SERVICE_API_KEY`, fail closed |
| §9 | Shared agent metadata JSON | **done** | `GET /metadata` |
| §10 | ERC-8004 identity | **done** | mainnet `265375`, testnet `1796`; both on the throwaway wallet, endpoints still `localhost` but **editable on-chain** — §4.12, gap G3 |
| §11 | ERC-8183 service integration | **partial** | `negotiate` verified (signed quote, chain 97); `notify_funded` blocked — buyer path reverts `PolicyNotWhitelisted()`, gap G4 |
| §12 | Testnet for dev, mainnet for production | **done** | both exercised; `[network].default` switches |
| §13 | Shared `config/bsc-contracts.json`, no hardcoded addresses | **done** | `blockchain._addresses` loads it; pool chosen by fee tier |
| §14 | Log timestamp, action, protocol, chain_id, tx hash, gas_used, gas_cost, amounts, status, error | **done** | `history[]` entries carry `agent_id`, `action`, `input_amount`, `output_amount`, `gas_cost_wei`, `verified`, `error` |
| §15 | Handle 10 named error classes | **done** | all ten; see 2.4 |
| §16 | Emergency stop; paused = no new transactions, reads continue | **done** | `pause()`; loop checks status each pass |
| §17/§18 | Marketplace card fields | **partial** | TVL/PnL/utilization present; APR and 30D PnL absent — gap G7 |
| §19 | Deliverables incl. public URL, both deployments, ERC-8004 ID | **partial** | source, testnet + mainnet execution, and ERC-8004 IDs done; no public URL (needs AWS) |
| §20 | README with 15 required sections | **done** | `README.md` |

### 2.4 §15 error handling coverage

| Error class | Handled | How |
|---|---|---|
| Insufficient balance | yes | `mint_position.py` pre-flight, gas-price-derived reserve |
| Insufficient allowance | yes | `approve_exact` before each spend |
| Transaction reverted | yes | `eth_call` simulation before send; raises on receipt `status != 1` |
| Slippage exceeded | yes | `amountOutMinimum` from live quote; mint floors |
| Gas estimation failure | yes | `eth_estimateGas` + 25% headroom, bounded by `risk.MAX_GAS_LIMIT`; falls back to the per-op fixed limit when estimation fails, so a lagging RPC cannot abort a rebalance mid-sequence |
| RPC failure | yes | `_retry_rpc` on reads; monitor loop survives any pass failure |
| Protocol unavailable | yes | `risk.ProtocolUnavailable` + `protocol_problems()` — `eth_getCode` per role; checked before any withdrawal and reported by `/health` |
| Price data unavailable | yes | `get_status` returns `{error}` rather than throwing |
| Invalid strategy config | yes | `check_config_consistency()` at boot; `managed_token_id()` raises |
| Wallet unavailable | yes | `bag dev` refuses without `WALLET_PASSWORD` |

No failed transaction disappears silently: every write raises with the tx hash,
and the monitor logs exceptions with a stack trace (§15 closing requirement).

---

## 3. Architecture

### 3.1 Layout

```
bnbLpRangeRebalancer/
├── config/bsc-contracts.json   ★ SHARED address book (§13) — all four agents
├── README.md                   ★ §20, 15 sections
├── app/agent/                  Agent Layer — LLM, strategy, risk, key
│   ├── main.py            A2A entrypoint; boots config guard + monitor loop
│   ├── executor.py        SellerAgentExecutor (scaffold) — negotiate/notify_funded
│   ├── signing.py         ERC-8183 money ops (scaffold): quote-sign, submit, settle
│   ├── lp_signing.py      ★ LP write path — wrap/approve/swap/mint/increase/decrease/collect
│   ├── risk.py            ★ Risk Engine (§2) — the gate before any signature
│   ├── blockchain.py      ★ V3 reads + all range/tick/liquidity math (was pancake.py)
│   ├── strategy.py        ★ monitor loop, six user actions, fee/PnL accounting
│   ├── tools.py           the LLM-visible tool list (read-only ONLY)
│   ├── mint_position.py   one-off position bootstrap
│   ├── test_blockchain.py ★ math tests + live chain checks
│   ├── test_strategy.py   ★ accounting, locking, config rewriting
│   ├── test_service.py    ★ REST routes + auth gating
│   └── studio.toml        network, wallet, LLM, ERC-8183 pricing, [strategy]
├── app/service/                Service Layer — public surface, holds no key
│   ├── main.py            REST entrypoint (:8080)
│   ├── api.py             ★ the §8 routes + §9 metadata
│   └── studio.toml
├── agentcore/             AWS Bedrock AgentCore deploy config + CDK
└── .studio/               keystore + .env.local — NEVER COMMITTED
```
★ = written for this agent; the rest is `bag init` scaffold.

### 3.2 Control flow

```
        A2A (JSON-RPC :9000)                 background thread
                │                                    │
     negotiate / notify_funded              strategy._loop() every 60s
                │                                    │
                │                            strategy.check()
         signing.py (fixed)                          │
                │                        blockchain.* (read-only, retried)
                │                                    │
                │                         rebalance_required?
                │                                    │ yes
                └──────────────┐             strategy.rebalance()
                               │                     │
                               ▼                     ▼
                          get_wallet()  ◄──── lp_signing.* (fixed)
                               │                     │
                               ▼                     ▼
                          BSC (chain 56 / 97)
```

### 3.3 The security boundary (§3.1)

The spec forbids `LLM → arbitrary calldata → sign`. The implemented split:

| Layer | Decides | Can it sign? |
|---|---|---|
| LLM | nothing that matters; reads status, writes prose | **no** |
| `strategy.py` | *whether* to rebalance (deterministic) | no |
| `lp_signing.py` | *what calldata* that means | yes — fixed code |

Enforced concretely:
- `tools.py` contains **only** read-only functions. A test asserts no
  fund-moving or control function name appears in `LLM_READ_TOOLS`.
- `activate` / `pause` / `rebalance` are **not** LLM tools. `rebalance` moves
  funds; `activate`/`pause` control the loop that moves funds autonomously.
  They are operator actions (`python strategy.py <action>`), so no prompt
  injection can start, stop, or trigger the strategy.
- `rebalance()` re-derives the decision from live chain state and refuses
  unless genuinely due (`force=True` is the explicit §4.7 manual action).

---

## 4. Decision log

Each entry: what was decided, and *why* — especially where the obvious choice
is wrong.

### 4.1 The LLM produces neither calldata nor figures
§3.1 bans LLM-generated calldata. We extended the same principle to **numbers**
after observing a real failure: handed the raw status dict, the model reported
the raw V3 liquidity integer as *"TVL: 335,389.79 BNB"* (real TVL: **$0.81**)
and invented pending-fee values ~1e12 too large. For a paid status report the
numbers *are* the product, so `get_status_report()` formats every figure in
code and the agent quotes it verbatim. The ambiguous `liquidity` key was renamed
`liquidity_raw`. Re-verified: every figure matches exactly.

### 4.2 token0 is USDT, so BNB price is INVERSE to the tick
USDT sorts below WBNB on both networks, so the pool's `token0` is USDT. A V3
tick prices token0 in token1, making the BNB price the reciprocal — and meaning
**`tickLower` is the UPPER BNB price bound**. Every conversion funnels through
`_bnb_price_from_tick` / `price_range_to_ticks` so the flip happens in exactly
one place. Getting this backwards silently produces a range that never contains
the price. Independently confirmed: mainnet reads ~$609 consistently across all
four fee tiers, which only happens if the inversion is right.

### 4.3 Every contract address verified on-chain, not from docs
Required by §13 ("official documentation **or verified on-chain**"). This
mattered: `docs.pancakeswap.finance` lists a BSC mainnet **Factory V3
`0x1296b67b…` and Router V3 `0xEfF92A26…` that have no code on mainnet**, and
lists them identically for Ethereum. The real mainnet factory is
`0x0BFbCF9f…` — the same address as testnet. Two traps encoded in the table:

- `position_manager` **differs** per network (testnet `0x427bF5b3`, mainnet
  `0x46A15B0b`) even though the factory address is identical on both.
- `quoter_v2` addresses are effectively **swapped**: both contracts exist on
  both chains, but `0xbC203d7f` only answers on testnet and `0xB048Bbc1` only
  on mainnet. The wrong one reverts with a bare `execution reverted: 0x`.

`_live_addressbook()` calls every address on both chains so this cannot rot.

### 4.4 The 5% trigger rule was derived, not assumed
§4.3 says "Rebalance Trigger: 5%" and gives 763/637 for a 630–770 range.
770−763 = 637−630 = 7 = **5% of the full range width (140)**. That is the only
reading reproducing both numbers, so it is what `trigger_pct` means.
`test_spec_example_triggers` pins it to the spec's own figures.

### 4.5 `range_utilization` is our definition — the spec's is unreproducible
§4.6 lists `range_utilization: 87` for price 704.21 in 630–770, but that is not
derivable from those numbers by any reading we tried (linear position = 53%,
log-space = 55.5%). We define it as **distance from centre**: 0% = dead centre,
100% = sitting on a bound — the quantity that actually matters to a rebalancer.
Documented in code. **Open question for the spec author.**

### 4.6 `SigningPolicy` does not protect the LP path — so we wrote guards
The SDK's `SigningPolicy` gates `sign_typed_data` (EIP-712) **only**.
`sign_transaction` is unchecked, and every LP operation is a plain transaction.
So the policy visible in `bag wallet policy` gives this path zero protection.
`lp_signing.py` therefore carries the real boundary:
- `_require_allowed` refuses any address outside the verified table
- approvals are **exact-amount**, never unlimited (an unlimited router approval
  is the classic way an agent wallet is drained later)
- swaps carry a quote-derived `amountOutMinimum`; everything carries a deadline
- gas price ceiling; `eth_call` simulation before every send

### 4.7 Mint floors are derived, not a flat percentage
`amount0Min/amount1Min` are computed by predicting the deposit the contract will
actually take (`amounts_to_liquidity` → `liquidity_to_amounts`, the same math it
uses internally), then flooring by `mint_slippage_pct`. A flat percentage of the
*desired* amounts would revert every one-sided mint, because a range sitting off
spot legitimately consumes almost none of one token. Pinned by
`test_liquidity_amounts_out_of_range_is_single_sided`.

### 4.8 `fees_24h` needs snapshots; the window flag is not optional
Chain state exposes only fees pending *right now*, and rebalancing zeroes it by
collecting. So the monitor samples its running total and differences the
samples. Because the agent is blind before its first snapshot, the payload
carries `fees_24h_window_complete` — under 24h of watching reports a floor and
**says so** rather than passing a partial figure off as a full day.

### 4.9 Per-network state and config
`token_id` is network-specific: an ID minted on testnet names a different (or
absent) position on mainnet. State files are `.lp_state.<network>.json` so
testnet history is never read as mainnet money, and `check_config_consistency()`
flags a `token_id` that does not belong to the active chain.

### 4.10 Testnet sizes must stay small; mainnet economics are the opposite
The testnet BNB/USDT pool is unarbitraged (~16 USDT/BNB) and shallow: swapping
1 BNB moves it ~44%. At 0.01 BNB the impact is 0.5%. Mainnet is the reverse —
gas is 0.05 gwei, so a full mint costs **$0.026** and a rebalance **$0.03**,
while a $0.50 swap has 0.057% impact. This is why the $1 mainnet test is
economically silly (3% of position per rebalance) but technically valid.

### 4.11 Pool choice: fee-500 over the deeper fee-100
The mainnet fee-100 pool is ~2× deeper, but a fixed position buys a *larger
share* of the shallower fee-500 pool (2.1×) at a 5× higher fee rate — roughly
10× the fee income per unit of volume. For a $1 position where the goal is
observing fees at all, fee-500 wins. Recorded in `ADDRESSES` with the tradeoff.

### 4.12 An ERC-8004 registration IS mutable — this section was wrong

**Corrected 2026-08-13.** This section previously claimed the registration was
immutable and that a production identity therefore had to be registered *last*,
after a public URL existed. That was wrong, and it was wrong in the expensive
direction: it made a fixable mistake look permanent.

`bag erc8004 register` mints an `agentURI` of the form
`data:application/json;base64,…`, so name, description and
`services[].endpoint` are embedded rather than pointed at. But the registry
exposes an on-chain `setAgentURI`, and the SDK wraps it twice:

* `update_service_endpoint(wallet, endpoint)` (CLI: `bag erc8004
  update-endpoint`) — decodes the current `data:` URI, patches
  `services[].endpoint`, re-encodes, writes. Preserves everything else.
* `update_endpoint(wallet, new_uri)` (library) — replaces the **whole** URI,
  so name and description move too.

Verified on testnet agent `1796`: `0x8750df07…` rewrote name, description and
endpoint together (`fxagent` → `BNB LP Range Rebalancer`, endpoint →
`https://example.invalid/…`), read back from chain; `0x72a11909…` then restored
a correct record. Nothing is frozen.

Why the original conclusion looked confirmed: `update-endpoint` was called with
the endpoint it already had. The SDK appends `/.well-known/agent-card.json` to
an A2A base URL, so passing the base URL produced a byte-identical document —
a confirmed receipt, real gas, and no visible change. That reads exactly like a
no-op setter. **A test whose input equals the current state cannot detect
mutability**, and one 158k-gas receipt was treated as proof for both chains.

The endpoint form was also not a mistake: for A2A the on-chain endpoint IS the
agent-card discovery URL, which the SDK builds. `--agent-id` resolution fails
for a different reason — the document advertises A2A only and no ERC8183
service, so a buyer finds no ERC-8183 endpoint to negotiate against.

What actually follows:

1. **Order is a preference, not a constraint.** Registering before a public URL
   exists is fine; point the endpoint at the real URL afterwards. Registration
   is no longer a one-shot.
2. Mainnet `265375` still says `BNB LP Rebalancer (Test)` and `localhost` — both
   now fixable, and worth fixing once the deploy has a real URL rather than
   burning gas on an interim value.
3. `register --agent-uri` with an `https://` URL is still the cheapest way to
   keep the document editable **off-chain**, i.e. without a transaction per edit.

### 4.13 ERC-8183 buying is broken on testnet, not in our code

`bag erc8183 buy` reverts at step 2 of 4 (`create_job` → **`register_job`** →
`set_budget` → `fund`) with `0xc94463e3` = `PolicyNotWhitelisted()`.

Established by elimination, then confirmed directly on-chain:

1. **Not self-dealing.** First seen buying from the agent's own wallet. A second,
   unrelated buyer wallet (`0x3b5Da020…659C`, funded with 0.06 tBNB + 2 U) gives
   the identical revert.
2. **Not our code.** `EvaluatorRouter.registerJob(job_id, policy)` checks a
   `policyWhitelist` mapping. Reading it directly:

   | chain | configured policy | `policyWhitelist` |
   |---|---|---|
   | 97 testnet | `0x4F4678D4…78A6` | **false** |
   | 56 mainnet | `0x9C018457…6dE5` | **true** |

3. **Not fixable by us.** `setPolicyWhitelist` is owner-only; the testnet router
   owner is `0x1001b2C0…D134` (the SDK's own treasury address), not a wallet we
   hold.
4. **Nobody else is using it either.** Zero `JobRegistered` and zero
   `PolicyWhitelisted` events on the testnet router across the last 45k blocks
   (~1.5 days).

So the ERC-8183 job lifecycle is functional on **mainnet** and unusable on
**testnet** with this SDK version. That inverts the usual "develop on testnet
first" order for this one flow, and it is the whole of G4: our seller half is
built and verified, and the missing evidence is gated on someone else's
allowlist. Worth reporting upstream.

---

## 5. Bug log

Every one of these was found by running the thing, not by reading it.

| # | Bug | Root cause | Fix |
|---|---|---|---|
| B1 | Fee/BNB labels wrong for foreign positions | labelled by comparing `token0` to configured USDT; a foreign NFT decodes cleanly | `is_managed_pair`; refuse to name sides otherwise |
| B2 | QuoterV2 testnet address reverted | assumed the same address on both chains | per-network quoter; live address-book test |
| B3 | `nonce too low` mid-sequence | `get_transaction_count` defaulted to `latest`; node hadn't surfaced the prior tx | use `pending` |
| B4 | **Principal booked as fee income** | read pending fees *after* `decreaseLiquidity`, which moves principal into `tokensOwed` — reported $0.154 "fees" on a $0.166 position | read fees **before** the decrease |
| B5 | Gas reported as 7.2e-13 BNB | summed gas *units* as if wei | `_send` returns `gas_cost_wei` |
| B6 | Mainnet gas reserve blocked a $1 position | flat 0.01 BNB headroom — trivial on testnet, ~$6 on mainnet | derive from live gas price |
| B7 | **Mainnet quotes payable in the testnet token** | switching `[network].default` doesn't update `[payments.erc8183].currency`; scaffold prefills testnet | correct address + `check_config_consistency()` at boot |
| B8 | Spurious `Invalid token ID` on a valid NFT | public BSC endpoints are load balanced; a lagging node reverts. Seen twice; an immediate 20-call rerun passed 20/20 | `_retry_rpc` on chain reads |
| B9 | LLM misreported TVL and fees | raw integers + scientific notation handed to a model | code-formatted report, quoted verbatim (4.1) |

B4, B5 and B9 were only observable *after* a real rebalance ran — a dry run
would have shown none of them. B7 was only observable by calling `negotiate` and
reading the signed terms back.

### Round 2 — found by code review, before they fired

B1–B9 were found by running the agent. The next seven came out of a review of
the write path and the monitor loop. None had fired yet; all were reachable.

| # | Bug | Root cause | Fix |
|---|---|---|---|
| B10 | **Two sources of truth for `token_id`** | `check`/`rebalance` read `[strategy].token_id` from studio.toml while `getStatus`/`getPerformance` read the state file, and `_persist_token_id` logs-and-continues on failure. One failed toml write and the agent manages the old, emptied NFT while reporting on the new one | `current_token_id()` — state file only. studio.toml is bootstrap, never a second live answer |
| B11 | **`rebalance()` had no mutual exclusion** | `_lock` is a `threading.Lock` covering only state writes. The documented operator path (`python strategy.py rebalance --force`) is a **separate process** racing the server's monitor thread; both read the same liquidity and both act | `flock`-based `_rebalance_lock()` around the whole sequence |
| B12 | **Withdrawal had no slippage floor** | `decrease_liquidity` defaulted `amount0Min = amount1Min = 0` — the one unprotected leg. A V3 withdrawal's token split follows the current tick, so a searcher who pushes price to a bound in-block makes the position pay out entirely in the cheap side, then restores it | mins derived from `liquidity_to_amounts` at the live tick, floored by `max_slippage_pct` — same shape as the mint |
| B13 | Failed rebalance retried at full speed | `_loop` caught everything and re-entered on the next 60s tick. A mint that fails persistently leaves liquidity already withdrawn, so each pass re-sends `collect` — ~1440 paid transactions/day against ~$1.39 of gas | exponential backoff to a ~32min ceiling; counter resets on success |
| B14 | `fees_24h` counted BNB price moves as income | snapshots stored one combined USDT value; differencing two taken at different prices books the revaluation of the whole historical BNB fee balance as fees. `max(0, …)` hid only the downward half, so the bias was always flattering | store both token sides; value only the **delta** at the current price |
| B15 | `_persist_token_id` rewrote any table | matched `line.strip().startswith("token_id")` with no section tracking, first hit wins | track the `[table]` header; only rewrite under `[strategy]` |
| B17 | **Service Layer served DEFAULT strategy params** | `load_studio_toml()` resolves from the CURRENT WORKING DIRECTORY. `app/service/` has its own studio.toml with no `[strategy]` table, so running the service silently fell back to defaults — `token_id 0`, and any tuned `range_pct`/`trigger_pct`/slippage ignored. Found by reading `/strategy` output during the first e2e run | `blockchain.AGENT_STUDIO_TOML` — config always loads from the agent's own file, whatever the cwd |
| B16 | USDT decimals hardcoded `1e18` | the ratio-balancing step assumed 18 decimals while `pancake.py` (now `blockchain.py`) reads them. Correct for BSC-USDT; against a 6-decimal stable the imbalance test always trips and the swap size clamps to the whole balance | `pcs._decimals()` for both sides |

B17 only became reachable when the Service Layer was added, and only became
VISIBLE by calling the API and reading the numbers back — the same way B7 was
found. A layer that loads config relative to cwd is correct until something
runs it from a different directory.

B10, B11 and B12 are the ones that could have lost funds. All three are
*sequencing* faults, not arithmetic: the math was right, the ordering and the
concurrency were not. That is the same shape as B4.

### Round 3 — found by reading the log a real run produced

| # | Bug | Root cause | Fix |
|---|---|---|---|
| B18 | §14 `input_amount` logged `0.0` for both tokens | read `pos["tokens_owed0"/"1"]`. V3 only refreshes `tokensOwed` when a position is *touched*, so on an untouched position both read zero — the log recorded a rebalance that consumed nothing, while `get_pending_fees()` (which simulates a `collect`) reported real fees on the same position | derive from `get_position_value` — liquidity → amounts at the live tick, i.e. what the withdrawal actually moves. That call was already being made for `tvl_usdt`, so the fix removes an RPC round trip rather than adding one |

| B19 | Monitor liveness invisible over HTTP | `check()` persisted `last_check` on every pass, but `get_status()` never returned it and `/health` reported only `monitor_running` — which is thread liveness, so a loop throwing on every pass still reads as healthy. Three real passes on testnet looked identical to a dead monitor from outside the process | return `last_check` from `get_status()` and `/health`; `test_service.py` asserts both routes expose it |

B19 was found by asking whether monitoring worked and checking rather than
answering from the startup log. The loop was correct; the evidence that it was
correct was not reachable from outside. That is its own class of bug — a
component that works but cannot be observed to work is operationally the same as
one that does not, because nothing can alert on it.

B18 is the B4 family again: `tokensOwed` is a *lazily updated* field, and both
bugs come from reading it at a moment when it does not mean what it looks like.
B4 read it after a decrease, when it held principal; B18 read it before any
interaction, when it held nothing. Neither is visible without executing a
rebalance and then reading the record it wrote.

### Round 4 — found by running a paid job on mainnet

| # | Bug | Root cause | Fix |
|---|---|---|---|
| B20 | **Every chain function defaulted to the literal `"bsc-testnet"`** | 17 signatures across `blockchain.py` and `lp_signing.py` carried `network: str = "bsc-testnet"`, including the whole write path (`mint`/`swap`/`decrease`/`collect`). Correct for as long as the agent only ran on testnet, so 35 tests and every prior run agreed with it. On mainnet the LLM called a tool without the argument, the read went to chain 97, and mainnet token `7116214` decoded to nothing — killing a **funded** delivery | `network: str | None = None` plus `network = network or default_network()` at every site, and `default_network()` now honours `$BNB_NETWORK` |
| B21 | LLM invented `network='bsc'` | B20's fix made the parameter optional but left it *visible*, so the model still filled it in — with a value that is not a supported network | `tools.py` wraps the seven chain reads and exposes **neither** `network` nor a required `token_id`. §3.1 puts "what the action operates on" in deterministic code; the chain is part of that |
| B20b | **The seller runtime kept its own network resolver** | found by re-checking B20 rather than trusting it closed. `main._default_network()` read `[network].default` itself with an `or "bsc-testnet"` fallback and never consulted `$BNB_NETWORK`; `seller_core.__init__` had the same literal. Exporting mainnet therefore moved the strategy while leaving the **seller polling testnet jobs** — a funded mainnet job would never be swept. The B20 test could not see it: it scans `def` lines, and this default lived in a body | both delegate to `blockchain.default_network()`; new source-scan test forbids a network fallback literal anywhere in the Agent Layer |
| B23 | **The deliverable a buyer paid for was unreachable** | found by running a second paid job (`56588`) and then trying to *collect* it, which the first run never did. `submit` publishes `deliverable_url = {ERC8183_AGENT_URL}/job/{id}/response` — a `file://` URL being useless to a buyer — but `serve_a2a` mounts only the card, `/ping` and JSON-RPC, so the seller **404'd its own advertised URL**. The manifest was on disk the whole time (`~/.bag/deliverables/app/erc8183-job-56588.json`); only the route was missing. `bag erc8183 fetch` is no fallback — it scans logs the public RPC rate-limits | **fixed**: `main._mount_deliverable_route` serves that path, taking its prefix from the same `ERC8183_AGENT_URL` the published URL is built from, so route and URL cannot drift |
| B25 | **A paid deliverable reported TVL 411,000x too high** | job `56589` delivered "TVL: 335,389.79 BNB" for a position holding **$0.81**. That figure is the raw V3 liquidity integer scaled by 1e12. This is B9 regressing, and the regression came through **my own B21 fix**: `get_position_summary` was added as a convenience tool, and it returns liquidity with **no TVL field at all** — so a model asked for TVL found none, took the only large number present, and converted it. Job `56588` was correct purely because the model happened to call `get_status_report` instead. `main.py` already forbade this in the prompt and named this exact number; prompting did not hold | **fixed** in `tools.py`: raw liquidity integers are replaced with a self-describing string, and `get_position_summary` now always carries `tvl_usdt` (or says it is unavailable, never omits it). Verified through the real LLM path — TVL now reads `0.8137 USDT`. Offline regression test added |
| B26 | Audit trail for a real submit existed only as stdout | the SDK writes `<project>/.studio/audit-log.jsonl`, which is root-owned in the image while the process runs as uid 10001. Job `56589` logged `audit log file write failed ... Permission denied` for `8183_submit_work`; the submit succeeded, the durable record did not. Then the obvious fix did not work either: `/data` is a named volume, and Docker seeds a volume from the image **only when it is empty at creation**, so a build-time `mkdir` is invisible to any already-deployed volume | `STUDIO_AUDIT_LOG_PATH` onto the volume, plus `docker-entrypoint.sh` creating the directories at START — the only thing that fixes an existing deployment |
| B24 | **State writes were non-atomic on a 60-second path** | found while choosing a datastore, not from a failure. `save_state` was `Path.write_text` — truncate, then write. A crash, OOM kill or full disk between the two leaves a truncated file; `load_state` catches the parse error, logs `starting fresh`, and returns defaults — at which point `token_id` falls back to the `studio.toml` BOOTSTRAP value, i.e. an NFT a past rebalance already emptied. That is B10 reachable by a badly-timed restart, with one WARNING line as the only symptom. The same path also rewrote the entire accumulated `history` + up to 500 `snapshots` every 60s to append one row | **fixed**: SQLite (`state_store.py`). Every write is a transaction; appends are one INSERT. A corrupt legacy file now REFUSES to start rather than silently starting fresh |
| B22 | `range_pct`, `trigger_pct` and both slippage values silently reverted to defaults | mine: writing the new per-network map as a `[strategy.token_ids]` **table header** captured every `[strategy]` key below it. Caught by `test_service.py` before it ran | inline table on one line, with a comment saying why |

B20 is the most expensive bug in this log, and the one the test suite was least
able to see: a default that is right on the network you develop on is
indistinguishable from a correct default until the day you switch. It is also
why `$BNB_NETWORK` exists — the network was previously *three* coupled edits
(`[network].default`, `token_id`, `currency`), and B7 and B22 are both what
happens when one of the three drifts.

---

## 6. On-chain evidence

Satisfies §19 "Blockchain Evidence" for testnet, and mainnet protocol
interaction with a verified agent wallet.

**Wallet:** `0x20f1cA5d1e5A3Ee94C29DbF95e6BF6ceA6a8d64b`

### BSC Testnet (chain 97)
| Action | Tx / ID |
|---|---|
| Mint → position `36779` | `98b1a8fe22a72f497983be3fd28dcde148f8ec5bca1b197a232d343774fa603e` |
| Swap WBNB→USDT | `c0cd45744c29bb4596c163c9538bea8286878b58b5208082f0dd80f45d6c6e3e` |
| Rebalance `36779`→`36780` | `28360b8b…`, `f875e01e…`, `befff314…` |
| **Rebalance `36780`→`36799`** (out-of-range trigger) | `476a88fe…`, `a4c16c0e…`, `b45f4421…`, `0b01f266…` |
| ERC-8004 agent id | `1796` (agentURI frozen as `fxagent` — see G3) |
| ERC-8004 metadata `name` | `2ea29e31c795f2fca28af519d01e645ba6a44052a4e02ae56f835e2d8a768f28` |
| ERC-8004 metadata `description` | `7aaf767a3c2ff600821edfee9f9b8ba2b25eeee7def97287c67b141bfb208354` |

The `36780`→`36799` rebalance is the strongest §4.8 evidence in the project: the
position was genuinely **out of range** (price $12.69 against a $14.92–$18.24
band, utilization 234%), not merely near a bound. All six `verify_position`
checks passed and the replacement landed in range. It is also the first entry
written with the full §14 field set.

Note it lost value: TVL $0.2504 → $0.1609. That is **price impact, not a
slippage-guard failure** — the position held 87% of the pool's entire active
liquidity, so withdrawing and swapping moved the testnet pool against itself.
The 1% guard bounds movement between quote and execution; it cannot bound the
impact of being most of the pool. Mainnet's fee-500 pool is deep enough that
this does not arise (see §4.10).

### BSC Mainnet (chain 56)
| Action | Tx / ID |
|---|---|
| Wrap BNB→WBNB | `220700c8659464b6d9dbfeb847ab83b324c534b8ec97f242f1315cbdc15cb432` |
| Swap WBNB→USDT | `36c0ca812f0cf29bf44586ac74d715f1fcbba9308e045e72ceb83b3914f2dfbd` |
| Mint → position `7116193` | `55fdd0a4d688be7eb12dd958146d018ebdfe88b059e6ffc2aa50fac4da9c5c3d` |
| **Rebalance `7116193`→`7116214`** | `7068e8c3…`, `73890896…`, `4f2e4d57…` (gas $0.019) |
| **ERC-8004 registration** | `agent_id 265375`, registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| **ERC-8183 job `56587` submitted** | `4bd0271912b1dc9aa2e7f80c9b858db5d4ba3481401f2ff146a72ea9417d6836` (block 115539760) |
| **ERC-8183 job `56589`, containerised** | create `0xeb94d345…`, register `0xb6d5c031…`, set_budget `0xe68eb6f6…`, fund `0x7d3df15a…`, submit `0x41f55948…`. Delivered from the Docker image, deliverable fetched back over HTTP. Its report carried the B25 TVL error |
| **ERC-8183 job `56588`, full buyer flow** | create `0x964acf96…`, register `0x41ddaf73…`, set_budget `0xdc703804…`, fund `0xe0bd8962…`, **submit `0x41288736…`** (status 1, block 115657374). Buyer `0xFAf0ffd1…`, 0.1 U, negotiated over A2A with a signed quote (`negotiation_hash 0xfa050296…`) |
| Buyer top-up swap | approve `0x02607273…`, WBNB→U `0xbebc36c6…` (0.0003 BNB → 0.1839 U on the fee-500 U/WBNB pool; the other three tiers exist but hold no liquidity) |

Position `7116214`: range $548.22–$670.27, TVL ~$0.81, in range.
TVL reconciles with the $0.85 committed once the swap fee and $0.04 of WBNB
dust are accounted for.

### Verified contract addresses

| | BSC Mainnet (56) | BSC Testnet (97) |
|---|---|---|
| Factory | `0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865` | *same* |
| PositionManager | `0x46A15B0b27311cedF172AB29E4f4766fbE7F4364` | `0x427bF5b37357632377eCbEC9de3626C71A5396c1` |
| QuoterV2 | `0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997` | `0xbC203d7f83677c7ed3F7acEc959963E7F4ECC5C2` |
| SwapRouter | `0x1b81D678ffb9C0263b24A97847620C99d213eB14` | *same* |
| WBNB | `0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c` | `0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd` |
| USDT | `0x55d398326f99059fF775485246999027B3197955` | `0x337610d27c682E347C9cD60BD4b3b107C9d34dDd` |
| Pool (fee 500) | `0x36696169C63e42cd08ce11f5deeBbCeBae652050` | `0x2dbB5a4c235164B9f772179A43faca2c71a8abDB` |
| $U token | `0xcE24439F2D9C6a2289F741120FE202248B666666` | `0xc70B8741B8B07A6d61E54fd4B20f22Fa648E5565` |

---

## 7. Operations

```bash
cd bnbLpRangeRebalancer/app/agent
export WALLET_PASSWORD=<password>          # never persisted to disk by design

# Read-only
.venv/bin/python strategy.py getStatus
.venv/bin/python strategy.py getPosition
.venv/bin/python strategy.py getPerformance

# Control (operator only — deliberately NOT LLM tools)
.venv/bin/python strategy.py activate
.venv/bin/python strategy.py pause          # emergency stop, §16
.venv/bin/python strategy.py rebalance          # only if genuinely due
.venv/bin/python strategy.py rebalance --force  # manual, §4.7

# Bootstrap a position
.venv/bin/python mint_position.py --bnb 0.0014 --dry-run

# Tests
.venv/bin/python test_blockchain.py          # 14 unit tests, no network
.venv/bin/python test_blockchain.py --live   # + live chain/address/guard checks
.venv/bin/python test_strategy.py            # 9 accounting / locking / config
.venv/bin/python test_service.py             # 6 REST routes + auth gating
.venv/bin/python test_service.py --live      # + every read route answers 200

# Full runtime (A2A + monitor loop)
cd bnbLpRangeRebalancer && WALLET_PASSWORD=... ../.venv/bin/bag dev
curl http://localhost:9000/ping
curl http://localhost:9000/.well-known/agent-card.json
```

Switching networks: edit `[network].default`, set a `token_id` valid for that
chain, and confirm `[payments.erc8183].currency` matches (the boot guard will
say so if not).

### Environment variables
| Var | Purpose | Where |
|---|---|---|
| `BNB_NETWORK` | `bsc-mainnet` / `bsc-testnet` — moves the chain **and** the managed position together (`[strategy].token_ids` maps one per network). Rejects unknown values rather than falling back. It cannot move `[payments.erc8183].currency`, which the SDK reads from studio.toml itself — `check_config_consistency()` says so explicitly when both are set | overrides `[network].default` |
| `WALLET_PASSWORD` | unlocks the keystore; sole signer | shell only — never written to disk |
| `OPENROUTER_API_KEY` | LLM provider | `.studio/.env.local` (gitignored) |
| `SERVICE_API_KEY` | gates `/activate` `/pause` `/execute`; unset ⇒ they 503 | required to control the agent over HTTP |
| `AGENT_RUN_MONITOR` | `1` ⇒ the A2A agent runs the monitor loop | **off by default** — see §11 |
| `SERVICE_RUN_MONITOR` | `1` ⇒ the service layer runs it instead | **off by default**; set exactly one of the two |
| `LP_STATE_DIR` | relocates the state file onto durable storage | required wherever the filesystem is ephemeral |
| `AGENT_PORT` / `SERVICE_PORT` | bind ports (default 9000 / 8080) | local dev and self-hosting |
| `STUDIO_BSC_RPC` / `STUDIO_BSC_TESTNET_RPC` | RPC overrides | optional |

---

## 8. Security posture

**Enforced**
- Signing is fixed code; the LLM's tool list is read-only and asserted so.
- Address allowlist on every write; exact-amount approvals; slippage floors;
  gas ceiling; pre-send simulation.
- Keystore lives at the workspace root outside the deploy code location and is
  gitignored. Commits are scanned for key material before landing.
- Paused = no new transactions (§16).

**Known weaknesses — deliberate, tracked**
1. **The wallet key was pasted in chat** and must be treated as compromised. It
   is fine as a throwaway holding ~$1; move the position and any real balance to
   a freshly generated key before scaling. The OpenRouter key was likewise
   pasted and should be rotated.
2. The wallet already carried an unrelated ERC-8004 identity (gap G3).
3. `mint_slippage_pct` defaults to 1%; review before larger positions.
4. Public RPC endpoints are unauthenticated and flaky (B8). Retries mitigate;
   a paid endpoint would be better for production.

---

## 9. Tests

29 offline tests across three files, plus 5 live groups.

| File | Offline | Live |
|---|---|---|
| `test_blockchain.py` | 14 — range/tick/liquidity math | 4 groups |
| `test_strategy.py` | 9 — accounting, locking, config rewriting (all pure: no RPC, no wallet, no transaction) | — |
| `test_service.py` | 6 — §8 routes + auth gating | 1 group |

| Test (`test_strategy.py`) | Guards against |
|---|---|
| `test_fees_since_is_price_neutral` | B14 — a BNB price move reading as fee income |
| `test_fees_since_counts_real_fees` | the B14 fix over-correcting to zero |
| `test_fees_since_skips_legacy_snapshots` | old one-sided snapshots being misread |
| `test_persist_token_id_is_section_scoped` | B15 — clobbering an unrelated `token_id` |
| `test_persist_token_id_survives_a_missing_file` | the best-effort contract B10 relies on |
| `test_rebalance_lock_excludes_a_second_process` | B11 — spawns a real second process |

| Test (`test_service.py`) | Guards against |
|---|---|
| `test_every_spec8_route_is_registered` | a §8 route silently disappearing |
| `test_control_routes_refuse_without_a_configured_key` | fail-open `/execute` — anyone triggering a paid rebalance |
| `test_control_routes_reject_a_wrong_key` | weak auth on the fund-moving routes |
| `test_pause_is_reachable_with_the_right_key` | the emergency stop being unreachable (§16) |
| `test_metadata_is_self_describing` | §9 metadata drifting from the §4.7 action list |
| `test_strategy_route_reports_configured_params_not_defaults` | B17 — cwd-relative config serving defaults |

| Test (`test_blockchain.py`) | Guards against |
|---|---|
| `test_spec_example_range/_triggers` | drift from §4.3's own worked numbers |
| `test_trigger_reasons` | out-of-range silently reading as "within" |
| `test_range_metrics_geometry` | utilization/position math |
| `test_degenerate_ranges_rejected` | inverted or zero-width ranges |
| `test_tick_price_inversion/_roundtrip` | the token0 inversion (4.2) |
| `test_snap_tick_direction` | tickSpacing rounding |
| `test_price_range_maps_to_inverted_ticks` | lower price → upper tick |
| `test_liquidity_amounts_roundtrip` | liquidity math vs the contract's |
| `test_liquidity_amounts_out_of_range_is_single_sided` | why flat mint floors are wrong |
| `test_zero_liquidity_has_no_amounts` | empty-position edge |
| `test_fees_since_window_incompleteness_is_reported` | partial window sold as 24h |
| `_live_addressbook` | every address on both chains; pool derives; quoter answers |
| `_live_guards` | unlisted address refused before signing |
| `_live_config_consistency` | wrong-chain currency (B7) — asserts the guard *fires* |
| `_live_smoke` | foreign position refuses BNB/USDT labels |

---

## 10. Open items

Everything closable by code is closed. One unblocked item remains; the rest wait
on something external.

### Unblocked

| ID | Gap | Effort |
|---|---|---|
| **G11** | **Agents #2–4.** §21 orders them Lending Guardian (§7) → Grid (§5) → Yield (§6). They inherit the two-layer shape, the shared address book, the risk-engine pattern and the test layout, so the per-agent cost should be well below Agent #1's | very large |

### Blocked

| ID | Gap | Blocked by |
|---|---|---|
| **G13** | **Rotate the wallet key and the OpenRouter key.** Both were pasted in plaintext during development and are in the session transcript. Acceptable while the wallet holds $0.81 of a throwaway position; not acceptable before real value | a decision |
| **G3** | §10 ERC-8004 **production** identity. Both networks are registered on the compromised wallet with `localhost` endpoints. Name/description/endpoint are all rewritable on-chain (§4.12), so this is now a `setAgentURI` call once a public URL exists — not a re-registration. The wallet is the only part that cannot be edited | a fresh wallet (G13) for the *owner*; a public URL (G10) for the endpoint |
| **G14** | Settle job `56587` (`approve` → `COMPLETED`). Calling it early reverts `0x17be5b7b` | the 24h dispute window |
| **G15** | `deliverable_url` is fetchable now (B23 fixed) but points at **`localhost`**, and `[storage].kind = "local"` keeps the manifest on one machine's disk. Fine for a local buyer; a remote one needs a public URL, and surviving a redeploy needs IPFS | G9/G10 |
| **G7** | §17/§18 card fields APR and 30D PnL | elapsed time — the agent has been watching under 24h |
| **G9** | Deploy: AWS credentials unset; `[storage].kind = "local"` is not deployable (needs IPFS) | credentials |
| **G10** | §19 public service URL | G9 |
| **G12** | `range_utilization` definition (§4.6). Ours is distance-from-centre; the spec's own example (704.21 in 630–770 → 87) is not reproducible from those numbers under any reading we found | an answer from the spec author |

### Closed

| ID | Gap | Closed by |
|---|---|---|
| ~~G1~~ | §8 REST interface | `app/service/api.py` — all 10 routes; control routes fail closed without `$SERVICE_API_KEY` |
| ~~G2~~ | §9 shared metadata | `GET /metadata` |
| ~~G5~~ | §13 shared address book | `config/bsc-contracts.json`; pool selected by fee tier |
| ~~G6~~ | §14 log fields | `agent_id`, `action`, `input_amount`, `output_amount`, `gas_cost_wei`, `verified`, `error` on every history entry |
| ~~G8~~ | §20 README | `README.md`, all 15 sections |
| — | §4.1 `increaseLiquidity()` | `lp_signing.increase_liquidity` |
| — | §4.5 `verify_position()` | `blockchain.verify_position` — the check existed inside `rebalance()` but was not callable |
| — | §2 `risk.py` / `blockchain.py` | risk logic extracted from `lp_signing`/`strategy`; `pancake.py` renamed |
| — | §22 ERC-8004 row, both networks | mainnet `agent_id 265375`; testnet `1796` with corrected metadata |
| — | §14 log fields *demonstrated* | testnet rebalance `36780`→`36799` is the first entry carrying the full set |
| — | B18 `input_amount` logged zeros | derived from `get_position_value`, not `tokensOwed` (§5 bug log) |
| ~~G4~~ | §11 `notify_funded` end-to-end | **mainnet job `56587`**: `negotiate` over A2A → `create` → `register` → `set_budget` → `fund` → `notify_funded` → on-chain `submit`, `SUBMITTED` (§6). `register` is the step that reverts on testnet, which confirms §4.13 exactly. Two things the flow required and nothing documented: `ERC8183_AGENT_URL` must point at the seller's `/erc8183` mount, and the budget must **equal** the quoted price, not exceed it |

**Note on G13.** Rotating is cheap; the ordering is what costs. The correct
sequence is now: **new wallet → fund → deploy (G9/G10) → register ERC-8004 with
the real public URL → migrate the position.** Registration must come *last*,
because the endpoint is frozen at registration time — which is the opposite of
what this session assumed going in. See §4.12.

---

## 11. Deployment split: who runs the monitor

The agent is two long-lived processes over one position:

```
app/agent/main.py      A2A seller     :9000   negotiate + notify_funded   (signs)
app/service/main.py    REST API       :8080   §8 routes + /execute        (signs)
                            │
                            └── .lp_state.<network>.json   ← ONE writer
```

**Exactly one process may run the monitor loop, and it is chosen explicitly.**
Neither starts it by default:

```bash
AGENT_RUN_MONITOR=1     # the A2A seller polls and rebalances
SERVICE_RUN_MONITOR=1   # the service layer does instead
```

### Why opt-in rather than a sensible default

`strategy._rebalance_lock` is an `flock` — a **filesystem** lock. It excludes a
second process on the same host (that is B11, and `test_strategy.py` proves it by
forking a real second process). It is blind to a process on another machine.

Split the seller and the monitor across two hosts with either half defaulting to
on, and both acquire their own local lock, both read the same liquidity, and both
rebalance. The guard cannot fire — not because it is wrong, but because the
premise it was written under (one filesystem) no longer holds.

Defaulting off trades a loud, harmless failure for a silent, expensive one. With
neither flag set nothing polls, and that is visible in three places: both
processes say so at startup, `/health` reports `monitor_running`, and
`strategy.is_monitor_running()` answers directly.

Both halves sign from the same EOA, and `lp_signing._send` takes its nonce from
`pending` with no cross-host coordination. On one host the sends serialise
naturally; across two, concurrent sends can collide on nonce. Another reason to
prefer co-location.

### State must outlive the process

`$LP_STATE_DIR` relocates the state file. It names a **directory**, not a file,
on purpose: the filename carries the network, and letting an operator name the
file is how mainnet and testnet end up sharing one — handing a mainnet rebalance
the testnet `token_id`.

This is mandatory wherever the filesystem is ephemeral. On AgentCore the microVM
is reclaimed after **15 minutes idle** or **8 hours** of lifetime, and only
`session storage` survives that. Losing the file is not cosmetic:

| Lost | Consequence |
|---|---|
| `status` | returns as `paused` — monitoring silently stops |
| `history`, snapshots | `fees_24h`, APR and 30D PnL can never accumulate (G7 becomes unfixable) |
| **`token_id`** | falls back to the **bootstrap** id in `studio.toml` — i.e. manages whichever NFT a past rebalance already emptied. **This is B10, reintroduced by the platform rather than the code.** |

**When deploying, copy the existing state file onto the volume first.** A fresh
directory starts at `rebalance_count: 0` with no history, and the `token_id`
falls back to the bootstrap value — which is correct only until the first
rebalance mints a new NFT.

### Verified

| Check | Result |
|---|---|
| agent, no flag | `LP monitor NOT started here` |
| agent, `AGENT_RUN_MONITOR=1` | `monitor loop started (poll=60s)` |
| service, `SERVICE_RUN_MONITOR=1` | started; `/health` → `monitor_running: true` |
| **monitor on MAINNET** | 2026-08-13: two 60s passes while active against position `7116214` (`price=613.56 util=7.1% required=False`), `last_check` advancing 07:11:06 → 07:12:11 over `/health` from a **second process**. Activated and paused through the gated routes; `/activate` returns 401 with no key and with a wrong key | |
| **pause semantics, observed** | after `/pause`, `monitor_running` stays `true` while `last_check` freezes at 07:12:11 across later samples — the loop is alive but performs no checks. So "paused" is NOT a state in which monitoring can be observed to work; proving the loop requires `active`, which is why the mainnet test needed real fund authority for its window | |
| `LP_STATE_DIR` | state **and** lock relocate; network stays in the filename; the repo's own state file untouched |
| guard test | fails on an ungated `start_monitor()` — confirmed against a simulated regression, not assumed |

31 offline tests pass (14 blockchain + 11 strategy + 6 service).

### Not yet decided

Where the always-on host lives. The changes above are host-agnostic: the
AgentCore entrypoint (`agentcore.json` → `entrypoint: main.py`,
`codeLocation: app/agent/`) is an ordinary Python program with a `__main__` that
runs uvicorn, so `python main.py` serves the identical A2A agent on EC2, Railway,
Fly or a VPS. Self-hosting does drop AgentCore's mandatory authorizer: the
endpoint is never anonymous there (IAM, or Cognito OAuth2 for external buyers),
and nothing replaces that automatically.
