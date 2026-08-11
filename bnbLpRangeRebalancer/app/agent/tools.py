"""Read-only chain tools exposed to this agent's LLM (ADK FunctionTool wrap).

Each entry in ``LLM_READ_TOOLS`` is a function from
``bnbagent_studio_core.tools.chain_readonly`` wrapped as an ADK ``FunctionTool``. The
LLM may call any tool in this list while producing the deliverable (the
``notify_funded`` work step); each function's docstring becomes the description
the LLM sees.

You own this file — edit ``LLM_READ_TOOLS`` to control exactly what your
agent can read on-chain. Lines for features your project doesn't use are
commented out by default; uncomment after you've added the dependency to
``studio.toml``.

**All tools are read-only** by the studio definition: no
on-chain state change, no transferable authority, no transaction signing, no
EIP-712 typed-data signing. The agent IS the sole on-chain signer,
but ALL of its signing — quote-sign, submit_result, settle, plus the
automatic budget-gated Pieverse LLM-credit auto-renew inside ``load_model()`` —
lives in ``signing.py`` as FIXED entrypoint code and is NEVER a tool the LLM
can invoke. The LLM only produces work text after a job is verified funded; it
can never price, sign, spend, or mutate chain state. Keep this list read-only.

(``pieverse_usage`` is the one exception in the underlying module: it does a
SIWE EIP-191 personal_sign, domain-locked to llm.pieverse.io, no on-chain
effect. It is commented out below.)
"""
from __future__ import annotations

from google.adk.tools import FunctionTool

from bnbagent_studio_core.tools import chain_readonly as cr

import pancake as pcs

# PancakeSwap V3 reads for the managed BNB/USDT LP position. All are eth_calls
# (`collect` is only ever simulated, never sent). The rebalance write path —
# decreaseLiquidity / collect / swap / mint — stays fixed code in signing.py and
# must NOT be added here; see spec section 3, which forbids the LLM from
# producing calldata.
LP_READ_TOOLS = [
    FunctionTool(pcs.get_bnb_price),
    FunctionTool(pcs.get_lp_position),
    FunctionTool(pcs.get_lp_current_range),
    FunctionTool(pcs.get_lp_liquidity),
    FunctionTool(pcs.get_pending_fees),
    FunctionTool(pcs.get_position_summary),
    # Pure math — no chain access, safe for the LLM to explore hypotheticals with.
    FunctionTool(pcs.calculate_range_metrics),
    FunctionTool(pcs.calculate_rebalance_required),
    FunctionTool(pcs.calculate_rebalance_range),
]

# Agent status (spec 4.6 / 4.7). ONLY the three read-only actions are exposed.
# activate / pause / rebalance are NOT here on purpose: rebalance moves funds,
# and activate/pause control the loop that moves funds autonomously. They are
# operator actions — `python strategy.py <action>` — so no prompt injection can
# start, stop, or trigger the strategy.
import strategy as strat  # noqa: E402 — after LP tools for readability

STATUS_READ_TOOLS = [
    FunctionTool(strat.get_status),
    FunctionTool(strat.get_position),
    FunctionTool(strat.get_performance),
]

LLM_READ_TOOLS = [
    *LP_READ_TOOLS,
    *STATUS_READ_TOOLS,

    # --- Wallet & chain basics ---
    FunctionTool(cr.wallet_info),
    FunctionTool(cr.balance_native),
    FunctionTool(cr.balance_u),         # requires [u_token] in studio.toml
    FunctionTool(cr.network_info),
    FunctionTool(cr.tx_status),

    # --- LLM provider ---
    # FunctionTool(cr.pieverse_usage),  # SIWE personal_sign; requires [llm.provider=pieverse-llm]

    # --- ERC-8004 identity (read-only lookups the LLM may want for context) ---
    FunctionTool(cr.agent_info),        # requires [erc8004] in studio.toml
    FunctionTool(cr.agent_by_address),  # requires [erc8004] in studio.toml

    # --- ERC-8183 jobs (READ-ONLY status/list — writes live in signing.py) ---
    FunctionTool(cr.job_status),        # requires [erc8183] in studio.toml
    FunctionTool(cr.job_list),          # requires [erc8183] in studio.toml
    # FunctionTool(cr.job_count),       # network-wide stat — usually noise

    # --- Advanced / footguns (commented by default) ---
    # FunctionTool(cr.contract_call_view),  # accepts any ABI — LLM-callable footgun
    # FunctionTool(cr.block_info),
    # FunctionTool(cr.wallet_list),         # multi-wallet management — dev concern
    # FunctionTool(cr.wallet_address),      # alias of wallet_info
]
