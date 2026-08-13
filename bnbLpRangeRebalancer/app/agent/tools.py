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

import blockchain as pcs

# --- LLM-facing wrappers -------------------------------------------------------
# The underlying functions take `network` and `token_id`. NEITHER is exposed to
# the model:
#
#   network  — a config fact, not a decision. Left visible, the model fills it
#              in: a real mainnet delivery died on `network='bsc'`, a value it
#              invented (B21). Spec 3.1 says deterministic code decides what the
#              action operates on; the chain is part of that.
#   token_id — defaults to the managed position. The NFT contract holds EVERY V3
#              position on the chain, so a hallucinated id reads a stranger's.
#              Still accepted, so the model can be asked about a specific one,
#              and still guarded by `is_managed_pair` downstream.
#
# Docstrings here are what the model sees, so they say what the tool returns
# rather than how it is wired.
def _managed(token_id: int | None) -> int:
    return int(token_id) if token_id else strat_token_id()


# V3 liquidity is an integer in the pool's own units. It is NOT a token amount
# and NOT a money value, but it is large and sits next to money figures, so a
# model asked for "TVL" will scale it and report it as one. That is not
# hypothetical twice over: it is B9, and it REGRESSED through this very file —
# `get_position_summary` (added in B21) carries liquidity and NO tvl, so paid
# job 56589 delivered "TVL: 335,389.79 BNB" for a position holding $0.81.
#
# Prompting did not hold: main.py already tells the model to quote
# `get_status_report` verbatim and names this exact number as the failure.
# So the fix is to stop handing over the material — the model cannot misread a
# number it never sees. Deterministic code decides what the model is given, the
# same principle spec 3.1 applies to calldata.
_LIQUIDITY_KEYS = ("position_liquidity", "pool_active_liquidity", "liquidity_raw")


def _hide_raw_liquidity(payload):
    """Replace raw V3 liquidity integers with a self-describing string."""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key in _LIQUIDITY_KEYS and isinstance(value, int):
                out[key] = (
                    f"{value} V3 liquidity units — NOT a token amount and NOT a "
                    f"money value; do not scale or report this as TVL"
                )
            else:
                out[key] = _hide_raw_liquidity(value)
        return out
    if isinstance(payload, list):
        return [_hide_raw_liquidity(v) for v in payload]
    return payload


def strat_token_id() -> int:
    import strategy
    return strategy.current_token_id()


def get_bnb_price() -> dict:
    """Current BNB price in USDT, read from the PancakeSwap V3 pool tick."""
    return pcs.get_bnb_price()


def get_lp_position(token_id: int | None = None) -> dict:
    """Raw LP position: token pair, fee tier, tick bounds, liquidity, owed fees.

    Defaults to the position this agent manages.
    """
    return _hide_raw_liquidity(pcs.get_lp_position(_managed(token_id)))


def get_lp_current_range(token_id: int | None = None) -> dict:
    """The position's price range in USDT per BNB, and whether price is inside it."""
    return pcs.get_lp_current_range(_managed(token_id))


def get_lp_liquidity(token_id: int | None = None) -> dict:
    """The position's share of pool liquidity. NOT a money figure — for the
    value of the position in USDT, call `get_status_report`."""
    return _hide_raw_liquidity(pcs.get_lp_liquidity(_managed(token_id)))


def get_pending_fees(token_id: int | None = None) -> dict:
    """Uncollected fees, in USDT and BNB, from a simulated `collect`."""
    return pcs.get_pending_fees(_managed(token_id))


def get_position_summary(token_id: int | None = None) -> dict:
    """Price, range, utilization, pending fees, TVL in USDT, and whether a
    rebalance is required. For a finished report of the economics, prefer
    `get_status_report` — its figures are formatted by code."""
    token_id = _managed(token_id)
    summary = _hide_raw_liquidity(pcs.get_position_summary(token_id))
    # The money figure MUST be present. Its absence is what made the model
    # manufacture a TVL out of the liquidity integer (B9 via B21).
    try:
        summary["tvl_usdt"] = pcs.get_position_value(token_id)["tvl_usdt"]
    except Exception as e:  # noqa: BLE001 — say it is unavailable, never omit it
        summary["tvl_usdt"] = f"unavailable ({type(e).__name__}) — do not estimate it"
    return summary


def verify_position(token_id: int | None = None, tx_hash: str | None = None) -> dict:
    """Re-read chain state and check the position exists, is the managed pair,
    is owned by this agent, holds liquidity, and is in range (spec 4.5 / 4.8)."""
    return pcs.verify_position(_managed(token_id), tx_hash)


# PancakeSwap V3 reads for the managed BNB/USDT LP position. All are eth_calls
# (`collect` is only ever simulated, never sent). The rebalance write path —
# decreaseLiquidity / collect / swap / mint — stays fixed code in signing.py and
# must NOT be added here; see spec section 3, which forbids the LLM from
# producing calldata.
LP_READ_TOOLS = [
    FunctionTool(get_bnb_price),
    FunctionTool(get_lp_position),
    FunctionTool(get_lp_current_range),
    FunctionTool(get_lp_liquidity),
    FunctionTool(get_pending_fees),
    FunctionTool(get_position_summary),
    FunctionTool(verify_position),       # spec 4.5 / 4.8 — read-only re-check
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
    FunctionTool(strat.get_status_report),  # prefer this: figures formatted by code
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
