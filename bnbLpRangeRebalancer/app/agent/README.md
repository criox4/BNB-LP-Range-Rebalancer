# bnbLpRangeRebalancer — A2A seller agent

The valuable Agent and the **SOLE key-holder/signer** for the bnbLpRangeRebalancer
seller. Serves A2A directly on AgentCore (two skills: negotiate + notify_funded),
runs the LLM + read-only chain tools; every signing op
(quote-clamp-sign / submit / settle) is fixed entrypoint code in
[`signing.py`](signing.py) — never an LLM-callable tool.

## What's here

- `main.py` — A2A serving entrypoint (`serve_a2a` on port 9000).
- `executor.py` — the `SellerAgentExecutor`: the negotiate + notify_funded A2A skills.
- `agent_card.py` — the discoverable `AgentCard` (+ OAuth2/Cognito scheme).
- `signing.py` — protocol-neutral signing entrypoints. ALL on-chain writes
  go through these functions.
- `managed_model.py` — provider × framework adapter (e.g. PieverseManagedModel
  for Pieverse-on-ADK). Wraps `bnbagent_studio_core.pieverse.PieverseCreditEnsurer`
  for budget-gated LLM-credit auto-renew.
- `tools.py` — framework-flavored read-only chain tools (ADK `FunctionTool`s).
- `studio.toml` — Agent's own config (wallet, LLM, price bounds, budget).
- `.env.local` — Agent secrets (`TWAK_WALLET_PASSWORD` for twak plus
  provider/storage keys). For evm-local, `WALLET_PASSWORD` is read from your
  shell environment and is not written here.
- the wallet key material lives OUTSIDE this sub-project so deploy packaging can
  never bundle it: an evm-local keystore at the WORKSPACE root `.studio/wallets/`,
  or the twak mnemonic at `~/.twak` (gitignored either way).

## Set up (this agent's own venv)

The Agent's `bnbagent-studio-core` pin resolves from PyPI. From this `agent/` dir:

```bash
python -m venv .venv && .venv/bin/pip install -e .
# …or with uv (drop-in): uv venv && uv pip install -e .
```

Working from a studio SOURCE checkout? Install that core editable first so the
pin resolves against it:

```bash
STUDIO=/path/to/bnbagent-studio
.venv/bin/pip install -e "$STUDIO/packages/bnbagent-studio-core" -e .
```

## Run locally

Run the Agent with `bag dev` from the workspace root — it auto-loads
`.studio/.env.local` (via python-dotenv; no need to `source` it) and runs the
agent in-process (`python main.py`, no Docker). Use `bag dev --container` to run
it via `agentcore dev` in Docker for image parity.

```bash
bag dev                                    # serve_a2a on http://localhost:9000
```

The Agent uses flat top-level imports, so it runs as the `main` module from
inside this dir (cwd `app/agent/`, with `app/agent/` on `PYTHONPATH`) — the
same shape `agentcore dev`/`deploy` use; cwd stays `app/agent/` so its config
walk-up finds `studio.toml`. `serve_a2a` hosts the agent card + JSON-RPC
`message/send` on port 9000 (plus `GET /ping`).

## Deploy

```bash
# From the workspace root:
bag deploy agent
# ships to AgentCore (--protocol A2A) after a readiness sweep; the wallet is
# injected via AWS Secrets Manager, never in the package.
```

In production the Agent serves A2A directly on AgentCore (`--protocol A2A`) — it
is its own public surface, not an invoke-only backend behind a relay.
